"""SQLAlchemy models. The Alembic migration in migrations/versions is the source of truth for DDL;
these models must stay in sync with it (tests/integration/test_schema.py checks this)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Identity,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NUM = Numeric(28, 10)


class Base(DeclarativeBase):
    pass


def _uuid() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts(nullable: bool = False, default: bool = True) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=nullable, server_default=func.now() if default else None)


# ---------------------------------------------------------------- registry
class Department(Base):
    __tablename__ = "departments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")


class WorkerDefinition(Base):
    __tablename__ = "worker_definitions"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    name: Mapped[str] = mapped_column(String(200))
    responsibility: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # ACTIVE | INACTIVE | PLANNED
    implemented: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_version: Mapped[str] = mapped_column(String(32), default="0")
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    schedule_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subscribes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    escalation_to: Mapped[str | None] = mapped_column(String(96), nullable=True)
    updated_at: Mapped[datetime] = _ts()


class WorkerConfigVersion(Base):
    __tablename__ = "worker_config_versions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("worker_definitions.id"))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    changed_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(ForeignKey("worker_definitions.id"), primary_key=True)
    last_seen: Mapped[datetime] = _ts()
    status: Mapped[str] = mapped_column(String(16))  # HEALTHY | DEGRADED | FAILING
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class WorkerRun(Base):
    __tablename__ = "worker_runs"
    id: Mapped[uuid.UUID] = _uuid()
    worker_id: Mapped[str] = mapped_column(String(96), index=True)
    worker_version: Mapped[str] = mapped_column(String(32))
    config_version: Mapped[int] = mapped_column(Integer)
    code_version: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(32))  # SCHEDULE | EVENT | MANUAL
    trigger_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    status: Mapped[str] = mapped_column(String(16))  # RUNNING | SUCCEEDED | FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    input_refs: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------- events
class Event(Base):
    __tablename__ = "events"
    id: Mapped[uuid.UUID] = _uuid()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    event_type: Mapped[str] = mapped_column(String(96), index=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    producer: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_refs: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stream_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EventConsumption(Base):
    __tablename__ = "event_consumptions"
    consumer: Mapped[str] = mapped_column(String(96), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))  # DONE | FAILED | DEAD | EXPIRED
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = _ts()


class DeadLetter(Base):
    __tablename__ = "dead_letters"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    consumer: Mapped[str] = mapped_column(String(96))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(96))
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _ts()
    resolved_at: Mapped[datetime | None] = _ts(nullable=True, default=False)


# ---------------------------------------------------------------- market data
class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)  # GOLD SILVER OIL BTC ETH XRP
    venue: Mapped[str] = mapped_column(String(32))
    product_type: Mapped[str] = mapped_column(String(16))  # FUTURE | SPOT
    display_name: Mapped[str] = mapped_column(String(200))
    quote_currency: Mapped[str] = mapped_column(String(16))
    price_increment: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    size_increment: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    min_size: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    contract_size: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    contract_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(64))  # SIMULATED | COINBASE
    status: Mapped[str] = mapped_column(String(16))  # ACTIVE | UNAVAILABLE | EXPIRED
    status_detail: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = _ts()


class Candle(Base):
    __tablename__ = "candles"
    instrument_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    granularity: Mapped[str] = mapped_column(String(24), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(NUM)
    high: Mapped[Decimal] = mapped_column(NUM)
    low: Mapped[Decimal] = mapped_column(NUM)
    close: Mapped[Decimal] = mapped_column(NUM)
    volume: Mapped[Decimal] = mapped_column(NUM)
    source: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = _ts()


class BookSnapshot(Base):
    __tablename__ = "book_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[str] = mapped_column(String(96), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    best_bid: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    best_ask: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    spread_bps: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    bid_depth: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    ask_depth: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    source: Mapped[str] = mapped_column(String(64))


class DataFreshness(Base):
    __tablename__ = "data_freshness"
    dataset: Mapped[str] = mapped_column(String(160), primary_key=True)
    last_data_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked: Mapped[datetime] = _ts()
    status: Mapped[str] = mapped_column(String(16))  # FRESH | STALE | UNAVAILABLE
    source: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")


class MacroEvent(Base):
    __tablename__ = "macro_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    country: Mapped[str] = mapped_column(String(16))
    impact: Mapped[str] = mapped_column(String(16))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    time_confidence: Mapped[str] = mapped_column(String(16))  # EXACT | DATE_ONLY | UNKNOWN
    forecast: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = _ts()


# ---------------------------------------------------------------- research cases
class ResearchCase(Base):
    __tablename__ = "research_cases"
    id: Mapped[uuid.UUID] = _uuid()
    instrument_id: Mapped[str] = mapped_column(String(96), index=True)
    asset: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))  # LONG | SHORT
    state: Mapped[str] = mapped_column(String(24), index=True)
    hypothesis: Mapped[dict[str, Any]] = mapped_column(JSONB)  # original, immutable (DB trigger)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    data_source: Mapped[str] = mapped_column(String(16))
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseRevision(Base):
    __tablename__ = "case_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_cases.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts()


class CaseEvidence(Base):
    __tablename__ = "case_evidence"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # SIGNAL MACRO CHALLENGE VOLATILITY
    stance: Mapped[str] = mapped_column(String(16))  # SUPPORTS | OPPOSES | NEUTRAL | UNAVAILABLE
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    producer: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts()


class CaseTransition(Base):
    __tablename__ = "case_transitions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_cases.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts()


# ---------------------------------------------------------------- risk / committee / CIO
class RiskConfig(Base):
    __tablename__ = "risk_configs"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))  # ACTIVE | SUPERSEDED
    created_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class RiskDecision(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[uuid.UUID] = _uuid()
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("research_cases.id"), nullable=True, index=True)
    config_version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16))  # ALLOW | RESTRICT | BLOCK
    reason_codes: Mapped[list[str]] = mapped_column(JSONB)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    sizing: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class CommitteeMemo(Base):
    __tablename__ = "committee_memos"
    id: Mapped[uuid.UUID] = _uuid()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_cases.id"), index=True)
    risk_decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("risk_decisions.id"))
    recommendation: Mapped[str] = mapped_column(String(16))  # RECOMMEND | DEFER | REJECT | BLOCKED
    score: Mapped[Decimal] = mapped_column(NUM)
    memo: Mapped[dict[str, Any]] = mapped_column(JSONB)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class CioDecision(Base):
    __tablename__ = "cio_decisions"
    id: Mapped[uuid.UUID] = _uuid()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_cases.id"), index=True)
    memo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("committee_memos.id"), nullable=True)
    decision: Mapped[str] = mapped_column(String(16))  # ACCEPT | DECLINE | DEFER
    note: Mapped[str] = mapped_column(Text, default="")
    username: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts()


# ---------------------------------------------------------------- CIO-recorded positions (executed outside MERIDIAN)
class Position(Base):
    __tablename__ = "positions"
    id: Mapped[uuid.UUID] = _uuid()
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("research_cases.id"), nullable=True)
    instrument_id: Mapped[str] = mapped_column(String(96))
    direction: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(NUM)
    entry_price: Mapped[Decimal] = mapped_column(NUM)
    stop_price: Mapped[Decimal] = mapped_column(NUM)
    target_price: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    contract_size: Mapped[Decimal] = mapped_column(NUM)
    status: Mapped[str] = mapped_column(String(16))  # OPEN | CLOSED
    opened_at: Mapped[datetime] = _ts()
    closed_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    exit_price: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text, default="")


class PositionAdvisory(Base):
    __tablename__ = "position_advisories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))  # HOLD | EXIT | REVIEW
    reason_codes: Mapped[list[str]] = mapped_column(JSONB)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts()


class CapitalEvent(Base):
    __tablename__ = "capital_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # DEPOSIT | WITHDRAW | ADJUST
    amount: Mapped[Decimal] = mapped_column(NUM)
    note: Mapped[str] = mapped_column(Text, default="")
    username: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts()


# ---------------------------------------------------------------- security / audit
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16))  # CIO | VIEWER
    created_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _ts()
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(96), index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
