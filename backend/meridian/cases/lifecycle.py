"""Research-case state machine. All transitions go through `transition()`, which validates the edge,
records the transition, emits an event, and writes audit. The DB trigger independently enforces the risk veto."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from meridian.bus.bus import publish
from meridian.bus.schemas import Envelope, make_event
from meridian.core import audit
from meridian.core.util import jsonable, utcnow
from meridian.db.models import CaseEvidence, CaseRevision, CaseTransition, Position, ResearchCase, RiskDecision

TERMINAL = {"EXPIRED", "INVALIDATED", "CLOSED"}
OPEN_STATES = {"DETECTED", "RESEARCHING", "CHALLENGED", "RISK_REVIEW", "COMMITTEE_REVIEW", "RECOMMENDED", "DEFERRED", "BLOCKED"}

TRANSITIONS: dict[str, set[str]] = {
    "DETECTED": {"RESEARCHING", "INVALIDATED", "EXPIRED"},
    "RESEARCHING": {"CHALLENGED", "INVALIDATED", "EXPIRED"},
    "CHALLENGED": {"RISK_REVIEW", "RESEARCHING", "INVALIDATED", "EXPIRED"},
    "RISK_REVIEW": {"COMMITTEE_REVIEW", "RESEARCHING", "INVALIDATED", "EXPIRED"},
    "COMMITTEE_REVIEW": {"RECOMMENDED", "DEFERRED", "REJECTED", "BLOCKED", "RESEARCHING"},
    "RECOMMENDED": {"CLOSED", "EXPIRED", "INVALIDATED", "RESEARCHING"},
    "DEFERRED": {"RESEARCHING", "EXPIRED", "CLOSED", "INVALIDATED"},
    "REJECTED": {"CLOSED"},
    "BLOCKED": {"CLOSED", "RESEARCHING", "EXPIRED"},
    "EXPIRED": set(),
    "INVALIDATED": set(),
    "CLOSED": set(),
}


class InvalidTransition(Exception):
    pass


class RiskVeto(Exception):
    pass


def transition(s: Session, case: ResearchCase, to_state: str, reason: str, actor: str,
               causation: Envelope | None = None) -> None:
    frm = case.state
    if to_state not in TRANSITIONS.get(frm, set()):
        raise InvalidTransition(f"{frm} -> {to_state} not allowed")
    if to_state == "RECOMMENDED":
        latest = latest_risk(s, case.id)
        if latest is None or latest.outcome == "BLOCK":
            raise RiskVeto(f"case {case.id}: risk outcome {latest.outcome if latest else 'MISSING'} forbids RECOMMENDED")
    case.state = to_state
    case.updated_at = utcnow()
    s.add(CaseTransition(case_id=case.id, from_state=frm, to_state=to_state, reason=reason, actor=actor))
    s.flush()
    publish(s, make_event("case.transitioned", {"case_id": case.id, "state": to_state, "note": reason},
                          producer=actor, correlation_id=case.correlation_id, causation=causation))
    audit.record(s, actor, "case.transition", "research_case", case.id, {"from": frm, "to": to_state, "reason": reason})


def create_case(s: Session, *, instrument_id: str, asset: str, direction: str, hypothesis: dict[str, Any],
                data_source: str, actor: str, correlation_id: Any) -> ResearchCase:
    case = ResearchCase(instrument_id=instrument_id, asset=asset, direction=direction, state="DETECTED",
                        hypothesis=jsonable(hypothesis), current_revision=1, data_source=data_source,
                        correlation_id=correlation_id)
    s.add(case)
    s.flush()
    s.add(CaseRevision(case_id=case.id, revision=1, content=case.hypothesis, reason="original hypothesis", created_by=actor))
    s.add(CaseTransition(case_id=case.id, from_state=None, to_state="DETECTED", reason="signal detected", actor=actor))
    audit.record(s, actor, "case.created", "research_case", case.id, {"instrument_id": instrument_id, "direction": direction})
    return case


def revise(s: Session, case: ResearchCase, content: dict[str, Any], reason: str, actor: str) -> None:
    case.current_revision += 1
    s.add(CaseRevision(case_id=case.id, revision=case.current_revision, content=jsonable(content), reason=reason, created_by=actor))


def add_evidence(s: Session, case: ResearchCase, kind: str, stance: str, summary: str, data: dict[str, Any], producer: str) -> CaseEvidence:
    ev = CaseEvidence(case_id=case.id, kind=kind, stance=stance, summary=summary, data=jsonable(data), producer=producer)
    s.add(ev)
    s.flush()
    return ev


def latest_risk(s: Session, case_id: Any) -> RiskDecision | None:
    return s.execute(select(RiskDecision).where(RiskDecision.case_id == case_id)
                     .order_by(RiskDecision.created_at.desc(), RiskDecision.id.desc()).limit(1)).scalar()


def latest_revision_content(s: Session, case: ResearchCase) -> dict[str, Any]:
    row = s.execute(select(CaseRevision).where(CaseRevision.case_id == case.id)
                    .order_by(CaseRevision.revision.desc()).limit(1)).scalar()
    return row.content if row else case.hypothesis


def open_case_for(s: Session, instrument_id: str) -> ResearchCase | None:
    return s.execute(select(ResearchCase).where(ResearchCase.instrument_id == instrument_id,
                                                ResearchCase.state.in_(OPEN_STATES))
                     .order_by(ResearchCase.created_at.desc()).limit(1)).scalar()


def count_by_state(s: Session) -> dict[str, int]:
    return dict(s.execute(select(ResearchCase.state, func.count()).group_by(ResearchCase.state)).all())


def rereview_after_portfolio_change(s: Session, actor: str, exclude_case: Any = None) -> list[str]:
    """Portfolio changed (position recorded/closed): send RECOMMENDED and BLOCKED cases back to research so
    risk re-evaluates them against the new open risk. This is the 'new evidence' back-transition."""
    moved = []
    for c in s.execute(select(ResearchCase).where(ResearchCase.state.in_(("RECOMMENDED", "BLOCKED")))).scalars():
        if exclude_case is not None and c.id == exclude_case:
            continue
        if s.execute(select(func.count()).select_from(Position).where(Position.case_id == c.id)).scalar():
            continue  # already acted on
        transition(s, c, "RESEARCHING", "portfolio changed; re-review against current open risk", actor)
        c.expires_at = None
        publish(s, make_event("case.researched", {"case_id": c.id, "state": "RESEARCHING", "note": "re-review"},
                              producer=actor, correlation_id=c.correlation_id))
        moved.append(str(c.id))
    return moved
