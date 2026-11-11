"""Pure HOLD / EXIT / REVIEW logic for positions the CIO has recorded. Deterministic and explainable.
EXIT reasons are listed in priority order; REVIEW means MERIDIAN cannot vouch for HOLD (e.g. stale data)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

MONITOR_VERSION = "1.0.0"


@dataclass
class MonitorInputs:
    now: datetime
    direction: str
    entry: Decimal
    stop: Decimal
    target: Decimal | None
    quantity: Decimal
    contract_size: Decimal
    equity: Decimal | None
    last_price: Decimal | None
    last_low: Decimal | None
    last_high: Decimal | None
    data_fresh: bool | None
    ema_fast: float | None
    ema_slow: float | None
    contract_expiry: datetime | None
    min_days_to_expiry: int
    max_loss_pct: Decimal
    blackout_event: str | None  # title of a relevant event inside the blackout window, if any


def advise(m: MonitorInputs) -> tuple[str, list[str], dict[str, Any]]:
    codes: list[str] = []
    detail: dict[str, Any] = {"monitor_version": MONITOR_VERSION}
    if m.last_price is None or not m.data_fresh:
        return "REVIEW", ["DATA_STALE" if m.last_price is not None else "DATA_MISSING"], \
            {**detail, "note": "No fresh price; MERIDIAN cannot confirm HOLD. Check the position yourself."}
    long = m.direction == "LONG"
    pnl = ((m.last_price - m.entry) if long else (m.entry - m.last_price)) * m.quantity * m.contract_size
    detail.update({"last_price": str(m.last_price), "unrealized_pnl_usd": str(pnl.quantize(Decimal("0.01")))})

    breached = (m.last_low is not None and m.last_low <= m.stop) if long else (m.last_high is not None and m.last_high >= m.stop)
    if breached or (m.last_price <= m.stop if long else m.last_price >= m.stop):
        codes.append("STOP_BREACHED")
    if m.equity and pnl <= -(m.equity * m.max_loss_pct):
        codes.append("LOSS_LIMIT_REACHED")
    if m.target is not None and ((m.last_price >= m.target) if long else (m.last_price <= m.target)):
        codes.append("TARGET_REACHED")
    if m.ema_fast is not None and m.ema_slow is not None and ((m.ema_fast < m.ema_slow) if long else (m.ema_fast > m.ema_slow)):
        codes.append("TREND_INVALIDATED")
    if m.contract_expiry is not None and m.contract_expiry - m.now < timedelta(days=m.min_days_to_expiry):
        codes.append("CONTRACT_EXPIRING")
    if codes:
        return "EXIT", codes, detail
    if m.blackout_event:
        return "REVIEW", ["EVENT_BLACKOUT"], {**detail, "event": m.blackout_event,
                                               "note": "High-impact event imminent; decide whether to hold through it."}
    return "HOLD", ["TREND_INTACT"], detail
