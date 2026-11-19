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
from meridian.marketdata.types import ASSETS, margin_rate
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
            rate = margin_rate(inst.raw if inst else None, p.direction)
            if m not in (None, ""):
                cap = Decimal(str(m)) * p.quantity
            elif rate is not None:
                cap = rate * p.entry_price * p.contract_size * p.quantity
            else:
                cap = None
        out.append(OpenPositionView(asset=asset, correlation_group=group, direction=p.direction,
                                    risk_usd=position_risk(p), capital_used_usd=cap,
                                    notional_usd=p.quantity * p.entry_price * p.contract_size))
    return out


def pnl_for(p: Position, price: Decimal) -> Decimal:
    per = (price - p.entry_price) if p.direction == "LONG" else (p.entry_price - price)
    return per * p.quantity * p.contract_size


MARK_BOOK_MAX_AGE = timedelta(minutes=20)
MARK_CANDLE_MAX_AGE = timedelta(hours=3)


def mark_price(s: Session, instrument_id: str, now: datetime | None = None) -> tuple[Decimal | None, datetime | None, str]:
    """Latest usable price for valuing an open position: order-book mid (<= 20 min old), else the latest hourly close
    (<= 3 h old). Returns (price, as_of, source); price is None when nothing recent enough exists."""
    from meridian.core.config import get_settings
    from meridian.marketdata import service as md
    now = now or utcnow()
    b = md.latest_book(s, instrument_id)
    if b is not None and b.best_bid is not None and b.best_ask is not None and now - b.ts <= MARK_BOOK_MAX_AGE:
        return (Decimal(b.best_bid) + Decimal(b.best_ask)) / 2, b.ts, "order book"
    c = md.load_candles(s, instrument_id, get_settings().candle_granularity, limit=1)
    if c and now - c[-1].ts <= MARK_CANDLE_MAX_AGE + timedelta(hours=1):
        return Decimal(c[-1].close), c[-1].ts, "latest close"
    return None, None, "no recent price"


def open_pnl(s: Session, now: datetime | None = None) -> tuple[Decimal, list[dict[str, Any]]]:
    """Unrealized P&L of every open position at its mark price (positions with no recent price count as 0 and are flagged)."""
    total, rows = Decimal(0), []
    for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars():
        px, ts, src = mark_price(s, p.instrument_id, now)
        pnl = pnl_for(p, px) - Decimal(p.fees or 0) if px is not None else None
        if pnl is not None:
            total += pnl
        rows.append({"position_id": p.id, "mark": px, "as_of": ts, "source": src, "unrealized_pnl": pnl})
    return total, rows


def snapshot(s: Session) -> dict[str, Any]:
    now = utcnow()
    day, week = period_starts(now)
    eq = equity(s)
    open_risk = sum((position_risk(p) for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars()), Decimal(0))
    upnl, marks = open_pnl(s, now)
    return {"capital_base": str(capital_base(s)), "equity": str(eq) if eq is not None else None,
            "unrealized_pnl": str(upnl.quantize(Decimal("0.01"))),
            "total_equity": str((eq + upnl).quantize(Decimal("0.01"))) if eq is not None else None,
            "unpriced_positions": sum(1 for m in marks if m["unrealized_pnl"] is None),
            "realized_total": str(realized_pnl(s)), "realized_today": str(realized_pnl(s, day)),
            "realized_week": str(realized_pnl(s, week)), "open_risk_usd": str(open_risk.quantize(Decimal("0.01"))),
            "open_risk_pct": str((open_risk / eq * 100).quantize(Decimal("0.01"))) if eq else None}
