"""Capital and positions as recorded by the CIO. MERIDIAN never executes; positions here are the CIO's
own record of trades executed elsewhere, used for risk aggregation and HOLD/EXIT advisories."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from meridian.core.util import utcnow
from meridian.db.models import CapitalEvent, Instrument, Position
from meridian.marketdata.types import ASSETS
from meridian.risk.engine import OpenPositionView


def capital_base(s: Session) -> Decimal:
    rows = s.execute(select(CapitalEvent.kind, func.coalesce(func.sum(CapitalEvent.amount), 0)).group_by(CapitalEvent.kind)).all()
    total = Decimal(0)
    for kind, amt in rows:
        total += -Decimal(amt) if kind == "WITHDRAW" else Decimal(amt)
    return total


def realized_pnl(s: Session, since: datetime | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(Position.realized_pnl), 0)).where(Position.status == "CLOSED")
    if since is not None:
        q = q.where(Position.closed_at >= since)
    return Decimal(s.execute(q).scalar() or 0)


def equity(s: Session) -> Decimal | None:
    base = capital_base(s)
    if base <= 0:
        return None
    return base + realized_pnl(s)


def period_starts(now: datetime) -> tuple[datetime, datetime]:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    return day, week


def position_risk(p: Position) -> Decimal:
    """Loss from entry to the recorded stop. If stop has moved past entry (locked profit) risk is 0."""
    per = (p.entry_price - p.stop_price) if p.direction == "LONG" else (p.stop_price - p.entry_price)
    return max(per, Decimal(0)) * p.quantity * p.contract_size


def open_position_views(s: Session, margin_cfg: dict[str, Any]) -> list[OpenPositionView]:
    out = []
    for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars():
        inst = s.get(Instrument, p.instrument_id)
        asset = inst.asset if inst else "UNKNOWN"
        group = ASSETS[asset].correlation_group if asset in ASSETS else "unknown"
        if inst and inst.product_type == "SPOT":
            cap: Decimal | None = p.quantity * p.entry_price * p.contract_size
        else:
            m = (margin_cfg or {}).get(asset)
            cap = Decimal(str(m)) * p.quantity if m not in (None, "") else None
        out.append(OpenPositionView(asset=asset, correlation_group=group, direction=p.direction,
                                    risk_usd=position_risk(p), capital_used_usd=cap,
                                    notional_usd=p.quantity * p.entry_price * p.contract_size))
    return out


def pnl_for(p: Position, price: Decimal) -> Decimal:
    per = (price - p.entry_price) if p.direction == "LONG" else (p.entry_price - price)
    return per * p.quantity * p.contract_size


def snapshot(s: Session) -> dict[str, Any]:
    now = utcnow()
    day, week = period_starts(now)
    eq = equity(s)
    open_risk = sum((position_risk(p) for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars()), Decimal(0))
    return {"capital_base": str(capital_base(s)), "equity": str(eq) if eq is not None else None,
            "realized_total": str(realized_pnl(s)), "realized_today": str(realized_pnl(s, day)),
            "realized_week": str(realized_pnl(s, week)), "open_risk_usd": str(open_risk.quantize(Decimal("0.01"))),
            "open_risk_pct": str((open_risk / eq * 100).quantize(Decimal("0.01"))) if eq else None}
