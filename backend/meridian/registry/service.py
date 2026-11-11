from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.core import audit
from meridian.core.util import jsonable, utcnow
from meridian.db.models import Department, WorkerConfigVersion, WorkerDefinition, WorkerHeartbeat
from meridian.registry.catalog import DEPARTMENTS, ROLES
from meridian.workers.base import BaseWorker

INFRA_IMPLEMENTED = {"data.event_relay": "Runtime relay loop (no worker class)"}


def seed_registry(s: Session, workers: list[type[BaseWorker]]) -> None:
    """Idempotently upsert departments and all 52 role definitions. Never overwrites operator config/status."""
    for i, (did, name, desc) in enumerate(DEPARTMENTS, 1):
        d = s.get(Department, did) or Department(id=did)
        d.name, d.ordinal, d.description = name, i, desc
        s.add(d)
    s.flush()
    by_id = {w.spec.id: w for w in workers}
    for rid, dept, name, resp, esc in ROLES:
        w = by_id.get(rid)
        row = s.get(WorkerDefinition, rid)
        if row is None:
            row = WorkerDefinition(id=rid, department_id=dept, config_version=1, name=name, responsibility=resp,
                                   status="ACTIVE" if (w or rid in INFRA_IMPLEMENTED) else "PLANNED",
                                   config=jsonable(w.spec.default_config) if w else {})
            s.add(row)
            new_cfg = bool(w)
        else:
            new_cfg = False
        row.name = name
        row.responsibility = resp
        row.escalation_to = esc
        row.implemented = bool(w) or rid in INFRA_IMPLEMENTED
        if row.implemented and row.status == "PLANNED":
            row.status = "ACTIVE"
        if w:
            row.worker_version = w.spec.version
            row.schedule_seconds = w.spec.schedule_seconds
            row.subscribes = list(w.spec.subscribes)
            row.input_schema = w.spec.input_model.model_json_schema()
            row.output_schema = w.spec.output_model.model_json_schema()
        if new_cfg:
            s.flush()
            s.add(WorkerConfigVersion(worker_id=rid, version=1, config=row.config, changed_by="system", reason="initial seed"))
    s.flush()


def set_status(s: Session, worker_id: str, status: str, actor: str) -> WorkerDefinition:
    row = s.get(WorkerDefinition, worker_id)
    if row is None:
        raise KeyError(worker_id)
    if not row.implemented:
        raise ValueError("planned roles have no implementation and cannot be activated")
    if status not in ("ACTIVE", "INACTIVE"):
        raise ValueError("status must be ACTIVE or INACTIVE")
    old = row.status
    row.status = status
    row.updated_at = utcnow()
    audit.record(s, actor, "worker.status_changed", "worker", worker_id, {"from": old, "to": status})
    return row


def update_config(s: Session, worker_id: str, config: dict[str, Any], actor: str, reason: str) -> WorkerDefinition:
    row = s.get(WorkerDefinition, worker_id)
    if row is None:
        raise KeyError(worker_id)
    row.config_version += 1
    row.config = jsonable(config)
    s.add(WorkerConfigVersion(worker_id=worker_id, version=row.config_version, config=row.config, changed_by=actor, reason=reason))
    audit.record(s, actor, "worker.config_changed", "worker", worker_id, {"version": row.config_version, "reason": reason})
    return row


def worker_health(defn: WorkerDefinition, hb: WorkerHeartbeat | None) -> dict[str, Any]:
    """HEALTHY / DEGRADED / FAILING / STALE / INACTIVE / PLANNED / UNKNOWN."""
    if not defn.implemented:
        return {"health": "PLANNED", "last_seen": None}
    if defn.status != "ACTIVE":
        return {"health": "INACTIVE", "last_seen": hb.last_seen if hb else None}
    if hb is None:
        return {"health": "UNKNOWN", "last_seen": None}
    window = timedelta(seconds=max(3 * (defn.schedule_seconds or 60), 180))
    if utcnow() - hb.last_seen > window:
        return {"health": "STALE", "last_seen": hb.last_seen, "detail": hb.detail}
    health = {"HEALTHY": "HEALTHY", "FAILING": "FAILING"}.get(hb.status, "DEGRADED")
    return {"health": health, "last_seen": hb.last_seen, "detail": hb.detail}


def registry_snapshot(s: Session) -> list[dict[str, Any]]:
    hbs = {h.worker_id: h for h in s.execute(select(WorkerHeartbeat)).scalars()}
    out = []
    for d in s.execute(select(WorkerDefinition).order_by(WorkerDefinition.department_id, WorkerDefinition.id)).scalars():
        out.append({
            "id": d.id, "department_id": d.department_id, "name": d.name, "responsibility": d.responsibility,
            "status": d.status, "implemented": d.implemented, "worker_version": d.worker_version,
            "config_version": d.config_version, "config": d.config, "schedule_seconds": d.schedule_seconds,
            "subscribes": d.subscribes, "escalation_to": d.escalation_to,
            "input_schema": d.input_schema, "output_schema": d.output_schema,
            **worker_health(d, hbs.get(d.id)),
        })
    return out
