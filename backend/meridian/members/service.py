"""Ownership of the account by unit accounting (how funds track who owns what).

* Founding capital is split at $1.00 per unit (e.g. $600 -> 600 units, $100 -> 100 units = 1/7 of 700).
* Later money buys units at the current NAV per unit = account equity / units outstanding, so a new member
  neither dilutes nor benefits from gains and losses that happened before they joined.
* Withdrawals redeem units at the current NAV.
* Equity is capital plus realized P&L of closed trades (MERIDIAN does not mark open trades to market), so a
  trade's result is shared by the holders at the moment it closes.
* Any capital not linked to a member (recorded on the old capital page) is shown as "Unassigned" at $1.00 per unit.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.core.util import utcnow
from meridian.db.models import CapitalEvent, Instrument, Member, MemberLedger, Position
from meridian.portfolio import service as portfolio

UNASSIGNED = "unassigned"
Q2 = Decimal("0.01")
Q6 = Decimal("0.000001")


class MemberError(ValueError):
    pass


def _link(member_id: Any) -> str:
    return f"member:{member_id}"


def _cap_delta(c: CapitalEvent) -> Decimal:
    return -Decimal(c.amount) if c.kind == "WITHDRAW" else Decimal(c.amount)


@dataclass
class Book:
    """State of the account at one moment."""
    equity: Decimal = Decimal(0)
    units: dict[str, Decimal] = field(default_factory=dict)
    unlinked_capital: Decimal = Decimal(0)
    original_units: Decimal = Decimal(0)

    @property
    def unassigned_units(self) -> Decimal:
        return max(self.unlinked_capital - self.original_units, Decimal(0))

    def holdings(self) -> dict[str, Decimal]:
        h = {k: v for k, v in self.units.items() if v != 0}
        if self.unassigned_units > 0:
            h[UNASSIGNED] = self.unassigned_units
        return h

    def total_units(self) -> Decimal:
        return sum(self.holdings().values(), Decimal(0))

    def nav(self) -> Decimal:
        t = self.total_units()
        return (self.equity / t) if t > 0 else Decimal(1)


def _timeline(s: Session) -> list[tuple[datetime, int, str, Any]]:
    ev: list[tuple[datetime, int, str, Any]] = []
    for c in s.execute(select(CapitalEvent)).scalars():
        ev.append((c.created_at, 0, "capital", c))
    for e in s.execute(select(MemberLedger)).scalars():
        ev.append((e.effective_at, 1, "ledger", e))
    for p in s.execute(select(Position).where(Position.status == "CLOSED", Position.closed_at.isnot(None))).scalars():
        ev.append((p.closed_at, 2, "trade", p))  # type: ignore[arg-type]
    ev.sort(key=lambda x: (x[0], x[1]))
    return ev


def replay(s: Session, until: datetime | None = None, daily: bool = False) -> dict[str, Any]:
    """Walk capital, member entries and closed trades in time order. Returns the final book, each trade's split,
    and (optionally) an end-of-day value per holder."""
    b = Book()
    splits: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    insts = {i.id: i for i in s.execute(select(Instrument)).scalars()}
    events = [e for e in _timeline(s) if until is None or e[0] <= until]
    day_end: datetime | None = None

    def snap(t: datetime) -> None:
        nav = b.nav()
        series.append({"date": t.date().isoformat(), "equity": b.equity, "nav": nav,
                       "values": {k: v * nav for k, v in b.holdings().items()}})

    for t, _, kind, obj in events:
        if daily:
            if day_end is None:
                day_end = t.replace(hour=23, minute=59, second=59, microsecond=0)
            while t > day_end:
                snap(day_end)
                day_end += timedelta(days=1)
        if kind == "capital":
            b.equity += _cap_delta(obj)
            if not (obj.note or "").startswith("member:"):
                b.unlinked_capital += _cap_delta(obj)
        elif kind == "ledger":
            mid = str(obj.member_id)
            b.units[mid] = b.units.get(mid, Decimal(0)) + Decimal(obj.units)
            if obj.original:
                b.original_units += Decimal(obj.units)
        else:
            pnl = Decimal(obj.realized_pnl or 0)
            hold, tot = b.holdings(), b.total_units()
            share = {k: (pnl * v / tot) for k, v in hold.items()} if tot > 0 else {}
            inst = insts.get(obj.instrument_id)
            splits.append({"position_id": str(obj.id), "closed_at": t, "asset": inst.asset if inst else obj.instrument_id,
                           "direction": obj.direction, "pnl": pnl, "shares": share,
                           "fractions": {k: v / tot for k, v in hold.items()} if tot > 0 else {}})
            b.equity += pnl
    if daily:
        now = until or utcnow()
        if day_end is None:
            day_end = now
        while day_end < now:
            snap(day_end)
            day_end += timedelta(days=1)
        snap(now)
    return {"book": b, "splits": splits, "series": series}


def _q(x: Decimal, q: Decimal = Q2) -> str:
    return str(Decimal(x).quantize(q))


def overview(s: Session) -> dict[str, Any]:
    r = replay(s)
    b: Book = r["book"]
    eq = portfolio.equity(s) or Decimal(0)
    nav, tot = b.nav(), b.total_units()
    members = list(s.execute(select(Member).order_by(Member.created_at)).scalars())
    ledger = list(s.execute(select(MemberLedger)).scalars())
    rows = []
    for m in members:
        mine = [e for e in ledger if e.member_id == m.id]
        contributed = sum((Decimal(e.amount) for e in mine if e.kind == "CONTRIBUTION"), Decimal(0))
        withdrawn = sum((Decimal(e.amount) for e in mine if e.kind == "WITHDRAWAL"), Decimal(0))
        units = b.units.get(str(m.id), Decimal(0))
        value = units * nav
        gain = value + withdrawn - contributed
        pnl_share = sum((sp["shares"].get(str(m.id), Decimal(0)) for sp in r["splits"]), Decimal(0))
        rows.append({"id": str(m.id), "name": m.name, "role": m.role, "note": m.note, "active": m.active,
                     "joined": min((e.effective_at for e in mine), default=m.created_at),
                     "units": _q(units, Q6), "ownership_pct": _q(units / tot * 100) if tot > 0 else "0.00",
                     "value": _q(value), "contributed": _q(contributed), "withdrawn": _q(withdrawn),
                     "gain": _q(gain), "gain_pct": _q(gain / contributed * 100) if contributed > 0 else None,
                     "realized_pnl_share": _q(pnl_share)})
    un = b.unassigned_units
    return {"equity": _q(eq), "nav_per_unit": _q(nav, Q6), "total_units": _q(tot, Q6),
            "unassigned": {"units": _q(un, Q6), "value": _q(un * nav), "ownership_pct": _q(un / tot * 100) if tot > 0 else "0.00"}
            if un > 0 else None,
            "members": rows, "trades_split": len(r["splits"]),
            "note": "Value uses realized P&L from closed trades; open trades are shared when they close."}


def detail(s: Session, member_id: uuid.UUID) -> dict[str, Any]:
    m = s.get(Member, member_id)
    if m is None:
        raise MemberError("member not found")
    ov = overview(s)
    row = next(x for x in ov["members"] if x["id"] == str(m.id))
    r = replay(s, daily=True)
    key = str(m.id)
    series = [{"date": p["date"], "value": _q(p["values"].get(key, Decimal(0))), "nav": _q(p["nav"], Q6)} for p in r["series"]]
    trades = [{"closed_at": sp["closed_at"], "asset": sp["asset"], "direction": sp["direction"], "trade_pnl": _q(sp["pnl"]),
               "ownership_pct": _q(sp["fractions"].get(key, Decimal(0)) * 100), "share": _q(sp["shares"].get(key, Decimal(0)))}
              for sp in r["splits"] if key in sp["shares"]]
    wins = [Decimal(t["share"]) for t in trades if Decimal(t["share"]) > 0]
    losses = [Decimal(t["share"]) for t in trades if Decimal(t["share"]) < 0]
    ledger = [{"id": e.id, "kind": e.kind, "amount": _q(e.amount), "units": _q(e.units, Q6), "nav_per_unit": _q(e.nav_per_unit, Q6),
               "original": e.original, "note": e.note, "username": e.username, "effective_at": e.effective_at}
              for e in s.execute(select(MemberLedger).where(MemberLedger.member_id == m.id).order_by(MemberLedger.effective_at)).scalars()]
    vals = [Decimal(p["value"]) for p in series]
    peak, max_dd = Decimal(0), Decimal(0)
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    return {**row, "nav_per_unit": ov["nav_per_unit"], "series": series, "ledger": ledger, "trades": trades[::-1][:200],
            "stats": {"trades": len(trades), "winning": len(wins), "losing": len(losses),
                      "best": _q(max(wins)) if wins else None, "worst": _q(min(losses)) if losses else None,
                      "max_drawdown_pct": _q(max_dd * 100)}}


def record(s: Session, member_id: uuid.UUID, kind: str, amount: Decimal, original: bool, note: str, username: str,
           now: datetime | None = None) -> MemberLedger:
    now = now or utcnow()
    s.flush()
    m = s.get(Member, member_id)
    if m is None:
        raise MemberError("member not found")
    if amount <= 0:
        raise MemberError("amount must be positive")
    b: Book = replay(s)["book"]
    eq = b.equity
    if original:
        if kind != "CONTRIBUTION":
            raise MemberError("only a contribution can come from the original capital")
        if amount > b.unassigned_units:
            raise MemberError(f"only ${b.unassigned_units.quantize(Q2)} of the original capital is unassigned")
        nav, units = Decimal(1), amount
        t = s.execute(select(CapitalEvent.created_at).order_by(CapitalEvent.created_at).limit(1)).scalar() or now
    else:
        nav = b.nav()
        if nav <= 0:
            raise MemberError("account equity is zero or negative; units cannot be priced")
        units = amount / nav
        if kind == "WITHDRAWAL":
            held = b.units.get(str(m.id), Decimal(0))
            if units > held + Decimal("0.0000001"):
                raise MemberError(f"{m.name} holds ${(held * nav).quantize(Q2)}; cannot withdraw ${amount}")
            units = -min(units, held)
        s.add(CapitalEvent(kind="DEPOSIT" if kind == "CONTRIBUTION" else "WITHDRAW", amount=amount,
                           note=f"{_link(m.id)} {m.name}: {note}".strip(), username=username, created_at=now))
        t = now
    e = MemberLedger(member_id=m.id, kind=kind, amount=amount, units=units, nav_per_unit=nav, equity_before=eq,
                     original=original, note=note, username=username, effective_at=t, created_at=now)
    s.add(e)
    s.flush()
    return e


def seed_founders(s: Session) -> bool:
    """Mason $600 + one member $100 (1/7) of the original $700, once, if no members exist yet."""
    if s.execute(select(Member.id).limit(1)).first() is not None:
        return False
    if portfolio.capital_base(s) != Decimal(700):
        return False
    mason = Member(name="Mason", role="OWNER", note="Founder")
    m1 = Member(name="Member 1", role="MEMBER", note="Rename me: 1/7 of the original $700")
    s.add_all([mason, m1])
    s.flush()
    record(s, mason.id, "CONTRIBUTION", Decimal(600), True, "Share of the original $700", "system")
    record(s, m1.id, "CONTRIBUTION", Decimal(100), True, "Share of the original $700 (1/7)", "system")
    return True
