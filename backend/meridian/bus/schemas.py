"""Versioned event schemas. Every event on the bus is an Envelope whose payload validates against
the registered (event_type, schema_version) model. Unknown types/versions are rejected at publish time."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.core.util import new_id, utcnow


class InputRef(BaseModel):
    kind: str  # e.g. "candles", "macro_events", "risk_config", "case"
    ref: str  # identifier/range
    hash: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: uuid.UUID = Field(default_factory=new_id)
    event_type: str
    schema_version: int
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    producer: str
    occurred_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    input_refs: list[InputRef] = Field(default_factory=list)
    payload: dict[str, Any]

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or utcnow()) >= self.expires_at


# ---- payload models -------------------------------------------------------
class CandlesIngestedV1(BaseModel):
    instrument_id: str
    asset: str
    granularity: str
    last_ts: datetime
    count: int
    data_source: Literal["SIMULATED", "COINBASE"]


class SignalDetectedV1(BaseModel):
    instrument_id: str
    asset: str
    direction: Literal["LONG", "SHORT"]
    strategy: str
    strategy_version: str
    as_of: datetime
    entry_reference: str
    stop: str
    target: str
    strength: float
    calculations: dict[str, Any]
    data_source: Literal["SIMULATED", "COINBASE"]


class CaseEventV1(BaseModel):
    case_id: uuid.UUID
    state: str
    note: str = ""


class RiskDecisionV1(BaseModel):
    case_id: uuid.UUID
    risk_decision_id: uuid.UUID
    outcome: Literal["ALLOW", "RESTRICT", "BLOCK"]
    reason_codes: list[str]


class MemoIssuedV1(BaseModel):
    case_id: uuid.UUID
    memo_id: uuid.UUID
    recommendation: Literal["RECOMMEND", "DEFER", "REJECT", "BLOCKED"]


class CioDecisionV1(BaseModel):
    case_id: uuid.UUID
    decision: Literal["ACCEPT", "DECLINE", "DEFER"]


class MacroUpdatedV1(BaseModel):
    source: str
    status: Literal["FRESH", "STALE", "UNAVAILABLE"]
    event_count: int


class PositionAdvisoryV1(BaseModel):
    position_id: uuid.UUID
    action: Literal["HOLD", "EXIT", "REVIEW"]
    reason_codes: list[str]
    changed: bool


class WorkerFailedV1(BaseModel):
    worker_id: str
    run_id: uuid.UUID
    error: str
    escalation_to: str | None


class PositionRecordedV1(BaseModel):
    position_id: uuid.UUID
    status: Literal["OPEN", "CLOSED"]


REGISTRY: dict[tuple[str, int], type[BaseModel]] = {
    ("market.candles.ingested", 1): CandlesIngestedV1,
    ("signal.detected", 1): SignalDetectedV1,
    ("case.researched", 1): CaseEventV1,
    ("case.challenged", 1): CaseEventV1,
    ("case.transitioned", 1): CaseEventV1,
    ("risk.decision.recorded", 1): RiskDecisionV1,
    ("committee.memo.issued", 1): MemoIssuedV1,
    ("cio.decision.recorded", 1): CioDecisionV1,
    ("macro.calendar.updated", 1): MacroUpdatedV1,
    ("position.advisory.issued", 1): PositionAdvisoryV1,
    ("position.recorded", 1): PositionRecordedV1,
    ("worker.failed", 1): WorkerFailedV1,
}

DEFAULT_TTL: dict[str, timedelta] = {
    "market.candles.ingested": timedelta(hours=2),
    "signal.detected": timedelta(hours=6),
}


def make_event(
    event_type: str,
    payload: BaseModel | dict[str, Any],
    *,
    producer: str,
    correlation_id: uuid.UUID | None = None,
    causation: Envelope | None = None,
    input_refs: list[InputRef] | None = None,
    schema_version: int = 1,
    ttl: timedelta | None = None,
) -> Envelope:
    model = REGISTRY.get((event_type, schema_version))
    if model is None:
        raise ValueError(f"unregistered event schema {event_type} v{schema_version}")
    validated = model.model_validate(payload if isinstance(payload, dict) else payload.model_dump())
    now = utcnow()
    ttl = ttl or DEFAULT_TTL.get(event_type)
    return Envelope(
        event_type=event_type,
        schema_version=schema_version,
        correlation_id=correlation_id or (causation.correlation_id if causation else new_id()),
        causation_id=causation.event_id if causation else None,
        producer=producer,
        occurred_at=now,
        expires_at=(now + ttl) if ttl else None,
        input_refs=input_refs or [],
        payload=validated.model_dump(mode="json"),
    )


def validate_envelope(env: Envelope) -> None:
    model = REGISTRY.get((env.event_type, env.schema_version))
    if model is None:
        raise ValueError(f"unregistered event schema {env.event_type} v{env.schema_version}")
    model.model_validate(env.payload)
