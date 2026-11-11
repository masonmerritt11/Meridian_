"""Prediction markets desk end to end with fake Kalshi and BTC feeds (no network)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy.exc
from sqlalchemy import func, select

from meridian.db.models import PmBar, PmLabRun, PmSignal, PmTick, PmWindow
from meridian.db.session import session_scope
from meridian.predictions.kalshi import Bar, MarketQuote
from meridian.predictions.live import LiveDesk
from meridian.predictions.workers import PmHistorian, PmStrategyLab
from meridian.runtime import make_context
from meridian.workers.base import execute
from test_flow import FakeMacro, client, login

pytestmark = pytest.mark.integration
T0 = datetime(2026, 11, 11, 19, 0, tzinfo=UTC)


class FakeKalshi:
    def __init__(self):
        self.quote = (Decimal("0.30"), Decimal("0.33"))
        self.open_t = T0
        self.history: list[MarketQuote] = []

    def _q(self, open_t: datetime) -> MarketQuote:
        return MarketQuote(f"KXBTC15M-{open_t:%y%b%d%H%M}".upper(), "EV", open_t, open_t + timedelta(minutes=15),
                           Decimal("100000"), self.quote[0], self.quote[1], None, "active", None, None)

    def open_window(self, series, now):  # noqa: ANN001
        return self._q(self.open_t)

    def market(self, ticker):  # noqa: ANN001
        return self._q(self.open_t)

    def cutoff(self):
        return None

    def list_markets(self, series, status, min_close_ts=None, max_pages=20, historical=False):  # noqa: ANN001
        return [] if historical else self.history

    def batch_candles(self, windows):  # noqa: ANN001
        out = {}
        for w in windows:
            i = int(w.ticker.split("-")[-1])
            bounce = i % 3 != 0
            out[w.ticker] = [Bar(m, w.open_time + timedelta(minutes=m),
                                 Decimal("0.30") if m < 7 or not bounce else Decimal("0.55"),
                                 Decimal("0.33") if m < 7 or not bounce else Decimal("0.58"),
                                 None, None, None, None, Decimal(10)) for m in range(1, 16)]
        return out


class FakeBtc:
    def __init__(self, price=100_150.0):
        self.price = price

    def spot(self):
        return self.price

    def minutes(self, start, end):  # noqa: ANN001
        out, t = [], start.replace(second=0, microsecond=0)
        while t < end:
            out.append((t, 100_000.0 + (t.minute % 2) * 20))
            t += timedelta(minutes=1)
        return out


def test_live_desk_alerts_sizes_and_exits_before_close(clean_env):
    k, b = FakeKalshi(), FakeBtc()
    desk = LiveDesk(kalshi=k, btc=b, series="KXBTC15M", max_loss=70)
    with session_scope() as s:
        st = desk.tick(s, T0 + timedelta(minutes=6))
    assert st["state"] == "LIVE" and st["underdog"] == "YES" and st["fair_yes"] > 0.6
    with session_scope() as s:
        sigs = {x.kind: x for x in s.execute(select(PmSignal)).scalars()}
        assert set(sigs) == {"ALIGNED_UNDERDOG", "STRATEGY"}
        for x in sigs.values():
            assert x.side == "YES" and float(x.entry_price) == 0.33 and float(x.max_loss) <= 70 and x.contracts > 200
            assert x.exit_by <= T0 + timedelta(minutes=13) and not x.proven
        assert s.execute(select(func.count()).select_from(PmTick)).scalar() == 1
    # repeated ticks do not duplicate setups
    with session_scope() as s:
        desk.tick(s, T0 + timedelta(minutes=7))
        assert s.execute(select(func.count()).select_from(PmSignal)).scalar() == 2
    # minute 12 arrives with no flip -> EXIT NOW with a paper loss
    k.quote = (Decimal("0.28"), Decimal("0.31"))
    with session_scope() as s:
        desk.tick(s, T0 + timedelta(minutes=12, seconds=1))
        rows = list(s.execute(select(PmSignal)).scalars())
        assert {r.status for r in rows} == {"EXIT_NOW"} and all(r.paper_pnl < 0 for r in rows)
    # the database refuses any setup that would hold within 2 minutes of close
    with pytest.raises(sqlalchemy.exc.DBAPIError), session_scope() as s:
        w = s.execute(select(PmWindow)).scalar()
        s.add(PmSignal(ticker=w.ticker, kind="STRATEGY", strategy_key="x", side="YES", entry_price=Decimal("0.3"),
                       contracts=1, max_loss=Decimal("0.31"), exit_by=w.close_time - timedelta(seconds=30)))


def test_no_new_setups_in_final_minutes_and_take_profit(clean_env):
    k, b = FakeKalshi(), FakeBtc()
    desk = LiveDesk(kalshi=k, btc=b, series="KXBTC15M", max_loss=70)
    with session_scope() as s:
        desk.tick(s, T0 + timedelta(minutes=11, seconds=30))
        assert s.execute(select(func.count()).select_from(PmSignal)).scalar() == 0
    k.open_t = T0 + timedelta(minutes=15)
    k.quote = (Decimal("0.30"), Decimal("0.33"))
    with session_scope() as s:
        desk.tick(s, T0 + timedelta(minutes=21))
    k.quote = (Decimal("0.62"), Decimal("0.65"))
    with session_scope() as s:
        desk.tick(s, T0 + timedelta(minutes=23))
        rows = {r.kind: r for r in s.execute(select(PmSignal)).scalars()}
        assert rows["STRATEGY"].status == "TAKE_PROFIT" and rows["STRATEGY"].paper_pnl > 0  # playbook sells at 50c
        al = rows["ALIGNED_UNDERDOG"]  # targets BTC's fair price, which is higher than 62c here
        assert al.status == "ACTIVE" and float(al.take_profit) > 0.62


def test_historian_lab_and_api(clean_env):
    k, b = FakeKalshi(), FakeBtc()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(days=4)
    k.history = [MarketQuote(f"KXBTC15M-T-{i}", "EV", start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * i + 15),
                             Decimal("100000"), None, None, None, "finalized", "yes" if i % 3 else "no", None) for i in range(360)]
    ctx = make_context(clean_env, macro=FakeMacro())
    ctx.services.update({"kalshi": k, "btc": b})
    out = execute(PmHistorian(ctx), trigger="MANUAL")
    assert out.bars_loaded == 360 and out.btc_minutes > 5000
    out2 = execute(PmStrategyLab(ctx), trigger="MANUAL")
    assert out2.windows == 360 and out2.status == "OK"
    with session_scope() as s:
        run = s.execute(select(PmLabRun)).scalar()
        pb = next(r for r in run.result["rows"] if r["label"] == "Your playbook")
        assert pb["train"]["n"] > 0 and "late_flips" in run.result["studies"]
        assert s.execute(select(func.count()).select_from(PmBar)).scalar() == 360 * 15
    c = client()
    login(c)
    r = c.get("/api/predictions/research")
    assert r.status_code == 200 and r.json()["coverage"]["with_odds_history"] == 360 and r.json()["lab"]["rows"]
    live = c.get("/api/predictions/live").json()
    assert live["window"] is None and len(live["rules"]) == 4
    if pb["proven"]:
        assert live["active_strategy"]["proven"]
