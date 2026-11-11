"""Reusable worker framework.

Every worker declares a WorkerSpec (id, department, responsibility, version, input/output schemas, schedule,
subscriptions, escalation path, retry policy, default config). The executor wraps every execution with:
run records, structured logs, heartbeats, retries with backoff, metrics, failure escalation and audit.
Workers are independently testable: instantiate with a WorkerContext and call run()/handle() with a Session.
"""
from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from meridian.bus.bus import publish
from meridian.bus.schemas import Envelope, InputRef, make_event
from meridian.core import audit
from meridian.core.config import Settings, code_version
from meridian.core.logging import get_logger
from meridian.core.util import jsonable, new_id, utcnow
from meridian.db.models import WorkerDefinition, WorkerHeartbeat, WorkerRun


class EmptyInput(BaseModel):
    pass


class WorkerOutput(BaseModel):
    summary: str = ""
    metrics: dict[str, float] = {}


@dataclass(frozen=True)
class WorkerSpec:
    id: str
    department: str
    name: str
    responsibility: str
    version: str
    input_model: type[BaseModel] = EmptyInput
    output_model: type[BaseModel] = WorkerOutput
    schedule_seconds: int | None = None
    subscribes: tuple[str, ...] = ()
    escalation_to: str = "exec.chief_of_staff"
    max_retries: int = 3
    retry_backoff_s: float = 1.0
    default_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunContext:
    run_id: uuid.UUID
    correlation_id: uuid.UUID
    config: dict[str, Any]
    config_version: int
    trigger_event: Envelope | None = None
    input_refs: list[InputRef] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def ref(self, kind: str, ref: str, hash_: str | None = None) -> InputRef:
        r = InputRef(kind=kind, ref=ref, hash=hash_)
        self.input_refs.append(r)
        return r


@dataclass
class WorkerContext:
    settings: Settings
    session_factory: Callable[[], Session]
    services: dict[str, Any] = field(default_factory=dict)  # e.g. "market": MarketDataProvider, "macro": MacroProvider


class BaseWorker:
    spec: ClassVar[WorkerSpec]

    def __init__(self, ctx: WorkerContext):
        self.ctx = ctx
        self.log = get_logger(f"meridian.worker.{self.spec.id}")

    # Override one or both
    def run(self, s: Session, rc: RunContext) -> BaseModel:  # scheduled
        raise NotImplementedError

    def handle(self, s: Session, env: Envelope, rc: RunContext) -> BaseModel:  # event-driven
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers
    def emit(self, s: Session, event_type: str, payload: BaseModel | dict, rc: RunContext,
             ttl: timedelta | None = None, new_trace: bool = False) -> Envelope:
        env = make_event(event_type, payload, producer=f"{self.spec.id}@{self.spec.version}",
                         correlation_id=new_id() if new_trace else rc.correlation_id,
                         causation=None if new_trace else rc.trigger_event,
                         input_refs=list(rc.input_refs), ttl=ttl)
        publish(s, env)
        return env


# ---------------------------------------------------------------------- executor
def _definition(s: Session, worker_id: str) -> WorkerDefinition | None:
    return s.get(WorkerDefinition, worker_id)


def _heartbeat(s: Session, worker_id: str, status: str, detail: dict[str, Any]) -> None:
    stmt = pg_insert(WorkerHeartbeat).values(worker_id=worker_id, last_seen=utcnow(), status=status, detail=jsonable(detail))
    s.execute(stmt.on_conflict_do_update(index_elements=["worker_id"],
                                         set_={"last_seen": utcnow(), "status": status, "detail": jsonable(detail)}))


def heartbeat(session_factory: Callable[[], Session], worker_id: str, status: str = "HEALTHY", detail: dict | None = None) -> None:
    s = session_factory()
    try:
        if _definition(s, worker_id) is not None:
            _heartbeat(s, worker_id, status, detail or {})
            s.commit()
    finally:
        s.close()


def is_active(session_factory: Callable[[], Session], worker_id: str) -> bool:
    s = session_factory()
    try:
        d = _definition(s, worker_id)
        return bool(d and d.status == "ACTIVE")
    finally:
        s.close()


def _load_config(s: Session, worker: BaseWorker) -> tuple[dict[str, Any], int]:
    d = _definition(s, worker.spec.id)
    if d is None:
        return dict(worker.spec.default_config), 0
    return {**worker.spec.default_config, **(d.config or {})}, d.config_version


def execute(worker: BaseWorker, *, trigger: str = "SCHEDULE", env: Envelope | None = None,
            session: Session | None = None) -> BaseModel:
    """Run a worker once with full bookkeeping.

    For scheduled runs (session=None) the executor owns transactions and retries.
    For event runs the bus owns the transaction (session passed in) and retries; we still record the run.
    """
    sf = worker.ctx.session_factory
    spec = worker.spec
    run_id = new_id()
    corr = env.correlation_id if env else new_id()

    meta = sf()
    try:
        cfg, cfg_ver = _load_config(meta, worker)
        meta.add(WorkerRun(id=run_id, worker_id=spec.id, worker_version=spec.version, config_version=cfg_ver,
                           code_version=code_version(), trigger=trigger, trigger_event_id=env.event_id if env else None,
                           correlation_id=corr, status="RUNNING", attempts=0))
        meta.commit()
    finally:
        meta.close()

    attempts_allowed = 1 if session is not None else spec.max_retries
    last_exc: Exception | None = None
    t0 = time.perf_counter()
    for attempt in range(1, attempts_allowed + 1):
        rc = RunContext(run_id=run_id, correlation_id=corr, config=cfg, config_version=cfg_ver, trigger_event=env)
        own = session is None
        s = sf() if own else session
        try:
            out = worker.handle(s, env, rc) if env is not None else worker.run(s, rc)
            out = spec.output_model.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
            if own:
                s.commit()
            _finish(worker, run_id, "SUCCEEDED", attempt, rc, out, None, t0)
            return out
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if own:
                s.rollback()
            worker.log.warning("worker attempt failed", extra={"worker_id": spec.id, "run_id": str(run_id),
                                                               "attempt": attempt, "error": repr(exc)})
            if session is not None:
                break
            if attempt < attempts_allowed:
                time.sleep(spec.retry_backoff_s * (2 ** (attempt - 1)))
        finally:
            if own:
                s.close()

    err = "".join(traceback.format_exception(last_exc)) if last_exc else "unknown"
    _finish(worker, run_id, "FAILED", attempts_allowed, None, None, err, t0)
    _escalate(worker, run_id, corr, repr(last_exc))
    if session is not None:
        raise last_exc  # type: ignore[misc]  # let the bus retry / dead-letter
    raise last_exc  # type: ignore[misc]


def _finish(worker: BaseWorker, run_id: uuid.UUID, status: str, attempts: int, rc: RunContext | None,
            out: BaseModel | None, err: str | None, t0: float) -> None:
    sf = worker.ctx.session_factory
    s = sf()
    try:
        run = s.get(WorkerRun, run_id)
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        metrics = {"duration_ms": duration_ms, "attempts": attempts, **((rc.metrics if rc else {}))}
        if run:
            run.status = status
            run.attempts = attempts
            run.finished_at = utcnow()
            run.metrics = jsonable(metrics)
            run.output = jsonable(out.model_dump()) if out else {}
            run.input_refs = [r.model_dump() for r in rc.input_refs] if rc else []
            run.error = err
        _heartbeat(s, worker.spec.id, "HEALTHY" if status == "SUCCEEDED" else "FAILING",
                   {"last_run_id": str(run_id), "last_status": status, "duration_ms": duration_ms})
        s.commit()
    finally:
        s.close()
    worker.log.info("worker run finished", extra={"worker_id": worker.spec.id, "run_id": str(run_id),
                                                  "status": status, "attempts": attempts})


def _escalate(worker: BaseWorker, run_id: uuid.UUID, corr: uuid.UUID, error: str) -> None:
    s = worker.ctx.session_factory()
    try:
        env = make_event("worker.failed", {"worker_id": worker.spec.id, "run_id": run_id, "error": error[:2000],
                                           "escalation_to": worker.spec.escalation_to},
                         producer=f"{worker.spec.id}@{worker.spec.version}", correlation_id=corr)
        publish(s, env)
        audit.record(s, f"worker:{worker.spec.id}", "worker.failed", "worker_run", run_id,
                     {"error": error[:2000], "escalation_to": worker.spec.escalation_to})
        s.commit()
    finally:
        s.close()
