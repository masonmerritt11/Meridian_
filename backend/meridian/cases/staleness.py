"""Is a standing recommendation still actionable at the current price? Pure function."""
from __future__ import annotations

from decimal import Decimal

ENTRY_RUN_FRACTION = Decimal("0.5")  # price already covered half the way from entry to target -> entry missed


def missed_reason(direction: str, entry: Decimal | None, stop: Decimal | None, target: Decimal | None,
                  price: Decimal | None, high: Decimal | None = None, low: Decimal | None = None) -> tuple[str, str] | None:
    """Returns (code, explanation) when the setup can no longer be entered as recommended, else None.
    high/low are the extremes traded since the recommendation was made (optional)."""
    if entry is None or stop is None or target is None or price is None:
        return None
    long = direction == "LONG"
    hi = max(x for x in (high, price) if x is not None)
    lo = min(x for x in (low, price) if x is not None)
    if (long and hi >= target) or (not long and lo <= target):
        return "MISSED_TARGET_REACHED", f"price reached the target {target} before entry; the move already happened"
    if (long and lo <= stop) or (not long and hi >= stop):
        return "STOP_HIT_BEFORE_ENTRY", f"price traded through the stop {stop}; the setup failed"
    run = (price - entry) if long else (entry - price)
    if target != entry and run >= ENTRY_RUN_FRACTION * abs(target - entry):
        pct = run / abs(target - entry) * 100
        return "MISSED_ENTRY_PASSED", f"price {price} is already {pct:.0f}% of the way from entry {entry} to target {target}"
    return None
