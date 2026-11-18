"""Backtest engine (pure functions, no I/O).

Replays MERIDIAN's live trend strategy bar by bar on hourly history, with the SAME rules as live signals
(workers/indicators.py: EMA trend, Donchian breakout, ATR stop/target, and the shared filter_reason()):

  * a signal on bar i uses only bars 0..i (closed bars);
  * entry at the next bar's open; stop/target are the live levels computed from the signal bar's close;
    if the next open is already beyond the stop or target the trade is skipped (a missed recommendation);
  * one position per asset at a time (the live one-position-per-asset rule);
  * exit at the stop or target; if one bar touches both, the STOP is assumed first (conservative);
  * otherwise exit at the close after max_hold bars (10 days);
  * costs: cost_atr x ATR per round trip (slippage + fees), charged in R.

Results are in R (multiples of the dollars at risk). At MERIDIAN's sizing 1R = $70.

Proposals: variants that change one or two settings are compared with the current live settings. Each is tuned
and ranked on the older 70% of history, then checked on the newest 30% it never saw. Only variants that beat the
live strategy on BOTH, with enough trades and a bootstrap probability >= 90% that the improvement is real, become
proposals.
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from meridian.workers.indicators import TrendParams, atr, ema, filter_reason

COST_ATR = 0.1
MAX_HOLD = 240


@dataclass
class Series:
    asset: str
    product: str
    ts: list[datetime]
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741
    c: list[float]


@dataclass
class Sig:
    i: int
    direction: str
    ref: float
    atr: float
    sep: float
    brk: float


@dataclass
class Trade:
    asset: str
    product: str
    direction: str
    signal_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    entry: float
    stop: float
    target: float
    exit: float
    outcome: str  # TARGET | STOP | TIME
    r: float
    bars: int
    mfe_r: float
    mae_r: float


def _pad(vals: list[float], n_total: int) -> list[float | None]:
    return [None] * (n_total - len(vals)) + list(vals)


def _rolling_max_prev(x: list[float], n: int, sign: int) -> list[float | None]:
    """out[i] = max (sign=1) or min (sign=-1) of x[i-n:i] (the n bars BEFORE i); None until available."""
    out: list[float | None] = [None] * len(x)
    dq: deque[int] = deque()
    for i in range(len(x)):
        if i >= n:
            out[i] = x[dq[0]] if sign == 1 else x[dq[0]]
        # add x[i] for the next position
        while dq and ((x[dq[-1]] <= x[i]) if sign == 1 else (x[dq[-1]] >= x[i])):
            dq.pop()
        dq.append(i)
        while dq and dq[0] <= i - n:
            dq.popleft()
    return out


def signals(ser: Series, p: TrendParams) -> list[Sig]:
    n = len(ser.c)
    if n < p.min_bars or ser.asset in p.disabled_assets:
        return []
    ef = _pad(ema(ser.c, p.fast), n)
    es = _pad(ema(ser.c, p.slow), n)
    a = _pad(atr(ser.h, ser.l, ser.c, p.atr_period), n)
    phi = _rolling_max_prev(ser.h, p.breakout_lookback, 1)
    plo = _rolling_max_prev(ser.l, p.breakout_lookback, -1)
    csum = [0.0]
    for v in ser.c:
        csum.append(csum[-1] + v)
    out: list[Sig] = []
    for i in range(p.min_bars - 1, n):
        if ef[i] is None or es[i] is None or a[i] is None or phi[i] is None or plo[i] is None or a[i] <= 0:
            continue
        close = ser.c[i]
        if ef[i] > es[i] and close > phi[i]:
            d, brk = "LONG", close - phi[i]
        elif ef[i] < es[i] and close < plo[i]:
            d, brk = "SHORT", plo[i] - close
        else:
            continue
        sep = abs(ef[i] - es[i]) / a[i]
        sma200 = (csum[i + 1] - csum[i - 199]) / 200 if i >= 199 else None
        if filter_reason(p, d, ser.ts[i], close, sep, brk / a[i], sma200):
            continue
        out.append(Sig(i, d, close, a[i], sep, brk / a[i]))
    return out


def simulate(ser: Series, sigs: list[Sig], p: TrendParams, cost_atr: float = COST_ATR, max_hold: int = MAX_HOLD) -> list[Trade]:
    trades: list[Trade] = []
    busy_until = -1
    n = len(ser.c)
    for sg in sigs:
        if sg.i <= busy_until or sg.i + 1 >= n:
            continue
        long = sg.direction == "LONG"
        stop = sg.ref - p.stop_atr * sg.atr if long else sg.ref + p.stop_atr * sg.atr
        target = sg.ref + p.target_atr * sg.atr if long else sg.ref - p.target_atr * sg.atr
        j = sg.i + 1
        entry = ser.o[j]
        if (long and (entry <= stop or entry >= target)) or (not long and (entry >= stop or entry <= target)):
            continue
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        mfe = mae = 0.0
        exit_px, outcome, k_exit = None, None, None
        last = min(n - 1, j + max_hold)
        for k in range(j, last + 1):
            fav = (ser.h[k] - entry) if long else (entry - ser.l[k])
            adv = (entry - ser.l[k]) if long else (ser.h[k] - entry)
            mfe, mae = max(mfe, fav / risk), max(mae, adv / risk)
            if (long and ser.l[k] <= stop) or (not long and ser.h[k] >= stop):
                exit_px, outcome, k_exit = stop, "STOP", k
                break
            if (long and ser.h[k] >= target) or (not long and ser.l[k] <= target):
                exit_px, outcome, k_exit = target, "TARGET", k
                break
        if outcome is None:
            if last - j < max_hold:      # ran out of data: trade still open, not counted
                break
            exit_px, outcome, k_exit = ser.c[last], "TIME", last
        gross = ((exit_px - entry) if long else (entry - exit_px)) / risk
        r = gross - cost_atr * sg.atr / risk
        trades.append(Trade(ser.asset, ser.product, sg.direction, ser.ts[sg.i], ser.ts[j], ser.ts[k_exit], entry, stop, target,
                            exit_px, outcome, r, k_exit - j + 1, mfe, min(mae, 1.0) if outcome == "STOP" else mae))
        busy_until = k_exit
    return trades


def run(series: list[Series], p: TrendParams) -> list[Trade]:
    out: list[Trade] = []
    for ser in series:
        out += simulate(ser, signals(ser, p), p)
    return sorted(out, key=lambda t: t.entry_ts)


# ------------------------------------------------------------------ statistics
@dataclass
class Stats:
    n: int = 0
    win_rate: float = 0.0
    avg_r: float = 0.0
    total_r: float = 0.0
    profit_factor: float | None = None
    max_dd_r: float = 0.0
    t: float = 0.0
    targets: int = 0
    stops: int = 0
    time_exits: int = 0


def stats(trades: list[Trade]) -> Stats:
    n = len(trades)
    if n == 0:
        return Stats()
    rs = [t.r for t in trades]
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    gains = sum(x for x in rs if x > 0)
    losses = -sum(x for x in rs if x < 0)
    eq = peak = dd = 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return Stats(n=n, win_rate=sum(1 for x in rs if x > 0) / n, avg_r=mean, total_r=sum(rs),
                 profit_factor=(gains / losses) if losses > 0 else None, max_dd_r=dd,
                 t=(mean / (sd / math.sqrt(n))) if sd > 0 else 0.0,
                 targets=sum(1 for t in trades if t.outcome == "TARGET"), stops=sum(1 for t in trades if t.outcome == "STOP"),
                 time_exits=sum(1 for t in trades if t.outcome == "TIME"))


def monthly(trades: list[Trade]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trades:
        k = t.entry_ts.strftime("%Y-%m")
        out[k] = out.get(k, 0.0) + t.r
    return out


def split(trades: list[Trade], cutoff: datetime) -> tuple[list[Trade], list[Trade]]:
    return [t for t in trades if t.entry_ts < cutoff], [t for t in trades if t.entry_ts >= cutoff]


def bootstrap_prob(base: list[Trade], var: list[Trade], months: list[str], n: int = 2000, seed: int = 7) -> float:
    """Probability (resampling whole months) that the variant's total R beats the baseline's."""
    bm, vm = monthly(base), monthly(var)
    diffs = [vm.get(m, 0.0) - bm.get(m, 0.0) for m in months]
    if not diffs:
        return 0.0
    rng = random.Random(seed)
    wins = sum(1 for _ in range(n) if sum(rng.choice(diffs) for _ in diffs) > 0)
    return wins / n


def curve(trades: list[Trade]) -> list[dict[str, Any]]:
    eq, out = 0.0, []
    for t in trades:
        eq += t.r
        out.append({"t": t.exit_ts.isoformat(), "r": round(eq, 3)})
    return out


# ------------------------------------------------------------------ variants
def variants(base: TrendParams) -> list[tuple[str, dict[str, Any]]]:
    """Small, explainable changes: one or two settings at a time."""
    out: list[tuple[str, dict[str, Any]]] = []
    for v in (1.5, 2.5, 3.0):
        if v != base.stop_atr:
            out.append((f"Stop at {v}x ATR (now {base.stop_atr}x)", {"stop_atr": v}))
    for v in (2.0, 2.5, 4.0, 5.0):
        if v != base.target_atr:
            out.append((f"Target at {v}x ATR (now {base.target_atr}x)", {"target_atr": v}))
    for sv in (1.5, 2.5, 3.0):
        for tv in (2.0, 4.0, 5.0):
            if sv != base.stop_atr and tv != base.target_atr:
                out.append((f"Stop {sv}x and target {tv}x ATR (now {base.stop_atr}x / {base.target_atr}x)", {"stop_atr": sv, "target_atr": tv}))
    for v in (10, 15, 30, 55):
        if v != base.breakout_lookback:
            out.append((f"Breakout over {v} hours (now {base.breakout_lookback})", {"breakout_lookback": v}))
    for f, s in ((10, 30), (20, 100), (50, 100)):
        if (f, s) != (base.fast, base.slow):
            out.append((f"Trend EMAs {f}/{s} (now {base.fast}/{base.slow})", {"fast": f, "slow": s}))
    for b in range(6):
        hrs = list(range(b * 4, b * 4 + 4))
        if not set(hrs) <= set(base.skip_hours_utc):
            out.append((f"Skip signals {b*4:02d}:00-{b*4+3:02d}:59 UTC", {"skip_hours_utc": sorted(set(base.skip_hours_utc) | set(hrs))}))
    if not base.skip_weekends:
        out.append(("Skip weekend signals", {"skip_weekends": True}))
    for v in (0.25, 0.5, 1.0):
        if v > base.min_sep_atr:
            out.append((f"Require a trend of at least {v} ATR between the EMAs", {"min_sep_atr": v}))
    for v in (0.1, 0.25, 0.5):
        if v > base.min_brk_atr:
            out.append((f"Require the breakout to clear the level by {v} ATR", {"min_brk_atr": v}))
    for v in (1.0, 1.5):
        if not base.max_brk_atr or v < base.max_brk_atr:
            out.append((f"Skip extended breakouts (more than {v} ATR past the level)", {"max_brk_atr": v}))
    if not base.htf_align:
        out.append(("Only trade with the 200-hour average (longs above, shorts below)", {"htf_align": True}))
    return out


def asset_variants(base: TrendParams, assets: list[str]) -> list[tuple[str, dict[str, Any]]]:
    return [(f"Stop recommending {a}", {"disabled_assets": sorted(set(base.disabled_assets) | {a})})
            for a in assets if a not in base.disabled_assets]


@dataclass
class Verdict:
    title: str
    overrides: dict[str, Any]
    base_train: Stats
    base_test: Stats
    var_train: Stats
    var_test: Stats
    prob: float
    passed: bool
    reasons: list[str] = field(default_factory=list)
    months: int = 0
    base_curve: list[dict[str, Any]] = field(default_factory=list)
    var_curve: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = key(self.overrides)
        return d


def key(overrides: dict[str, Any]) -> str:
    return ";".join(f"{k}={overrides[k]}" for k in sorted(overrides))


MIN_TRAIN, MIN_TEST, MIN_PROB, MIN_MONTHS = 60, 30, 0.90, 3


def judge(title: str, overrides: dict[str, Any], base_tr: list[Trade], base_te: list[Trade],
          var_tr: list[Trade], var_te: list[Trade]) -> Verdict:
    bt_, bte, vt, vte = stats(base_tr), stats(base_te), stats(var_tr), stats(var_te)
    months = sorted(set(monthly(base_te)) | set(monthly(var_te)))
    prob = bootstrap_prob(base_te, var_te, months)
    why: list[str] = []
    if vt.n < MIN_TRAIN:
        why.append(f"only {vt.n} trades in the older data (need {MIN_TRAIN})")
    if vte.n < MIN_TEST:
        why.append(f"only {vte.n} trades in the newest data (need {MIN_TEST})")
    if len(months) < MIN_MONTHS:
        why.append(f"newest data covers {len(months)} months (need {MIN_MONTHS})")
    if not (vt.total_r > bt_.total_r and vt.avg_r > bt_.avg_r):
        why.append("not better on the older data")
    if not (vte.total_r > bte.total_r and vte.avg_r > bte.avg_r):
        why.append("not better on the newest data")
    if prob < MIN_PROB:
        why.append(f"improvement could be luck ({prob:.0%} < {MIN_PROB:.0%})")
    if vte.max_dd_r > bte.max_dd_r * 1.25 + 1:
        why.append("deeper losing streak than the current strategy")
    return Verdict(title, overrides, bt_, bte, vt, vte, prob, not why, why, len(months), curve(base_te), curve(var_te))


def lab(series: list[Series], base: TrendParams, extra: list[tuple[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    if not series:
        return {"verdicts": [], "baseline": None}
    t0 = min(s.ts[0] for s in series if s.ts)
    t1 = max(s.ts[-1] for s in series if s.ts)
    cutoff = t0 + (t1 - t0) * 0.7
    base_trades = run(series, base)
    b_tr, b_te = split(base_trades, cutoff)
    verdicts: list[Verdict] = []
    cfg = base.as_config()
    for title, ov in variants(base) + asset_variants(base, sorted({s.asset for s in series})) + (extra or []):
        p = TrendParams.from_config({**cfg, **ov})
        vt = run(series, p)
        v_tr, v_te = split(vt, cutoff)
        verdicts.append(judge(title, ov, b_tr, b_te, v_tr, v_te))
    by_asset = {}
    for a in sorted({s.asset for s in series}):
        mine = [t for t in base_trades if t.asset == a]
        by_asset[a] = asdict(stats(mine))
    return {"cutoff": cutoff, "start": t0, "end": t1, "baseline": {"all": asdict(stats(base_trades)), "train": asdict(stats(b_tr)),
            "test": asdict(stats(b_te)), "by_asset": by_asset, "curve": curve(base_trades)},
            "verdicts": verdicts, "base_trades": base_trades}


# ------------------------------------------------------------------ recommendation paper-testing
STOP_MULTS = (0.5, 0.75, 1.0, 1.5, 2.0)
TARGET_MULTS: tuple[float | None, ...] = (0.5, 1.0, 1.5, 2.0, 3.0, None)  # None = no target, exit after 48 bars
FILL_BARS = 12        # a recommendation is live for 12 hours
FOLLOW_BARS = 120     # look 5 days past the exit for "what happened next"


def _walk(direction: str, entry: float, stop: float, target: float | None, bars: list[tuple[float, float, float]],
          max_hold: int) -> tuple[str, float | None, int | None, float, float]:
    """bars = [(high, low, close)] starting with the fill bar. Returns outcome, R, exit index, MFE R, MAE R."""
    long = direction == "LONG"
    risk = abs(entry - stop)
    if risk <= 0:
        return "INVALID", None, None, 0.0, 0.0
    mfe = mae = 0.0
    for k, (hi, lo, cl) in enumerate(bars[: max_hold + 1]):
        mfe = max(mfe, ((hi - entry) if long else (entry - lo)) / risk)
        mae = max(mae, ((entry - lo) if long else (hi - entry)) / risk)
        if (long and lo <= stop) or (not long and hi >= stop):
            return "STOP", -1.0, k, mfe, 1.0
        if target is not None and ((long and hi >= target) or (not long and lo <= target)):
            return "TARGET", abs(target - entry) / risk, k, mfe, mae
    if len(bars) > max_hold:
        cl = bars[max_hold][2]
        return "TIME", ((cl - entry) if long else (entry - cl)) / risk, max_hold, mfe, mae
    return "OPEN", None, None, mfe, mae


def _breakeven(direction: str, entry: float, stop: float, target: float, bars: list[tuple[float, float, float]]) -> float | None:
    """Same trade, but the stop moves to the entry price once it is 1R in profit (a stop-out after that is 0R)."""
    long = direction == "LONG"
    risk = abs(entry - stop)
    tr = abs(target - entry) / risk
    cur_stop, armed = stop, False
    for k, (hi, lo, cl) in enumerate(bars[: MAX_HOLD + 1]):
        if (long and lo <= cur_stop) or (not long and hi >= cur_stop):
            return 0.0 if armed else -1.0
        if (long and hi >= target) or (not long and lo <= target):
            return tr
        if not armed and ((hi - entry) if long else (entry - lo)) >= risk:
            armed, cur_stop = True, entry
    if len(bars) > MAX_HOLD:
        cl = bars[MAX_HOLD][2]
        return ((cl - entry) if long else (entry - cl)) / risk
    return None


def paper_test(direction: str, entry: float, stop: float, target: float,
               bars: list[tuple[float, float, float]]) -> dict[str, Any]:
    """Test a recommendation as if placed: bars = [(high, low, close)] after it was made (hourly)."""
    long = direction == "LONG"
    risk0 = abs(entry - stop)
    fill = next((k for k, (hi, lo, _) in enumerate(bars[:FILL_BARS]) if lo <= entry <= hi), None)
    if fill is None:
        if len(bars) < FILL_BARS:
            return {"status": "PENDING"}
        last = bars[FILL_BARS - 1][2]
        moved = ((last - entry) if long else (entry - last)) / risk0 if risk0 else 0.0
        return {"status": "NOT_FILLED", "moved_r": moved,
                "note": "price ran in our favour without coming back to the entry" if moved > 0 else "price moved against the idea before entry"}
    after = bars[fill:]
    outcome, r, k, mfe, mae = _walk(direction, entry, stop, target, after, MAX_HOLD)
    res: dict[str, Any] = {"status": "FILLED", "fill_bar": fill, "outcome": outcome, "r": r, "exit_bar": None if k is None else fill + k,
                           "mfe_r": mfe, "mae_r": mae, "bars": len(after)}
    if k is not None and outcome in ("STOP", "TARGET"):
        follow = after[k + 1: k + 1 + FOLLOW_BARS]
        best = max([((hi - entry) if long else (entry - lo)) / risk0 for hi, lo, _ in follow] or [0.0])
        res["after_exit_best_r"] = best
        if outcome == "STOP":
            res["stopped_then_target"] = any((hi >= target) if long else (lo <= target) for hi, lo, _ in follow)
    alts: dict[str, float | None] = {}
    for sm in STOP_MULTS:
        st = entry - sm * risk0 if long else entry + sm * risk0
        for tm in TARGET_MULTS:
            tg = None if tm is None else (entry + tm * risk0 if long else entry - tm * risk0)
            o, rr, _, _, _ = _walk(direction, entry, st, tg, after, 48 if tm is None else MAX_HOLD)
            alts[f"s{sm}_t{tm if tm is not None else 'none'}"] = None if o == "OPEN" else rr
    alts["breakeven_at_1r"] = _breakeven(direction, entry, stop, target, after)
    res["alternatives"] = alts
    return res
