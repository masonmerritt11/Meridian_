"""Prediction Markets department workers: the Odds Historian (backfill) and the Strategy Lab (search + studies).
The Live Odds Desk is a 2-second runtime loop (predictions/live.py), not a scheduled worker."""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from meridian.core.util import jsonable
from meridian.db.models import PmBtcMinute, PmLabRun, PmTick, PmWindow
from meridian.predictions import backtest as bt
from meridian.predictions.kalshi import KalshiMarkets, MarketQuote
from meridian.predictions.store import BtcSource, CoinbaseBtc, load_windows, save_bars, save_btc, upsert_window
from meridian.workers.base import BaseWorker, RunContext, WorkerContext, WorkerOutput, WorkerSpec


def services(ctx: WorkerContext) -> tuple[KalshiMarkets | None, BtcSource | None]:
    """Real network clients only in live-data mode; tests and demos inject fakes."""
    k, b = ctx.services.get("kalshi"), ctx.services.get("btc")
    if (k is None or b is None) and ctx.settings.market_data_mode == "coinbase" and ctx.settings.pm_enabled:
        k = k or KalshiMarkets()
        mk = ctx.services.get("market")
        b = b or CoinbaseBtc(getattr(mk, "client", None))
    return k, b


class HistorianOutput(WorkerOutput):
    status: str = "OK"
    windows_listed: int = 0
    bars_loaded: int = 0
    btc_minutes: int = 0


class PmHistorian(BaseWorker):
    slow = True
    spec = WorkerSpec(id="pm.historian", department="predictions", name="Odds Historian",
                      responsibility="Backfills settled 15-minute windows (1-minute odds bars and outcomes) and BTC minutes.",
                      version="1.0.0", output_model=HistorianOutput, schedule_seconds=30 * 60, max_retries=1,
                      escalation_to="pm.strategy_lab",
                      default_config={"max_live_batches": 15, "max_historical_calls": 250, "tick_retention_days": 3})

    def run(self, s: Session, rc: RunContext) -> HistorianOutput:
        km, btc = services(self.ctx)
        if km is None or btc is None:
            return HistorianOutput(summary="IDLE: prediction desk runs only with live market data", status="IDLE")
        series = self.ctx.settings.pm_series
        now = datetime.now(UTC)
        since = now - timedelta(days=self.ctx.settings.pm_backfill_days)
        cutoff = km.cutoff()

        latest = s.execute(select(func.max(PmWindow.close_time)).where(PmWindow.series == series,
                                                                       PmWindow.result.isnot(None))).scalar()
        start = max(since, latest - timedelta(hours=2)) if latest else since
        listed: list[MarketQuote] = km.list_markets(series, "settled", min_close_ts=int(start.timestamp()))
        oldest = s.execute(select(func.min(PmWindow.open_time)).where(PmWindow.series == series)).scalar()
        if cutoff and cutoff > since and (oldest is None or oldest > since + timedelta(hours=6)):
            listed += [m for m in km.list_markets(series, "", historical=True, max_pages=10)
                       if m.close_time and m.close_time >= since]
        for q in listed:
            upsert_window(s, series, q)
        s.flush()

        todo = list(s.execute(select(PmWindow).where(PmWindow.series == series, PmWindow.bars_loaded.is_(False),
                                                     PmWindow.result.isnot(None), PmWindow.open_time >= since)
                              .order_by(PmWindow.open_time.desc())).scalars())
        quotes = [MarketQuote(w.ticker, None, w.open_time, w.close_time, w.strike, None, None, None, w.status, w.result, None)
                  for w in todo]
        recent = [q for q in quotes if not cutoff or (q.close_time and q.close_time >= cutoff)]
        old = [q for q in quotes if cutoff and q.close_time and q.close_time < cutoff]
        loaded = 0
        for i in range(0, min(len(recent), 100 * int(rc.config["max_live_batches"])), 100):
            chunk = recent[i:i + 100]
            for t, bars in km.batch_candles(chunk).items():
                if bars:
                    save_bars(s, t, bars)
                    loaded += 1
            s.commit()
        for q in old[: int(rc.config["max_historical_calls"])]:
            bars = km.candles(series, q, historical=True)
            if bars:
                save_bars(s, q.ticker, bars)
                loaded += 1
            if loaded % 25 == 0:
                s.commit()

        last_btc = s.execute(select(func.max(PmBtcMinute.ts))).scalar()
        b_start = (last_btc + timedelta(minutes=1)) if last_btc else since - timedelta(hours=2)
        n_btc = save_btc(s, btc.minutes(b_start, now)) if now - b_start > timedelta(minutes=1) else 0

        s.execute(delete(PmTick).where(PmTick.ts < now - timedelta(days=int(rc.config["tick_retention_days"]))))
        total = s.execute(select(func.count()).select_from(PmWindow).where(PmWindow.bars_loaded.is_(True))).scalar()
        rc.metrics.update({"listed": len(listed), "bars_loaded": loaded, "btc_minutes": n_btc, "windows_with_bars": total})
        return HistorianOutput(summary=f"{len(listed)} windows listed, odds history for {loaded} more "
                                       f"({total} total), {n_btc} BTC minutes",
                               windows_listed=len(listed), bars_loaded=loaded, btc_minutes=n_btc)


class LabOutput(WorkerOutput):
    status: str = "OK"
    windows: int = 0
    proven: int = 0


class PmStrategyLab(BaseWorker):
    slow = True
    spec = WorkerSpec(id="pm.strategy_lab", department="predictions", name="Strategy Lab",
                      responsibility="Tests thousands of entry/exit rules on past windows (train on older, verify on newer) "
                                     "and studies how the odds behave, including last-minute flips.",
                      version="1.0.0", output_model=LabOutput, schedule_seconds=60 * 60, max_retries=1,
                      escalation_to="cio", default_config={"min_windows": 300})

    def run(self, s: Session, rc: RunContext) -> LabOutput:
        series = self.ctx.settings.pm_series
        since = datetime.now(UTC) - timedelta(days=self.ctx.settings.pm_backfill_days)
        ws = load_windows(s, series, since)
        if len(ws) < int(rc.config["min_windows"]):
            return LabOutput(summary=f"WAITING: {len(ws)} windows with odds history; need {rc.config['min_windows']}",
                             status="WAITING", windows=len(ws))
        res = bt.run_lab(ws)
        result: dict[str, Any] = {**asdict(res), "series": series,
                                  "studies": {"calibration": bt.calibration(ws), "late_flips": bt.late_flips(ws),
                                              "underdog_bounce": bt.underdog_bounce(ws),
                                              "model_vs_market": [bt.model_vs_market(ws, m) for m in (5, 8, 11)]},
                                  "range": {"from": ws[0].open_time.isoformat(), "to": ws[-1].open_time.isoformat()}}
        s.add(PmLabRun(windows=len(ws), result=jsonable(result)))
        proven = sum(1 for r in res.rows if r["proven"])
        rc.metrics.update({"windows": len(ws), "tested": res.tested, "proven": proven})
        best = res.rows[0] if res.rows else None
        return LabOutput(summary=f"{res.tested} rules tested on {len(ws)} windows; {proven} proven"
                                 + (f"; best: {best['describe']}" if best and best["proven"] else ""),
                         windows=len(ws), proven=proven)


PREDICTION_WORKERS: list[type[BaseWorker]] = [PmHistorian, PmStrategyLab]
