"""Pure math for the prediction-markets desk: fees, fair value from the real BTC market, sizing and the hard rules.

Contract: a 15-minute window pays $1 to YES if the 60-second average of the settlement index just before close is
at or above the strike (itself the 60-second average just before the window opened); otherwise NO is paid.
We use Coinbase BTC-USD as a proxy for the settlement index (CF Benchmarks BRTI), so a small basis is expected.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TAKER_RATE = 0.07  # Kalshi general fee schedule: round up(0.07 x C x P x (1-P)); resting (maker) orders pay 0 on standard series
WINDOW_SECONDS = 15 * 60
NO_ENTRY_LAST_SECONDS = 4 * 60       # hard rule: no new entries in the final 4 minutes
MUST_EXIT_BEFORE_CLOSE_SECONDS = 120  # hard rule: every setup's exit time is at least 2 minutes before close
DEFAULT_SIGMA = 6e-5                  # per-second log vol fallback (~0.05% per minute)


def fee_per_contract(p: float) -> float:
    return TAKER_RATE * p * (1 - p)


def order_fee(contracts: int, p: float) -> Decimal:
    raw = Decimal(str(TAKER_RATE)) * contracts * Decimal(str(p)) * (1 - Decimal(str(p)))
    return raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def averaging_variance(tau_s: float, sigma_s: float) -> float:
    """Variance of log(settlement average / current price) when tau_s seconds remain.
    The final value is a 60-second average, so the last minute only counts partially."""
    if tau_s <= 0:
        return 0.0
    if tau_s > 60:
        return sigma_s ** 2 * (tau_s - 40.0)
    return sigma_s ** 2 * (tau_s / 60.0) ** 2 * tau_s / 3.0


def fair_yes(spot: float | None, strike: float | None, tau_s: float, sigma_s: float | None) -> float | None:
    """Probability the window settles YES, from where BTC is now relative to the strike and how much it typically moves."""
    if not spot or not strike or spot <= 0 or strike <= 0:
        return None
    var = averaging_variance(tau_s, sigma_s or DEFAULT_SIGMA)
    x = math.log(spot / strike)
    if var <= 0:
        return 1.0 if x >= 0 else 0.0
    return min(0.999, max(0.001, norm_cdf(x / math.sqrt(var))))


def sigma_from_closes(closes: list[float]) -> float | None:
    """Per-second volatility from consecutive 1-minute closes."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return max(math.sqrt(var) / math.sqrt(60), 5e-6)


@dataclass(frozen=True)
class Sides:
    """Prices for both sides of one window at one moment. NO prices come from the YES book: NO ask = 1 - YES bid."""
    yes_bid: float | None
    yes_ask: float | None

    def ask(self, side: str) -> float | None:
        if side == "YES":
            return self.yes_ask
        return None if self.yes_bid is None else round(1 - self.yes_bid, 4)

    def bid(self, side: str) -> float | None:
        if side == "YES":
            return self.yes_bid
        return None if self.yes_ask is None else round(1 - self.yes_ask, 4)

    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return self.yes_bid if self.yes_ask is None else self.yes_ask
        return (self.yes_bid + self.yes_ask) / 2

    def underdog(self) -> str | None:
        m = self.mid()
        return None if m is None else ("YES" if m < 0.5 else "NO")


def edge(fair_yes_p: float | None, side: str, ask: float | None) -> float | None:
    """Model probability for `side` minus what it costs to buy it now (price + taker fee)."""
    if fair_yes_p is None or ask is None:
        return None
    p = fair_yes_p if side == "YES" else 1 - fair_yes_p
    return p - ask - fee_per_contract(ask)


def size(max_loss_usd: float, ask: float) -> tuple[int, Decimal]:
    """Most contracts whose total cost (price + taker fee) stays within the max loss. Worst case = the whole premium."""
    if ask <= 0 or ask >= 1:
        return 0, Decimal(0)
    n = int((Decimal(str(max_loss_usd)) / Decimal(str(ask + fee_per_contract(ask)))).to_integral_value(ROUND_FLOOR))
    while n > 0 and Decimal(str(ask)) * n + order_fee(n, ask) > Decimal(str(max_loss_usd)):
        n -= 1
    return n, (Decimal(str(ask)) * n + order_fee(n, ask)).quantize(Decimal("0.01"))
