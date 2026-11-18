"""Member ownership by unit accounting."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
import sqlalchemy.exc
from sqlalchemy import select, text

from meridian.db.models import CapitalEvent, Member, MemberLedger, Position
from meridian.db.session import session_scope
from meridian.members import service as ms
from test_flow import client, login

pytestmark = pytest.mark.integration
T0 = datetime.now(UTC) + timedelta(minutes=5)  # after the startup seed


def _trade(s, pnl, at):  # noqa: ANN001
    s.add(Position(instrument_id="GOL-25NOV26-CDE", direction="LONG", quantity=D(1), entry_price=D(4000), stop_price=D(3930),
                   contract_size=D(1), status="CLOSED", opened_at=at - timedelta(hours=2), closed_at=at,
                   exit_price=D(4000) + D(pnl), realized_pnl=D(pnl), recorded_by="cio"))


def test_original_split_then_later_member_and_trades(clean_env):
    with session_scope() as s:  # startup recorded the $700 and seeded Mason $600 + Member 1 $100
        mason = s.execute(select(Member).where(Member.name == "Mason")).scalar_one()
        m1 = s.execute(select(Member).where(Member.name == "Member 1")).scalar_one()
        with pytest.raises(ms.MemberError):
            ms.record(s, m1.id, "CONTRIBUTION", D(1), True, "", "cio")  # nothing left unassigned
        ids = (mason.id, m1.id)
    with session_scope() as s:
        ov = ms.overview(s)
        by = {r["name"]: r for r in ov["members"]}
        assert by["Member 1"]["ownership_pct"] == "14.29" and by["Mason"]["ownership_pct"] == "85.71"
        assert ov["unassigned"] is None and ov["nav_per_unit"] == "1.000000"
        _trade(s, 70, T0 + timedelta(days=1))  # +10%
    with session_scope() as s:
        by = {r["name"]: r for r in ms.overview(s)["members"]}
        assert by["Member 1"]["value"] == "110.00" and by["Mason"]["value"] == "660.00"
        # a new member joins at NAV 1.10: buys units without taking any of the earlier gain
        late = Member(name="Late")
        s.add(late)
        s.flush()
        e = ms.record(s, late.id, "CONTRIBUTION", D(220), False, "joins", "cio", now=T0 + timedelta(days=2))
        assert e.nav_per_unit == D("1.1") and e.units == D(200)
        late_id = late.id
    with session_scope() as s:
        ov = ms.overview(s)
        by = {r["name"]: r for r in ov["members"]}
        assert ov["equity"] == "990.00" and by["Late"]["value"] == "220.00" and by["Member 1"]["value"] == "110.00"
        _trade(s, -99, T0 + timedelta(days=3))  # -10%, shared by all three
    with session_scope() as s:
        by = {r["name"]: r for r in ms.overview(s)["members"]}
        assert by["Late"]["value"] == "198.00" and by["Member 1"]["value"] == "99.00" and by["Mason"]["value"] == "594.00"
        d = ms.detail(s, ids[1])
        assert [t["share"] for t in d["trades"]] == ["-11.00", "10.00"] and d["stats"]["trades"] == 2
        assert d["series"] and d["gain"] == "-1.00"
        with pytest.raises(ms.MemberError):
            ms.record(s, late_id, "WITHDRAWAL", D(500), False, "", "cio")
        ms.record(s, late_id, "WITHDRAWAL", D(98), False, "partial", "cio", now=T0 + timedelta(days=4))
    with session_scope() as s:
        by = {r["name"]: r for r in ms.overview(s)["members"]}
        assert by["Late"]["value"] == "100.00" and by["Late"]["withdrawn"] == "98.00"
    with pytest.raises(sqlalchemy.exc.DBAPIError), session_scope() as s:
        s.execute(text("UPDATE member_ledger SET amount = 1"))


def test_members_api_and_seed_sql(clean_env):
    # the migration's seed block (for databases that already had the $700) gives the same result as startup seeding
    from pathlib import Path
    with session_scope() as s:
        s.execute(text("ALTER TABLE member_ledger DISABLE TRIGGER member_ledger_append_only"))
        s.execute(text("DELETE FROM member_ledger; DELETE FROM members;"))
        s.execute(text("ALTER TABLE member_ledger ENABLE TRIGGER member_ledger_append_only"))
    src = Path(__file__).parents[2] / "migrations/versions/0006_members.py"
    body = src.read_text().split('op.execute("""\n        DO $$')[1].split('$$;\n    """)')[0]
    with session_scope() as s:
        s.execute(text("DO $$" + body + "$$;"))
    c = client()
    login(c)
    ov = c.get("/api/members").json()
    names = {m["name"]: m for m in ov["members"]}
    assert names["Mason"]["value"] == "600.00" and names["Member 1"]["ownership_pct"] == "14.29"
    mid = names["Member 1"]["id"]
    assert c.patch(f"/api/members/{mid}", json={"name": "Jordan"}).status_code == 200
    new = c.post("/api/members", json={"name": "Taylor"}).json()["id"]
    r = c.post(f"/api/members/{new}/ledger", json={"kind": "CONTRIBUTION", "amount": "350", "note": "joined"})
    assert r.status_code == 200, r.text
    ov = c.get("/api/members").json()
    assert {m["name"] for m in ov["members"]} == {"Mason", "Jordan", "Taylor"} and ov["equity"] == "1050.00"
    assert c.get(f"/api/members/{new}").json()["ownership_pct"] == "33.33"
    assert c.post(f"/api/members/{new}/ledger", json={"kind": "WITHDRAWAL", "amount": "9999"}).status_code == 400
    with session_scope() as s:
        assert s.execute(select(CapitalEvent).where(CapitalEvent.note.like("member:%"))).scalars().first() is not None
        assert len(list(s.execute(select(MemberLedger)).scalars())) == 3
