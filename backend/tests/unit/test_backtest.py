import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from meridian.backtest import engine as bt
from meridian.workers.indicators import TrendParams, evaluate_trend

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _series(n=900, seed=3, drift=0.0, asset="BTC"):
    rng = random.Random(seed)
    px, ts, o, h, l, c = 100.0, [], [], [], [], []
    for i in range(n):
        op = px
        px *= 1 + rng.gauss(drift, 0.006)
        hi, lo = max(op, px) * (1 + abs(rng.gauss(0, 0.002))), min(op, px) * (1 - abs(rng.gauss(0, 0.002)))
        ts.append(T0 + timedelta(hours=i)); o.append(op); h.append(hi); l.append(lo); c.append(px)
    return bt.Series(asset, asset + "-USD", ts, o, h, l, c)


def _live_dirs(ser, p, window=None):
    out = {}
    for i in range(p.min_bars - 1, len(ser.c)):
        lo = 0 if window is None else max(0, i - window + 1)
        bars = [{"ts": ser.ts[k], "open": ser.o[k], "high": ser.h[k], "low": ser.l[k], "close": ser.c[k]} for k in range(lo, i + 1)]
        s = evaluate_trend(bars, Decimal("0.01"), p)
        if s.direction:
            out[i] = s.direction
    return out


def test_backtest_signals_match_live_strategy_exactly():
    for params in (TrendParams(), TrendParams(skip_hours_utc=(0, 1, 2, 3), min_brk_atr=0.1, htf_align=True, skip_weekends=True)):
        for seed in (1, 2):
            ser = _series(seed=seed)
            live = _live_dirs(ser, params)
            back = {sg.i: sg.direction for sg in bt.signals(ser, params)}
            assert back == live and len(back) > 5


def test_live_300_bar_window_matches_closely():
    ser = _series(seed=5)
    p = TrendParams()
    live = _live_dirs(ser, p, window=300)
    back = {sg.i: sg.direction for sg in bt.signals(ser, p) if sg.i >= 299}
    live = {i: d for i, d in live.items() if i >= 299}
    diff = set(live.items()) ^ set(back.items())
    assert len(diff) <= max(1, len(live) // 50)  # EMA warm-up differences are negligible


def test_simulation_rules():
    p = TrendParams()
    ser = _series(seed=8, drift=0.0006)
    trades = bt.run([ser], p)
    assert trades
    for a, b in zip(trades, trades[1:]):
        assert b.entry_ts > a.exit_ts or b.entry_ts >= a.exit_ts  # one position at a time
    for t in trades:
        assert t.outcome in ("STOP", "TARGET", "TIME") and t.bars <= bt.MAX_HOLD + 1
        if t.outcome == "STOP":
            assert -1.2 < t.r < -1.0          # -1R minus costs
        if t.outcome == "TARGET":
            assert t.r > 1.0
    s = bt.stats(trades)
    assert s.n == len(trades) and s.targets + s.stops + s.time_exits == s.n


def test_lab_finds_nothing_on_noise_and_judges_honestly():
    series = [_series(n=4000, seed=s, asset=a) for s, a in ((11, "BTC"), (12, "ETH"), (13, "XRP"))]
    res = bt.lab(series, TrendParams())
    passed = [v for v in res["verdicts"] if v.passed]
    assert len(res["verdicts"]) > 30
    assert len(passed) <= 1  # random walks: (almost) nothing should survive the out-of-sample + bootstrap test
    for v in res["verdicts"]:
        if not v.passed:
            assert v.reasons


def test_paper_test_outcomes_and_alternatives():
    # LONG entry 100, stop 98, target 103 (1.5R)
    up = [(100.5, 99.5, 100.2)] + [(101 + k * 0.5, 100 + k * 0.5, 100.6 + k * 0.5) for k in range(10)]
    r = bt.paper_test("LONG", 100, 98, 103, up)
    assert r["status"] == "FILLED" and r["outcome"] == "TARGET" and abs(r["r"] - 1.5) < 1e-9
    down = [(100.2, 99.8, 100)] + [(99.5, 97.5, 97.8)] + [(104, 98.5, 103.5)] * 5
    r = bt.paper_test("LONG", 100, 98, 103, down)
    assert r["outcome"] == "STOP" and r["r"] == -1.0 and r["stopped_then_target"] is True
    assert r["alternatives"]["s1.5_t1.5"] is not None  # a wider stop survives that dip
    never = [(110, 105, 108)] * 12
    r = bt.paper_test("LONG", 100, 98, 103, never)
    assert r["status"] == "NOT_FILLED" and r["moved_r"] > 0
    assert bt.paper_test("LONG", 100, 98, 103, never[:3])["status"] == "PENDING"
    # breakeven: +1R then back to entry -> 0R instead of -1R
    be = [(100.1, 99.9, 100)] + [(102.2, 100.5, 101.5)] + [(100.5, 97.0, 97.5)]
    r = bt.paper_test("LONG", 100, 98, 103, be)
    assert r["outcome"] == "STOP" and r["alternatives"]["breakeven_at_1r"] == 0.0
