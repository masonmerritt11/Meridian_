"""Append-only, hash-chained audit log. Each row's hash covers its content and the previous hash, so any
tampering with history is detectable via verify_chain()."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from meridian.core.util import jsonable, sha256, utcnow
from meridian.db.models import AuditLog

GENESIS = "0" * 64


def record(s: Session, actor: str, action: str, entity_type: str, entity_id: Any, details: dict | None = None) -> AuditLog:
    s.execute(text("SELECT pg_advisory_xact_lock(724001)"))  # serialize chain appends
    prev = s.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)).scalar()
    prev = prev or GENESIS
    ts = utcnow()
    body = {"ts": ts.isoformat(), "actor": actor, "action": action, "entity_type": entity_type,
            "entity_id": str(entity_id), "details": jsonable(details or {}), "prev": prev}
    row = AuditLog(ts=ts, actor=actor, action=action, entity_type=entity_type, entity_id=str(entity_id),
                   details=body["details"], prev_hash=prev, hash=sha256(body))
    s.add(row)
    s.flush()
    return row


def verify_chain(s: Session) -> tuple[bool, int | None]:
    prev = GENESIS
    for row in s.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        body = {"ts": row.ts.isoformat(), "actor": row.actor, "action": row.action, "entity_type": row.entity_type,
                "entity_id": row.entity_id, "details": row.details, "prev": prev}
        if row.prev_hash != prev or sha256(body) != row.hash:
            return False, row.id
        prev = row.hash
    return True, None
