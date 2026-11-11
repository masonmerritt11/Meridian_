"""Durable event bus: PostgreSQL transactional outbox + Redis Streams delivery.

Write path: producers call `publish(session, envelope)` inside the same DB transaction as their state change.
The relay moves unpublished rows to a Redis stream. If Redis is down, events wait safely in Postgres.

Read path: each logical consumer is a Redis consumer group. A message is processed inside a DB transaction
that also records (consumer, event_id) in event_consumptions — so processing is exactly-once in effect
(idempotent) even though delivery is at-least-once. Failures stay pending and are re-claimed after a backoff;
after `max_attempts` they go to the dead-letter table + DLQ stream and are acknowledged.
"""
from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import redis
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from meridian.bus.schemas import Envelope, validate_envelope
from meridian.core.logging import get_logger
from meridian.core.util import utcnow
from meridian.db.models import DeadLetter, Event, EventConsumption

log = get_logger("meridian.bus")

Handler = Callable[[Session, Envelope], None]


@dataclass
class Subscription:
    group: str
    event_types: set[str]
    handler: Handler
    max_attempts: int = 3
    retry_backoff_ms: int = 5_000
    stats: dict[str, int] = field(default_factory=lambda: {"processed": 0, "skipped": 0, "failed": 0, "dead": 0, "expired": 0})


def publish(s: Session, env: Envelope) -> Event:
    """Write an event to the outbox. Must be called inside the producer's transaction."""
    validate_envelope(env)
    row = Event(
        id=env.event_id, event_type=env.event_type, schema_version=env.schema_version,
        correlation_id=env.correlation_id, causation_id=env.causation_id, producer=env.producer,
        occurred_at=env.occurred_at, expires_at=env.expires_at,
        input_refs=[r.model_dump() for r in env.input_refs], payload=env.payload,
    )
    s.add(row)
    s.flush()
    return row


def envelope_from_row(row: Event) -> Envelope:
    return Envelope(
        event_id=row.id, event_type=row.event_type, schema_version=row.schema_version,
        correlation_id=row.correlation_id, causation_id=row.causation_id, producer=row.producer,
        occurred_at=row.occurred_at, expires_at=row.expires_at, input_refs=row.input_refs, payload=row.payload,
    )


class EventBus:
    def __init__(self, r: redis.Redis, session_factory: Callable[[], Session], prefix: str = "meridian"):
        self.r = r
        self.session_factory = session_factory
        self.stream = f"{prefix}:events"
        self.dlq_stream = f"{prefix}:dlq"
        self.consumer_name = f"{socket.gethostname()}"

    # ------------------------------------------------------------ relay
    def relay_once(self, batch: int = 200) -> int:
        s = self.session_factory()
        try:
            rows = s.execute(
                select(Event).where(Event.published_at.is_(None)).order_by(Event.seq).limit(batch).with_for_update(skip_locked=True)
            ).scalars().all()
            for row in rows:
                env = envelope_from_row(row)
                sid = self.r.xadd(self.stream, {"envelope": env.model_dump_json(), "type": row.event_type},
                                  maxlen=200_000, approximate=True)
                row.published_at = utcnow()
                row.stream_id = sid if isinstance(sid, str) else sid.decode()
            s.commit()
            return len(rows)
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ------------------------------------------------------------ consume
    def ensure_group(self, group: str) -> None:
        try:
            self.r.xgroup_create(self.stream, group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def consume_once(self, sub: Subscription, block_ms: int = 1000, count: int = 50) -> int:
        self.ensure_group(sub.group)
        handled = 0
        # 1) retry messages that have been pending longer than the backoff
        _, claimed, *_ = self.r.xautoclaim(self.stream, sub.group, self.consumer_name,
                                           min_idle_time=sub.retry_backoff_ms, start_id="0-0", count=count)
        for msg_id, fields in claimed:
            if fields:
                self._handle_message(sub, msg_id, fields)
                handled += 1
        # 2) new messages
        resp = self.r.xreadgroup(sub.group, self.consumer_name, {self.stream: ">"}, count=count, block=block_ms)
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                self._handle_message(sub, msg_id, fields)
                handled += 1
        return handled

    def _handle_message(self, sub: Subscription, msg_id: str, fields: dict[str, Any]) -> None:
        etype = fields.get("type")
        if etype not in sub.event_types:
            self.r.xack(self.stream, sub.group, msg_id)
            return
        env = Envelope.model_validate_json(fields["envelope"])
        outcome = self.process(sub, env)
        if outcome != "RETRY":
            self.r.xack(self.stream, sub.group, msg_id)

    def process(self, sub: Subscription, env: Envelope, force: bool = False) -> str:
        """Process one envelope for one subscription. Returns DONE | SKIPPED | EXPIRED | RETRY | DEAD."""
        s = self.session_factory()
        try:
            existing = s.get(EventConsumption, (sub.group, env.event_id))
            if existing and existing.status in ("DONE", "DEAD", "EXPIRED") and not force:
                sub.stats["skipped"] += 1
                return "SKIPPED"
            if env.is_expired():
                self._upsert_consumption(s, sub.group, env, "EXPIRED", None)
                s.commit()
                sub.stats["expired"] += 1
                log.info("event expired", extra={"consumer": sub.group, "event_id": str(env.event_id), "event_type": env.event_type})
                return "EXPIRED"
            sub.handler(s, env)
            self._upsert_consumption(s, sub.group, env, "DONE", None)
            s.commit()
            sub.stats["processed"] += 1
            return "DONE"
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            return self._record_failure(sub, env, exc)
        finally:
            s.close()

    def _record_failure(self, sub: Subscription, env: Envelope, exc: Exception) -> str:
        s = self.session_factory()
        try:
            row = s.get(EventConsumption, (sub.group, env.event_id))
            attempts = (row.attempts if row else 0) + 1
            dead = attempts >= sub.max_attempts
            self._upsert_consumption(s, sub.group, env, "DEAD" if dead else "FAILED", repr(exc), attempts)
            if dead:
                s.add(DeadLetter(consumer=sub.group, event_id=env.event_id, event_type=env.event_type,
                                 error=repr(exc), attempts=attempts))
            s.commit()
        finally:
            s.close()
        log.warning("event handler failed", extra={"consumer": sub.group, "event_id": str(env.event_id),
                                                   "event_type": env.event_type, "error": repr(exc), "dead": dead})
        if dead:
            self.r.xadd(self.dlq_stream, {"envelope": env.model_dump_json(), "consumer": sub.group, "error": repr(exc)})
            sub.stats["dead"] += 1
            return "DEAD"
        sub.stats["failed"] += 1
        return "RETRY"

    @staticmethod
    def _upsert_consumption(s: Session, group: str, env: Envelope, status: str, err: str | None, attempts: int | None = None) -> None:
        stmt = pg_insert(EventConsumption).values(consumer=group, event_id=env.event_id, status=status,
                                                  attempts=attempts or 1, last_error=err, updated_at=utcnow())
        stmt = stmt.on_conflict_do_update(index_elements=["consumer", "event_id"],
                                          set_={"status": status, "last_error": err, "updated_at": utcnow(),
                                                **({"attempts": attempts} if attempts else {})})
        s.execute(stmt)

    # ------------------------------------------------------------ replay
    def replay(self, sub: Subscription, *, since: datetime | None = None, correlation_id: Any = None,
               event_types: Iterable[str] | None = None, force: bool = False) -> dict[str, int]:
        """Re-run stored events through a consumer directly from the durable log (Postgres).
        Without force, already-processed events are skipped (idempotency). With force, they are reprocessed."""
        types = set(event_types) if event_types else sub.event_types
        s = self.session_factory()
        try:
            q = select(Event).where(Event.event_type.in_(types)).order_by(Event.seq)
            if since:
                q = q.where(Event.occurred_at >= since)
            if correlation_id:
                q = q.where(Event.correlation_id == correlation_id)
            envs = [envelope_from_row(r) for r in s.execute(q).scalars()]
            if force:
                s.execute(delete(EventConsumption).where(EventConsumption.consumer == sub.group,
                                                         EventConsumption.event_id.in_([e.event_id for e in envs])))
                s.commit()
        finally:
            s.close()
        counts: dict[str, int] = {}
        for env in envs:
            # replay ignores expiry: the point is to reconstruct what happened
            out = self.process(sub, env.model_copy(update={"expires_at": None}))
            counts[out] = counts.get(out, 0) + 1
        return counts

    def dlq_depth(self) -> int:
        try:
            return int(self.r.xlen(self.dlq_stream))
        except redis.ResponseError:
            return 0

    def pending(self, group: str) -> int:
        try:
            info = self.r.xpending(self.stream, group)
            return int(info.get("pending", 0)) if isinstance(info, dict) else 0
        except redis.ResponseError:
            return 0


def envelope_dict(env: Envelope) -> dict[str, Any]:
    return json.loads(env.model_dump_json())
