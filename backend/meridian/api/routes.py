"""HTTP API. Read endpoints require any authenticated user; every state-changing endpoint requires the CIO.
There are deliberately no endpoints that place, modify or cancel orders."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from meridian.api.auth import (
    COOKIE,
    Principal,
    authenticate,
    current_principal,
    get_db,
    issue_token,
    note_failure,
    require_cio,
    throttled,
    verify_password,
)
from meridian.bus.bus import publish
from meridian.bus.schemas import make_event
from meridian.cases import lifecycle as lc
from meridian.committee.logic import decide
from meridian.core import audit
from meridian.core.config import get_settings
from meridian.core.util import jsonable, sha256, utcnow
from meridian.db.models import (
    AuditLog,
    Candle,
    CapitalEvent,
    CaseEvidence,
    CaseRevision,
    CaseTransition,
    CioDecision,
    CommitteeMemo,
    DataFreshness,
    DeadLetter,
    Department,
    Event,
    Instrument,
    MacroEvent,
    Position,
    PositionAdvisory,
    ResearchCase,
    RiskDecision,
    User,
    WorkerRun,
)
from meridian.portfolio import service as portfolio
from meridian.registry import service as registry
from meridian.risk import service as risk_svc

router = APIRouter(prefix="/api")
Read = Depends(current_principal)
Cio = Depends(require_cio)


def _row(o: Any, *fields: str) -> dict[str, Any]:
    return jsonable({f: getattr(o, f) for f in fields})


# ------------------------------------------------------------------ auth
class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, s: Session = Depends(get_db)) -> dict[str, Any]:
    key = f"{request.client.host if request.client else '?'}:{body.username}"
    if throttled(key):
        raise HTTPException(429, "too many failed attempts; try again later")
    u = authenticate(s, body.username, body.password)
    if u is None:
        note_failure(key)
        audit.record(s, body.username, "auth.login_failed", "user", body.username, {})
        s.commit()
        raise HTTPException(401, "invalid credentials")
    response.set_cookie(COOKIE, issue_token(u), httponly=True, samesite="strict",
                        secure=get_settings().env == "production", max_age=get_settings().jwt_ttl_minutes * 60)
    audit.record(s, u.username, "auth.login", "user", u.username, {})
    s.commit()
    return {"username": u.username, "role": u.role}


@router.post("/auth/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(COOKIE)
    return {"ok": "logged out"}


@router.get("/auth/me")
def me(p: Principal = Read) -> dict[str, str]:
    return {"username": p.username, "role": p.role}


# ------------------------------------------------------------------ overview
@router.get("/overview")
def overview(request: Request, s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    reg = registry.registry_snapshot(s)
    impl = [w for w in reg if w["implemented"]]
    health: dict[str, int] = {}
    for w in impl:
        health[w["health"]] = health.get(w["health"], 0) + 1
    fresh = list(s.execute(select(DataFreshness).order_by(DataFreshness.dataset)).scalars())
    memos = list(s.execute(select(CommitteeMemo).order_by(desc(CommitteeMemo.created_at)).limit(8)).scalars())
    positions = _positions(s, open_only=True)
    alerts = _alerts(s)
    modes = {i.source for i in s.execute(select(Instrument).where(Instrument.status == "ACTIVE")).scalars()}
    rt = getattr(request.app.state, "runtime", None)
    return {
        "mode": get_settings().market_data_mode,
        "data_sources": sorted(modes),
        "simulated": "SIMULATED" in modes,
        "workers_running": bool(rt),
        "portfolio": portfolio.snapshot(s),
        "case_counts": lc.count_by_state(s),
        "worker_health": health,
        "workers_total": len(reg), "workers_implemented": len(impl),
        "freshness": [_row(f, "dataset", "status", "last_data_ts", "last_checked", "source", "detail") for f in fresh],
        "latest_memos": [_memo_summary(m) for m in memos],
        "open_positions": positions,
        "alerts": alerts[:20],
        "dlq_depth": s.execute(select(func.count()).select_from(DeadLetter).where(DeadLetter.resolved_at.is_(None))).scalar(),
        "generated_at": utcnow().isoformat(),
    }


def _memo_summary(m: CommitteeMemo) -> dict[str, Any]:
    return jsonable({"id": m.id, "case_id": m.case_id, "recommendation": m.recommendation, "score": m.score,
                     "headline": m.memo.get("headline"), "trade": m.memo.get("trade"),
                     "risk": m.memo.get("risk"), "data_source": m.memo.get("data_source"), "created_at": m.created_at})


def _alerts(s: Session) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=3)
    out: list[dict[str, Any]] = []
    for d in s.execute(select(RiskDecision).where(RiskDecision.created_at >= since, RiskDecision.outcome != "ALLOW")
                       .order_by(desc(RiskDecision.created_at)).limit(20)).scalars():
        out.append(jsonable({"kind": "RISK", "severity": "high" if d.outcome == "BLOCK" else "medium", "ts": d.created_at,
                             "title": f"Risk {d.outcome}", "codes": d.reason_codes, "case_id": d.case_id}))
    for a in s.execute(select(PositionAdvisory).where(PositionAdvisory.created_at >= since, PositionAdvisory.action != "HOLD")
                       .order_by(desc(PositionAdvisory.created_at)).limit(20)).scalars():
        out.append(jsonable({"kind": "POSITION", "severity": "high" if a.action == "EXIT" else "medium", "ts": a.created_at,
                             "title": f"{a.action} advisory", "codes": a.reason_codes, "position_id": a.position_id}))
    for f in s.execute(select(DataFreshness).where(DataFreshness.status != "FRESH")).scalars():
        out.append(jsonable({"kind": "DATA", "severity": "medium", "ts": f.last_checked, "title": f"{f.dataset} {f.status}",
                             "codes": [f.status], "detail": f.detail}))
    return sorted(out, key=lambda a: a["ts"] or "", reverse=True)


# ------------------------------------------------------------------ registry
@router.get("/departments")
def departments(s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    reg = registry.registry_snapshot(s)
    out = []
    for d in s.execute(select(Department).order_by(Department.ordinal)).scalars():
        ws = [w for w in reg if w["department_id"] == d.id]
        impl = [w for w in ws if w["implemented"]]
        status = "PLANNED" if not impl else ("FAILING" if any(w["health"] in ("FAILING", "STALE") for w in impl)
                                             else "DEGRADED" if any(w["health"] in ("DEGRADED", "UNKNOWN", "INACTIVE") for w in impl)
                                             else "HEALTHY")
        out.append({"id": d.id, "name": d.name, "description": d.description, "roles": len(ws),
                    "implemented": len(impl), "status": status, "workers": ws})
    return out


@router.get("/workers/{worker_id}/runs")
def worker_runs(worker_id: str, limit: int = 25, s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    rows = s.execute(select(WorkerRun).where(WorkerRun.worker_id == worker_id).order_by(desc(WorkerRun.started_at)).limit(min(limit, 200))).scalars()
    return [_row(r, "id", "worker_version", "config_version", "code_version", "trigger", "status", "attempts",
                 "started_at", "finished_at", "metrics", "output", "input_refs", "error", "correlation_id") for r in rows]


class StatusBody(BaseModel):
    status: Literal["ACTIVE", "INACTIVE"]


@router.post("/workers/{worker_id}/status")
def worker_status(worker_id: str, body: StatusBody, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    try:
        row = registry.set_status(s, worker_id, body.status, p.username)
    except KeyError as e:
        raise HTTPException(404, "unknown worker") from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    s.commit()
    return {"id": row.id, "status": row.status}


# ------------------------------------------------------------------ cases
@router.get("/cases")
def cases(state: str | None = None, limit: int = 100, s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    q = select(ResearchCase).order_by(desc(ResearchCase.created_at)).limit(min(limit, 500))
    if state:
        q = q.where(ResearchCase.state.in_(state.split(",")))
    out = []
    for c in s.execute(q).scalars():
        memo = s.execute(select(CommitteeMemo).where(CommitteeMemo.case_id == c.id).order_by(desc(CommitteeMemo.created_at)).limit(1)).scalar()
        risk = lc.latest_risk(s, c.id)
        out.append(jsonable({"id": c.id, "instrument_id": c.instrument_id, "asset": c.asset, "direction": c.direction,
                             "state": c.state, "data_source": c.data_source, "created_at": c.created_at,
                             "updated_at": c.updated_at, "expires_at": c.expires_at,
                             "strength": c.hypothesis.get("strength"), "entry": c.hypothesis.get("entry_reference"),
                             "stop": c.hypothesis.get("stop"), "target": c.hypothesis.get("target"),
                             "risk_outcome": risk.outcome if risk else None,
                             "recommendation": memo.recommendation if memo else None,
                             "score": memo.score if memo else None, "memo_id": memo.id if memo else None}))
    return out


@router.get("/cases/{case_id}")
def case_detail(case_id: uuid.UUID, s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    c = s.get(ResearchCase, case_id)
    if c is None:
        raise HTTPException(404, "case not found")
    def q(model, order):  # noqa: ANN001, ANN202
        return list(s.execute(select(model).where(model.case_id == c.id).order_by(order)).scalars())
    return jsonable({
        "case": _row(c, "id", "instrument_id", "asset", "direction", "state", "hypothesis", "current_revision",
                     "data_source", "correlation_id", "created_at", "updated_at", "expires_at"),
        "revisions": [_row(r, "revision", "content", "reason", "created_by", "created_at") for r in q(CaseRevision, CaseRevision.revision)],
        "evidence": [_row(e, "id", "kind", "stance", "summary", "data", "producer", "created_at") for e in q(CaseEvidence, CaseEvidence.id)],
        "transitions": [_row(t, "from_state", "to_state", "reason", "actor", "created_at") for t in q(CaseTransition, CaseTransition.id)],
        "risk_decisions": [_row(r, "id", "outcome", "reason_codes", "checks", "sizing", "config_version", "inputs_hash",
                                "code_version", "created_at") for r in q(RiskDecision, RiskDecision.created_at)],
        "memos": [_row(m, "id", "recommendation", "score", "memo", "inputs_hash", "code_version", "created_at")
                  for m in q(CommitteeMemo, CommitteeMemo.created_at)],
        "cio_decisions": [_row(d, "id", "decision", "note", "username", "created_at") for d in q(CioDecision, CioDecision.created_at)],
        "positions": [_position_json(s, p) for p in q(Position, Position.opened_at)],
        "events": [_row(e, "id", "event_type", "producer", "occurred_at", "causation_id")
                   for e in s.execute(select(Event).where(Event.correlation_id == c.correlation_id).order_by(Event.seq)).scalars()],
    })


class DecisionBody(BaseModel):
    decision: Literal["ACCEPT", "DECLINE", "DEFER"]
    note: str = Field("", max_length=2000)


@router.post("/cases/{case_id}/decision")
def cio_decision(case_id: uuid.UUID, body: DecisionBody, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    c = s.get(ResearchCase, case_id)
    if c is None:
        raise HTTPException(404, "case not found")
    memo = s.execute(select(CommitteeMemo).where(CommitteeMemo.case_id == c.id).order_by(desc(CommitteeMemo.created_at)).limit(1)).scalar()
    if memo is None:
        raise HTTPException(409, "case has no committee memo yet")
    if body.decision == "ACCEPT" and c.state != "RECOMMENDED":
        raise HTTPException(409, f"only RECOMMENDED cases can be accepted (case is {c.state}; risk vetoes are final)")
    d = CioDecision(case_id=c.id, memo_id=memo.id, decision=body.decision, note=body.note, username=p.username)
    s.add(d)
    s.flush()
    actor = f"cio:{p.username}"
    if body.decision == "DECLINE" and "CLOSED" in lc.TRANSITIONS[c.state]:
        lc.transition(s, c, "CLOSED", f"CIO declined: {body.note}"[:500], actor)
    publish(s, make_event("cio.decision.recorded", {"case_id": c.id, "decision": body.decision}, producer=actor,
                          correlation_id=c.correlation_id))
    audit.record(s, actor, "cio.decision", "research_case", c.id, {"decision": body.decision, "note": body.note, "memo_id": str(memo.id)})
    s.commit()
    return jsonable({"id": d.id, "decision": d.decision, "case_state": c.state})


@router.get("/memos/{memo_id}/reconstruct")
def reconstruct(memo_id: uuid.UUID, s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    return reconstruct_memo(s, memo_id)


def reconstruct_memo(s: Session, memo_id: uuid.UUID) -> dict[str, Any]:
    """Rebuild a recommendation from its stored inputs and verify each layer matches."""
    from meridian.workers.impl import _bars, _window_hash
    from meridian.workers.indicators import TrendParams, evaluate_trend

    m = s.get(CommitteeMemo, memo_id)
    if m is None:
        raise HTTPException(404, "memo not found")
    dec = s.get(RiskDecision, m.risk_decision_id)
    case = s.get(ResearchCase, m.case_id)
    report: dict[str, Any] = {"memo_id": str(m.id), "case_id": str(case.id), "checks": []}

    def chk(name: str, ok: bool, detail: Any) -> None:
        report["checks"].append({"layer": name, "match": ok, "detail": jsonable(detail)})

    calc = case.hypothesis["calculations"]
    win = calc["window"]
    rows = [r for r in s.execute(select(Candle).where(Candle.instrument_id == win["instrument_id"],
                                                      Candle.granularity == win["granularity"],
                                                      Candle.ts >= datetime.fromisoformat(win["start"]),
                                                      Candle.ts <= datetime.fromisoformat(win["end"]))
                                 .order_by(Candle.ts)).scalars()]
    chk("input_candles", _window_hash(rows) == calc["window_hash"], {"bars": len(rows), "expected_hash": calc["window_hash"]})
    inst = s.get(Instrument, case.instrument_id)
    sig = evaluate_trend(_bars(rows), inst.price_increment or Decimal("0.01"), TrendParams.from_config(calc["params"]))
    chk("trend_signal", sig.direction == case.direction and str(sig.stop) == case.hypothesis["stop"]
        and str(sig.entry) == case.hypothesis["entry_reference"],
        {"recomputed": {"direction": sig.direction, "entry": str(sig.entry), "stop": str(sig.stop)}})
    rr = risk_svc.recompute(dec.inputs)
    chk("risk_inputs_hash", sha256(dec.inputs) == dec.inputs_hash, {"hash": dec.inputs_hash})
    chk("risk_decision", rr.outcome == dec.outcome and rr.reason_codes == dec.reason_codes and jsonable(rr.sizing) == dec.sizing,
        {"recomputed": risk_svc.result_to_json(rr)["outcome"], "stored": dec.outcome})
    chk("committee_inputs_hash", sha256(m.inputs) == m.inputs_hash, {"hash": m.inputs_hash})
    re_memo = decide(m.inputs)
    chk("committee_memo", re_memo == m.memo, {"recommendation": re_memo["recommendation"]})
    report["reconstructed"] = all(c["match"] for c in report["checks"])
    report["versions"] = {"risk": dec.code_version, "committee": m.code_version, "risk_config": dec.config_version}
    return report


# ------------------------------------------------------------------ risk
@router.get("/risk/config")
def risk_config(s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    from meridian.db.models import RiskConfig
    rows = s.execute(select(RiskConfig).order_by(desc(RiskConfig.version)).limit(20)).scalars()
    return {"versions": [_row(r, "version", "config", "status", "created_by", "reason", "created_at") for r in rows]}


class RiskConfigBody(BaseModel):
    config: dict[str, Any]
    reason: str = Field(min_length=5, max_length=2000)
    password: str  # explicit re-authentication for every risk change


@router.post("/risk/config")
def change_risk_config(body: RiskConfigBody, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    u = s.execute(select(User).where(User.username == p.username)).scalar()
    if u is None or not verify_password(body.password, u.password_hash):
        audit.record(s, p.username, "risk.config_change_denied", "risk_config", "-", {"reason": "re-authentication failed"})
        s.commit()
        raise HTTPException(403, "re-authentication failed; risk configuration unchanged")
    try:
        row = risk_svc.change_config(s, body.config, f"cio:{p.username}", body.reason)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.commit()
    return {"version": row.version, "config": row.config}


@router.get("/risk/decisions")
def risk_decisions(limit: int = 50, s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    rows = s.execute(select(RiskDecision).order_by(desc(RiskDecision.created_at)).limit(min(limit, 300))).scalars()
    return [_row(r, "id", "case_id", "outcome", "reason_codes", "checks", "sizing", "config_version", "created_at") for r in rows]


@router.get("/risk/alerts")
def risk_alerts(s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    return _alerts(s)


# ------------------------------------------------------------------ positions & capital
def _position_json(s: Session, p: Position) -> dict[str, Any]:
    adv = s.execute(select(PositionAdvisory).where(PositionAdvisory.position_id == p.id).order_by(desc(PositionAdvisory.id)).limit(1)).scalar()
    return jsonable({**_row(p, "id", "case_id", "instrument_id", "direction", "quantity", "entry_price", "stop_price",
                            "target_price", "contract_size", "status", "opened_at", "closed_at", "exit_price",
                            "realized_pnl", "note"),
                     "risk_usd": portfolio.position_risk(p),
                     "advisory": _row(adv, "action", "reason_codes", "detail", "created_at") if adv else None})


def _positions(s: Session, open_only: bool = False) -> list[dict[str, Any]]:
    q = select(Position).order_by(desc(Position.opened_at)).limit(200)
    if open_only:
        q = q.where(Position.status == "OPEN")
    return [_position_json(s, p) for p in s.execute(q).scalars()]


@router.get("/positions")
def positions(s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    return _positions(s)


@router.get("/positions/{position_id}/advisories")
def advisories(position_id: uuid.UUID, s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    rows = s.execute(select(PositionAdvisory).where(PositionAdvisory.position_id == position_id).order_by(desc(PositionAdvisory.id)).limit(100)).scalars()
    return [_row(a, "action", "reason_codes", "detail", "created_at") for a in rows]


class PositionBody(BaseModel):
    case_id: uuid.UUID | None = None
    instrument_id: str
    direction: Literal["LONG", "SHORT"]
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    target_price: Decimal | None = Field(default=None, gt=0)
    note: str = Field("", max_length=2000)


@router.post("/positions")
def record_position(body: PositionBody, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    """Record a trade the CIO executed outside MERIDIAN, so it can be monitored. Does not execute anything."""
    inst = s.get(Instrument, body.instrument_id)
    if inst is None:
        raise HTTPException(404, "unknown instrument")
    if (body.direction == "LONG" and body.stop_price >= body.entry_price) or (body.direction == "SHORT" and body.stop_price <= body.entry_price):
        raise HTTPException(422, "stop must be below entry for LONG and above entry for SHORT")
    pos = Position(case_id=body.case_id, instrument_id=inst.id, direction=body.direction, quantity=body.quantity,
                   entry_price=body.entry_price, stop_price=body.stop_price, target_price=body.target_price,
                   contract_size=inst.contract_size or Decimal(1), status="OPEN", recorded_by=p.username, note=body.note)
    s.add(pos)
    s.flush()
    publish(s, make_event("position.recorded", {"position_id": pos.id, "status": "OPEN"}, producer=f"cio:{p.username}"))
    audit.record(s, f"cio:{p.username}", "position.recorded", "position", pos.id, jsonable(body.model_dump()))
    if body.case_id:
        c = s.get(ResearchCase, body.case_id)
        if c:
            c.expires_at = None  # acted on: no longer expires
    rereviewed = lc.rereview_after_portfolio_change(s, f"cio:{p.username}", exclude_case=body.case_id)
    eq = portfolio.equity(s)
    risk = portfolio.position_risk(pos)
    s.commit()
    return jsonable({"position": _position_json(s, pos), "rereviewed_cases": rereviewed,
                     "risk_pct_of_equity": (risk / eq * 100).quantize(Decimal("0.01")) if eq else None})


class CloseBody(BaseModel):
    exit_price: Decimal = Field(gt=0)
    note: str = Field("", max_length=2000)


@router.post("/positions/{position_id}/close")
def close_position(position_id: uuid.UUID, body: CloseBody, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    pos = s.get(Position, position_id)
    if pos is None or pos.status != "OPEN":
        raise HTTPException(404, "open position not found")
    pos.exit_price, pos.closed_at, pos.status = body.exit_price, utcnow(), "CLOSED"
    pos.realized_pnl = portfolio.pnl_for(pos, body.exit_price)
    pos.note = (pos.note + "\n" + body.note).strip()
    if pos.case_id:
        c = s.get(ResearchCase, pos.case_id)
        if c and "CLOSED" in lc.TRANSITIONS[c.state]:
            lc.transition(s, c, "CLOSED", "CIO closed the position", f"cio:{p.username}")
    publish(s, make_event("position.recorded", {"position_id": pos.id, "status": "CLOSED"}, producer=f"cio:{p.username}"))
    lc.rereview_after_portfolio_change(s, f"cio:{p.username}")
    audit.record(s, f"cio:{p.username}", "position.closed", "position", pos.id,
                 {"exit_price": str(body.exit_price), "realized_pnl": str(pos.realized_pnl)})
    s.commit()
    return _position_json(s, pos)


class StopBody(BaseModel):
    stop_price: Decimal = Field(gt=0)
    note: str = Field("", max_length=2000)


@router.post("/positions/{position_id}/stop")
def move_stop(position_id: uuid.UUID, body: StopBody, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    pos = s.get(Position, position_id)
    if pos is None or pos.status != "OPEN":
        raise HTTPException(404, "open position not found")
    old = pos.stop_price
    pos.stop_price = body.stop_price
    audit.record(s, f"cio:{p.username}", "position.stop_recorded", "position", pos.id, {"from": str(old), "to": str(body.stop_price), "note": body.note})
    s.commit()
    return _position_json(s, pos)


class CapitalBody(BaseModel):
    kind: Literal["DEPOSIT", "WITHDRAW", "ADJUST"]
    amount: Decimal
    note: str = Field(min_length=3, max_length=2000)


@router.post("/capital")
def capital(body: CapitalBody, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    s.add(CapitalEvent(kind=body.kind, amount=body.amount, note=body.note, username=p.username))
    audit.record(s, f"cio:{p.username}", "capital.event", "capital", body.kind, jsonable(body.model_dump()))
    s.commit()
    return portfolio.snapshot(s)


@router.get("/portfolio")
def portfolio_view(s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    ev = s.execute(select(CapitalEvent).order_by(desc(CapitalEvent.created_at))).scalars()
    return {**portfolio.snapshot(s), "capital_events": [_row(c, "kind", "amount", "note", "username", "created_at") for c in ev]}


# ------------------------------------------------------------------ market data & macro
@router.get("/marketdata/instruments")
def instruments(s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    rows = s.execute(select(Instrument).order_by(Instrument.status, Instrument.asset)).scalars()
    return [_row(i, "id", "asset", "venue", "product_type", "display_name", "price_increment", "size_increment",
                 "min_size", "contract_size", "contract_expiry", "source", "status", "status_detail", "updated_at") for i in rows]


@router.get("/marketdata/candles/{instrument_id}")
def candles(instrument_id: str, limit: int = 200, s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    rows = list(s.execute(select(Candle).where(Candle.instrument_id == instrument_id, Candle.granularity == get_settings().candle_granularity)
                          .order_by(desc(Candle.ts)).limit(min(limit, 1000))).scalars())[::-1]
    return [_row(c, "ts", "open", "high", "low", "close", "volume", "source") for c in rows]


@router.get("/marketdata/freshness")
def freshness(s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    return [_row(f, "dataset", "status", "last_data_ts", "last_checked", "source", "detail")
            for f in s.execute(select(DataFreshness).order_by(DataFreshness.dataset)).scalars()]


@router.get("/macro/events")
def macro_events(s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    fr = s.get(DataFreshness, "macro_calendar")
    rows = s.execute(select(MacroEvent).where(MacroEvent.event_time >= utcnow() - timedelta(days=2))
                     .order_by(MacroEvent.event_time).limit(300)).scalars()
    return {"status": _row(fr, "status", "last_data_ts", "detail", "source") if fr else {"status": "UNAVAILABLE"},
            "events": [_row(e, "id", "title", "country", "impact", "event_time", "time_confidence", "forecast", "previous", "source") for e in rows]}


# ------------------------------------------------------------------ events, audit
@router.get("/events")
def events(limit: int = 100, correlation_id: uuid.UUID | None = None, s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    q = select(Event).order_by(desc(Event.seq)).limit(min(limit, 500))
    if correlation_id:
        q = q.where(Event.correlation_id == correlation_id)
    return [_row(e, "id", "seq", "event_type", "schema_version", "producer", "occurred_at", "expires_at", "correlation_id",
                 "causation_id", "published_at", "payload", "input_refs") for e in s.execute(q).scalars()]


@router.get("/events/dlq")
def dlq(s: Session = Depends(get_db), _: Principal = Read) -> list[dict[str, Any]]:
    return [_row(d, "id", "consumer", "event_id", "event_type", "error", "attempts", "created_at", "resolved_at")
            for d in s.execute(select(DeadLetter).order_by(desc(DeadLetter.created_at)).limit(200)).scalars()]


class ReplayBody(BaseModel):
    consumer: str
    correlation_id: uuid.UUID | None = None
    force: bool = False


@router.post("/events/replay")
def replay(body: ReplayBody, request: Request, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise HTTPException(409, "runtime not running in this process")
    sub = next((x for x in rt.subs if x.group == body.consumer), None)
    if sub is None:
        raise HTTPException(404, "unknown consumer")
    audit.record(s, f"cio:{p.username}", "events.replay", "consumer", body.consumer, jsonable(body.model_dump()))
    s.commit()
    return rt.bus.replay(sub, correlation_id=body.correlation_id, force=body.force)


@router.get("/audit")
def audit_log(limit: int = 200, s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    ok, bad = audit.verify_chain(s)
    rows = s.execute(select(AuditLog).order_by(desc(AuditLog.id)).limit(min(limit, 1000))).scalars()
    return {"chain_valid": ok, "first_bad_id": bad,
            "entries": [_row(a, "id", "ts", "actor", "action", "entity_type", "entity_id", "details", "hash") for a in rows]}


@router.get("/archive")
def archive(s: Session = Depends(get_db), _: Principal = Read) -> dict[str, Any]:
    memos = list(s.execute(select(CommitteeMemo).order_by(desc(CommitteeMemo.created_at)).limit(200)).scalars())
    decisions = {d.memo_id: d for d in s.execute(select(CioDecision)).scalars()}
    return {"memos": [{**_memo_summary(m), "cio_decision": _row(decisions[m.id], "decision", "note", "username", "created_at")
                       if m.id in decisions else None} for m in memos]}


# ------------------------------------------------------------------ demo (simulated only)
@router.post("/demo/run")
def demo_run(request: Request, s: Session = Depends(get_db), p: Principal = Cio) -> dict[str, Any]:
    if get_settings().market_data_mode != "simulated":
        raise HTTPException(409, "demo is only available when MARKET_DATA_MODE=simulated")
    from meridian.demo import run_demo
    audit.record(s, f"cio:{p.username}", "demo.run", "demo", "simulated", {})
    s.commit()
    return run_demo(get_settings())
