"""Pure, deterministic calculations for the analyst workers (no I/O). Inputs are lists of floats, oldest first."""
from __future__ import annotations

import math
from statistics import mean, pstdev

from meridian.workers.indicators import atr, ema


def returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, lo in zip(gains[n:], losses[n:], strict=False):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + lo) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def percentile_rank(values: list[float], x: float) -> float:
    return sum(1 for v in values if v <= x) / len(values) if values else 0.0


def bollinger_width(closes: list[float], n: int = 20) -> list[float]:
    out = []
    for i in range(n, len(closes) + 1):
        w = closes[i - n:i]
        m = mean(w)
        out.append(4 * pstdev(w) / m if m else 0.0)
    return out


def zscore(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    w = values[-n:]
    sd = pstdev(w)
    return (values[-1] - mean(w)) / sd if sd > 0 else 0.0


def efficiency_ratio(closes: list[float], n: int = 48) -> float | None:
    """Kaufman efficiency ratio: 1 = straight-line trend, ~0 = chop."""
    if len(closes) < n + 1:
        return None
    w = closes[-n - 1:]
    path = sum(abs(w[i] - w[i - 1]) for i in range(1, len(w)))
    return abs(w[-1] - w[0]) / path if path else 0.0


def realized_vol(closes: list[float], bars: int, periods_per_year: float = 24 * 365) -> float | None:
    r = returns(closes[-bars - 1:])
    return pstdev(r) * math.sqrt(periods_per_year) if len(r) >= 5 else None


def ewma_vol(closes: list[float], lam: float = 0.94) -> float | None:
    r = returns(closes)
    if len(r) < 20:
        return None
    var = pstdev(r[:20]) ** 2
    for x in r[20:]:
        var = lam * var + (1 - lam) * x * x
    return math.sqrt(var)


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = mean(a), mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return cov / (va * vb) if va and vb else None


def ols(y: list[float], x: list[float]) -> tuple[float, float]:
    mx, my = mean(x), mean(y)
    sxx = sum((xi - mx) ** 2 for xi in x)
    beta = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True)) / sxx if sxx else 0.0
    return my - beta * mx, beta


def adf_tstat(series: list[float]) -> float | None:
    """Dickey-Fuller t-statistic (no lags): regress diff on lagged level. More negative = more mean-reverting.
    Approximate 5% critical value for a cointegration residual: about -3.4."""
    if len(series) < 30:
        return None
    y = [series[i] - series[i - 1] for i in range(1, len(series))]
    x = series[:-1]
    a, b = ols(y, x)
    resid = [yi - (a + b * xi) for xi, yi in zip(x, y, strict=True)]
    n = len(y)
    s2 = sum(r * r for r in resid) / (n - 2)
    sxx = sum((xi - mean(x)) ** 2 for xi in x)
    se = math.sqrt(s2 / sxx) if sxx and s2 > 0 else None
    return b / se if se else None


def half_life(series: list[float]) -> float | None:
    if len(series) < 30:
        return None
    y = [series[i] - series[i - 1] for i in range(1, len(series))]
    _, b = ols(y, series[:-1])
    return -math.log(2) / b if b < 0 else None


def mtf_alignment(closes: list[float]) -> dict[str, int | None]:
    """+1/-1/None trend sign on 1h (EMA20/50), 4h (EMA10/20) and daily (EMA5/10) from hourly closes."""
    def sign(series: list[float], f: int, s: int) -> int | None:
        ef, es = ema(series, f), ema(series, s)
        if not ef or not es:
            return None
        return 1 if ef[-1] > es[-1] else -1
    h4 = closes[len(closes) % 4::4][1:] if len(closes) >= 8 else []
    d1 = closes[len(closes) % 24::24][1:] if len(closes) >= 48 else []
    return {"1h": sign(closes, 20, 50), "4h": sign(h4, 10, 20), "1d": sign(d1, 5, 10)}


def atr_pct(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    a = atr(highs, lows, closes, n)
    return a[-1] / closes[-1] * 100 if a and closes[-1] else None


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
