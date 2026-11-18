"""Pure, deterministic indicator + strategy functions (no I/O). Unit-tested directly."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

STRATEGY = "ema_donchian_atr"
STRATEGY_VERSION = "1.0.0"


def ema(values: list[float], n: int) -> list[float]:
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out  # aligned to values[n-1:]


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[float]:
    if len(closes) < n + 1:
        return []
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    out = [sum(trs[:n]) / n]
    for tr in trs[n:]:
        out.append((out[-1] * (n - 1) + tr) / n)  # Wilder smoothing
    return out


def round_to_tick(x: Decimal, tick: Decimal, mode: str) -> Decimal:
    tick = Decimal(format(tick.normalize(), "f"))  # strip storage padding (0.0050000000 -> 0.005)
    return (x / tick).to_integral_value(rounding=ROUND_FLOOR if mode == "down" else ROUND_CEILING) * tick


@dataclass
class TrendParams:
    fast: int = 20
    slow: int = 50
    breakout_lookback: int = 20
    atr_period: int = 14
    stop_atr: float = 2.0
    target_atr: float = 3.0
    min_bars: int = 60
    # Optional filters (all off by default). Each can only be switched on by accepting a backtested proposal.
    skip_hours_utc: tuple[int, ...] = ()   # no new signals when the signal bar starts in these UTC hours
    skip_weekends: bool = False            # no new signals on Saturday/Sunday (UTC)
    min_sep_atr: float = 0.0               # EMA fast/slow separation must be at least this many ATRs
    min_brk_atr: float = 0.0               # close must clear the breakout level by at least this many ATRs
    max_brk_atr: float = 0.0               # ... and by no more than this (0 = no cap): avoid chasing extended moves
    htf_align: bool = False                # LONG only above the 200-bar average, SHORT only below
    disabled_assets: tuple[str, ...] = ()  # assets that get no new signals

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> TrendParams:
        vals = {k: cfg[k] for k in cls.__dataclass_fields__ if k in cfg}
        for k in ("skip_hours_utc", "disabled_assets"):
            if k in vals:
                vals[k] = tuple(vals[k] or ())
        return cls(**vals)

    def as_config(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["skip_hours_utc"], d["disabled_assets"] = list(self.skip_hours_utc), list(self.disabled_assets)
        return d


def _ts_of(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def filter_reason(p: TrendParams, direction: str, ts: Any, close: float, sep_atr: float, brk_atr: float,
                  sma200: float | None) -> str | None:
    """Shared by live signals and the backtester so both apply identical filters."""
    t = _ts_of(ts)
    if t is not None:
        t = t.astimezone(UTC)
        if p.skip_hours_utc and t.hour in p.skip_hours_utc:
            return "FILTER_HOUR"
        if p.skip_weekends and t.weekday() >= 5:
            return "FILTER_WEEKEND"
    if p.min_sep_atr and sep_atr < p.min_sep_atr:
        return "FILTER_WEAK_TREND"
    if p.min_brk_atr and brk_atr < p.min_brk_atr:
        return "FILTER_WEAK_BREAKOUT"
    if p.max_brk_atr and brk_atr > p.max_brk_atr:
        return "FILTER_EXTENDED"
    if p.htf_align and sma200 is not None:
        if (direction == "LONG" and close <= sma200) or (direction == "SHORT" and close >= sma200):
            return "FILTER_HIGHER_TIMEFRAME"
    return None


@dataclass
class TrendSignal:
    direction: str | None
    strength: float
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    calc: dict[str, Any]


def evaluate_trend(bars: list[dict[str, Any]], tick: Decimal, p: TrendParams) -> TrendSignal:
    """bars: oldest->newest dicts with ts, open, high, low, close (Decimal or float). Uses closed bars only."""
    if len(bars) < p.min_bars:
        return TrendSignal(None, 0.0, None, None, None, {"reason": "INSUFFICIENT_HISTORY", "bars": len(bars)})
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    ef, es = ema(closes, p.fast)[-1], ema(closes, p.slow)[-1]
    a = atr(highs, lows, closes, p.atr_period)[-1]
    prior_high = max(highs[-p.breakout_lookback - 1:-1])
    prior_low = min(lows[-p.breakout_lookback - 1:-1])
    close = closes[-1]
    calc = {
        "as_of": str(bars[-1]["ts"]), "close": close, "ema_fast": round(ef, 6), "ema_slow": round(es, 6),
        "atr": round(a, 6), "atr_pct": round(a / close * 100, 4), "prior_high": prior_high, "prior_low": prior_low,
        "params": p.__dict__, "strategy": STRATEGY, "strategy_version": STRATEGY_VERSION,
    }
    direction = None
    if ef > es and close > prior_high:
        direction, brk = "LONG", close - prior_high
    elif ef < es and close < prior_low:
        direction, brk = "SHORT", prior_low - close
    if direction is None or a <= 0:
        calc["reason"] = "NO_SETUP"
        return TrendSignal(None, 0.0, None, None, None, calc)
    sep = abs(ef - es) / a
    sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
    why = filter_reason(p, direction, bars[-1]["ts"], close, sep, brk / a, sma200)
    if why:
        calc.update({"reason": why, "ema_separation_atr": round(sep, 4), "breakout_atr": round(brk / a, 4)})
        return TrendSignal(None, 0.0, None, None, None, calc)
    strength = round(0.5 * min(1.0, sep / 3) + 0.5 * min(1.0, brk / a), 4)
    entry = Decimal(str(close))
    if direction == "LONG":
        stop = round_to_tick(entry - Decimal(str(p.stop_atr * a)), tick, "down")
        target = round_to_tick(entry + Decimal(str(p.target_atr * a)), tick, "down")
    else:
        stop = round_to_tick(entry + Decimal(str(p.stop_atr * a)), tick, "up")
        target = round_to_tick(entry - Decimal(str(p.target_atr * a)), tick, "up")
    calc.update({"ema_separation_atr": round(sep, 4), "breakout_atr": round(brk / a, 4)})
    return TrendSignal(direction, strength, entry, stop, target, calc)


def resample(bars: list[dict[str, Any]], factor: int) -> list[dict[str, Any]]:
    out = []
    usable = len(bars) - len(bars) % factor
    start = len(bars) - usable
    for i in range(start, len(bars), factor):
        g = bars[i:i + factor]
        out.append({"ts": g[0]["ts"], "open": g[0]["open"], "high": max(b["high"] for b in g),
                    "low": min(b["low"] for b in g), "close": g[-1]["close"]})
    return out
