"""Concrete Milestone-1 workers. Each is independently testable via meridian.workers.base.execute()."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.bus.schemas import CandlesIngestedV1, CaseEventV1, Envelope, RiskDecisionV1, SignalDetectedV1
from meridian.cases import lifecycle as lc
from meridian.committee.logic import COMMITTEE_VERSION, decide
from meridian.core import audit
from meridian.core.config import code_version
from meridian.core.util import jsonable, sha256, utcnow
from meridian.db.models import (
    CaseEvidence,
    CommitteeMemo,
    Instrument,
    MacroEvent,
    Position,
    PositionAdvisory,
    ResearchCase,
    RiskDecision,
)
from meridian.marketdata import service as md
from meridian.marketdata.types import ProviderUnavailable, margin_rate
from meridian.risk import service as risk_svc
from meridian.risk.engine import relevant_events
from meridian.risk.position_monitor import MonitorInputs, advise
from meridian.workers.base import BaseWorker, RunContext, WorkerOutput, WorkerSpec
from meridian.workers.indicators import STRATEGY, STRATEGY_VERSION, TrendParams, ema, evaluate_trend, resample


def _bars(rows: list[Any]) -> list[dict[str, Any]]:
    return [{"ts": r.ts.isoformat(), "open": r.open, "high": r.high, "low": r.low, "close": r.close} for r in rows]


def _window_hash(rows: list[Any]) -> str:
    return sha256([[r.ts.isoformat(), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume)] for r in rows])


# =============================================================== data_ops
class DiscoveryOutput(WorkerOutput):
    active: list[str] = []
    unavailable: list[str] = []


class InstrumentDiscoveryWorker(BaseWorker):
    spec = WorkerSpec(id="data.instrument_discovery", department="data_ops", name="Instrument Discovery",
                      responsibility="Resolve GOLD/SILVER/OIL/BTC/ETH/XRP to tradable products.", version="1.0.0",
                      output_model=DiscoveryOutput, schedule_seconds=6 * 3600, escalation_to="exec.chief_of_staff")

    def run(self, s: Session, rc: RunContext) -> DiscoveryOutput:
        provider = self.ctx.services["market"]
        now = utcnow()
        specs = provider.discover()
        rows = md.upsert_instruments(s, specs, now)
        rc.ref("provider", provider.source)
        for r in rows:
            self.log.info(f"discovery {r.asset}: {r.id} status={r.status} type={r.product_type} "
                          f"contract_size={r.contract_size} tick={r.price_increment} expiry={r.contract_expiry} "
                          f"margin_long={margin_rate(r.raw, 'LONG')} margin_short={margin_rate(r.raw, 'SHORT')} "
                          f"{r.status_detail}")
        act = [r.id for r in rows if r.status == "ACTIVE"]
        un = [f"{r.asset}:{r.id}:{r.status_detail}" for r in rows if r.status != "ACTIVE"]
        rc.metrics.update({"active": len(act), "unavailable": len(un)})
        return DiscoveryOutput(summary=f"{len(act)} active, {len(un)} unavailable", active=act, unavailable=un)


class IngestOutput(WorkerOutput):
    updated: list[str] = []
    failed: list[str] = []


class MarketIngestWorker(BaseWorker):
    spec = WorkerSpec(id="data.market_ingest", department="data_ops", name="Market Data Ingestor",
                      responsibility="Fetch, validate, normalize and store closed candles and order books.",
                      version="1.0.0", output_model=IngestOutput, schedule_seconds=300,
                      default_config={"history_bars": 300}, escalation_to="exec.chief_of_staff")

    def run(self, s: Session, rc: RunContext) -> IngestOutput:
        provider = self.ctx.services["market"]
        gran = self.ctx.settings.candle_granularity
        now = utcnow()
        updated, failed = [], []
        insts = md.active_instruments(s)
        for inst in insts:
            if inst.source != provider.source:
                continue
            prev_last = s.execute(select(md.Candle.ts).where(md.Candle.instrument_id == inst.id, md.Candle.granularity == gran)
                                  .order_by(md.Candle.ts.desc()).limit(1)).scalar()
            start, end = md.window_for(gran, int(rc.config["history_bars"]), now)
            if prev_last is not None:
                start = max(start, prev_last - timedelta(seconds=3 * 3600))
            try:
                sp = s.begin_nested()
                candles = provider.candles(md.to_spec(inst), gran, start, end)
                rep = md.store_candles(s, inst, gran, candles, now)
                try:
                    md.store_book(s, inst, provider.book(md.to_spec(inst)), now)
                except ProviderUnavailable:
                    md.store_book(s, inst, None, now)
                sp.commit()
            except ProviderUnavailable as e:
                sp.rollback()
                self.log.warning(f"market data unavailable for {inst.id}: {e}")
                md.set_freshness(s, f"candles:{inst.id}:{gran}", prev_last, "UNAVAILABLE", inst.source, str(e)[:300], now)
                failed.append(inst.id)
                continue
            last = rep.accepted[-1].ts if rep.accepted else None
            rc.ref("candles", f"{inst.id}:{gran}:{start.isoformat()}..{end.isoformat()}")
            if last is not None and (prev_last is None or last > prev_last):
                self.emit(s, "market.candles.ingested",
                          CandlesIngestedV1(instrument_id=inst.id, asset=inst.asset, granularity=gran, last_ts=last,
                                            count=len(rep.accepted), data_source=inst.source), rc, new_trace=True)  # type: ignore[arg-type]
                updated.append(inst.id)
        rc.metrics.update({"instruments": len(insts), "updated": len(updated), "failed": len(failed)})
        if insts and len(failed) == len([i for i in insts if i.source == provider.source]):
            raise ProviderUnavailable(f"all instruments failed: {failed}")
        return IngestOutput(summary=f"updated {len(updated)}, failed {len(failed)}", updated=updated, failed=failed)


class FreshnessMonitorWorker(BaseWorker):
    spec = WorkerSpec(id="data.freshness_monitor", department="data_ops", name="Data Quality Monitor",
                      responsibility="Re-evaluate freshness of every dataset.", version="1.0.0", schedule_seconds=60)

    def run(self, s: Session, rc: RunContext) -> WorkerOutput:
        rows = [r for r in md.recheck_freshness(s, self.ctx.settings.candle_granularity)
                if r.status != "RETIRED" and not r.dataset.startswith("simulated:")]
        stale = [r.dataset for r in rows if r.status != "FRESH"]
        rc.metrics.update({"datasets": len(rows), "not_fresh": len(stale)})
        return WorkerOutput(summary=f"{len(rows) - len(stale)}/{len(rows)} fresh")


# =============================================================== macro
class MacroOutput(WorkerOutput):
    status: str = "UNAVAILABLE"
    events: int = 0


class MacroCalendarWorker(BaseWorker):
    spec = WorkerSpec(id="macro.calendar_analyst", department="global_macro", name="Economic Calendar Analyst",
                      responsibility="Look up scheduled macro events and maintain the calendar.", version="1.0.0",
                      output_model=MacroOutput, schedule_seconds=3600, escalation_to="exec.chief_macro_officer",
                      max_retries=2, default_config={"stale_after_hours": 12})

    def run(self, s: Session, rc: RunContext) -> MacroOutput:
        provider = self.ctx.services.get("macro")
        now = utcnow()
        if provider is None:
            md.set_freshness(s, "macro_calendar", None, "UNAVAILABLE", "NONE", "no macro provider configured", now)
            self.emit(s, "macro.calendar.updated", {"source": "none", "status": "UNAVAILABLE", "event_count": 0}, rc)
            return MacroOutput(summary="no provider", status="UNAVAILABLE")
        try:
            events = provider.fetch()
        except Exception as e:  # noqa: BLE001 — record explicit unavailable state, keep old events
            self.log.warning(f"macro calendar fetch failed: {e!r}")
            prev = s.get(md.DataFreshness, "macro_calendar")
            stale_after = timedelta(hours=int(rc.config["stale_after_hours"]))
            status = "STALE" if prev and prev.last_data_ts and now - prev.last_data_ts < stale_after * 4 else "UNAVAILABLE"
            md.set_freshness(s, "macro_calendar", prev.last_data_ts if prev else None, status, provider.name,
                             f"fetch failed: {e!r}"[:300], now)
            self.emit(s, "macro.calendar.updated", {"source": provider.name, "status": status, "event_count": 0}, rc)
            rc.metrics["fetch_failed"] = 1
            return MacroOutput(summary=f"fetch failed ({status})", status=status)
        for e in events:
            row = s.get(MacroEvent, e.id) or MacroEvent(id=e.id)
            row.title, row.country, row.impact, row.event_time = e.title, e.country, e.impact, e.event_time
            row.time_confidence, row.forecast, row.previous, row.source = e.time_confidence, e.forecast, e.previous, e.source
            row.fetched_at = now
            s.add(row)
        md.set_freshness(s, "macro_calendar", now, "FRESH" if events else "STALE", provider.name,
                         f"{len(events)} events; source covers current week only", now)
        rc.ref("macro_feed", provider.name, sha256([e.id for e in events]))
        self.emit(s, "macro.calendar.updated", {"source": provider.name, "status": "FRESH" if events else "STALE",
                                                "event_count": len(events)}, rc)
        rc.metrics["events"] = len(events)
        return MacroOutput(summary=f"{len(events)} events", status="FRESH" if events else "STALE", events=len(events))


# =============================================================== trend
class TrendOutput(WorkerOutput):
    signal: bool = False
    direction: str | None = None
    strength: float = 0.0


class TrendSignalWorker(BaseWorker):
    spec = WorkerSpec(id="trend.signal_analyst", department="trend", name="Trend Signal Analyst",
                      responsibility="EMA(20/50) trend + 20-bar Donchian breakout, 2xATR stop, 3xATR target.",
                      version=STRATEGY_VERSION, input_model=CandlesIngestedV1, output_model=TrendOutput,
                      subscribes=("market.candles.ingested",), escalation_to="exec.research_officer",
                      default_config=TrendParams().__dict__)

    def handle(self, s: Session, env: Envelope, rc: RunContext) -> TrendOutput:
        p = CandlesIngestedV1.model_validate(env.payload)
        inst = s.get(Instrument, p.instrument_id)
        if inst is None or inst.status != "ACTIVE":
            return TrendOutput(summary="instrument not active")
        rows = md.load_candles(s, inst.id, p.granularity, limit=300, until=p.last_ts)
        rc.ref("candles", f"{inst.id}:{p.granularity}:{rows[0].ts.isoformat() if rows else ''}..{p.last_ts.isoformat()}",
               _window_hash(rows))
        params = TrendParams.from_config(rc.config)
        sig = evaluate_trend(_bars(rows), inst.price_increment or Decimal("0.01"), params)
        rc.metrics.update({"bars": len(rows), "strength": sig.strength})
        if sig.direction is None:
            return TrendOutput(summary=sig.calc.get("reason", "NO_SETUP"))
        sig.calc["window_hash"] = _window_hash(rows)
        sig.calc["window"] = {"instrument_id": inst.id, "granularity": p.granularity,
                              "start": rows[0].ts.isoformat(), "end": rows[-1].ts.isoformat(), "bars": len(rows)}
        self.emit(s, "signal.detected", SignalDetectedV1(
            instrument_id=inst.id, asset=inst.asset, direction=sig.direction, strategy=STRATEGY,  # type: ignore[arg-type]
            strategy_version=STRATEGY_VERSION, as_of=p.last_ts, entry_reference=str(sig.entry), stop=str(sig.stop),
            target=str(sig.target), strength=sig.strength, calculations=jsonable(sig.calc),
            data_source=inst.source), rc)  # type: ignore[arg-type]
        audit.record(s, self.spec.id, "signal.calculated", "instrument", inst.id, sig.calc)
        return TrendOutput(summary=f"{sig.direction} signal", signal=True, direction=sig.direction, strength=sig.strength)


# =============================================================== executive
class CaseOutput(WorkerOutput):
    case_id: str | None = None
    state: str | None = None


class ChiefOfStaffWorker(BaseWorker):
    """Case intake (create case + attach macro context) and scheduled housekeeping (expiry sweeps)."""
    spec = WorkerSpec(id="exec.chief_of_staff", department="executive", name="Chief of Staff",
                      responsibility="Case intake, macro attachment, expiry sweeps.", version="1.0.0",
                      input_model=SignalDetectedV1, output_model=CaseOutput, subscribes=("signal.detected",),
                      schedule_seconds=300, escalation_to="cio",
                      default_config={"pipeline_timeout_hours": 24, "deferred_ttl_hours": 24})

    def handle(self, s: Session, env: Envelope, rc: RunContext) -> CaseOutput:
        sig = SignalDetectedV1.model_validate(env.payload)
        actor = f"{self.spec.id}@{self.spec.version}"
        existing = lc.open_case_for(s, sig.instrument_id)
        if existing is not None and existing.state in ("BLOCKED", "DEFERRED"):
            # A fresh signal gets a fresh review: risk conditions (event windows, open risk, capital) change during
            # the day, so a block or deferral must not freeze the asset until the case times out.
            lc.add_evidence(s, existing, "SIGNAL", "NEUTRAL", f"Superseded by a new {sig.direction} signal at {sig.as_of}",
                            sig.model_dump(mode="json"), actor)
            lc.transition(s, existing, "EXPIRED", f"superseded by new {sig.direction} signal; re-reviewing", actor, env)
            s.flush()
            existing = None
        if existing is not None:
            if existing.direction != sig.direction:
                if "INVALIDATED" in lc.TRANSITIONS[existing.state]:
                    lc.add_evidence(s, existing, "SIGNAL", "OPPOSES", f"Opposite {sig.direction} signal at {sig.as_of}",
                                    sig.model_dump(mode="json"), actor)
                    lc.transition(s, existing, "INVALIDATED", f"opposite {sig.direction} signal", actor, env)
                    s.flush()
                else:
                    return CaseOutput(summary="opposite signal while case in review; ignored", case_id=str(existing.id))
            else:
                lc.add_evidence(s, existing, "SIGNAL", "SUPPORTS", f"Signal reconfirmed at {sig.as_of}", {"check": "signal", **sig.model_dump(mode="json")}, actor)
                lc.revise(s, existing, {**lc.latest_revision_content(s, existing), "reconfirmed_at": sig.as_of.isoformat(),
                                        "latest_signal": sig.model_dump(mode="json")}, "signal reconfirmed", actor)
                return CaseOutput(summary="existing case reconfirmed", case_id=str(existing.id), state=existing.state)

        case = lc.create_case(s, instrument_id=sig.instrument_id, asset=sig.asset, direction=sig.direction,
                              hypothesis={**sig.model_dump(mode="json"),
                                          "statement": f"{sig.asset} {sig.direction}: trend + breakout per {sig.strategy} v{sig.strategy_version}",
                                          "signal_event_id": str(env.event_id)},
                              data_source=sig.data_source, actor=actor, correlation_id=env.correlation_id)
        lc.add_evidence(s, case, "SIGNAL", "SUPPORTS",
                        f"{sig.direction} breakout, strength {sig.strength}, ATR {sig.calculations.get('atr_pct')}%",
                        {"check": "signal", "calculations": sig.calculations}, f"trend.signal_analyst@{sig.strategy_version}")
        lc.transition(s, case, "RESEARCHING", "intake complete", actor, env)

        mstatus, events = risk_svc.macro_context(s, utcnow())
        cfg = (risk_svc.active_config(s).config if risk_svc.active_config(s) else {})
        rel = relevant_events(events, sig.asset, {**risk_svc.DEFAULT_RISK_CONFIG, **cfg})
        rc.ref("macro_events", f"status={mstatus}", sha256([e["id"] for e in events]))
        if mstatus != "FRESH":
            lc.add_evidence(s, case, "MACRO", "UNAVAILABLE", f"Macro calendar {mstatus or 'never fetched'}; event risk unverified",
                            {"check": "macro", "status": mstatus}, "macro.calendar_analyst")
        else:
            summary = (f"{len(rel)} relevant event(s) in next 48h: " + "; ".join(f"{e['title']} {e['event_time']}" for e in rel[:4])) \
                if rel else "No relevant high-impact events in next 48h"
            lc.add_evidence(s, case, "MACRO", "NEUTRAL" if rel else "SUPPORTS", summary, {"check": "macro", "events": rel, "status": mstatus},
                            "macro.calendar_analyst")
        self.emit(s, "case.researched", CaseEventV1(case_id=case.id, state=case.state), rc)
        return CaseOutput(summary="case created", case_id=str(case.id), state=case.state)

    def run(self, s: Session, rc: RunContext) -> CaseOutput:
        now = utcnow()
        actor = f"{self.spec.id}@{self.spec.version}"
        n = 0
        for case in s.execute(select(ResearchCase).where(ResearchCase.state.in_(lc.OPEN_STATES))).scalars():
            if case.expires_at and now >= case.expires_at and "EXPIRED" in lc.TRANSITIONS[case.state]:
                lc.transition(s, case, "EXPIRED", "recommendation window elapsed without CIO action", actor)
                n += 1
            elif case.state in ("DETECTED", "RESEARCHING", "CHALLENGED", "RISK_REVIEW") and \
                    now - case.updated_at > timedelta(hours=int(rc.config["pipeline_timeout_hours"])):
                lc.transition(s, case, "EXPIRED", "pipeline timeout", actor)
                n += 1
        rc.metrics["expired"] = n
        return CaseOutput(summary=f"expired {n} case(s)")


class ResearchOfficerWorker(BaseWorker):
    """Independent opposing analysis: tries to find reasons the signal is wrong."""
    spec = WorkerSpec(id="exec.research_officer", department="executive", name="Chief Research Officer",
                      responsibility="Independent challenge: higher timeframe, overextension, volatility regime.",
                      version="1.0.0", input_model=CaseEventV1, output_model=CaseOutput, subscribes=("case.researched",),
                      escalation_to="cio", default_config={"overextension_atr": 2.5, "vol_percentile_oppose": 0.9})

    def handle(self, s: Session, env: Envelope, rc: RunContext) -> CaseOutput:
        ce = CaseEventV1.model_validate(env.payload)
        case = s.get(ResearchCase, ce.case_id)
        if case is None or case.state != "RESEARCHING":
            return CaseOutput(summary="case not in RESEARCHING; skipped", case_id=str(ce.case_id))
        actor = f"{self.spec.id}@{self.spec.version}"
        hyp = case.hypothesis
        win = hyp["calculations"]["window"]
        rows = md.load_candles(s, case.instrument_id, win["granularity"], limit=300, until=_parse(win["end"]))
        rc.ref("candles", f"{case.instrument_id}:{win['granularity']}:..{win['end']}", _window_hash(rows))
        bars = _bars(rows)
        long = case.direction == "LONG"

        # 1. higher timeframe (4x resample)
        htf = resample(bars, 4)
        closes4 = [float(b["close"]) for b in htf]
        e10, e20 = ema(closes4, 10), ema(closes4, 20)
        if e10 and e20:
            agree = (e10[-1] > e20[-1]) if long else (e10[-1] < e20[-1])
            lc.add_evidence(s, case, "CHALLENGE", "SUPPORTS" if agree else "OPPOSES",
                            f"Higher timeframe (4x) EMA10 {'agrees' if agree else 'disagrees'} with {case.direction}",
                            {"check": "htf", "htf_ema10": round(e10[-1], 6), "htf_ema20": round(e20[-1], 6)}, actor)
        else:
            lc.add_evidence(s, case, "CHALLENGE", "UNAVAILABLE", "Insufficient history for higher-timeframe check", {"check": "htf"}, actor)

        # 2. overextension
        calc = hyp["calculations"]
        ext = abs(calc["close"] - calc["ema_fast"]) / calc["atr"] if calc["atr"] else 0
        over = ext > float(rc.config["overextension_atr"])
        lc.add_evidence(s, case, "CHALLENGE", "OPPOSES" if over else "NEUTRAL",
                        f"Price {ext:.2f} ATR from EMA{calc['params']['fast']}" + (" — overextended, chase risk" if over else ""),
                        {"check": "extension", "extension_atr": round(ext, 4)}, actor)

        # 3. volatility regime: ATR% percentile over the window
        closes = [float(b["close"]) for b in bars]
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        tr_pct = sorted(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) / closes[i]
                        for i in range(1, len(bars)))
        cur = (max(highs[-1] - lows[-1], abs(highs[-1] - closes[-2]), abs(lows[-1] - closes[-2])) / closes[-1]) if len(bars) > 1 else 0
        pct = (sum(1 for x in tr_pct if x <= cur) / len(tr_pct)) if tr_pct else 0
        hot = pct >= float(rc.config["vol_percentile_oppose"])
        lc.add_evidence(s, case, "VOLATILITY", "OPPOSES" if hot else "NEUTRAL",
                        f"Breakout bar range at {pct:.0%} percentile of window" + (" — volatility shock" if hot else ""),
                        {"check": "range_pct", "range_percentile": round(pct, 4)}, "vol.realized")

        from meridian.analysts.evidence import attach_assessments
        rc.metrics["analyst_views"] = attach_assessments(s, case, actor)
        lc.transition(s, case, "CHALLENGED", "independent challenge complete", actor, env)
        lc.transition(s, case, "RISK_REVIEW", "submitted to risk", actor, env)
        self.emit(s, "case.challenged", CaseEventV1(case_id=case.id, state=case.state), rc)
        return CaseOutput(summary="challenged", case_id=str(case.id), state=case.state)


def _parse(ts: str):  # noqa: ANN202
    from datetime import datetime
    return datetime.fromisoformat(ts)


# =============================================================== risk
class RiskOutput(WorkerOutput):
    case_id: str
    outcome: str
    reason_codes: list[str]


class RiskControlWorker(BaseWorker):
    spec = WorkerSpec(id="risk.chief_risk_officer", department="risk", name="Chief Risk Officer (Risk Control Engine)",
                      responsibility="Deterministic ALLOW/RESTRICT/BLOCK. Veto is final.", version="1.0.0",
                      input_model=CaseEventV1, output_model=RiskOutput, subscribes=("case.challenged",), escalation_to="cio")

    def handle(self, s: Session, env: Envelope, rc: RunContext) -> RiskOutput:
        ce = CaseEventV1.model_validate(env.payload)
        case = s.get(ResearchCase, ce.case_id)
        if case is None or case.state != "RISK_REVIEW":
            return RiskOutput(summary="not in RISK_REVIEW", case_id=str(ce.case_id), outcome="NONE", reason_codes=[])
        actor = f"{self.spec.id}@{self.spec.version}"
        dec = risk_svc.decide_for_case(s, case, self.ctx.settings.candle_granularity, actor)
        rc.ref("risk_config", f"v{dec.config_version}")
        rc.ref("risk_inputs", str(dec.id), dec.inputs_hash)
        lc.transition(s, case, "COMMITTEE_REVIEW", f"risk {dec.outcome}", actor, env)
        self.emit(s, "risk.decision.recorded", RiskDecisionV1(case_id=case.id, risk_decision_id=dec.id,
                                                              outcome=dec.outcome, reason_codes=dec.reason_codes), rc)  # type: ignore[arg-type]
        rc.metrics["blocked"] = 1.0 if dec.outcome == "BLOCK" else 0.0
        blocking = [c.get("code") or c.get("reason_code") for c in (dec.checks or []) if isinstance(c, dict) and c.get("outcome") == dec.outcome] if dec.outcome != "ALLOW" else []
        why = ", ".join(blocking or list(dec.reason_codes)[:6])
        return RiskOutput(summary=f"risk {dec.outcome} {case.asset} {case.direction}" + (f": {why}" if why and dec.outcome != "ALLOW" else ""),
                          case_id=str(case.id), outcome=dec.outcome, reason_codes=dec.reason_codes)


# =============================================================== committee
class MemoOutput(WorkerOutput):
    case_id: str
    memo_id: str | None = None
    recommendation: str | None = None


def committee_inputs(s: Session, case: ResearchCase, dec: RiskDecision) -> dict[str, Any]:
    rows = list(s.execute(select(CaseEvidence).where(CaseEvidence.case_id == case.id).order_by(CaseEvidence.id)).scalars())
    latest: dict[tuple[str, str], CaseEvidence] = {}
    for e in rows:  # later evidence for the same check supersedes earlier evidence
        latest[(e.kind, str((e.data or {}).get("check", e.id)))] = e
    ev = sorted(latest.values(), key=lambda e: e.id)
    return jsonable({
        "case": {"id": str(case.id), "instrument_id": case.instrument_id, "asset": case.asset, "direction": case.direction,
                 "hypothesis": case.hypothesis},
        "evidence": [{"kind": e.kind, "stance": e.stance, "summary": e.summary, "producer": e.producer} for e in ev],
        "risk": {"id": str(dec.id), "outcome": dec.outcome, "reason_codes": dec.reason_codes, "checks": dec.checks,
                 "sizing": dec.sizing, "config_version": dec.config_version},
        "macro_events": next((e.data.get("events", []) for e in ev if e.kind == "MACRO"), []),
        "data_source": case.data_source,
    })


class CommitteeCoordinatorWorker(BaseWorker):
    spec = WorkerSpec(id="exec.committee_coordinator", department="executive", name="Investment Committee Coordinator",
                      responsibility="Combine validated research + risk into an advisory memo with dissent.", version=COMMITTEE_VERSION,
                      input_model=RiskDecisionV1, output_model=MemoOutput, subscribes=("risk.decision.recorded",), escalation_to="cio")

    def handle(self, s: Session, env: Envelope, rc: RunContext) -> MemoOutput:
        p = RiskDecisionV1.model_validate(env.payload)
        case = s.get(ResearchCase, p.case_id)
        dec = s.get(RiskDecision, p.risk_decision_id)
        if case is None or dec is None or case.state != "COMMITTEE_REVIEW":
            return MemoOutput(summary="not in COMMITTEE_REVIEW", case_id=str(p.case_id))
        actor = f"{self.spec.id}@{self.spec.version}"
        inputs = committee_inputs(s, case, dec)
        out = decide(inputs)
        memo = CommitteeMemo(case_id=case.id, risk_decision_id=dec.id, recommendation=out["recommendation"],
                             score=Decimal(out["score"]), memo=out, inputs=inputs, inputs_hash=sha256(inputs),
                             code_version=f"{code_version()}+committee-{COMMITTEE_VERSION}")
        s.add(memo)
        s.flush()
        rc.ref("committee_inputs", str(memo.id), memo.inputs_hash)
        state = {"RECOMMEND": "RECOMMENDED", "DEFER": "DEFERRED", "REJECT": "REJECTED", "BLOCKED": "BLOCKED"}[out["recommendation"]]
        lc.transition(s, case, state, out["rationale"], actor, env)
        cfg = risk_svc.active_config(s)
        ttl = int((cfg.config if cfg else {}).get("recommendation_ttl_hours", 12))
        if state in ("RECOMMENDED", "DEFERRED"):
            case.expires_at = utcnow() + timedelta(hours=ttl)
        audit.record(s, actor, "committee.memo", "committee_memo", memo.id,
                     {"case_id": str(case.id), "recommendation": out["recommendation"], "score": out["score"]})
        self.emit(s, "committee.memo.issued", {"case_id": case.id, "memo_id": memo.id, "recommendation": out["recommendation"]}, rc)
        return MemoOutput(summary=out["headline"], case_id=str(case.id), memo_id=str(memo.id), recommendation=out["recommendation"])


# =============================================================== position monitor
class MonitorOutput(WorkerOutput):
    evaluated: int = 0
    exits: int = 0


class PositionMonitorWorker(BaseWorker):
    spec = WorkerSpec(id="risk.position_monitor", department="risk", name="Position Monitor",
                      responsibility="HOLD / EXIT / REVIEW on CIO-recorded positions.", version="1.0.0",
                      output_model=MonitorOutput, schedule_seconds=60, subscribes=("market.candles.ingested", "position.recorded"),
                      escalation_to="cio", default_config={"repeat_unchanged_minutes": 60})

    def handle(self, s: Session, env: Envelope, rc: RunContext) -> MonitorOutput:
        return self.run(s, rc)

    def run(self, s: Session, rc: RunContext) -> MonitorOutput:
        now = utcnow()
        gran = self.ctx.settings.candle_granularity
        cfg_row = risk_svc.active_config(s)
        cfg = {**risk_svc.DEFAULT_RISK_CONFIG, **(cfg_row.config if cfg_row else {})}
        from meridian.portfolio import service as portfolio
        eq = portfolio.equity(s)
        mstatus, events = risk_svc.macro_context(s, now, hours_back=1, hours_ahead=2)
        n = exits = 0
        for pos in s.execute(select(Position).where(Position.status == "OPEN")).scalars():
            inst = s.get(Instrument, pos.instrument_id)
            rows = md.load_candles(s, pos.instrument_id, gran, limit=120)
            fr = s.get(md.DataFreshness, f"candles:{pos.instrument_id}:{gran}")
            closes = [float(r.close) for r in rows]
            ef, es = ema(closes, 20), ema(closes, 50)
            blackout = None
            before = timedelta(minutes=int(cfg["event_blackout_minutes_before"]))
            for e in relevant_events(events, inst.asset if inst else "", cfg):
                if e["event_time"] and e["time_confidence"] == "EXACT":
                    t = _parse(e["event_time"])
                    if t - before <= now <= t:
                        blackout = e["title"]
            last = rows[-1] if rows else None
            action, codes, detail = advise(MonitorInputs(
                now=now, direction=pos.direction, entry=pos.entry_price, stop=pos.stop_price, target=pos.target_price,
                quantity=pos.quantity, contract_size=pos.contract_size, equity=eq,
                last_price=last.close if last else None, last_low=last.low if last else None, last_high=last.high if last else None,
                data_fresh=(fr.status == "FRESH") if fr else None, ema_fast=ef[-1] if ef else None, ema_slow=es[-1] if es else None,
                contract_expiry=inst.contract_expiry if inst else None, min_days_to_expiry=int(cfg["min_days_to_expiry"]),
                max_loss_pct=Decimal(str(cfg["max_loss_per_trade_pct"])), blackout_event=blackout))
            prev = s.execute(select(PositionAdvisory).where(PositionAdvisory.position_id == pos.id)
                             .order_by(PositionAdvisory.id.desc()).limit(1)).scalar()
            changed = prev is None or prev.action != action or sorted(prev.reason_codes) != sorted(codes)
            due = prev is None or now - prev.created_at > timedelta(minutes=int(rc.config["repeat_unchanged_minutes"]))
            if changed or due:
                s.add(PositionAdvisory(position_id=pos.id, action=action, reason_codes=codes,
                                       detail=jsonable({**detail, "as_of": last.ts.isoformat() if last else None})))
                self.emit(s, "position.advisory.issued", {"position_id": pos.id, "action": action,
                                                          "reason_codes": codes, "changed": changed}, rc)
                if changed:
                    audit.record(s, f"{self.spec.id}@{self.spec.version}", "position.advisory", "position", pos.id,
                                 {"action": action, "reason_codes": codes})
            n += 1
            exits += action == "EXIT"
        rc.metrics.update({"evaluated": n, "exits": exits})
        return MonitorOutput(summary=f"{n} position(s), {exits} EXIT", evaluated=n, exits=exits)


CORE_WORKERS: list[type[BaseWorker]] = [
    InstrumentDiscoveryWorker, MarketIngestWorker, FreshnessMonitorWorker, MacroCalendarWorker, TrendSignalWorker,
    ChiefOfStaffWorker, ResearchOfficerWorker, RiskControlWorker, CommitteeCoordinatorWorker, PositionMonitorWorker,
]

from meridian.analysts.desk import DESK_WORKERS  # noqa: E402
from meridian.analysts.macro_llm import MACRO_LLM_WORKERS  # noqa: E402
from meridian.analysts.market import MARKET_ANALYSTS  # noqa: E402
from meridian.predictions.workers import PREDICTION_WORKERS  # noqa: E402
from meridian.research.workers import RESEARCH_WORKERS  # noqa: E402

ALL_WORKERS: list[type[BaseWorker]] = CORE_WORKERS + MARKET_ANALYSTS + DESK_WORKERS + MACRO_LLM_WORKERS + RESEARCH_WORKERS + PREDICTION_WORKERS

__all__ = ["ALL_WORKERS", "BaseModel", "uuid"]
