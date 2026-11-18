"""Member logins: see only their own account; locked out of everything else."""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from meridian.db.models import Member
from meridian.db.session import session_scope
from test_flow import client, login

pytestmark = pytest.mark.integration
CIO_PW = "test-password-123"


def _ids():
    with session_scope() as s:
        return {m.name: str(m.id) for m in s.execute(select(Member)).scalars()}


def _member_client(username, temp, new="a-much-better-password"):
    c = client()
    r = c.post("/api/auth/login", json={"username": username, "password": temp})
    assert r.status_code == 200 and r.json()["role"] == "MEMBER" and r.json()["must_change_password"]
    assert c.post("/api/auth/password", json={"current": temp, "new": new}).status_code == 200
    return c


def test_member_portal_isolation(clean_env):
    ids = _ids()
    cio = client()
    login(cio)
    assert cio.post(f"/api/members/{ids['Member 1']}/login", json={"username": "jordan", "password": "wrong"}).status_code == 403
    r = cio.post(f"/api/members/{ids['Member 1']}/login", json={"username": "jordan", "password": CIO_PW})
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert cio.get(f"/api/members/{ids['Member 1']}/login").json()["must_change_password"] is True
    m = _member_client("jordan", temp)
    acct = m.get("/api/me/account").json()
    assert acct["name"] == "Member 1" and acct["value"] == "100.00" and "note" not in acct
    # every internal API route is closed to member logins
    from meridian.api.routes import router
    checked = 0
    for route in router.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods or not route.path.startswith("/api/"):
            continue
        if route.path in ("/api/me/account", "/api/auth/me", "/api/health", "/api/healthz", "/api/ready"):
            continue
        path = route.path
        for prm in route.param_convertors:
            path = path.replace("{" + prm + "}", ids["Mason"] if "member" in prm else "00000000-0000-0000-0000-000000000000" if "id" in prm else "x")
        code = m.get(path).status_code
        assert code in (401, 403, 404, 405, 422), (route.path, code)
        assert code != 200
        checked += 1
    assert checked > 30
    assert m.post("/api/members", json={"name": "x"}).status_code == 403
    assert m.post(f"/api/members/{ids['Mason']}/ledger", json={"kind": "WITHDRAWAL", "amount": "1"}).status_code == 403
    # Mason's own staff login still works everywhere
    assert cio.get("/api/overview").status_code == 200 and cio.get("/api/me/account").status_code == 403
    # disabling takes effect immediately, even with a live session
    assert cio.post(f"/api/members/{ids['Member 1']}/login/disable").status_code == 200
    assert m.get("/api/me/account").status_code == 401
    assert client().post("/api/auth/login", json={"username": "jordan", "password": "a-much-better-password"}).status_code == 401
    # reset re-enables with a new one-time password
    r = cio.post(f"/api/members/{ids['Member 1']}/login", json={"username": "jordan", "password": CIO_PW})
    m2 = _member_client("jordan", r.json()["temporary_password"], new="another-good-password")
    assert m2.get("/api/me/account").json()["name"] == "Member 1"


def test_username_cannot_hijack_staff(clean_env):
    ids = _ids()
    cio = client()
    login(cio)
    assert cio.post(f"/api/members/{ids['Member 1']}/login", json={"username": "cio", "password": CIO_PW}).status_code == 409
