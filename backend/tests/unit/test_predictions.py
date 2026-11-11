from datetime import UTC, datetime, timedelta

import httpx
import pytest

from meridian.marketdata.coinbase import ForbiddenRequest
from meridian.predictions import backtest as bt
from meridian.predictions.kalshi import KalshiPublicClient, check_allowed, parse_candles, parse_market
from meridian.predictions.model import Sides, edge, fair_yes, order_fee, size

T0 = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)


def test_kalshi_client_is_read_only():
    for bad in ("/trade-api/v2/portfolio/orders", "/trade-api/v2/portfolio/balance", "/trade-api/v2/markets/X/orders",
                "/trade-api/v2/anything"):
        with pytest.raises(ForbiddenRequest):
            check_allowed("GET", bad)
    with pytest.raises(ForbiddenRequest):
        check_allowed("POST", "/trade-api/v2/markets")
    check_allowed("GET", "/trade-api/v2/markets/KXBTC15M-26NOV111315-15")
    check_allowed("GET", "/trade-api/v2/series/KXBTC15M/markets/KXBTC15M-26NOV111315-15/candlesticks")
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"markets": []})
    c = KalshiPublicClient(transport=httpx.MockTransport(handler))
    c.get("/markets", {"series_ticker": "KXBTC15M"})
    assert seen[0].method == "GET" and "authorization" not in {k.lower() for k in seen[0].headers}
    assert not any(k.lower().startswith("kalshi-access") for k in seen[0].headers)


def test_parse_market_dollars_and_legacy_cents():
    m = parse_market({"ticker": "A", "open_time": "2026-11-11T19:00:00Z", "close_time": "2026-11-11T19:15:00Z",
                      "floor_strike": 98000.5, "yes_bid_dollars": "0.3300", "yes_ask_dollars": "0.3500", "status": "active"})
    assert float(m.yes_bid) == 0.33 and float(m.yes_ask) == 0.35 and float(m.strike) == 98000.5 and m.result is None
    legacy = parse_market({"ticker": "B", "yes_bid": 40, "yes_ask": 42, "status": "settled", "result": "no"})
    assert float(legacy.yes_bid) == 0.40 and legacy.result == "no"
    sv = parse_market({"ticker": "C", "status": "finalized", "settlement_value_dollars": "1.0000"})
    assert sv.result == "yes"


def test_parse_candles_minutes():
    c = [{"end_period_ts": int((T0 + timedelta(minutes=m)).timestamp()),
          "yes_bid": {"close_dollars": "0.30", "high_dollars": "0.35"}, "yes_ask": {"close_dollars": "0.32", "low_dollars": "0.31"},
          "volume_fp": "12.00"} for m in (1, 2, 16)]
    bars = parse_candles(c, T0)
    assert [b.minute for b in bars] == [1, 2] and float(bars[0].yes_bid_high) == 0.35


def test_fair_value_and_averaging():
    assert fair_yes(100_000, 100_000, 600, 6e-5) == pytest.approx(0.5, abs=1e-6)
    above = fair_yes(100_100, 100_000, 600, 6e-5)
    assert 0.5 < above < 1
    # less time left -> more certain
    assert fair_yes(100_100, 100_000, 120, 6e-5) > above
    # the 60s average makes the final minute less decisive than a point-in-time settle would be
    assert fair_yes(100_020, 100_000, 30, 6e-5) > fair_yes(100_020, 100_000, 90, 6e-5)


def test_fee_and_sizing_respect_max_loss():
    assert float(order_fee(100, 0.5)) == 1.75 and float(order_fee(100, 0.10)) == 0.63
    n, loss = size(70, 0.33)
    assert n > 0 and float(loss) <= 70 and float(size(70, 0.33)[1]) > 69
    assert size(70, 0.0)[0] == 0


def test_edge_and_sides():
    sd = Sides(0.30, 0.33)
    assert sd.underdog() == "YES" and sd.ask("NO") == 0.70 and sd.bid("NO") == 0.67
    assert edge(0.45, "YES", 0.33) == pytest.approx(0.45 - 0.33 - 0.07 * 0.33 * 0.67)


def _w(result: str, bids: list[float], asks: list[float], hi: list[float] | None = None, t: int = 0) -> bt.WindowData:
    pad = lambda xs: [None] + xs  # noqa: E731
    return bt.WindowData(ticker=f"W{t}", open_time=T0 + timedelta(minutes=15 * t), result=result, strike=100_000.0,
                         sigma=6e-5, bid=pad(bids), ask=pad(asks), bid_hi=pad(hi or bids), ask_lo=pad(asks),
                         btc=[100_000.0] * 16)


def test_playbook_take_profit_and_time_exit_never_past_minute_13():
    flat = [0.30] * 15
    up = [0.30] * 6 + [0.55] * 9  # underdog YES bid jumps at minute 7
    w_tp = _w("yes", up, [b + 0.03 for b in up], t=0)
    tr = bt.simulate(bt.PLAYBOOK, w_tp)
    assert tr and tr.how == "TAKE_PROFIT" and tr.exit == 0.50 and tr.entry_min == 5
    w_cut = _w("no", flat, [0.33] * 15, t=1)
    tr2 = bt.simulate(bt.PLAYBOOK, w_cut)
    assert tr2 and tr2.how == "TIME_EXIT" and tr2.exit_min == 12 and tr2.pnl < 0
    late = bt.Strategy("underdog", 5, 8, 0.25, 0.40, 15, None, None, None)
    assert bt.simulate(late, w_cut).exit_min == 13
    assert all(s.exit_min <= 13 for s in bt.grid())


def test_lab_walk_forward_and_studies():
    ws = []
    for i in range(400):  # synthetic: underdog bounces to 55c in 2 of every 3 windows
        bounce = i % 3 != 0
        bids = [0.30] * 6 + ([0.55] if bounce else [0.20]) * 9
        ws.append(_w("yes" if bounce else "no", bids, [b + 0.03 for b in bids], t=i))
    res = bt.run_lab(ws)
    assert res.train_windows == 280 and res.test_windows == 120
    pb = next(r for r in res.rows if r["label"] == "Your playbook")
    assert pb["test"]["n"] > 30 and pb["proven"]
    assert bt.underdog_bounce(ws)["reached_50c_rate"] == pytest.approx(266 / 400, abs=0.01)
    assert bt.late_flips(ws) and bt.calibration(ws)


def test_candle_batches_respect_kalshi_cap():
    from meridian.predictions.kalshi import MarketQuote
    from meridian.predictions.workers import MAX_CANDLES_PER_REQUEST, candle_chunks
    qs = [MarketQuote(f"T{i}", None, T0 + timedelta(minutes=15 * i), T0 + timedelta(minutes=15 * i + 15), None, None, None,
                      None, "", "yes", None) for i in range(2880)]
    chunks = candle_chunks(qs)
    assert sum(len(c) for c in chunks) == 2880 and len(chunks) < 200
    for c in chunks:
        span = (c[-1].close_time - c[0].open_time).total_seconds() / 60 + 1
        assert len(c) * span <= MAX_CANDLES_PER_REQUEST
