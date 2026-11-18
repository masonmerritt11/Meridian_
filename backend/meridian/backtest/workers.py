"""Backtest Lab department: History Loader, Strategy Lab (proposals), Trade Tester (every recommendation paper-tested)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from meridian.backtest import engine as bt
from meridian.core.util import jsonable, utcnow
from meridian.db.models import (
    BtCandle,
    BtRun,
    CommitteeMemo,
    Instrument,
    RecSim,
    ResearchCase,
    StrategyProposal,
    WorkerDefinition,
)
from meridian.marketdata import service as md
from meridian.marketdata.coinbase import ReadOnlyPublicClient
from meridian.workers.base import BaseWorker, RunContext, WorkerContext, WorkerOutput, WorkerSpec
from meridian.workers.indicators import TrendParams

# Long hourly histories on Coinbase: spot markets for crypto, PAXG (a token backed 1:1 by physical gold) for gold.
SPOT_PROXIES = {"BTC": "BTC-USD", "ETH": "ETH-USD", "XRP": "XRP-USD", "GOLD": "PAXG-USD"}
CONTRACT_ASSETS = ("SILVER", "OIL")  # no long spot proxy: each futures contract is tested on its own history
TREND_WORKER = "trend.signal_analyst"


class CandleSource(Protocol):
    def hourly(self, product: str, start: datetime, end: datetime) -> list[tuple[datetime, float, float, float, float]]: ...


class CoinbaseHourly:
    def __init__(self, client: ReadOnlyPublicClient | None = None):
        self.client = client or ReadOnlyPublicClient()

    def hourly(self, product: str, start: datetime, end: datetime) -> list[tuple[datetime, float, float, float, float]]:
        data = self.client.get(f"/api/v3/brokerage/market/products/{product}/candles",
                               {"start": str(int(start.timestamp())), "end": str(int(end.timestamp())),
                                "granularity": "ONE_HOUR", "limit": 350})
        out = []
        for c in data.get("candles") or []:
            try:
                out.append((datetime.fromtimestamp(int(c["start"]), UTC), float(c["open"]), float(c["high"]),
                            float(c["low"]), float(c["close"])))
            except (KeyError, ValueError):
                continue
        return sorted(out)


def _source(ctx: WorkerContext) -> CandleSource | None:
    src = ctx.services.get("bt_source")
    if src is None and ctx.settings.market_data_mode == "coinbase":
        mk = ctx.services.get("market")
        src = CoinbaseHourly(getattr(mk, "client", None))
    return src


def products(s: Session) -> list[tuple[str, str]]:
    out = [(a, p) for a, p in SPOT_PROXIES.items()]
    for i in s.execute(select(Instrument).where(Instrument.asset.in_(CONTRACT_ASSETS))).scalars():
        out.append((i.asset, i.id))
    return out


def live_trend_config(s: Session) -> tuple[TrendParams, int, dict[str, Any]]:
    w = s.get(WorkerDefinition, TREND_WORKER)
    cfg = dict(w.config or {}) if w else {}
    return TrendParams.from_config(cfg), (w.config_version if w else 0), cfg


def load_series(s: Session) -> list[bt.Series]:
    rows = s.execute(select(BtCandle).order_by(BtCandle.product, BtCandle.ts)).scalars()
    by: dict[str, bt.Series] = {}
    for r in rows:
        ser = by.get(r.product)
        if ser is None:
            ser = by[r.product] = bt.Series(r.asset, r.product, [], [], [], [], [])
        ser.ts.append(r.ts)
        ser.o.append(float(r.open)); ser.h.append(float(r.high)); ser.l.append(float(r.low)); ser.c.append(float(r.close))  # noqa: E702
    return [x for x in by.values() if len(x.c) >= 300]


# ------------------------------------------------------------------ History Loader
class LoaderOutput(WorkerOutput):
    status: str = "OK"
    bars_added: int = 0


class BtHistorian(BaseWorker):
    slow = True
    spec = WorkerSpec(id="bt.historian", department="backtest", name="History Loader",
                      responsibility="Keeps up to two years of hourly charts for every asset (spot proxies and each futures contract).",
                      version="1.0.0", output_model=LoaderOutput, schedule_seconds=6 * 3600, max_retries=1,
                      escalation_to="bt.strategy_lab", default_config={"history_days": 730, "max_requests": 400})

    def run(self, s: Session, rc: RunContext) -> LoaderOutput:
        src = _source(self.ctx)
        if src is None:
            return LoaderOutput(summary="IDLE: backtest history loads only with live market data", status="IDLE")
        now = utcnow().replace(minute=0, second=0, microsecond=0)
        floor = now - timedelta(days=int(rc.config["history_days"]))
        budget, added, per = int(rc.config["max_requests"]), 0, {}
        step = timedelta(hours=340)
        for asset, product in products(s):
            lo, hi = s.execute(select(func.min(BtCandle.ts), func.max(BtCandle.ts)).where(BtCandle.product == product)).first()
            n0 = added
            # forward: from the newest stored bar to now
            cur = (hi + timedelta(hours=1)) if hi else now - step
            while cur < now and budget > 0:
                rows = src.hourly(product, cur, min(cur + step, now))
                budget -= 1
                added += self._save(s, asset, product, rows)
                cur += step
            # backward: older history until the floor or until the market has no older data
            cur = lo or (now - step)
            empty = 0
            while cur > floor and budget > 0 and empty < 2:
                rows = src.hourly(product, max(cur - step, floor), cur)
                budget -= 1
                got = self._save(s, asset, product, rows)
                added += got
                empty = empty + 1 if got == 0 else 0
                cur -= step
            s.commit()
            per[product] = added - n0
        rc.metrics.update({"bars_added": added, "by_product": per, "requests_left": budget})
        total = s.execute(select(func.count()).select_from(BtCandle)).scalar()
        return LoaderOutput(summary=f"{added} hourly bars added ({total} stored) across {len(per)} markets", bars_added=added)

    @staticmethod
    def _save(s: Session, asset: str, product: str, rows: list[tuple[datetime, float, float, float, float]]) -> int:
        """Returns how many of these bars were new."""
        if not rows:
            return 0
        before = s.execute(select(func.count()).select_from(BtCandle).where(BtCandle.product == product)).scalar() or 0
        vals = [{"product": product, "ts": t, "asset": asset, "open": Decimal(str(o)), "high": Decimal(str(h)),
                 "low": Decimal(str(l)), "close": Decimal(str(c))} for t, o, h, l, c in rows]  # noqa: E741
        s.execute(pg_insert(BtCandle).values(vals).on_conflict_do_nothing())
        s.flush()
        return (s.execute(select(func.count()).select_from(BtCandle).where(BtCandle.product == product)).scalar() or 0) - before


# ------------------------------------------------------------------ Strategy Lab
class LabOutput(WorkerOutput):
    status: str = "OK"
    tested: int = 0
    proposals: int = 0


def _verdict_json(v: bt.Verdict, curves: bool) -> dict[str, Any]:
    d = v.as_dict()
    if not curves:
        d.pop("base_curve", None)
        d.pop("var_curve", None)
    return jsonable(d)


class BtStrategyLab(BaseWorker):
    slow = True
    spec = WorkerSpec(id="bt.strategy_lab", department="backtest", name="Strategy Lab",
                      responsibility="Replays the live strategy on years of hourly charts, tests small changes, and proposes only "
                                     "those that beat it on data they were never tuned on.",
                      version="1.0.0", output_model=LabOutput, schedule_seconds=6 * 3600, max_retries=1, escalation_to="cio",
                      default_config={"reject_cooldown_days": 30})

    def run(self, s: Session, rc: RunContext) -> LabOutput:
        series = load_series(s)
        if not series:
            return LabOutput(summary="WAITING: no history loaded yet", status="WAITING")
        base, ver, cfg = live_trend_config(s)
        res = bt.lab(series, base)
        verdicts: list[bt.Verdict] = res["verdicts"]
        passed = [v for v in verdicts if v.passed]
        coverage = [{"asset": x.asset, "product": x.product, "bars": len(x.c), "from": x.ts[0], "to": x.ts[-1]} for x in series]
        ranked = sorted(verdicts, key=lambda v: -(v.var_test.total_r - v.base_test.total_r))
        s.add(BtRun(config_version=ver, config=jsonable(cfg), result=jsonable({
            "cutoff": res["cutoff"], "start": res["start"], "end": res["end"], "baseline": res["baseline"], "coverage": coverage,
            "tested": len(verdicts), "passed": len(passed),
            "variants": [_verdict_json(v, False) for v in ranked]})))
        now = utcnow()
        keys = {bt.key(v.overrides): v for v in passed}
        for p in s.execute(select(StrategyProposal).where(StrategyProposal.status == "PROPOSED")).scalars():
            if p.base_config_version != ver:
                p.status, p.note, p.updated_at = "SUPERSEDED", "the live strategy changed; re-tested against the new version", now
            elif p.key not in keys:
                p.status, p.note, p.updated_at = "EXPIRED", "the evidence no longer holds on the latest data", now
            else:
                p.evidence, p.updated_at = _verdict_json(keys.pop(p.key), True) | {"range": jsonable(coverage)}, now
        cooldown = now - timedelta(days=int(rc.config["reject_cooldown_days"]))
        created = 0
        for k, v in keys.items():
            recent_reject = s.execute(select(StrategyProposal.id).where(
                StrategyProposal.key == k, StrategyProposal.status == "REJECTED", StrategyProposal.decided_at >= cooldown)).first()
            if recent_reject:
                continue
            s.add(StrategyProposal(key=k, title=v.title, overrides=jsonable(v.overrides), base_config=jsonable(cfg),
                                   base_config_version=ver, evidence=_verdict_json(v, True) | {"range": jsonable(coverage)}))
            created += 1
        rc.metrics.update({"series": len(series), "tested": len(verdicts), "passed": len(passed), "new_proposals": created,
                           "baseline_trades": res["baseline"]["all"]["n"]})
        b = res["baseline"]["all"]
        return LabOutput(summary=f"live strategy: {b['n']} trades, {b['avg_r']:+.2f}R avg; {len(verdicts)} changes tested, "
                                 f"{len(passed)} proven, {created} new proposal(s)", tested=len(verdicts), proposals=created)


# ------------------------------------------------------------------ Trade Tester
class TesterOutput(WorkerOutput):
    status: str = "OK"
    tested: int = 0


class BtTradeTester(BaseWorker):
    spec = WorkerSpec(id="bt.trade_tester", department="backtest", name="Trade Tester",
                      responsibility="Tests every recommendation (and every blocked or rejected idea) as if it had been placed: "
                                     "winner or loser, and whether the stop or target helped.",
                      version="1.0.0", output_model=TesterOutput, schedule_seconds=900, max_retries=1, escalation_to="bt.strategy_lab",
                      default_config={})

    def run(self, s: Session, rc: RunContext) -> TesterOutput:
        gran = self.ctx.settings.candle_granularity
        n = 0
        memos = s.execute(select(CommitteeMemo).order_by(CommitteeMemo.created_at)).scalars()
        for m in memos:
            row = s.get(RecSim, m.id)
            if row is not None and row.done:
                continue
            t = (m.memo or {}).get("trade") or {}
            case = s.get(ResearchCase, m.case_id)
            try:
                entry, stop, target = float(t["entry_reference"]), float(t["stop"]), float(t["target"])
            except (KeyError, TypeError, ValueError):
                continue
            if case is None:
                continue
            bars = [(float(b.high), float(b.low), float(b.close))
                    for b in md.load_candles(s, case.instrument_id, gran, limit=2000) if b.ts >= m.created_at]
            res = bt.paper_test(case.direction, entry, stop, target, bars)
            done = res["status"] == "NOT_FILLED" or (
                res.get("outcome") in ("STOP", "TARGET", "TIME") and
                (res.get("exit_bar") is None or len(bars) - res["exit_bar"] > bt.FOLLOW_BARS or utcnow() - m.created_at > timedelta(days=20)))
            if row is None:
                row = RecSim(memo_id=m.id, case_id=case.id, asset=case.asset, direction=case.direction, decision=m.recommendation,
                             made_at=m.created_at, entry=Decimal(str(entry)), stop=Decimal(str(stop)), target=Decimal(str(target)),
                             status=res["status"])
                s.add(row)
            row.status, row.outcome = res["status"], res.get("outcome")
            row.r = None if res.get("r") is None else Decimal(str(round(res["r"], 4)))
            row.result, row.done, row.updated_at = jsonable(res), done, utcnow()
            n += 1
        rc.metrics["tested"] = n
        return TesterOutput(summary=f"{n} recommendation(s) re-tested", tested=n)


# ------------------------------------------------------------------ summaries for the API
def rec_summary(s: Session) -> dict[str, Any]:
    rows = list(s.execute(select(RecSim).order_by(RecSim.made_at.desc())).scalars())
    groups: dict[str, Any] = {}
    for dec in ("RECOMMEND", "BLOCKED", "REJECT", "DEFER"):
        xs = [r for r in rows if r.decision == dec]
        filled = [r for r in xs if r.status == "FILLED"]
        resolved = [r for r in filled if r.outcome in ("STOP", "TARGET", "TIME")]
        rs = [float(r.r) for r in resolved if r.r is not None]
        groups[dec] = {"total": len(xs), "filled": len(filled), "not_filled": sum(1 for r in xs if r.status == "NOT_FILLED"),
                       "pending": sum(1 for r in xs if r.status == "PENDING"), "open": sum(1 for r in filled if r.outcome == "OPEN"),
                       "winners": sum(1 for x in rs if x > 0), "losers": sum(1 for x in rs if x <= 0),
                       "targets": sum(1 for r in resolved if r.outcome == "TARGET"), "stops": sum(1 for r in resolved if r.outcome == "STOP"),
                       "win_rate": (sum(1 for x in rs if x > 0) / len(rs)) if rs else None,
                       "avg_r": (sum(rs) / len(rs)) if rs else None, "total_r": sum(rs), "usd_at_70": sum(rs) * 70}
    recs = [r for r in rows if r.decision == "RECOMMEND" and r.status == "FILLED"]
    stops = [r for r in recs if r.outcome == "STOP"]
    tgts = [r for r in recs if r.outcome == "TARGET"]
    diag = {"stopped": len(stops),
            "stopped_then_target": sum(1 for r in stops if r.result.get("stopped_then_target")),
            "losers_avg_best_r_before_stop": (sum(r.result.get("mfe_r", 0) for r in stops) / len(stops)) if stops else None,
            "targets": len(tgts),
            "after_target_avg_extra_r": (sum(max(r.result.get("after_exit_best_r", 0) - float(r.r or 0), 0) for r in tgts) / len(tgts)) if tgts else None}
    grid: dict[str, dict[str, Any]] = {}
    for r in recs:
        for k, v in (r.result.get("alternatives") or {}).items():
            g = grid.setdefault(k, {"n": 0, "total_r": 0.0})
            if v is not None:
                g["n"] += 1
                g["total_r"] += v
    for g in grid.values():
        g["avg_r"] = g["total_r"] / g["n"] if g["n"] else None
    return jsonable({"groups": groups, "diagnostics": diag, "alternatives": grid,
                     "rows": [{"memo_id": r.memo_id, "case_id": r.case_id, "asset": r.asset, "direction": r.direction, "decision": r.decision,
                               "made_at": r.made_at, "entry": r.entry, "stop": r.stop, "target": r.target, "status": r.status,
                               "outcome": r.outcome, "r": r.r, "mfe_r": r.result.get("mfe_r"), "mae_r": r.result.get("mae_r"),
                               "stopped_then_target": r.result.get("stopped_then_target"), "note": r.result.get("note")}
                              for r in rows[:300]]})


BACKTEST_WORKERS: list[type[BaseWorker]] = [BtHistorian, BtStrategyLab, BtTradeTester]
