"""Fund structure: capital accounts, fees, management-company equity."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
import sqlalchemy.exc
from sqlalchemy import select, text

from meridian.db.models import CapitalEvent, FundTerms, Member, MemberLedger, Position
from meridian.db.session import session_scope
from meridian.members import service as ms
from test_flow import client, login

pytestmark = pytest.mark.integration


def _trade(s, pnl, at):  # noqa: ANN001
    s.add(Position(instrument_id="GOL-25NOV26-CDE", direction="LONG", quantity=D(1), entry_price=D(4000), stop_price=D(3930),
                   contract_size=D(1), status="CLOSED", opened_at=at - timedelta(hours=2), closed_at=at,
                   exit_price=D(4000) + D(pnl), realized_pnl=D(pnl), recorded_by="cio"))


def _no_fees(s):  # noqa: ANN001
    ms.set_terms(s, D(0), D(0), D(0), True, "QUARTERLY", "test: no fees", "cio")


def _by(s):  # noqa: ANN001
    return {r["name"]: r for r in ms.overview(s)["members"]}


def test_seed_and_allocation_by_capital_account(clean_env):
    import time
    with session_scope() as s:
        _no_fees(s)
        t0 = s.execute(select(FundTerms.effective_at).order_by(FundTerms.id.desc())).scalars().first()
    time.sleep(0.05)
    step = lambda n: t0 + timedelta(milliseconds=5 * n)  # noqa: E731  (all inside the past few ms, after the seed)
    with session_scope() as s:
        by = _by(s)
        assert by["Member 1"]["ownership_pct"] == "14.29" and by["Mason"]["ownership_pct"] == "85.71"
        assert by["Mason"]["fee_exempt"] and not by["Member 1"]["fee_exempt"]
        co = ms.company(s)
        assert [(g["name"], g["class"], g["pct"], g["voting"]) for g in co["grants"]] == [("Mason", "A", "100.0000", True)]
        m1 = s.execute(select(Member).where(Member.name == "Member 1")).scalar_one()
        with pytest.raises(ms.MemberError):
            ms.record(s, m1.id, "CONTRIBUTION", D(1), True, "", "cio")  # nothing left unassigned
        _trade(s, 70, step(1))   # +10%
    with session_scope() as s:
        by = _by(s)
        assert by["Member 1"]["value"] == "110.00" and by["Mason"]["value"] == "660.00"
        late = Member(name="Late")
        s.add(late)
        s.flush()
        ms.record(s, late.id, "CONTRIBUTION", D(220), False, "joins", "cio", now=step(2))
        late_id, m1_id = late.id, s.execute(select(Member.id).where(Member.name == "Member 1")).scalar_one()
    with session_scope() as s:
        ov = ms.overview(s)
        by = {r["name"]: r for r in ov["members"]}
        assert ov["fund_total"] == "990.00" and by["Late"]["value"] == "220.00" and by["Member 1"]["value"] == "110.00"
        _trade(s, -99, step(3))  # -10%, shared by all three
    with session_scope() as s:
        by = _by(s)
        assert by["Late"]["value"] == "198.00" and by["Member 1"]["value"] == "99.00" and by["Mason"]["value"] == "594.00"
        d = ms.detail(s, m1_id)
        assert [t["share"] for t in d["trades"]] == ["-11.00", "10.00"] and d["gain"] == "-1.00" and d["series"]
        with pytest.raises(ms.MemberError):
            ms.record(s, late_id, "WITHDRAWAL", D(500), False, "", "cio")
        ms.record(s, late_id, "WITHDRAWAL", D(98), False, "partial", "cio", now=step(4))
    with session_scope() as s:
        by = _by(s)
        assert by["Late"]["value"] == "100.00" and by["Late"]["withdrawn"] == "98.00"
    with pytest.raises(sqlalchemy.exc.DBAPIError), session_scope() as s:
        s.execute(text("UPDATE member_ledger SET amount = 1"))


def test_fees_high_water_mark_and_company(clean_env):
    with session_scope() as s:  # 2% management fee, 20% performance above the high-water mark, monthly
        ms.set_terms(s, D(2), D(20), D(0), True, "MONTHLY", "test", "cio")
        t = s.execute(select(FundTerms).order_by(FundTerms.id.desc())).scalars().first()
        start = t.effective_at
        _trade(s, 70, start + timedelta(days=1))           # +10% early in month 1
    # one year later: management fee ~2% of Member 1's capital; performance fee charged once on the early gain
    later = start + timedelta(days=365)
    with session_scope() as s:
        r = ms.replay(s, until=later)
        f = r["fund"]
        m1 = s.execute(select(Member).where(Member.name == "Member 1")).scalar_one()
        k = str(m1.id)
        mgmt = f.mgmt_fees[k]
        perf = f.perf_fees[k]
        assert D("1.9") < mgmt < D("2.3")           # ~2% of ~$108-110
        assert D("1.7") < perf < D("2.1")           # 20% of ~$9.9 profit above the $100 mark
        assert f.mgmt_fees.get(str(s.execute(select(Member.id).where(Member.name == "Mason")).scalar_one())) is None  # exempt
        total = f.total()
        assert abs(total - D(770)) < D("0.0001")    # fees move money, never create or destroy it
        assert abs(f.accounts[ms.GP] - (mgmt + perf)) < D("0.01")  # plus its tiny share of later trades
        assert f.hwm[k] == f.accounts[k] or f.hwm[k] > f.accounts[k]


def test_company_grants_come_from_founder(clean_env):
    with session_scope() as s:
        m1 = s.execute(select(Member).where(Member.name == "Member 1")).scalar_one()
        g = ms.grant(s, m1.id, "B", D(2), 0, 0, "strategic partner", "cio")
        rows = {(x["name"], x["class"]): x for x in ms.company(s)["grants"]}
        assert rows[("Mason", "A")]["pct"] == "98.0000" and rows[("Member 1", "B")]["pct"] == "2.0000"
        assert not rows[("Member 1", "B")]["voting"]
        with pytest.raises(ms.MemberError):
            ms.grant(s, m1.id, "C", D(99), 48, 12, "too much", "cio")
        c = ms.grant(s, m1.id, "C", D(5), 48, 12, "employee", "cio")
        assert next(x for x in ms.company(s)["grants"] if x["id"] == c.id)["vested_pct"] == "0.0000"  # inside the cliff
        ms.revoke(s, g.id, "cio")
        rows = {(x["name"], x["class"]): x for x in ms.company(s)["grants"]}
        assert rows[("Mason", "A")]["pct"] == "95.0000" and ("Member 1", "B") not in rows
        assert ms.company(s)["issued_pct"] == "100.0000"


def test_members_api(clean_env):
    c = client()
    login(c)
    ov = c.get("/api/members").json()
    names = {m["name"]: m for m in ov["members"]}
    assert names["Mason"]["value"] == "600.00" and names["Member 1"]["ownership_pct"] == "14.29"
    assert ov["terms"]["mgmt_fee_pct"] == "2.00" and ov["company"]["grants"][0]["pct"] == "100.0000"
    mid = names["Member 1"]["id"]
    assert c.patch(f"/api/members/{mid}", json={"name": "Jordan"}).status_code == 200
    new = c.post("/api/members", json={"name": "Taylor"}).json()["id"]
    r = c.post(f"/api/members/{new}/ledger", json={"kind": "CONTRIBUTION", "amount": "350", "note": "joined"})
    assert r.status_code == 200, r.text
    ov = c.get("/api/members").json()
    assert {m["name"] for m in ov["members"]} == {"Mason", "Jordan", "Taylor"} and ov["equity"] == "1050.00"
    assert c.get(f"/api/members/{new}").json()["ownership_pct"] == "33.33"
    assert c.post(f"/api/members/{new}/ledger", json={"kind": "WITHDRAWAL", "amount": "9999"}).status_code == 400
    bad = c.post("/api/members/terms", json={"mgmt_fee_pct": "1", "perf_alloc_pct": "15", "reason": "smaller pool", "password": "nope"})
    assert bad.status_code == 403
    ok = c.post("/api/members/terms", json={"mgmt_fee_pct": "1", "perf_alloc_pct": "15", "reason": "smaller pool",
                                             "password": "test-password-123"})
    assert ok.status_code == 200 and c.get("/api/members").json()["terms"]["perf_alloc_pct"] == "15.00"
    g = c.post("/api/members/company/grants", json={"member_id": new, "equity_class": "B", "pct": "3",
                                                     "password": "test-password-123"})
    assert g.status_code == 200, g.text
    assert c.get(f"/api/members/{new}").json()["company_equity"][0]["pct"] == "3.0000"
    with session_scope() as s:
        assert s.execute(select(CapitalEvent).where(CapitalEvent.note.like("member:%"))).scalars().first() is not None
        assert len(list(s.execute(select(MemberLedger)).scalars())) == 3


def test_open_positions_marked_to_market_at_deposits(clean_env):
    """A late investor must not get gains made before they joined, even while the trade is still open."""
    import time
    from meridian.db.models import BookSnapshot, Instrument
    gol = "GOL-25NOV26-CDE"
    with session_scope() as s:
        _no_fees(s)
        s.add(Instrument(id=gol, asset="GOLD", venue="coinbase", product_type="FUTURE", display_name="Gold",
                         quote_currency="USD", contract_size=D(1), source="COINBASE", status="ACTIVE"))
    with session_scope() as s:
        p = Position(instrument_id=gol, direction="LONG", quantity=D(1), entry_price=D(4000), stop_price=D(3900),
                     contract_size=D(1), status="OPEN", recorded_by="cio", opened_at=datetime.now(UTC))
        s.add(p)
        s.add(BookSnapshot(instrument_id=gol, ts=datetime.now(UTC), best_bid=D(4070), best_ask=D(4070), spread_bps=D(0), source="t"))
        s.flush()
        pid = p.id
    with session_scope() as s:   # +$70 open: shown in everyone's value already
        by = _by(s)
        assert by["Member 1"]["value"] == "110.00" and by["Mason"]["value"] == "660.00" and by["Member 1"]["open_pnl"] == "10.00"
        late = Member(name="Late")
        s.add(late)
        s.flush()
        e = ms.record(s, late.id, "CONTRIBUTION", D(220), False, "joins mid-trade", "cio")
        assert e.open_marks == {gol: "70.0000000000"} or D(e.open_marks[gol]) == D(70)
    time.sleep(0.05)
    with session_scope() as s:   # price moves up another $99 after Late joined
        s.add(BookSnapshot(instrument_id=gol, ts=datetime.now(UTC), best_bid=D(4169), best_ask=D(4169), spread_bps=D(0), source="t"))
    with session_scope() as s:
        by = _by(s)
        # the first $70 belongs to Mason/Member 1; the next $99 is shared by all (Late owns 220/990)
        assert by["Late"]["value"] == "242.00" and by["Member 1"]["value"] == "121.00" and by["Mason"]["value"] == "726.00"
    with session_scope() as s:   # close at the same price: booked results equal the open view
        pos = s.get(Position, pid)
        pos.status, pos.exit_price, pos.closed_at, pos.realized_pnl = "CLOSED", D(4169), datetime.now(UTC), D(169)
    with session_scope() as s:
        by = _by(s)
        assert by["Late"]["value"] == "242.00" and by["Member 1"]["value"] == "121.00" and by["Mason"]["value"] == "726.00"
        assert by["Late"]["open_pnl"] == "0.00"
        m1 = s.execute(select(Member).where(Member.name == "Member 1")).scalar_one()
        [t] = ms.detail(s, m1.id)["trades"]
        assert t["share"] == "21.00"                                   # $10 before Late joined + $11 after
