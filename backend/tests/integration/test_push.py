"""Phone alerts: only new recommendations and EXIT flips, once each; staff only; signing key never exposed."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from meridian.core.util import utcnow
from meridian.db.models import (
    AppSecret, CaseTransition, Instrument, Position, PositionAdvisory, PushSent, PushSubscription, ResearchCase,
)
from meridian.db.session import session_scope
from meridian.notify import push
from sqlalchemy import func, select
from test_flow import client, login

pytestmark = pytest.mark.integration


@pytest.fixture()
def sent(monkeypatch):
    calls = []
    import pywebpush
    monkeypatch.setattr(pywebpush, "webpush", lambda info, data, **kw: calls.append((info["endpoint"], data, kw)))
    return calls


def _case(s, state="RECOMMENDED", age=timedelta(0)):
    c = ResearchCase(id=uuid.uuid4(), instrument_id="GOL-TEST-CDE", asset="GOLD", direction="LONG", state=state,
                     hypothesis={}, data_source="SIM", correlation_id=uuid.uuid4())
    s.add(c); s.flush()
    s.add(CaseTransition(case_id=c.id, from_state="COMMITTEE_REVIEW", to_state="RECOMMENDED", reason="t", actor="t",
                         created_at=utcnow() - age))
    s.flush()
    return c


def _sub(s, user="cio", ep="https://push.example.com/abc"):
    s.add(PushSubscription(username=user, endpoint=ep, p256dh="x" * 20, auth="y" * 16))


def test_recommendations_and_exits_alert_once(clean_env, sent):
    with session_scope() as s:
        _sub(s)
        fresh = _case(s)
        _case(s, age=timedelta(hours=3))           # too old (e.g. before a deploy): no alert
        _case(s, state="EXPIRED")                   # no longer recommended: no alert
        s.add(Instrument(id="GOL-TEST-CDE", asset="GOLD", venue="TEST", product_type="FUTURE", display_name="Gold test",
                         quote_currency="USD", source="SIMULATED", status="ACTIVE"))
        s.flush()
        pos = Position(instrument_id="GOL-TEST-CDE", direction="LONG", quantity=Decimal(1), entry_price=Decimal(100),
                       stop_price=Decimal(95), contract_size=Decimal(1), status="OPEN", recorded_by="cio")
        s.add(pos); s.flush()
        for action in ("HOLD", "EXIT", "EXIT", "HOLD", "EXIT"):  # two flips into EXIT, one hourly repeat
            s.add(PositionAdvisory(position_id=pos.id, action=action, reason_codes=["STOP_BREACHED"] if action == "EXIT" else [],
                                   detail={"last_price": "94.5", "unrealized_pnl_usd": "-5.50"}))
            s.flush()
        s.commit()
        assert push.run_once(s, "https://x.test") == 3
        assert push.run_once(s, "https://x.test") == 0   # never twice
    titles = [d for _, d, _ in sent]
    assert len(sent) == 3
    assert sum("New recommendation: LONG GOLD" in t for t in titles) == 1
    assert sum("Exit advisory" in t and "stop breached" in t for t in titles) == 2
    assert all(kw["vapid_claims"] == {"sub": "https://x.test"} for _, _, kw in sent)
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(PushSent)) == 3
        assert str(fresh.id) in "".join(k for k in s.execute(select(PushSent.key)).scalars())


def test_prediction_desk_never_pushes(clean_env, sent):
    with session_scope() as s:
        _sub(s)
        s.commit()
        assert push.run_once(s, "https://x.test") == 0
    assert "PmSignal" not in open(push.__file__).read() and sent == []


def test_push_api_staff_only_and_key_private(clean_env, sent):
    c = client(); login(c)
    st = c.get("/api/push/status").json()
    assert st["devices"] == 0 and len(st["public_key"]) > 80
    assert c.post("/api/push/test").status_code == 409
    assert c.post("/api/push/subscribe", json={"endpoint": "http://evil", "keys": {"p256dh": "x" * 20, "auth": "y" * 16}}).status_code == 422
    ep = "https://web.push.apple.com/abc123"
    assert c.post("/api/push/subscribe", json={"endpoint": ep, "keys": {"p256dh": "x" * 20, "auth": "y" * 16}}).status_code == 200
    assert c.get("/api/push/status", params={"endpoint": ep}).json()["this_device"] is True
    assert c.post("/api/push/test").json()["delivered"] == 1 and sent[-1][0] == ep
    with session_scope() as s:
        pem = s.get(AppSecret, push.VAPID_NAME).value
    for path in ("/api/push/status",):
        assert "PRIVATE" not in c.get(path).text and pem[30:60] not in c.get(path).text
    assert c.post("/api/push/unsubscribe", json={"endpoint": ep}).status_code == 200
    assert c.get("/api/push/status").json()["devices"] == 0
    anon = client()
    assert anon.get("/api/push/status").status_code == 401
    assert anon.post("/api/push/subscribe", json={"endpoint": ep, "keys": {"p256dh": "x" * 20, "auth": "y" * 16}}).status_code == 401
