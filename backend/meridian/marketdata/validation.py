"""Deterministic market-data validation. Invalid bars are dropped and reported, never silently 'fixed'."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from meridian.marketdata.types import GRANULARITY_SECONDS, CandleDTO, InstrumentSpec


@dataclass
class ValidationReport:
    accepted: list[CandleDTO] = field(default_factory=list)
    dropped_incomplete: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        return len(self.issues)


def _on_increment(price: Decimal, inc: Decimal | None) -> bool:
    if not inc or inc <= 0:
        return True
    q = price / inc
    return abs(q - q.to_integral_value()) < Decimal("1e-6")


def validate_candles(candles: list[CandleDTO], granularity: str, now: datetime,
                     inst: InstrumentSpec | None = None) -> ValidationReport:
    step = GRANULARITY_SECONDS[granularity]
    rep = ValidationReport()
    seen: set[datetime] = set()
    for c in sorted(candles, key=lambda c: c.ts):
        if c.ts.tzinfo is None:
            rep.issues.append(f"{c.ts}: naive timestamp")
            continue
        if int(c.ts.timestamp()) % step != 0:
            rep.issues.append(f"{c.ts.isoformat()}: not aligned to {granularity}")
            continue
        if c.ts + timedelta(seconds=step) > now:
            rep.dropped_incomplete += 1  # still-forming bar: never used for signals
            continue
        if c.ts in seen:
            rep.issues.append(f"{c.ts.isoformat()}: duplicate bucket")
            continue
        if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
            rep.issues.append(f"{c.ts.isoformat()}: non-positive price or negative volume")
            continue
        if not (c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) and c.low <= c.high):
            rep.issues.append(f"{c.ts.isoformat()}: OHLC inconsistent")
            continue
        if inst is not None and not all(_on_increment(p, inst.price_increment) for p in (c.open, c.high, c.low, c.close)):
            # Reported as a data-quality issue but kept: venues may print off-tick for spot aggregates.
            rep.issues.append(f"{c.ts.isoformat()}: WARN price off tick {inst.price_increment}")
        seen.add(c.ts)
        rep.accepted.append(c)
    return rep


def validate_instrument(inst: InstrumentSpec, now: datetime, min_days_to_expiry: int = 3) -> list[str]:
    problems: list[str] = []
    if inst.price_increment is None or inst.price_increment <= 0:
        problems.append("PRICE_INCREMENT_MISSING")
    if inst.product_type == "FUTURE":
        if inst.contract_size is None or inst.contract_size <= 0:
            problems.append("CONTRACT_SIZE_MISSING")
        if inst.contract_expiry is None:
            problems.append("CONTRACT_EXPIRY_MISSING")
        elif inst.contract_expiry <= now:
            problems.append("CONTRACT_EXPIRED")
        elif inst.contract_expiry - now < timedelta(days=min_days_to_expiry):
            problems.append("CONTRACT_NEAR_EXPIRY")
    return problems


def is_fresh(last_ts: datetime | None, granularity: str, now: datetime, tolerance_bars: int = 2) -> bool:
    if last_ts is None:
        return False
    return now - last_ts <= timedelta(seconds=GRANULARITY_SECONDS[granularity] * tolerance_bars)
