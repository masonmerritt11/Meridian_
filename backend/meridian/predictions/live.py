"""Live Odds Desk: every couple of seconds, read the open 15-minute window and the real BTC price, price the window
from BTC, and raise or manage setups. Advisory only; the CIO places every order.

Hard rules (also enforced by a database trigger on pm_signals.exit_by):
  * worst-case loss per setup <= PM_MAX_LOSS_USD (the full premium plus fee; 0 if the side loses),
  * one setup of each kind per window,
  * no new setups in the final 4 minutes,
  * every setup carries an exit time at least 2 minutes before close; we never advise holding through close.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from meridian.core.logging import get_logger
from meridian.core.util import jsonable
from meridian.db.models import PmLabRun, PmSignal, PmTick
from meridian.predictions import backtest as bt
from meridian.predictions.kalshi import KalshiMarkets, MarketQuote
from meridian.predictions.model import (
    MUST_EXIT_BEFORE_CLOSE_SECONDS,
    NO_ENTRY_LAST_SECONDS,
    Sides,
    edge,
    fair_yes,
    fee_per_contract,
    sigma_from_closes,
    size,
)
from meridian.predictions.store import BtcSource, upsert_window

log = get_logger("meridian.predictions.live")
ALIGN_MIN_EDGE = 0.03         # underdog must be at least 3¢ cheaper than real BTC implies, after the taker fee
ALIGN_MINUTES = (2, 10)
ALIGN_EXIT_MINUTE = 12


def active_strategy(s: Session) -> tuple[bt.Strategy, bool, str]:
    run = s.execute(select(PmLabRun).order_by(desc(PmLabRun.id)).limit(1)).scalar()
    if run:
        for r in run.result.get("rows", []):
            if r.get("proven"):
                return bt.Strategy(**r["params"]), True, r["describe"]
    return bt.PLAYBOOK, False, bt.PLAYBOOK.describe()


@dataclass
class LiveDesk:
    kalshi: KalshiMarkets
    btc: BtcSource
    series: str
    max_loss: float = 70.0
    current: MarketQuote | None = None
    sigma: float | None = None
    sigma_at: datetime | None = None
    last: dict[str, Any] = field(default_factory=dict)

    def tick(self, s: Session, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        if self.current is None or (self.current.close_time and now >= self.current.close_time):
            self.current = self.kalshi.open_window(self.series, now)
            self._close_stale(s, now)
            if self.current is None:
                self.last = {"state": "NO_OPEN_WINDOW", "at": now.isoformat()}
                return self.last
            upsert_window(s, self.series, self.current)
        q = self.kalshi.market(self.current.ticker)
        if q.strike is None:
            q.strike = self.current.strike
        q.open_time, q.close_time = q.open_time or self.current.open_time, q.close_time or self.current.close_time
        self.current = q
        upsert_window(s, self.series, q)
        spot = self.btc.spot()
        if self.sigma_at is None or now - self.sigma_at > timedelta(seconds=60):
            closes = [c for _, c in self.btc.minutes(now - timedelta(minutes=65), now)]
            self.sigma, self.sigma_at = sigma_from_closes(closes) or self.sigma, now
        assert q.open_time and q.close_time
        sec_left = int((q.close_time - now).total_seconds())
        elapsed_min = int((now - q.open_time).total_seconds() // 60)
        f = fair_yes(spot, float(q.strike) if q.strike else None, sec_left, self.sigma)
        sd = Sides(float(q.yes_bid) if q.yes_bid is not None else None, float(q.yes_ask) if q.yes_ask is not None else None)
        s.add(PmTick(ticker=q.ticker, ts=now, sec_left=sec_left, yes_bid=q.yes_bid, yes_ask=q.yes_ask,
                     btc=Decimal(str(spot)) if spot else None, fair_yes=Decimal(str(round(f, 4))) if f is not None else None))
        self._manage(s, q, sd, now)
        if sec_left > NO_ENTRY_LAST_SECONDS and elapsed_min >= 1:
            self._maybe_align(s, q, sd, f, elapsed_min, now)
            self._maybe_strategy(s, q, sd, f, elapsed_min, now)
        u = sd.underdog()
        self.last = {"state": "LIVE", "at": now.isoformat(), "ticker": q.ticker, "sec_left": sec_left,
                     "minute": elapsed_min, "fair_yes": f, "btc": spot, "strike": float(q.strike) if q.strike else None,
                     "sigma": self.sigma, "underdog": u,
                     "underdog_edge": edge(f, u, sd.ask(u)) if u else None}
        return self.last

    # ---------------------------------------------------------------- setups
    def _exit_by(self, q: MarketQuote, exit_minute: int) -> datetime:
        assert q.open_time and q.close_time
        return min(q.open_time + timedelta(minutes=exit_minute),
                   q.close_time - timedelta(seconds=MUST_EXIT_BEFORE_CLOSE_SECONDS))

    def _exists(self, s: Session, ticker: str, kind: str) -> bool:
        return s.execute(select(PmSignal.id).where(PmSignal.ticker == ticker, PmSignal.kind == kind)).first() is not None

    def _create(self, s: Session, q: MarketQuote, kind: str, key: str, proven: bool, side: str, ask: float,
                fair_side: float | None, e: float | None, tp: float | None, exit_by: datetime, detail: dict[str, Any]) -> None:
        n, max_loss = size(self.max_loss, ask)
        if n <= 0:
            return
        s.add(PmSignal(ticker=q.ticker, kind=kind, strategy_key=key, proven=proven, side=side,
                       entry_price=Decimal(str(ask)), fair=Decimal(str(round(fair_side, 4))) if fair_side is not None else None,
                       edge=Decimal(str(round(e, 4))) if e is not None else None, contracts=n, max_loss=max_loss,
                       take_profit=Decimal(str(round(tp, 2))) if tp is not None else None, exit_by=exit_by,
                       detail=jsonable({**detail, "fee_per_contract": round(fee_per_contract(ask), 4)})))
        log.info(f"prediction setup: {kind} {q.ticker} buy {side} @ {ask:.2f} x{n} exit by {exit_by.isoformat()}")

    def _maybe_align(self, s: Session, q: MarketQuote, sd: Sides, f: float | None, minute: int, now: datetime) -> None:
        lo, hi = ALIGN_MINUTES
        u = sd.underdog()
        if f is None or u is None or not lo <= minute <= hi or self._exists(s, q.ticker, "ALIGNED_UNDERDOG"):
            return
        a = sd.ask(u)
        e = edge(f, u, a)
        if a is None or e is None or e < ALIGN_MIN_EDGE or not 0.05 <= a <= 0.45:
            return
        fu = f if u == "YES" else 1 - f
        self._create(s, q, "ALIGNED_UNDERDOG", "aligned_underdog", False, u, a, fu, e, min(round(fu, 2), 0.95),
                     self._exit_by(q, ALIGN_EXIT_MINUTE),
                     {"why": f"Kalshi prices the underdog ({u}) at {a*100:.0f}¢ but live BTC vs the strike implies "
                             f"{fu*100:.0f}¢. Sell into the move toward fair value; exit by minute {ALIGN_EXIT_MINUTE} regardless."})

    def _maybe_strategy(self, s: Session, q: MarketQuote, sd: Sides, f: float | None, minute: int, now: datetime) -> None:
        st, proven, desc_ = active_strategy(s)
        if not st.entry_from <= minute <= st.entry_to or self._exists(s, q.ticker, "STRATEGY"):
            return
        hit = bt.entry_at(st, sd, f)
        if not hit:
            return
        side, a = hit
        tp = None if st.tp_mode is None else min(st.tp if st.tp_mode == "abs" else a + st.tp, 0.97)  # type: ignore[operator]
        fs = None if f is None else (f if side == "YES" else 1 - f)
        self._create(s, q, "STRATEGY", st.key, proven, side, a, fs, edge(f, side, a), tp, self._exit_by(q, st.exit_min),
                     {"why": desc_, "proven": proven})

    def _manage(self, s: Session, q: MarketQuote, sd: Sides, now: datetime) -> None:
        for sig in s.execute(select(PmSignal).where(PmSignal.ticker == q.ticker,
                                                    PmSignal.status.in_(("ACTIVE",)))).scalars():
            bid = sd.bid(sig.side)
            entry = float(sig.entry_price)
            cost = entry + fee_per_contract(entry)
            if sig.take_profit is not None and bid is not None and bid >= float(sig.take_profit):
                sig.status, sig.exit_price = "TAKE_PROFIT", sig.take_profit
                sig.paper_pnl = Decimal(str(round(float(sig.take_profit) - cost, 4)))
            elif now >= sig.exit_by:
                b = bid if bid is not None else 0.01
                sig.status, sig.exit_price = "EXIT_NOW", Decimal(str(b))
                sig.paper_pnl = Decimal(str(round(b - fee_per_contract(b) - cost, 4)))
            else:
                continue
            sig.updated_at = now
            log.info(f"prediction setup {sig.id} {sig.status}: {q.ticker} {sig.side} bid {bid}")

    def _close_stale(self, s: Session, now: datetime) -> None:
        for sig in s.execute(select(PmSignal).where(PmSignal.status != "CLOSED", PmSignal.exit_by <= now - timedelta(seconds=60))).scalars():
            sig.status, sig.updated_at = "CLOSED", now
