"""Portfolio Constructor — turns the set of live recommendations into one allocation plan for the account.

Deterministic and advisory-only. Ranking uses the committee score (signal strength, supporting vs opposing
evidence, risk restrictions) and then reward:risk. The score is a ranking heuristic, NOT a probability of
profit — the strategy has not been backtested.

Allocation is greedy, best-ranked first, and never exceeds any of:
  * the per-position loss budget (each risk decision already sized to <= max_loss_per_trade_pct of equity),
  * total open risk room (max_total_open_risk_pct minus risk already open),
  * capital room (max_capital_usage_pct of equity minus margin already used),
  * max_positions_per_asset (one position per asset by default; a position may be several contracts).
Capital is left idle rather than forced into ideas that did not pass research, risk and committee.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any

CONSTRUCTOR_VERSION = "1.0.0"


@dataclass
class Candidate:
    case_id: str
    memo_id: str
    asset: str
    instrument_id: str
    direction: str
    score: Decimal
    reward_risk: Decimal | None
    entry: str
    stop: str
    target: str | None
    suggested_qty: Decimal
    risk_per_unit: Decimal
    capital_per_unit: Decimal | None
    size_increment: Decimal
    min_size: Decimal
    risk_outcome: str
    data_source: str


@dataclass
class Allocation:
    rank: int
    case_id: str
    memo_id: str
    asset: str
    instrument_id: str
    direction: str
    quantity: str
    entry: str
    stop: str
    target: str | None
    risk_usd: str
    capital_usd: str | None
    score: str
    reward_risk: str | None
    reduced_from: str | None = None
    notes: list[str] = field(default_factory=list)


def _floor(q: Decimal, inc: Decimal) -> Decimal:
    return (q / inc).to_integral_value(rounding=ROUND_FLOOR) * inc if inc > 0 else q


def _s(d: Decimal) -> str:
    return str(d.to_integral_value()) if d == d.to_integral_value() else format(d.normalize(), "f")


def build_plan(cands: list[Candidate], *, equity: Decimal, open_risk: Decimal, open_capital: Decimal,
               open_capital_unknown: bool, held_assets: dict[str, int], cfg: dict[str, Any]) -> dict[str, Any]:
    risk_room = equity * Decimal(str(cfg["max_total_open_risk_pct"])) - open_risk
    cap_limit = equity * Decimal(str(cfg["max_capital_usage_pct"]))
    cap_room = cap_limit - open_capital
    per_asset = int(cfg.get("max_positions_per_asset", 1))
    held = dict(held_assets)

    ranked = sorted(cands, key=lambda c: (c.score, c.reward_risk or Decimal(0)), reverse=True)
    allocations: list[Allocation] = []
    skipped: list[dict[str, Any]] = []
    capital_unknown = open_capital_unknown
    for c in ranked:
        def skip(reason: str, detail: str) -> None:
            skipped.append({"case_id": c.case_id, "asset": c.asset, "direction": c.direction, "score": _s(c.score),
                            "reason": reason, "detail": detail})
        if c.risk_outcome == "BLOCK":
            skip("RISK_BLOCK", "risk veto")
            continue
        if held.get(c.asset, 0) >= per_asset:
            skip("ASSET_HELD", f"already {held[c.asset]} {c.asset} position(s)")
            continue
        qty = c.suggested_qty
        notes: list[str] = []
        if c.risk_per_unit > 0:
            by_risk = _floor(max(risk_room, Decimal(0)) / c.risk_per_unit, c.size_increment)
            if by_risk < qty:
                qty = by_risk
                notes.append(f"limited by total open-risk room ${max(risk_room, Decimal(0)):.2f}")
        if c.capital_per_unit is not None and c.capital_per_unit > 0:
            by_cap = _floor(max(cap_room, Decimal(0)) / c.capital_per_unit, c.size_increment)
            if by_cap < qty:
                qty = by_cap
                notes.append(f"limited by remaining capital ${max(cap_room, Decimal(0)):.2f}")
        else:
            capital_unknown = True
            notes.append("capital per contract unknown; confirm margin before acting")
        if qty < c.min_size:
            skip("NO_ROOM", "; ".join(notes) or "size below minimum")
            continue
        risk_used = (qty * c.risk_per_unit).quantize(Decimal("0.01"))
        cap_used = (qty * c.capital_per_unit).quantize(Decimal("0.01")) if c.capital_per_unit is not None else None
        risk_room -= risk_used
        if cap_used is not None:
            cap_room -= cap_used
        held[c.asset] = held.get(c.asset, 0) + 1
        allocations.append(Allocation(
            rank=len(allocations) + 1, case_id=c.case_id, memo_id=c.memo_id, asset=c.asset,
            instrument_id=c.instrument_id, direction=c.direction, quantity=_s(qty), entry=c.entry, stop=c.stop,
            target=c.target, risk_usd=str(risk_used), capital_usd=str(cap_used) if cap_used is not None else None,
            score=_s(c.score), reward_risk=_s(c.reward_risk) if c.reward_risk is not None else None,
            reduced_from=_s(c.suggested_qty) if qty < c.suggested_qty else None, notes=notes))

    planned_risk = sum((Decimal(a.risk_usd) for a in allocations), Decimal(0))
    planned_cap = sum((Decimal(a.capital_usd) for a in allocations if a.capital_usd), Decimal(0))
    return {
        "constructor_version": CONSTRUCTOR_VERSION,
        "equity_usd": str(equity),
        "allocations": [asdict(a) for a in allocations],
        "skipped": skipped,
        "totals": {
            "planned_risk_usd": str(planned_risk), "open_risk_usd": str(open_risk.quantize(Decimal("0.01"))),
            "planned_capital_usd": str(planned_cap), "open_capital_usd": str(open_capital.quantize(Decimal("0.01"))),
            "capital_limit_usd": str(cap_limit.quantize(Decimal("0.01"))),
            "idle_capital_usd": str(max(cap_room, Decimal(0)).quantize(Decimal("0.01"))),
            "capital_unverified": capital_unknown,
        },
        "ranking_basis": "committee score (signal strength + supporting/opposing evidence - risk restrictions), "
                         "then reward:risk. A ranking heuristic, not a probability of profit.",
        "idle_note": "Capital stays idle when there are not enough ideas that passed research, risk and committee.",
    }


def plan_from_db(s: Any) -> dict[str, Any]:
    """Gather live recommendations + open positions from the database and build the plan."""
    from sqlalchemy import desc, func, select

    from meridian.cases.lifecycle import latest_risk
    from meridian.db.models import CommitteeMemo, Instrument, Position, ResearchCase
    from meridian.portfolio import service as portfolio
    from meridian.risk import service as risk_svc
    from meridian.risk.engine import DEFAULT_RISK_CONFIG

    cfg_row = risk_svc.active_config(s)
    cfg = {**DEFAULT_RISK_CONFIG, **(cfg_row.config if cfg_row else {})}
    eq = portfolio.equity(s) or Decimal(0)
    views = portfolio.open_position_views(s, cfg.get("margin_per_contract") or {})
    held: dict[str, int] = {}
    for v in views:
        held[v.asset] = held.get(v.asset, 0) + 1
    cands: list[Candidate] = []
    for c in s.execute(select(ResearchCase).where(ResearchCase.state == "RECOMMENDED")).scalars():
        if s.execute(select(func.count()).select_from(Position).where(Position.case_id == c.id)).scalar():
            continue
        memo = s.execute(select(CommitteeMemo).where(CommitteeMemo.case_id == c.id)
                         .order_by(desc(CommitteeMemo.created_at)).limit(1)).scalar()
        dec = latest_risk(s, c.id)
        inst = s.get(Instrument, c.instrument_id)
        if memo is None or dec is None or inst is None or inst.status != "ACTIVE":
            continue
        sz = dec.sizing or {}
        t = memo.memo.get("trade", {})
        cands.append(Candidate(
            case_id=str(c.id), memo_id=str(memo.id), asset=c.asset, instrument_id=c.instrument_id,
            direction=c.direction, score=Decimal(str(memo.score)),
            reward_risk=Decimal(t["reward_risk"]) if t.get("reward_risk") else None,
            entry=t.get("entry_reference"), stop=t.get("stop"), target=t.get("target"),
            suggested_qty=Decimal(sz.get("suggested_quantity", "0")),
            risk_per_unit=Decimal(sz.get("risk_per_unit_usd", "0")),
            capital_per_unit=Decimal(sz["capital_per_unit_usd"]) if sz.get("capital_per_unit_usd") else None,
            size_increment=Decimal(format((inst.size_increment or Decimal(1)).normalize(), "f")),
            min_size=Decimal(format((inst.min_size or inst.size_increment or Decimal(1)).normalize(), "f")),
            risk_outcome=dec.outcome, data_source=c.data_source))
    return build_plan(cands, equity=eq, open_risk=sum((v.risk_usd for v in views), Decimal(0)),
                      open_capital=sum((v.capital_used_usd for v in views if v.capital_used_usd is not None), Decimal(0)),
                      open_capital_unknown=any(v.capital_used_usd is None for v in views),
                      held_assets=held, cfg=cfg) | {"open_positions": len(views)}
