"""End-to-end and invariant tests against real PostgreSQL + Redis."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from meridian.api.app import create_app
from meridian.api.auth import hash_password
from meridian.bus.bus import EventBus, Subscription, publish
from meridian.bus.schemas import make_event
from meridian.core.audit import verify_chain
from meridian.db.models import (
    CommitteeMemo,
    DeadLetter,
    Event,
    Position,
    PositionAdvisory,
    ResearchCase,
    RiskDecision,
    User,
    WorkerHeartbeat,
    WorkerRun,
)
from meridian.db.session import SessionLocal, get_engine, session_scope
from meridian.demo import run_demo
from meridian.runtime import Runtime, make_context
from meridian.workers.base import BaseWorker, WorkerSpec, execute
from meridian.workers.macro_provider import MacroEventDTO

pytestmark = pytest.mark.integration


class FakeMacro:
    """Deterministic macro provider for tests (no network)."""
    name = "test_calendar"

    def __init__(self, events=None):
        self.events = events

    def fetch(self):
        if self.events is not None:
            return self.events
        t = datetime.now(UTC) + timedelta(days=3)
        return [MacroEventDTO(id="ev1", title="Non-Farm Employment Change", country="USD", impact="High",
                              event_time=t, time_confidence="EXACT", forecast=None, previous=None, source=self.name)]


def client() -> TestClient:
    return TestClient(create_app(start_runtime=False, run_bootstrap=False))


def login(c: TestClient, user="cio", pw="test-password-123") -> None:
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------------------ full vertical slice
def test_end_to_end_vertical_slice(clean_env):
    out = run_demo(clean_env, macro=FakeMacro())
    assert out["memos"], out
    c = client()
    login(c)

    ov = c.get("/api/overview").json()
    assert ov["simulated"] is True and ov["workers_implemented"] == 58 and ov["workers_total"] == 59
    assert any(f["dataset"] == "macro_calendar" and f["status"] == "FRESH" for f in ov["freshness"])

    cases = c.get("/api/cases").json()
    rec = [x for x in cases if x["state"] == "RECOMMENDED"]
    assert rec, [(x["asset"], x["state"], x["risk_outcome"]) for x in cases]
    case = c.get(f"/api/cases/{rec[0]['id']}").json()
    states = [t["to_state"] for t in case["transitions"]]
    assert states[:7] == ["DETECTED", "RESEARCHING", "CHALLENGED", "RISK_REVIEW", "COMMITTEE_REVIEW", "RECOMMENDED"][:7] or \
        states == ["DETECTED", "RESEARCHING", "CHALLENGED", "RISK_REVIEW", "COMMITTEE_REVIEW", "RECOMMENDED"]
    kinds = {e["kind"] for e in case["evidence"]}
    assert {"SIGNAL", "MACRO", "CHALLENGE", "VOLATILITY"} <= kinds
    memo = case["memos"][0]["memo"]
    assert memo["data_source"] == "SIMULATED" and memo["disclaimer"].startswith("Advisory only")
    assert Decimal(memo["trade"]["max_loss_usd"]) <= Decimal("70.00")  # 10% of $700
    # every event in the case shares the correlation id and has causation links
    assert len(case["events"]) >= 6 and all(e["causation_id"] for e in case["events"][1:])

    # reconstruction from stored inputs
    rec_report = c.get(f"/api/memos/{case['memos'][0]['id']}/reconstruct").json()
    assert rec_report["reconstructed"] is True, rec_report

    # portfolio constructor ranks live recommendations and allocates within limits
    plan = c.get("/api/portfolio/plan").json()
    assert plan["allocations"] and all(Decimal(a["risk_usd"]) <= Decimal("70") for a in plan["allocations"])
    assert sum(Decimal(a["risk_usd"]) for a in plan["allocations"]) <= Decimal("70")  # default 10% total cap

    # CIO decision
    r = c.post(f"/api/cases/{rec[0]['id']}/decision", json={"decision": "ACCEPT", "note": "taking it"})
    assert r.status_code == 200, r.text

    # CIO records the executed trade -> other recommendations go back to research
    t = memo["trade"]
    r = c.post("/api/positions", json={"case_id": rec[0]["id"], "instrument_id": t["instrument_id"], "direction": t["direction"],
                                       "quantity": t["suggested_quantity"], "entry_price": t["entry_reference"],
                                       "stop_price": t["stop"], "target_price": t["target"]})
    assert r.status_code == 200, r.text
    rereviewed = r.json()["rereviewed_cases"]
    ctx = make_context(clean_env, macro=FakeMacro())
    rt = Runtime.build(clean_env, ctx)
    rt.drain()
    with session_scope() as s:
        for cid in rereviewed:
            decs = list(s.execute(select(RiskDecision).where(RiskDecision.case_id == uuid.UUID(cid))
                                  .order_by(RiskDecision.created_at)).scalars())
            assert len(decs) == 2
            assert any(code.startswith(("TOTAL_RISK", "SIZE_REDUCED_TOTAL", "CORRELATED", "SIZE_REDUCED_CORR"))
                       for code in decs[-1].reason_codes), decs[-1].reason_codes
            assert Decimal(decs[-1].sizing.get("max_loss_usd", "0")) + Decimal(t["max_loss_usd"]) <= Decimal("70.00")

    # position monitor issues an advisory
    rt.run_scheduled("risk.position_monitor")
    pos = c.get("/api/positions").json()[0]
    assert pos["advisory"]["action"] in ("HOLD", "EXIT", "REVIEW")

    # close it: realized PnL recorded, case closed
    r = c.post(f"/api/positions/{pos['id']}/close", json={"exit_price": t["stop"], "note": "stopped"})
    assert r.status_code == 200 and Decimal(r.json()["realized_pnl"]) == -Decimal(t["max_loss_usd"])
    assert c.get(f"/api/cases/{rec[0]['id']}").json()["case"]["state"] == "CLOSED"
    assert c.get("/api/archive").json()["memos"][0]
    a = c.get("/api/audit").json()
    assert a["chain_valid"] is True and any(e["action"] == "cio.decision" for e in a["entries"])


# ------------------------------------------------------------------------------ veto & immutability in the DB
def test_database_enforces_risk_veto_and_immutability(clean_env):
    with session_scope() as s:
        case = ResearchCase(instrument_id="SIM-GOLD", asset="GOLD", direction="LONG", state="COMMITTEE_REVIEW",
                            hypothesis={"x": 1}, data_source="SIMULATED", correlation_id=uuid.uuid4())
        s.add(case)
        s.flush()
        dec = RiskDecision(case_id=case.id, config_version=1, outcome="BLOCK", reason_codes=["X"], checks=[], sizing={},
                           inputs={}, inputs_hash="h", code_version="t")
        s.add(dec)
        s.flush()
        cid, did = case.id, dec.id
    eng = get_engine()
    for sql in (f"UPDATE research_cases SET state='RECOMMENDED' WHERE id='{cid}'",
                f"UPDATE research_cases SET hypothesis='{{\"x\": 2}}' WHERE id='{cid}'",
                f"INSERT INTO committee_memos (id, case_id, risk_decision_id, recommendation, score, memo, inputs, inputs_hash, code_version)"
                f" VALUES (gen_random_uuid(), '{cid}', '{did}', 'RECOMMEND', 1, '{{}}', '{{}}', 'h', 't')",
                f"INSERT INTO committee_memos (id, case_id, risk_decision_id, recommendation, score, memo, inputs, inputs_hash, code_version)"
                f" VALUES (gen_random_uuid(), '{cid}', '{did}', 'DEFER', 1, '{{}}', '{{}}', 'h', 't')",
                f"UPDATE risk_decisions SET outcome='ALLOW' WHERE id='{did}'",
                "UPDATE audit_log SET actor='x'",
                "DELETE FROM audit_log",
                "UPDATE risk_configs SET config='{}'"):
        with pytest.raises(Exception) as ei:
            with eng.begin() as c:
                c.execute(text(sql))
        assert any(k in str(ei.value) for k in ("RISK_VETO", "IMMUTABLE", "APPEND_ONLY")), sql


# ------------------------------------------------------------------------------ event bus
def test_bus_idempotency_retry_dlq_expiry_replay(clean_env):
    r = redis.Redis.from_url(clean_env.redis_url, decode_responses=True)
    bus = EventBus(r, SessionLocal, "bustest")
    seen: list[str] = []
    sub = Subscription(group="g1", event_types={"case.researched"}, handler=lambda s, e: seen.append(str(e.event_id)),
                       max_attempts=2, retry_backoff_ms=0)
    env = make_event("case.researched", {"case_id": uuid.uuid4(), "state": "RESEARCHING"}, producer="test")
    with session_scope() as s:
        publish(s, env)
    assert bus.relay_once() == 1 and bus.relay_once() == 0
    bus.consume_once(sub, block_ms=10)
    r.xadd(bus.stream, {"envelope": env.model_dump_json(), "type": env.event_type})  # duplicate delivery
    bus.consume_once(sub, block_ms=10)
    assert seen == [str(env.event_id)] and sub.stats["skipped"] == 1

    # failure -> retry -> dead letter
    def boom(s, e):  # noqa: ANN001
        raise RuntimeError("handler bug")
    bad = Subscription(group="g2", event_types={"case.researched"}, handler=boom, max_attempts=2, retry_backoff_ms=0)
    bus.consume_once(bad, block_ms=10)
    bus.consume_once(bad, block_ms=10)
    with session_scope() as s:
        dl = s.execute(select(DeadLetter)).scalars().all()
    assert len(dl) == 1 and dl[0].consumer == "g2" and r.xlen(bus.dlq_stream) == 1
    assert bus.pending("g2") == 0

    # expired events are acknowledged but not processed
    old = make_event("case.researched", {"case_id": uuid.uuid4(), "state": "X"}, producer="test", ttl=timedelta(seconds=-1))
    with session_scope() as s:
        publish(s, old)
    bus.relay_once()
    bus.consume_once(sub, block_ms=10)
    assert str(old.event_id) not in seen and sub.stats["expired"] == 1

    # replay from durable log: idempotent by default, reprocesses with force
    assert bus.replay(sub) == {"SKIPPED": 2}  # already DONE / EXPIRED -> idempotent
    n = len(seen)
    assert bus.replay(sub, force=True)["DONE"] == 2 and len(seen) == n + 2


# ------------------------------------------------------------------------------ worker framework
class Exploding(BaseWorker):
    spec = WorkerSpec(id="data.reliability", department="data_ops", name="x", responsibility="x", version="9",
                      max_retries=3, retry_backoff_s=0.0)

    def run(self, s, rc):  # noqa: ANN001, ANN201
        raise ValueError("kaboom")


def test_worker_failure_is_retried_recorded_and_escalated(clean_env):
    w = Exploding(make_context(clean_env, macro=FakeMacro()))
    with pytest.raises(ValueError):
        execute(w)
    with session_scope() as s:
        run = s.execute(select(WorkerRun).where(WorkerRun.worker_id == "data.reliability")).scalar_one()
        assert run.status == "FAILED" and run.attempts == 3 and "kaboom" in run.error
        assert s.get(WorkerHeartbeat, "data.reliability").status == "FAILING"
        ev = s.execute(select(Event).where(Event.event_type == "worker.failed")).scalar_one()
        assert ev.payload["escalation_to"] == "exec.chief_of_staff"


def test_inactive_worker_does_not_consume(clean_env):
    c = client()
    login(c)
    assert c.post("/api/workers/trend.signal_analyst/status", json={"status": "INACTIVE"}).status_code == 200
    assert c.post("/api/workers/stat_arb.cross_venue/status", json={"status": "ACTIVE"}).status_code == 409  # planned role
    out = run_demo(clean_env, macro=FakeMacro())
    assert out["memos"] == []
    with session_scope() as s:
        assert not s.execute(select(WorkerRun).where(WorkerRun.worker_id == "trend.signal_analyst")).first()
    # re-activate: the waiting events are consumed (durable)
    assert c.post("/api/workers/trend.signal_analyst/status", json={"status": "ACTIVE"}).status_code == 200
    rt = Runtime.build(clean_env, make_context(clean_env, macro=FakeMacro()))
    rt.drain()
    with session_scope() as s:
        assert s.execute(select(CommitteeMemo)).first() is not None


# ------------------------------------------------------------------------------ security
def test_auth_and_authorization(clean_env):
    c = client()
    for path in ("/api/overview", "/api/cases", "/api/audit", "/api/positions"):
        assert c.get(path).status_code == 401
    assert c.post("/api/auth/login", json={"username": "cio", "password": "wrong"}).status_code == 401
    with session_scope() as s:
        s.add(User(username="viewer", password_hash=hash_password("viewer-pass-123"), role="VIEWER"))
    login(c, "viewer", "viewer-pass-123")
    assert c.get("/api/overview").status_code == 200
    assert c.post("/api/positions", json={}).status_code in (403, 422)
    assert c.post("/api/risk/config", json={"config": {}, "reason": "hello", "password": "x"}).status_code == 403
    assert c.get("/api/health").status_code == 200


def test_risk_config_change_requires_reauth_and_is_audited(clean_env):
    c = client()
    login(c)
    body = {"config": {"max_loss_per_trade_pct": "0.05"}, "reason": "tighten while testing"}
    assert c.post("/api/risk/config", json={**body, "password": "nope"}).status_code == 403
    assert c.post("/api/risk/config", json={**body, "config": {"bogus": 1}, "password": "test-password-123"}).status_code == 422
    r = c.post("/api/risk/config", json={**body, "password": "test-password-123"})
    assert r.status_code == 200 and r.json()["version"] == 2
    vers = c.get("/api/risk/config").json()["versions"]
    assert [v["status"] for v in vers] == ["ACTIVE", "SUPERSEDED"]
    entries = c.get("/api/audit").json()["entries"]
    assert any(e["action"] == "risk.config_changed" for e in entries)
    assert any(e["action"] == "risk.config_change_denied" for e in entries)


def test_cannot_accept_blocked_case(clean_env):
    run_demo(clean_env, macro=FakeMacro([MacroEventDTO(
        id="now", title="FOMC Statement", country="USD", impact="High", event_time=datetime.now(UTC) + timedelta(minutes=20),
        time_confidence="EXACT", forecast=None, previous=None, source="t")]))
    c = client()
    login(c)
    cases = c.get("/api/cases").json()
    assert cases and all(x["state"] == "BLOCKED" for x in cases), [(x["asset"], x["state"]) for x in cases]
    with session_scope() as s:
        assert all("EVENT_BLACKOUT" in d.reason_codes for d in s.execute(select(RiskDecision)).scalars())
    r = c.post(f"/api/cases/{cases[0]['id']}/decision", json={"decision": "ACCEPT"})
    assert r.status_code == 409


def test_audit_chain_detects_tampering(clean_env):
    run_demo(clean_env, macro=FakeMacro())
    with session_scope() as s:
        assert verify_chain(s) == (True, None)
    with get_engine().begin() as c:
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER trg_audit_append_only"))
        c.execute(text("UPDATE audit_log SET actor='mallory' WHERE id = (SELECT min(id) + 2 FROM audit_log)"))
        c.execute(text("ALTER TABLE audit_log ENABLE TRIGGER trg_audit_append_only"))
    with session_scope() as s:
        ok, bad = verify_chain(s)
    assert ok is False and bad is not None


def test_positions_and_monitor_exit_on_stop(clean_env):
    run_demo(clean_env, macro=FakeMacro())
    c = client()
    login(c)
    inst = next(i for i in c.get("/api/marketdata/instruments").json() if i["asset"] == "BTC")
    last = Decimal(c.get(f"/api/marketdata/candles/{inst['id']}?limit=1").json()[-1]["close"])
    # LONG with a stop above the current price => monitor must say EXIT / STOP_BREACHED
    r = c.post("/api/positions", json={"instrument_id": inst["id"], "direction": "LONG", "quantity": "1",
                                       "entry_price": str(last * 2), "stop_price": str(last * Decimal("1.5"))})
    assert r.status_code == 200
    Runtime.build(clean_env, make_context(clean_env, macro=FakeMacro())).run_scheduled("risk.position_monitor")
    with session_scope() as s:
        adv = s.execute(select(PositionAdvisory)).scalars().all()
        assert adv[-1].action == "EXIT" and "STOP_BREACHED" in adv[-1].reason_codes
        assert s.execute(select(Position)).scalar_one().status == "OPEN"  # MERIDIAN never closes anything itself


def test_blocked_asset_is_rereviewed_on_next_signal(clean_env):
    """A BLOCK (e.g. during an event window) must not freeze the asset for the day: the next signal gets a fresh review."""
    from meridian.db.models import ResearchCase
    from meridian.marketdata.simulated import SimulatedProvider
    from meridian.marketdata.service import demo_anchor
    run_demo(clean_env, macro=FakeMacro([MacroEventDTO(
        id="now", title="FOMC Statement", country="USD", impact="High", event_time=datetime.now(UTC) + timedelta(minutes=20),
        time_confidence="EXACT", forecast=None, previous=None, source="t")]))
    with session_scope() as s:
        blocked = list(s.execute(select(ResearchCase).where(ResearchCase.state == "BLOCKED")).scalars())
        assert blocked
        replay = [(c.id, dict(c.hypothesis)) for c in blocked]
        anchor = demo_anchor(s)
    # the event has passed (move it 3 hours into the past); the same signals fire again
    from meridian.db.models import MacroEvent
    with session_scope() as s:
        for e in s.execute(select(MacroEvent)).scalars():
            e.event_time = datetime.now(UTC) - timedelta(hours=3)
    ctx = make_context(clean_env, market=SimulatedProvider(scenario="demo", demo_end=anchor), macro=FakeMacro())
    rt = Runtime.build(clean_env, ctx)
    with session_scope() as s:
        for _, h in replay:
            payload = {k: v for k, v in h.items() if k not in ("statement", "signal_event_id")}
            publish(s, make_event("signal.detected", payload, producer="test@1"))
    rt.drain()
    with session_scope() as s:
        old = {cid: s.get(ResearchCase, cid).state for cid, _ in replay}
        assert set(old.values()) == {"EXPIRED"}
        fresh = list(s.execute(select(ResearchCase).where(ResearchCase.id.notin_(list(old)))).scalars())
        assert len(fresh) == len(replay) and all(c.state != "BLOCKED" for c in fresh), [c.state for c in fresh]
        decisions = list(s.execute(select(RiskDecision).where(RiskDecision.case_id.in_([c.id for c in fresh]))).scalars())
        assert decisions and all("EVENT_BLACKOUT" not in d.reason_codes for d in decisions)
