"""Database reads/writes for the prediction-markets desk, plus the Coinbase BTC source used as the real-market reference."""
from __future__ import annotations

from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from meridian.db.models import PmBar, PmBtcMinute, PmWindow
from meridian.marketdata.coinbase import ReadOnlyPublicClient
from meridian.predictions.backtest import WindowData
from meridian.predictions.kalshi import Bar, MarketQuote
from meridian.predictions.model import sigma_from_closes


class BtcSource(Protocol):
    def spot(self) -> float | None: ...
    def minutes(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]: ...


class CoinbaseBtc:
    """BTC-USD spot from Coinbase's public, read-only market data (same allowlisted client as the futures feed)."""

    def __init__(self, client: ReadOnlyPublicClient | None = None, product: str = "BTC-USD"):
        self.client = client or ReadOnlyPublicClient()
        self.product = product

    def spot(self) -> float | None:
        p = self.client.get(f"/api/v3/brokerage/market/products/{self.product}").get("price")
        return float(p) if p not in (None, "") else None

    def minutes(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        cur = start
        while cur < end:
            nxt = min(end, cur + timedelta(minutes=350))
            data = self.client.get(f"/api/v3/brokerage/market/products/{self.product}/candles",
                                   {"start": str(int(cur.timestamp())), "end": str(int(nxt.timestamp())),
                                    "granularity": "ONE_MINUTE", "limit": 350})
            for c in data.get("candles") or []:
                try:
                    out.append((datetime.fromtimestamp(int(c["start"]), UTC), float(c["close"])))
                except (KeyError, ValueError):
                    continue
            cur = nxt
        return sorted(set(out))


def upsert_window(s: Session, series: str, q: MarketQuote) -> None:
    if not q.open_time or not q.close_time:
        return
    vals: dict[str, Any] = {"ticker": q.ticker, "series": series, "open_time": q.open_time, "close_time": q.close_time,
                            "strike": q.strike, "result": q.result, "status": q.status}
    upd = {"status": q.status, "updated_at": datetime.now(UTC)}
    if q.result:
        upd["result"] = q.result
    if q.strike is not None:
        upd["strike"] = q.strike
    s.execute(pg_insert(PmWindow).values(**vals).on_conflict_do_update(index_elements=["ticker"], set_=upd))


def save_bars(s: Session, ticker: str, bars: list[Bar]) -> None:
    for b in bars:
        vals = {"ticker": ticker, "minute": b.minute, "end_ts": b.end_ts, "yes_bid": b.yes_bid, "yes_ask": b.yes_ask,
                "yes_bid_high": b.yes_bid_high, "yes_bid_low": b.yes_bid_low, "yes_ask_high": b.yes_ask_high,
                "yes_ask_low": b.yes_ask_low, "volume": b.volume}
        s.execute(pg_insert(PmBar).values(**vals).on_conflict_do_update(
            index_elements=["ticker", "minute"], set_={k: v for k, v in vals.items() if k not in ("ticker", "minute")}))
    w = s.get(PmWindow, ticker)
    if w is not None:
        w.bars_loaded = True


def save_btc(s: Session, rows: list[tuple[datetime, float]]) -> int:
    for t, c in rows:
        s.execute(pg_insert(PmBtcMinute).values(ts=t, close=Decimal(str(c))).on_conflict_do_nothing())
    return len(rows)


def _ffill(vals: list[float | None]) -> list[float | None]:
    out, last = [], None
    for v in vals:
        last = v if v is not None else last
        out.append(last)
    return out


def load_windows(s: Session, series: str, since: datetime) -> list[WindowData]:
    wins = list(s.execute(select(PmWindow).where(PmWindow.series == series, PmWindow.open_time >= since,
                                                 PmWindow.result.in_(("yes", "no")), PmWindow.bars_loaded.is_(True))
                          .order_by(PmWindow.open_time)).scalars())
    if not wins:
        return []
    bars: dict[str, dict[int, PmBar]] = {}
    for b in s.execute(select(PmBar).where(PmBar.ticker.in_([w.ticker for w in wins]))).scalars():
        bars.setdefault(b.ticker, {})[b.minute] = b
    btc_rows = s.execute(select(PmBtcMinute.ts, PmBtcMinute.close).where(
        PmBtcMinute.ts >= wins[0].open_time - timedelta(minutes=70)).order_by(PmBtcMinute.ts)).all()
    bts = [r[0] for r in btc_rows]
    bcl = [float(r[1]) for r in btc_rows]

    def btc_close_at(t: datetime) -> float | None:  # close of the minute candle that ends at t
        i = bisect_left(bts, t - timedelta(minutes=1))
        return bcl[i] if i < len(bts) and bts[i] == t - timedelta(minutes=1) else None

    out: list[WindowData] = []
    f = lambda x: None if x is None else float(x)  # noqa: E731
    for w in wins:
        bw = bars.get(w.ticker, {})
        if len(bw) < 10:
            continue
        rng = range(0, 16)
        i0 = bisect_left(bts, w.open_time - timedelta(minutes=61))
        i1 = bisect_left(bts, w.open_time)
        out.append(WindowData(
            ticker=w.ticker, open_time=w.open_time, result=str(w.result), strike=f(w.strike),
            sigma=sigma_from_closes(bcl[i0:i1]),
            bid=_ffill([f(bw[m].yes_bid) if m in bw else None for m in rng]),
            ask=_ffill([f(bw[m].yes_ask) if m in bw else None for m in rng]),
            bid_hi=[f(bw[m].yes_bid_high) if m in bw else None for m in rng],
            ask_lo=[f(bw[m].yes_ask_low) if m in bw else None for m in rng],
            btc=[btc_close_at(w.open_time + timedelta(minutes=m)) for m in rng]))
    return out
