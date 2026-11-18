"""MERIDIAN fund structure (small-pool version of a standard hedge-fund setup).

Two separate things are owned:
1. MERIDIAN Fund I — the pool of investor money. Each person has a CAPITAL ACCOUNT. Every closed trade's P&L is
   shared in proportion to capital accounts at the moment it closes, so late joiners share only in later results.
2. MERIDIAN Capital Management — the management company. Ownership is by equity grants (Class A founders with
   votes, Class B strategic partners without votes, Class C employee incentive equity with vesting). Investing in
   the fund never automatically gives anyone a piece of the company.

Fees (fund terms, versioned, never retroactive) move money from fee-paying investors' capital accounts to the
management company's own account inside the fund:
  * management fee: annual % of capital, accrued continuously (pro rata by days);
  * performance allocation: % of profit above the investor's high-water mark (and optional hurdle), charged at
    the end of each period (monthly / quarterly / annual).
The management company's account is then shared by its owners by equity %.

Equity is capital plus realized P&L of closed trades (open trades are shared when they close). The ledger columns
`units`/`nav_per_unit` are kept for the record; balances come from capital accounts.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.core.util import utcnow
from meridian.db.models import CapitalEvent, FundTerms, Instrument, Member, MemberLedger, MgmtEquityGrant, Position
from meridian.portfolio import service as portfolio

GP = "management_company"
UNASSIGNED = "unassigned"
Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
YEAR_DAYS = Decimal(365)


class MemberError(ValueError):
    pass


def _cap_delta(c: CapitalEvent) -> Decimal:
    return -Decimal(c.amount) if c.kind == "WITHDRAW" else Decimal(c.amount)


def _q(x: Decimal | int | float, q: Decimal = Q2) -> str:
    return str(Decimal(x).quantize(q))


def period_ends(start: datetime, end: datetime, period: str) -> list[datetime]:
    """UTC period boundaries (first instant of the next month / quarter / year) after start and <= end."""
    step = {"MONTHLY": 1, "QUARTERLY": 3, "ANNUAL": 12}[period]
    out: list[datetime] = []
    y, m = start.year, start.month
    while True:
        last = ((m - 1) // step + 1) * step          # last month (1..12) of the period containing (y, m)
        b = datetime(y + 1, 1, 1, tzinfo=UTC) if last == 12 else datetime(y, last + 1, 1, tzinfo=UTC)
        if b > end:
            return out
        if b > start:
            out.append(b)
        y, m = b.year, b.month


@dataclass
class Fund:
    accounts: dict[str, Decimal] = field(default_factory=dict)
    hwm: dict[str, Decimal] = field(default_factory=dict)          # high-water mark per fee-paying investor
    last_accrual: dict[str, datetime] = field(default_factory=dict)
    last_crystal: dict[str, datetime] = field(default_factory=dict)
    mgmt_fees: dict[str, Decimal] = field(default_factory=dict)    # paid by each investor
    perf_fees: dict[str, Decimal] = field(default_factory=dict)
    unlinked_capital: Decimal = Decimal(0)
    original_assigned: Decimal = Decimal(0)

    def total(self) -> Decimal:
        return sum(self.accounts.values(), Decimal(0))

    def move(self, frm: str, to: str, amt: Decimal) -> None:
        self.accounts[frm] = self.accounts.get(frm, Decimal(0)) - amt
        self.accounts[to] = self.accounts.get(to, Decimal(0)) + amt


def _terms_at(terms: list[FundTerms], t: datetime) -> FundTerms | None:
    cur = None
    for x in terms:
        if x.effective_at <= t:
            cur = x
    return cur


def replay(s: Session, until: datetime | None = None, daily: bool = False) -> dict[str, Any]:
    now = until or utcnow()
    f = Fund()
    members = {str(m.id): m for m in s.execute(select(Member)).scalars()}
    payers = {k for k, m in members.items() if not m.fee_exempt}
    terms = list(s.execute(select(FundTerms).order_by(FundTerms.effective_at)).scalars())
    insts = {i.id: i for i in s.execute(select(Instrument)).scalars()}
    ev: list[tuple[datetime, int, str, Any]] = []
    for c in s.execute(select(CapitalEvent)).scalars():
        ev.append((c.created_at, 0, "capital", c))
    for e in s.execute(select(MemberLedger)).scalars():
        ev.append((e.effective_at, 1, "ledger", e))
    for p in s.execute(select(Position).where(Position.status == "CLOSED", Position.closed_at.isnot(None))).scalars():
        ev.append((p.closed_at, 3, "trade", p))  # type: ignore[arg-type]
    ev = [e for e in ev if e[0] <= now]
    if ev and terms:
        first = min(e[0] for e in ev)
        bounds: set[datetime] = set()
        for i, x in enumerate(terms):
            end = terms[i + 1].effective_at if i + 1 < len(terms) else now
            bounds.update(period_ends(max(first, x.effective_at), min(end, now), x.perf_period))
        ev += [(t, 2, "crystallize", None) for t in bounds]
    ev.sort(key=lambda x: (x[0], x[1]))
    splits: list[dict[str, Any]] = []
    fee_log: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    day_end: datetime | None = None

    def accrue(t: datetime) -> None:
        tr = _terms_at(terms, t)
        for k in payers:
            cap = f.accounts.get(k, Decimal(0))
            last = f.last_accrual.get(k)
            if last is None:
                continue
            f.last_accrual[k] = t
            if tr is None or cap <= 0 or tr.mgmt_fee_pct <= 0:
                continue
            start = max(last, tr.effective_at)
            days = Decimal(str((t - start).total_seconds())) / Decimal(86400)
            if days <= 0:
                continue
            fee = cap * Decimal(tr.mgmt_fee_pct) / 100 * days / YEAR_DAYS
            f.move(k, GP, fee)
            f.mgmt_fees[k] = f.mgmt_fees.get(k, Decimal(0)) + fee

    def crystallize(t: datetime, record: bool = True) -> dict[str, Decimal]:
        tr = _terms_at(terms, t - timedelta(microseconds=1))
        due: dict[str, Decimal] = {}
        if tr is None or tr.perf_alloc_pct <= 0:
            return due
        for k in payers:
            cap = f.accounts.get(k, Decimal(0))
            if k not in f.hwm or cap <= 0:
                continue
            since = max(f.last_crystal.get(k, tr.effective_at), tr.effective_at)
            days = Decimal(str(max((t - since).total_seconds(), 0))) / Decimal(86400)
            hurdle = f.hwm[k] * Decimal(tr.hurdle_pct) / 100 * days / YEAR_DAYS
            profit = cap - f.hwm[k] - hurdle
            fee = profit * Decimal(tr.perf_alloc_pct) / 100 if profit > 0 else Decimal(0)
            due[k] = fee
            if not record:
                continue
            if fee > 0:
                f.move(k, GP, fee)
                f.perf_fees[k] = f.perf_fees.get(k, Decimal(0)) + fee
                fee_log.append({"at": t, "member_id": k, "kind": "PERFORMANCE", "amount": fee})
            new_cap = f.accounts.get(k, Decimal(0))
            f.hwm[k] = max(f.hwm[k], new_cap) if tr.high_water_mark else new_cap
            f.last_crystal[k] = t
        return due

    def snap(t: datetime) -> None:
        series.append({"date": t.date().isoformat(), "equity": f.total(), "values": dict(f.accounts)})

    for t, _, kind, obj in ev:
        if daily:
            if day_end is None:
                day_end = t.replace(hour=23, minute=59, second=59, microsecond=0)
            while t > day_end:
                snap(day_end)
                day_end += timedelta(days=1)
        accrue(t)
        if kind == "capital":
            if not (obj.note or "").startswith("member:"):   # member-linked money is booked by its ledger entry
                f.unlinked_capital += _cap_delta(obj)
                f.accounts[UNASSIGNED] = f.accounts.get(UNASSIGNED, Decimal(0)) + _cap_delta(obj)
        elif kind == "ledger":
            k = str(obj.member_id)
            amt = Decimal(obj.amount)
            if obj.original:
                f.original_assigned += amt
                f.move(UNASSIGNED, k, amt)
                f.hwm[k] = f.hwm.get(k, Decimal(0)) + amt
            elif obj.kind == "CONTRIBUTION":
                f.accounts[k] = f.accounts.get(k, Decimal(0)) + amt
                f.hwm[k] = f.hwm.get(k, Decimal(0)) + amt
            else:
                before = f.accounts.get(k, Decimal(0))
                f.accounts[k] = before - amt
                if before > 0 and k in f.hwm:
                    f.hwm[k] = f.hwm[k] * (1 - amt / before)
            f.last_accrual.setdefault(k, t)
        elif kind == "crystallize":
            crystallize(t)
        else:
            pnl = Decimal(obj.realized_pnl or 0)
            hold = {k: v for k, v in f.accounts.items() if v > 0}
            tot = sum(hold.values(), Decimal(0))
            shares = {k: pnl * v / tot for k, v in hold.items()} if tot > 0 else {}
            for k, v in shares.items():
                f.accounts[k] += v
            inst = insts.get(obj.instrument_id)
            splits.append({"closed_at": t, "asset": inst.asset if inst else obj.instrument_id, "direction": obj.direction,
                           "pnl": pnl, "shares": shares, "fractions": {k: v / tot for k, v in hold.items()} if tot > 0 else {}})
    accrue(now)
    pending = crystallize(now, record=False)
    if daily:
        if day_end is None:
            day_end = now
        while day_end < now:
            snap(day_end)
            day_end += timedelta(days=1)
        snap(now)
    return {"fund": f, "splits": splits, "series": series, "pending_perf": pending, "fee_log": fee_log,
            "members": members, "terms": _terms_at(terms, now)}


# ------------------------------------------------------------------ management company
def _vested(g: MgmtEquityGrant, now: datetime) -> Decimal:
    if not g.vesting_months:
        return Decimal(g.pct)
    months = (now - g.vesting_start).days / 30.4375
    if months < g.cliff_months:
        return Decimal(0)
    return Decimal(g.pct) * Decimal(str(min(1.0, months / g.vesting_months)))


def active_grants(s: Session) -> list[MgmtEquityGrant]:
    return list(s.execute(select(MgmtEquityGrant).where(MgmtEquityGrant.revoked_at.is_(None))
                          .order_by(MgmtEquityGrant.granted_at, MgmtEquityGrant.id)).scalars())


def _founder_grant(s: Session) -> MgmtEquityGrant | None:
    for g in active_grants(s):
        m = s.get(Member, g.member_id)
        if g.equity_class == "A" and m is not None and m.role == "OWNER":
            return g
    return None


def company(s: Session, r: dict[str, Any] | None = None) -> dict[str, Any]:
    r = r or replay(s)
    f: Fund = r["fund"]
    now = utcnow()
    members = r["members"]
    gp_cap = f.accounts.get(GP, Decimal(0))
    earned_mgmt = sum(f.mgmt_fees.values(), Decimal(0))
    earned_perf = sum(f.perf_fees.values(), Decimal(0))
    pending = sum(r["pending_perf"].values(), Decimal(0))
    rows = []
    for g in active_grants(s):
        m = members.get(str(g.member_id))
        rows.append({"id": g.id, "member_id": str(g.member_id), "name": m.name if m else "?", "class": g.equity_class,
                     "pct": _q(g.pct, Q4), "vested_pct": _q(_vested(g, now), Q4), "voting": g.voting,
                     "vesting_months": g.vesting_months, "cliff_months": g.cliff_months, "vesting_start": g.vesting_start,
                     "note": g.note, "share_of_company_value": _q(gp_cap * Decimal(g.pct) / 100)})
    issued = sum((Decimal(x["pct"]) for x in rows), Decimal(0))
    return {"name": "MERIDIAN Capital Management", "capital_in_fund": _q(gp_cap),
            "fees_earned": {"management": _q(earned_mgmt), "performance": _q(earned_perf), "total": _q(earned_mgmt + earned_perf)},
            "performance_fee_if_period_ended_now": _q(pending), "grants": rows, "issued_pct": _q(issued, Q4),
            "unissued_pct": _q(Decimal(100) - issued, Q4)}


def grant(s: Session, member_id: uuid.UUID, equity_class: str, pct: Decimal, vesting_months: int, cliff_months: int,
          note: str, username: str) -> MgmtEquityGrant:
    """New management-company equity comes out of unissued equity first, then the founder's Class A stake."""
    if pct <= 0 or pct > 100:
        raise MemberError("percent must be between 0 and 100")
    if equity_class not in ("A", "B", "C"):
        raise MemberError("class must be A, B or C")
    if vesting_months < 0 or cliff_months < 0 or cliff_months > max(vesting_months, 0):
        raise MemberError("cliff cannot be longer than the vesting period")
    m = s.get(Member, member_id)
    if m is None:
        raise MemberError("person not found")
    now = utcnow()
    issued = sum((Decimal(g.pct) for g in active_grants(s)), Decimal(0))
    take = max(pct - (Decimal(100) - issued), Decimal(0))
    if take > 0:
        founder = _founder_grant(s)
        if founder is None or Decimal(founder.pct) < take:
            raise MemberError("not enough founder equity to issue that grant")
        founder.revoked_at = now
        s.add(MgmtEquityGrant(member_id=founder.member_id, equity_class="A", pct=Decimal(founder.pct) - take, voting=True,
                              note=f"founder stake reduced by {take}% for a grant to {m.name}", username=username,
                              granted_at=now, vesting_start=founder.vesting_start))
    g = MgmtEquityGrant(member_id=m.id, equity_class=equity_class, pct=pct, voting=equity_class == "A",
                        vesting_months=vesting_months, cliff_months=cliff_months, vesting_start=now, note=note,
                        username=username, granted_at=now)
    s.add(g)
    s.flush()
    return g


def revoke(s: Session, grant_id: int, username: str) -> None:
    """Ends a grant; its equity returns to the founder."""
    g = s.get(MgmtEquityGrant, grant_id)
    if g is None or g.revoked_at is not None:
        raise MemberError("grant not found")
    founder = _founder_grant(s)
    if founder is not None and founder.id == g.id:
        raise MemberError("the founder grant cannot be revoked")
    now = utcnow()
    g.revoked_at = now
    if founder is not None:
        founder.revoked_at = now
        s.add(MgmtEquityGrant(member_id=founder.member_id, equity_class="A", pct=Decimal(founder.pct) + Decimal(g.pct),
                              voting=True, note=f"{g.pct}% returned from ended grant #{g.id}", username=username,
                              granted_at=now, vesting_start=founder.vesting_start))
    s.flush()


# ------------------------------------------------------------------ fund views
def _member_row(k: str, m: Member, f: Fund, r: dict[str, Any], ledger: list[MemberLedger]) -> dict[str, Any]:
    mine = [e for e in ledger if str(e.member_id) == k]
    contributed = sum((Decimal(e.amount) for e in mine if e.kind == "CONTRIBUTION"), Decimal(0))
    withdrawn = sum((Decimal(e.amount) for e in mine if e.kind == "WITHDRAWAL"), Decimal(0))
    value = f.accounts.get(k, Decimal(0))
    tot = f.total()
    gain = value + withdrawn - contributed
    return {"id": k, "name": m.name, "role": m.role, "note": m.note, "active": m.active, "fee_exempt": m.fee_exempt,
            "joined": min((e.effective_at for e in mine), default=m.created_at),
            "ownership_pct": _q(value / tot * 100) if tot > 0 else "0.00", "value": _q(value),
            "contributed": _q(contributed), "withdrawn": _q(withdrawn), "gain": _q(gain),
            "gain_pct": _q(gain / contributed * 100) if contributed > 0 else None,
            "realized_pnl_share": _q(sum((sp["shares"].get(k, Decimal(0)) for sp in r["splits"]), Decimal(0))),
            "fees_paid": {"management": _q(f.mgmt_fees.get(k, Decimal(0))), "performance": _q(f.perf_fees.get(k, Decimal(0)))},
            "performance_fee_if_period_ended_now": _q(r["pending_perf"].get(k, Decimal(0))),
            "high_water_mark": _q(f.hwm[k]) if k in f.hwm and not m.fee_exempt else None}


def terms_view(t: FundTerms | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {"id": t.id, "mgmt_fee_pct": _q(t.mgmt_fee_pct), "perf_alloc_pct": _q(t.perf_alloc_pct), "hurdle_pct": _q(t.hurdle_pct),
            "high_water_mark": t.high_water_mark, "perf_period": t.perf_period, "effective_at": t.effective_at,
            "changed_by": t.changed_by, "reason": t.reason}


def overview(s: Session) -> dict[str, Any]:
    r = replay(s)
    f: Fund = r["fund"]
    ledger = list(s.execute(select(MemberLedger)).scalars())
    rows = [_member_row(k, m, f, r, ledger) for k, m in sorted(r["members"].items(), key=lambda kv: kv[1].created_at)]
    tot = f.total()
    un = f.accounts.get(UNASSIGNED, Decimal(0))
    gp = f.accounts.get(GP, Decimal(0))
    return {"fund_name": "MERIDIAN Fund I", "equity": _q(portfolio.equity(s) or 0), "fund_total": _q(tot),
            "members": rows, "trades_split": len(r["splits"]),
            "management_company": {"value": _q(gp), "ownership_pct": _q(gp / tot * 100) if tot > 0 else "0.00"},
            "unassigned": {"value": _q(un), "ownership_pct": _q(un / tot * 100) if tot > 0 else "0.00"} if abs(un) >= Q2 else None,
            "terms": terms_view(r["terms"]), "company": company(s, r),
            "terms_history": [terms_view(t) for t in s.execute(select(FundTerms).order_by(FundTerms.effective_at.desc())).scalars()],
            "note": "Balances use realized P&L from closed trades; open trades are shared when they close."}


def detail(s: Session, member_id: uuid.UUID) -> dict[str, Any]:
    m = s.get(Member, member_id)
    if m is None:
        raise MemberError("member not found")
    r = replay(s, daily=True)
    f: Fund = r["fund"]
    k = str(m.id)
    ledger = list(s.execute(select(MemberLedger)).scalars())
    row = _member_row(k, m, f, r, ledger)
    series = [{"date": p["date"], "value": _q(p["values"].get(k, Decimal(0)))} for p in r["series"]]
    trades = [{"closed_at": sp["closed_at"], "asset": sp["asset"], "direction": sp["direction"], "trade_pnl": _q(sp["pnl"]),
               "ownership_pct": _q(sp["fractions"].get(k, Decimal(0)) * 100), "share": _q(sp["shares"].get(k, Decimal(0)))}
              for sp in r["splits"] if k in sp["shares"]]
    wins = [Decimal(t["share"]) for t in trades if Decimal(t["share"]) > 0]
    losses = [Decimal(t["share"]) for t in trades if Decimal(t["share"]) < 0]
    peak, max_dd = Decimal(0), Decimal(0)
    for p in series:
        v = Decimal(p["value"])
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    co = company(s, r)
    return {**row, "series": series, "trades": trades[::-1][:200],
            "ledger": [{"id": e.id, "kind": e.kind, "amount": _q(e.amount), "original": e.original, "note": e.note,
                        "username": e.username, "effective_at": e.effective_at}
                       for e in sorted(ledger, key=lambda e: e.effective_at) if str(e.member_id) == k],
            "fee_history": [{"at": x["at"], "kind": x["kind"], "amount": _q(x["amount"])} for x in r["fee_log"] if x["member_id"] == k],
            "company_equity": [g for g in co["grants"] if g["member_id"] == k],
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
    r = replay(s, until=now)
    f: Fund = r["fund"]
    if original:
        if kind != "CONTRIBUTION":
            raise MemberError("only a contribution can come from the original capital")
        avail = f.unlinked_capital - f.original_assigned
        if amount > avail:
            raise MemberError(f"only ${avail.quantize(Q2)} of the original capital is unassigned")
        t = s.execute(select(CapitalEvent.created_at).order_by(CapitalEvent.created_at).limit(1)).scalar() or now
    else:
        if kind == "WITHDRAWAL":
            held = f.accounts.get(str(m.id), Decimal(0))
            if amount > held + Q2:
                raise MemberError(f"{m.name} has ${held.quantize(Q2)}; cannot withdraw ${amount}")
        s.add(CapitalEvent(kind="DEPOSIT" if kind == "CONTRIBUTION" else "WITHDRAW", amount=amount,
                           note=f"member:{m.id} {m.name}: {note}".strip(), username=username, created_at=now))
        t = now
    e = MemberLedger(member_id=m.id, kind=kind, amount=amount, units=amount if kind == "CONTRIBUTION" else -amount,
                     nav_per_unit=Decimal(1), equity_before=f.total(), original=original, note=note, username=username,
                     effective_at=t, created_at=now)
    s.add(e)
    s.flush()
    return e


def set_terms(s: Session, mgmt_fee_pct: Decimal, perf_alloc_pct: Decimal, hurdle_pct: Decimal, high_water_mark: bool,
              perf_period: str, reason: str, username: str) -> FundTerms:
    if not (0 <= mgmt_fee_pct <= 5 and 0 <= perf_alloc_pct <= 50 and 0 <= hurdle_pct <= 50):
        raise MemberError("management fee 0-5%, performance 0-50%, hurdle 0-50%")
    if perf_period not in ("MONTHLY", "QUARTERLY", "ANNUAL"):
        raise MemberError("period must be MONTHLY, QUARTERLY or ANNUAL")
    t = FundTerms(mgmt_fee_pct=mgmt_fee_pct, perf_alloc_pct=perf_alloc_pct, hurdle_pct=hurdle_pct,
                  high_water_mark=high_water_mark, perf_period=perf_period, reason=reason, changed_by=username,
                  effective_at=utcnow())
    s.add(t)
    s.flush()
    return t


def seed_founders(s: Session) -> bool:
    """Fresh database: 2/20 terms; Mason $600 (fee-exempt, 100% Class A of the company) + Member 1 $100 (1/7)."""
    if s.execute(select(FundTerms.id).limit(1)).first() is None:
        s.add(FundTerms(mgmt_fee_pct=Decimal(2), perf_alloc_pct=Decimal(20), hurdle_pct=Decimal(0), high_water_mark=True,
                        perf_period="QUARTERLY", reason="initial terms (2 and 20, high-water mark)", changed_by="system"))
    if s.execute(select(Member.id).limit(1)).first() is not None:
        return False
    if portfolio.capital_base(s) != Decimal(700):
        return False
    mason = Member(name="Mason", role="OWNER", note="Founder", fee_exempt=True)
    m1 = Member(name="Member 1", role="MEMBER", note="Rename me: 1/7 of the original $700")
    s.add_all([mason, m1])
    s.flush()
    record(s, mason.id, "CONTRIBUTION", Decimal(600), True, "Share of the original $700", "system")
    record(s, m1.id, "CONTRIBUTION", Decimal(100), True, "Share of the original $700 (1/7)", "system")
    s.add(MgmtEquityGrant(member_id=mason.id, equity_class="A", pct=Decimal(100), voting=True, note="Founder", username="system"))
    return True
