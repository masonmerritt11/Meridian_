"""Investment committee — deterministic. It combines evidence, preserves dissent and uncertainty, and
can never produce RECOMMEND when risk is BLOCK. No language model, and therefore no invented numbers:
every number in a memo is copied from the signal calculation or the risk engine's sizing."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

COMMITTEE_VERSION = "1.0.0"
MIN_STRENGTH = 0.25


def decide(inputs: dict[str, Any]) -> dict[str, Any]:
    """inputs: {case, evidence[], risk{outcome, reason_codes, checks, sizing}, data_source, macro_events[]}"""
    case, risk, evidence = inputs["case"], inputs["risk"], inputs["evidence"]
    hyp = case["hypothesis"]
    strength = float(hyp.get("strength", 0))
    supports = [e for e in evidence if e["stance"] == "SUPPORTS"]
    opposes = [e for e in evidence if e["stance"] == "OPPOSES"]
    unavailable = [e for e in evidence if e["stance"] == "UNAVAILABLE"]
    restrictions = [c for c in risk["checks"] if c["outcome"] == "RESTRICT"]

    score = Decimal(str(round(strength * 50 + 10 * len(supports) - 15 * len(opposes) - 5 * len(restrictions), 4)))

    if risk["outcome"] == "BLOCK":
        rec, why = "BLOCKED", "Risk engine BLOCK is final and cannot be overridden by the committee."
    elif strength < MIN_STRENGTH:
        rec, why = "REJECT", f"Signal strength {strength} below committee minimum {MIN_STRENGTH}."
    elif len(opposes) >= 2 or len(opposes) > len(supports):
        rec, why = "DEFER", "Opposing evidence outweighs supporting evidence; waiting for confirmation."
    else:
        rec, why = "RECOMMEND", "Signal confirmed; risk permits a sized position."

    sizing = risk.get("sizing", {})
    uncertainty = []
    if inputs.get("data_source") == "SIMULATED":
        uncertainty.append("Built from SIMULATED data. This is a demonstration, not market information.")
    for c in restrictions:
        uncertainty.append(f"{c['code']}: {c['detail']}")
    for e in unavailable:
        uncertainty.append(f"{e['kind']} unavailable: {e['summary']}")

    entry, stop, target = hyp.get("entry_reference"), hyp.get("stop"), hyp.get("target")
    rr = None
    if entry and stop and target:
        e_, s_, t_ = Decimal(entry), Decimal(stop), Decimal(target)
        rr = str(((t_ - e_).copy_abs() / (e_ - s_).copy_abs()).quantize(Decimal("0.01"))) if e_ != s_ else None

    action = None
    if rec == "RECOMMEND":
        action = (f"{case['direction']} {sizing.get('suggested_quantity')} x {case['instrument_id']} near {entry}; "
                  f"stop {stop}; target {target}. Max loss at stop ${sizing.get('max_loss_usd')}.")

    return {
        "committee_version": COMMITTEE_VERSION,
        "recommendation": rec,
        "score": str(score),
        "rationale": why,
        "headline": f"{rec}: {case['direction']} {case['asset']} ({case['instrument_id']})",
        "action_for_cio": action,
        "trade": {"direction": case["direction"], "instrument_id": case["instrument_id"], "entry_reference": entry,
                  "stop": stop, "target": target, "reward_risk": rr,
                  "suggested_quantity": sizing.get("suggested_quantity"), "max_loss_usd": sizing.get("max_loss_usd"),
                  "notional_usd": sizing.get("notional_usd")},
        "risk": {"outcome": risk["outcome"], "reason_codes": risk["reason_codes"]},
        "evidence_for": [e["summary"] for e in supports],
        "evidence_against": [e["summary"] for e in opposes],
        "dissent": [f"{e['producer']}: {e['summary']}" for e in opposes],
        "uncertainty": uncertainty,
        "macro_events": inputs.get("macro_events", []),
        "data_source": inputs.get("data_source"),
        "sizing_note": "Sized against positions open at decision time. Recording or closing a position automatically "
                       "sends other open recommendations back through risk review.",
        "disclaimer": "Advisory only. MERIDIAN does not place, modify or cancel orders. The CIO decides and executes.",
    }
