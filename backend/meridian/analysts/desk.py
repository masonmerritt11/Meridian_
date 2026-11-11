"""Risk, portfolio, operations and executive desk workers (deterministic)."""
from __future__ import annotations

import math
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from meridian.analysts.base import AnalystOutput, AnalystWorker, Assessment, MarketContext, latest, latest_by_worker
from meridian.analysts.market import spec
from meridian.core.audit import verify_chain
from meridian.core.util import utcnow
from meridian.db.models import (
    AuditLog,
    CommitteeMemo,
    DataFreshness,
    DeadLetter,
    Event,
    Position,
    PositionAdvisory,
    RecommendationOutcome,
    ResearchCase,
    ResearchNote,
    WorkerDefinition,
    WorkerHeartbeat,
    WorkerRun,
)
from meridian.marketdata import service as md
from meridian.marketdata.types import ASSETS, margin_rate
from meridian.portfolio import service as portfolio
from meridian.registry.service import worker_health
from meridian.risk import service as risk_svc
from meridian.risk.engine import DEFAULT_RISK_CONFIG, relevant_events
from meridian.workers.base import RunContext
from meridian.workers.indicators import atr


def _cfg(s: Session) -> dict:
    row = risk_svc.active_config(s)
    return {**DEFAULT_RISK_CONFIG, **(row.config if row else {})}


# ======================================================================= risk
class ExposureAnalyst(AnalystWorker):
    spec = spec("risk.exposure", "risk", "Exposure Analyst", "Gross/net notional and capital in use, by asset and group.",
                every=900, esc="risk.chief_risk_officer")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        cfg = _cfg(s)
        views = portfolio.open_position_views(s, cfg.get("margin_per_contract") or {})
        eq = float(portfolio.equity(s) or 0)
        gross = sum(float(v.notional_usd) for v in views)
        net = sum(float(v.notional_usd) * (1 if v.direction == "LONG" else -1) for v in views)
        cap = sum(float(v.capital_used_usd or 0) for v in views)
        by_group: dict[str, float] = {}
        for v in views:
            by_group[v.correlation_group] = by_group.get(v.correlation_group, 0) + float(v.notional_usd)
        lev = gross / eq if eq else 0
        return [Assessment("PORTFOLIO", "FLAT" if not views else "EXPOSED",
                           f"{len(views)} open; gross ${gross:,.0f} ({lev:.1f}x equity), net ${net:+,.0f}, capital in use ${cap:,.0f} of ${eq:,.0f}.",
                           None, 1.0, {"gross": gross, "net": net, "capital_used": cap, "leverage": round(lev, 2), "by_group": by_group})]


class LimitsOfficer(AnalystWorker):
    spec = spec("risk.limits", "risk", "Limits Officer", "Daily/weekly loss and open-risk usage against CIO limits.",
                every=900, esc="cio")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        cfg = _cfg(s)
        eq = portfolio.equity(s) or Decimal(0)
        snap = portfolio.snapshot(s)
        day_lim = eq * Decimal(str(cfg["daily_loss_limit_pct"]))
        wk_lim = eq * Decimal(str(cfg["weekly_loss_limit_pct"]))
        risk_lim = eq * Decimal(str(cfg["max_total_open_risk_pct"]))
        used = [(-Decimal(snap["realized_today"])) / day_lim if day_lim else 0,
                (-Decimal(snap["realized_week"])) / wk_lim if wk_lim else 0,
                Decimal(snap["open_risk_usd"]) / risk_lim if risk_lim else 0]
        worst = max(float(u) for u in used)
        lab = "BREACHED" if worst >= 1 else "WARNING" if worst >= 0.7 else "OK"
        return [Assessment("PORTFOLIO", lab,
                           f"Daily loss {float(used[0]):.0%} of limit, weekly {float(used[1]):.0%}, open risk {float(used[2]):.0%} of cap.",
                           None, 1.0, {"daily_used": float(used[0]), "weekly_used": float(used[1]), "open_risk_used": float(used[2])})]


BANNED = re.compile(r"(/orders\b|create_order|place_order|cancel_order|edit_order|submit_order|/brokerage/orders)", re.I)  # banned-pattern definition (detection only)
REQUIRED_TRIGGERS = ("trg_case_risk_veto", "trg_memo_risk_veto", "trg_audit_append_only",
                     "trg_risk_decisions_append_only", "trg_risk_config_immutable")


class ComplianceOfficer(AnalystWorker):
    spec = spec("risk.compliance", "risk", "Compliance Officer",
                "Verifies the advisory-only boundary: no order code, veto triggers present, audit chain intact.",
                every=6 * 3600, esc="cio")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        problems = []
        pkg = Path(__file__).resolve().parents[1]
        for p in pkg.rglob("*.py"):
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if BANNED.search(line) and "BANNED" not in line and "banned" not in line:
                    problems.append(f"order-like code at {p.name}:{i}")
        have = {r[0] for r in s.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
        problems += [f"missing trigger {t}" for t in REQUIRED_TRIGGERS if t not in have]
        ok, bad = verify_chain(s)
        if not ok:
            problems.append(f"audit chain broken at {bad}")
        if risk_svc.active_config(s) is None:
            problems.append("no active risk config")
        return [Assessment("SYSTEM", "FAIL" if problems else "PASS",
                           "; ".join(problems) if problems else "Advisory-only boundary verified: no order code, veto triggers present, audit chain intact.",
                           None, 1.0, {"problems": problems})]


class EventRiskAnalyst(AnalystWorker):
    spec = spec("risk.event_risk", "risk", "Event Risk Analyst", "Upcoming high-impact events per asset in the next 24 hours.",
                every=1800, esc="risk.chief_risk_officer")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        from datetime import datetime
        cfg = _cfg(s)
        status, events = risk_svc.macro_context(s, utcnow(), hours_back=0, hours_ahead=24)
        out = []
        for a in ctx.instruments:
            rel = [e for e in relevant_events(events, a, cfg) if e.get("event_time")]
            if status != "FRESH":
                out.append(Assessment(a, "CALENDAR_UNAVAILABLE", f"Macro calendar {status}; event risk unknown.", None, 0.2,
                                      {}, None))
                continue
            if not rel:
                out.append(Assessment(a, "CLEAR", "No relevant high-impact events in the next 24 hours.", None, 0.6, {}))
                continue
            nxt = min(rel, key=lambda e: e["event_time"])
            hrs = (datetime.fromisoformat(nxt["event_time"]) - utcnow()).total_seconds() / 3600
            out.append(Assessment(a, "EVENT_SOON" if hrs <= 6 else "EVENT_TODAY",
                                  f"{len(rel)} event(s); next: {nxt['title']} in {hrs:.1f}h.", None, 0.6,
                                  {"events": rel[:5]}, "OPPOSES" if hrs <= 6 else None))
        return out


# ======================================================================= portfolio
class PositionSizer(AnalystWorker):
    spec = spec("portfolio.sizer", "portfolio", "Position Sizer",
                "Contracts that fit the per-position loss budget at a 2xATR stop, and cash each needs.", esc="risk.chief_risk_officer")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        cfg = _cfg(s)
        eq = float(portfolio.equity(s) or 0)
        budget = eq * float(cfg["max_loss_per_trade_pct"])
        out = []
        for a, inst in ctx.instruments.items():
            cl, hi, lo = ctx.closes(a), ctx.highs(a), ctx.lows(a)
            at = atr(hi, lo, cl)
            if not at or not inst.contract_size:
                continue
            risk_unit = 2 * at[-1] * float(inst.contract_size)
            units = int(budget // risk_unit) if risk_unit else 0
            rate = margin_rate(inst.raw, "LONG")
            cash = float(rate) * cl[-1] * float(inst.contract_size) if rate else None
            if units < 1:
                out.append(Assessment(a, "TOO_BIG", f"One contract risks ${risk_unit:,.0f} at a 2xATR stop; budget is ${budget:,.0f}.",
                                      None, 1.0, {"risk_per_contract": round(risk_unit, 2), "units": 0}, "OPPOSES"))
            else:
                out.append(Assessment(a, f"FITS_{units}", f"{units} contract(s) fit the ${budget:,.0f} budget (${risk_unit:,.0f} risk each"
                                      + (f", ~${cash:,.0f} cash each)." if cash else ")."), None, 1.0,
                                      {"risk_per_contract": round(risk_unit, 2), "units": units,
                                       "cash_per_contract": round(cash, 2) if cash else None}))
        return out


class CorrelationBudgeter(AnalystWorker):
    spec = spec("portfolio.correlation_budget", "portfolio", "Correlation Budgeter",
                "How many truly independent bets the open book and current plan represent.", esc="risk.chief_risk_officer")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        from meridian.portfolio.constructor import plan_from_db
        corr = latest(s, "vol.correlation", "MARKET", timedelta(hours=6))
        if corr is None:
            return []
        m = corr.data.get("matrix", {})
        book = [(v.asset, 1 if v.direction == "LONG" else -1) for v in portfolio.open_position_views(s, {})]
        book += [(a["asset"], 1 if a["direction"] == "LONG" else -1) for a in plan_from_db(s)["allocations"]]
        if len(book) < 2:
            return [Assessment("PORTFOLIO", "SINGLE_BET" if book else "EMPTY", f"{len(book)} position(s) open or planned.", None, 0.8,
                               {"positions": len(book)})]
        n = len(book)
        tot = sum(m.get(a, {}).get(b, 0) * da * db for a, da in book for b, db in book)
        eff = n * n / tot if tot > 0 else n
        return [Assessment("PORTFOLIO", "CONCENTRATED" if eff < 1.5 else "DIVERSIFIED",
                           f"{n} positions behave like {eff:.1f} independent bets (direction-adjusted correlation).", None, 0.8,
                           {"positions": n, "effective_bets": round(eff, 2)})]


class Attribution(AnalystWorker):
    """Track record: for every RECOMMEND memo, whether price reached target or stop first (taken or not)."""
    spec = spec("portfolio.attribution", "portfolio", "Performance Attribution Analyst",
                "Recommendation track record (target vs stop first) and realized P&L by asset.", esc="exec.chief_of_staff")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        now = utcnow()
        for m in s.execute(select(CommitteeMemo).where(CommitteeMemo.recommendation == "RECOMMEND")).scalars():
            row = s.get(RecommendationOutcome, m.id)
            if row is not None and row.outcome != "OPEN":
                continue
            case = s.get(ResearchCase, m.case_id)
            t = m.memo.get("trade", {})
            try:
                entry, stop, target = float(t["entry_reference"]), float(t["stop"]), float(t["target"])
            except (KeyError, TypeError, ValueError):
                continue
            bars = [b for b in md.load_candles(s, case.instrument_id, self.ctx.settings.candle_granularity, limit=2000)
                    if b.ts > m.created_at]
            outcome, r_mult = "OPEN", None
            long = case.direction == "LONG"
            risk = abs(entry - stop)
            for b in bars:
                hit_stop = float(b.low) <= stop if long else float(b.high) >= stop
                hit_tgt = float(b.high) >= target if long else float(b.low) <= target
                if hit_stop:  # conservative: if both in one bar, count the stop
                    outcome, r_mult = "STOP_FIRST", -1.0
                    break
                if hit_tgt:
                    outcome, r_mult = "TARGET_FIRST", abs(target - entry) / risk if risk else None
                    break
            if outcome == "OPEN" and bars and now - m.created_at > timedelta(days=10):
                last = float(bars[-1].close)
                outcome, r_mult = "EXPIRED_FLAT", ((last - entry) if long else (entry - last)) / risk if risk else None
            if row is None:
                row = RecommendationOutcome(memo_id=m.id, case_id=case.id, asset=case.asset, direction=case.direction, outcome=outcome)
                s.add(row)
            row.outcome, row.bars_observed, row.updated_at = outcome, len(bars), now
            row.r_multiple = Decimal(str(round(r_mult, 4))) if r_mult is not None else None
            if outcome != "OPEN":
                row.resolved_at = now
        s.flush()
        rows = list(s.execute(select(RecommendationOutcome)).scalars())
        done = [r for r in rows if r.outcome in ("TARGET_FIRST", "STOP_FIRST", "EXPIRED_FLAT")]
        wins = sum(1 for r in done if r.outcome == "TARGET_FIRST")
        avg_r = sum(float(r.r_multiple or 0) for r in done) / len(done) if done else 0
        by_asset: dict[str, dict] = {}
        for r in done:
            d = by_asset.setdefault(r.asset, {"n": 0, "wins": 0, "r": 0.0})
            d["n"] += 1
            d["wins"] += r.outcome == "TARGET_FIRST"
            d["r"] += float(r.r_multiple or 0)
        realized = {}
        for p in s.execute(select(Position).where(Position.status == "CLOSED")).scalars():
            realized[p.instrument_id] = realized.get(p.instrument_id, 0) + float(p.realized_pnl or 0)
        summ = (f"{len(done)} resolved recommendations: {wins} hit target first ({wins / len(done):.0%}), "
                f"average {avg_r:+.2f}R; {len(rows) - len(done)} still open.") if done else \
            f"No resolved recommendations yet ({len(rows)} open). Track record builds as recommendations play out."
        return [Assessment("PORTFOLIO", "TRACK_RECORD", summ, None, min(1.0, len(done) / 30),
                           {"resolved": len(done), "wins": wins, "avg_r": round(avg_r, 3), "by_asset": by_asset,
                            "realized_by_instrument": realized})]


class PnlTracker(AnalystWorker):
    spec = spec("portfolio.pnl", "portfolio", "Equity & PnL Tracker", "Equity, realized and marked-to-market P&L from recorded positions.",
                every=1800, esc="exec.chief_of_staff")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        eq = portfolio.equity(s) or Decimal(0)
        unreal = Decimal(0)
        marked = 0
        for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars():
            last = md.load_candles(s, p.instrument_id, self.ctx.settings.candle_granularity, limit=1)
            if last:
                unreal += portfolio.pnl_for(p, last[-1].close)
                marked += 1
        snap = portfolio.snapshot(s)
        return [Assessment("PORTFOLIO", "EQUITY", f"Equity ${eq:,.2f} realized; open P&L ${unreal:+,.2f} on {marked} position(s); "
                           f"marked equity ${eq + unreal:,.2f}.", None, 1.0,
                           {"equity": float(eq), "unrealized": float(unreal), "marked_equity": float(eq + unreal),
                            "realized_today": snap["realized_today"], "realized_week": snap["realized_week"]})]


# ======================================================================= data & operations
class ReliabilityEngineer(AnalystWorker):
    spec = spec("data.reliability", "data_ops", "Platform Reliability Engineer",
                "Worker health, outbox lag, dead letters and failure rate; escalates problems.", every=300, esc="cio")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        hbs = {h.worker_id: h for h in s.execute(select(WorkerHeartbeat)).scalars()}
        bad = []
        for d in s.execute(select(WorkerDefinition).where(WorkerDefinition.implemented.is_(True),
                                                          WorkerDefinition.status == "ACTIVE")).scalars():
            h = worker_health(d, hbs.get(d.id))["health"]
            if h in ("FAILING", "STALE"):
                bad.append(f"{d.id} {h}")
        lag = s.execute(select(func.count()).select_from(Event).where(Event.published_at.is_(None),
                                                                      Event.occurred_at < utcnow() - timedelta(minutes=2))).scalar()
        dlq = s.execute(select(func.count()).select_from(DeadLetter).where(DeadLetter.resolved_at.is_(None))).scalar()
        since = utcnow() - timedelta(hours=24)
        runs = s.execute(select(WorkerRun.status, func.count()).where(WorkerRun.started_at >= since).group_by(WorkerRun.status)).all()
        rc_ = dict(runs)
        fail_rate = rc_.get("FAILED", 0) / max(1, sum(rc_.values()))
        lab = "DEGRADED" if bad or lag or dlq or fail_rate > 0.05 else "HEALTHY"
        return [Assessment("SYSTEM", lab, (f"{len(bad)} unhealthy worker(s){': ' + ', '.join(bad[:5]) if bad else ''}; "
                                           f"outbox lag {lag}; dead letters {dlq}; 24h failure rate {fail_rate:.1%}."),
                           None, 1.0, {"unhealthy": bad, "outbox_lag": lag, "dlq": dlq, "runs_24h": rc_})]


class AuditArchivist(AnalystWorker):
    spec = spec("data.archivist", "data_ops", "Audit Archivist", "Verifies the audit hash chain and reports record counts.",
                every=6 * 3600, esc="risk.compliance")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        ok, bad = verify_chain(s)
        n = s.execute(select(func.count()).select_from(AuditLog)).scalar()
        stale = s.execute(select(func.count()).select_from(DataFreshness).where(DataFreshness.status.not_in(("FRESH", "RETIRED")))).scalar()
        return [Assessment("SYSTEM", "VERIFIED" if ok else "BROKEN",
                           f"Audit chain {'intact' if ok else f'BROKEN at entry {bad}'} across {n:,} entries; {stale} dataset(s) not fresh.",
                           None, 1.0, {"entries": n, "chain_ok": ok, "first_bad": bad})]


# ======================================================================= executive
class ChiefMacroOfficer(AnalystWorker):
    spec = spec("exec.chief_macro_officer", "executive", "Chief Macro Officer",
                "Combines the macro desk (central banks, inflation, rates/dollar, energy, geopolitics, regime) into one view per asset.",
                every=3600, esc="cio")
    min_bars = 0
    members = ("macro.central_bank", "macro.inflation", "macro.rates_dollar", "macro.energy", "macro.geopolitical", "macro.regime")

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        out = []
        for a in ctx.instruments:
            rows = [r for r in latest_by_worker(s, a, timedelta(hours=26)) if r.worker_id in self.members and r.score is not None]
            if not rows:
                continue
            w = [float(r.confidence or 0.3) for r in rows]
            sc = sum(float(r.score) * wi for r, wi in zip(rows, w, strict=True)) / sum(w)
            out.append(Assessment(a, "MACRO_TAILWIND" if sc >= 0.2 else "MACRO_HEADWIND" if sc <= -0.2 else "MACRO_NEUTRAL",
                                  f"Macro desk ({len(rows)} views) nets {sc:+.2f}: "
                                  + "; ".join(f"{r.worker_id.split('.')[1]} {float(r.score):+.1f}" for r in rows),
                                  sc, min(1.0, len(rows) / len(self.members)), {"members": {r.worker_id: float(r.score) for r in rows}}))
        return out


class CioLiaison(AnalystWorker):
    spec = spec("exec.cio_liaison", "executive", "CIO Liaison", "Writes the CIO's brief: plan, positions, alerts, research.",
                every=3600, esc="cio")
    min_bars = 0

    def analyze(self, s, ctx, rc):  # noqa: ANN001, ANN201
        from meridian.portfolio.constructor import plan_from_db
        plan = plan_from_db(s)
        lines = []
        allocs = plan["allocations"]
        lines.append(f"Plan: {len(allocs)} position(s) "
                     + (", ".join(f"{a['direction']} {a['quantity']} {a['asset']}" for a in allocs) if allocs else "(nothing qualifies)")
                     + f"; idle cash ${plan['totals']['idle_capital_usd']}.")
        exits = []
        for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars():
            adv = s.execute(select(PositionAdvisory).where(PositionAdvisory.position_id == p.id)
                            .order_by(desc(PositionAdvisory.id)).limit(1)).scalar()
            if adv and adv.action != "HOLD":
                exits.append(f"{adv.action} {p.instrument_id} ({', '.join(adv.reason_codes)})")
        lines.append("Positions: " + ("; ".join(exits) if exits else "no exit or review advisories."))
        for wid, label in (("data.reliability", "System"), ("risk.limits", "Limits"), ("portfolio.attribution", "Track record")):
            subj = "SYSTEM" if wid == "data.reliability" else "PORTFOLIO"
            r = latest(s, wid, subj, timedelta(hours=6))
            if r:
                lines.append(f"{label}: {r.summary}")
        syn = s.execute(select(ResearchNote).where(ResearchNote.kind == "SYNTHESIS").order_by(desc(ResearchNote.created_at)).limit(1)).scalar()
        if syn:
            lines.append(f"Research: {syn.title}")
        return [Assessment("PORTFOLIO", "DAILY_BRIEF", " ".join(lines), None, 1.0, {"lines": lines})]


DESK_WORKERS = [ExposureAnalyst, LimitsOfficer, ComplianceOfficer, EventRiskAnalyst, PositionSizer, CorrelationBudgeter,
                Attribution, PnlTracker, ReliabilityEngineer, AuditArchivist, ChiefMacroOfficer, CioLiaison]

_ = (math, MarketContext, RunContext, AnalystOutput, ASSETS)
