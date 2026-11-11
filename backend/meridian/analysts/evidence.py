"""Attach the latest analyst assessments for a case's asset as evidence (for, against or neutral)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from meridian.analysts.base import latest_by_worker
from meridian.cases import lifecycle as lc
from meridian.core.util import utcnow
from meridian.db.models import ResearchCase

# Consensus/summary views are shown on their own pages but not counted as separate evidence (no double counting).
NON_EVIDENCE = {"trend.portfolio_lead", "stat_arb.lead", "exec.chief_macro_officer", "stat_arb.basis", "vol.realized",
                "liquidity.order_book"}
KIND = {"trend": "TREND", "stat_arb": "STAT_ARB", "macro": "MACRO", "vol": "VOLATILITY", "liquidity": "LIQUIDITY",
        "risk": "RISK", "portfolio": "PORTFOLIO"}
THRESHOLD = 0.2


def stance_for(direction: str, score: float | None, override: str | None) -> str:
    if override in ("SUPPORTS", "OPPOSES"):
        return override
    if score is None:
        return "NEUTRAL"
    s = score if direction == "LONG" else -score
    return "SUPPORTS" if s >= THRESHOLD else "OPPOSES" if s <= -THRESHOLD else "NEUTRAL"


def attach_assessments(s: Session, case: ResearchCase, actor: str) -> int:
    n = 0
    for r in latest_by_worker(s, case.asset, timedelta(hours=26)):
        if r.worker_id in NON_EVIDENCE:
            continue
        llm = r.worker_id.startswith("macro.") and r.worker_id != "macro.regime"
        if not llm and utcnow() - r.created_at > timedelta(hours=4):
            continue
        if r.label == "UNAVAILABLE":
            continue
        score = float(r.score) if r.score is not None else None
        st = stance_for(case.direction, score, (r.data or {}).get("stance"))
        kind = KIND.get(r.worker_id.split(".")[0], "ANALYST")
        lc.add_evidence(s, case, kind, st, f"{r.label.replace('_', ' ').lower()}: {r.summary}"[:600],
                        {"check": r.worker_id, "label": r.label, "score": score,
                         "confidence": float(r.confidence) if r.confidence is not None else None,
                         "assessment_id": r.id, "as_of": r.created_at.isoformat()}, r.worker_id)
        n += 1
    return n

