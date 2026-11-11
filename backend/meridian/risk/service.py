"""Gathers risk inputs from the database, runs the pure engine, persists the decision. Also owns
versioned risk configuration, which can only change with CIO authorization (enforced at the API) and is audited."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.core import audit
from meridian.core.config import code_version
from meridian.core.util import jsonable, sha256, utcnow
from meridian.db.models import DataFreshness, Instrument, MacroEvent, ResearchCase, RiskConfig, RiskDecision
from meridian.marketdata.service import latest_book
from meridian.marketdata.types import ASSETS
from meridian.portfolio import service as portfolio
from meridian.risk.engine import DEFAULT_RISK_CONFIG, RiskInputs, RiskResult, evaluate


def active_config(s: Session) -> RiskConfig | None:
    return s.execute(select(RiskConfig).where(RiskConfig.status == "ACTIVE")).scalar()


def ensure_default_config(s: Session) -> RiskConfig:
    cfg = active_config(s)
    if cfg is None:
        cfg = RiskConfig(version=1, config=jsonable(DEFAULT_RISK_CONFIG), status="ACTIVE", created_by="system",
                         reason="initial defaults from CIO instructions (10% max loss per trade)")
        s.add(cfg)
        s.flush()
        audit.record(s, "system", "risk.config_created", "risk_config", 1, {"config": cfg.config})
    return cfg


def change_config(s: Session, new_config: dict[str, Any], actor: str, reason: str) -> RiskConfig:
    """Caller MUST have verified CIO identity + re-authentication. Creates a new immutable version."""
    if not reason or len(reason.strip()) < 5:
        raise ValueError("a reason is required for risk config changes")
    merged = {**DEFAULT_RISK_CONFIG, **new_config}
    unknown = set(new_config) - set(DEFAULT_RISK_CONFIG)
    if unknown:
        raise ValueError(f"unknown risk config keys: {sorted(unknown)}")
    cur = active_config(s)
    ver = (cur.version + 1) if cur else 1
    if cur:
        cur.status = "SUPERSEDED"
        s.flush()
    row = RiskConfig(version=ver, config=jsonable(merged), status="ACTIVE", created_by=actor, reason=reason)
    s.add(row)
    s.flush()
    audit.record(s, actor, "risk.config_changed", "risk_config", ver,
                 {"previous_version": cur.version if cur else None, "reason": reason,
                  "before": cur.config if cur else None, "after": row.config})
    return row


def macro_context(s: Session, now: Any, hours_back: int = 2, hours_ahead: int = 48) -> tuple[str | None, list[dict[str, Any]]]:
    fr = s.get(DataFreshness, "macro_calendar")
    status = fr.status if fr else None
    rows = s.execute(select(MacroEvent).where(MacroEvent.event_time >= now - timedelta(hours=hours_back),
                                              MacroEvent.event_time <= now + timedelta(hours=hours_ahead))
                     .order_by(MacroEvent.event_time)).scalars()
    events = [{"id": e.id, "title": e.title, "country": e.country, "impact": e.impact,
               "event_time": e.event_time.isoformat() if e.event_time else None,
               "time_confidence": e.time_confidence, "forecast": e.forecast, "previous": e.previous, "source": e.source}
              for e in rows]
    return status, events


def build_inputs(s: Session, *, instrument_id: str, direction: str, entry: Decimal | None, stop: Decimal | None,
                 atr_pct: Decimal | None, granularity: str, cfg: dict[str, Any]) -> RiskInputs:
    now = utcnow()
    inst = s.get(Instrument, instrument_id)
    asset = inst.asset if inst else "UNKNOWN"
    adef = ASSETS.get(asset)
    fr = s.get(DataFreshness, f"candles:{instrument_id}:{granularity}")
    book = latest_book(s, instrument_id)
    spread = book.spread_bps if book and now - book.ts < timedelta(minutes=30) else None
    mstatus, mevents = macro_context(s, now)
    day, week = portfolio.period_starts(now)
    return RiskInputs(
        now=now, config=cfg, equity=portfolio.equity(s), asset=asset,
        asset_class=adef.asset_class if adef else "CRYPTO", correlation_group=adef.correlation_group if adef else "unknown",
        instrument_status=inst.status if inst else None, product_type=inst.product_type if inst else None,
        contract_size=inst.contract_size if inst else None, size_increment=inst.size_increment if inst else None,
        min_size=inst.min_size if inst else None, contract_expiry=inst.contract_expiry if inst else None,
        direction=direction, entry=entry, stop=stop, candles_fresh=(fr.status == "FRESH") if fr else None,
        spread_bps=spread, macro_status=mstatus, macro_events=mevents,
        open_positions=portfolio.open_position_views(s, cfg.get("margin_per_contract") or {}),
        realized_today=portfolio.realized_pnl(s, day), realized_week=portfolio.realized_pnl(s, week), atr_pct=atr_pct,
    )


def result_to_json(r: RiskResult) -> dict[str, Any]:
    return {"outcome": r.outcome, "reason_codes": r.reason_codes, "checks": [c.__dict__ for c in r.checks],
            "sizing": r.sizing, "engine_version": r.engine_version}


def decide_for_case(s: Session, case: ResearchCase, granularity: str, actor: str) -> RiskDecision:
    cfg_row = active_config(s)
    cfg = cfg_row.config if cfg_row else {}
    content = case.hypothesis
    calc = content.get("calculations", {})
    inp = build_inputs(s, instrument_id=case.instrument_id, direction=case.direction,
                       entry=Decimal(content["entry_reference"]) if content.get("entry_reference") else None,
                       stop=Decimal(content["stop"]) if content.get("stop") else None,
                       atr_pct=Decimal(str(calc["atr_pct"])) if calc.get("atr_pct") is not None else None,
                       granularity=granularity, cfg=cfg)
    result = evaluate(inp)
    inputs_json = jsonable(inp.to_json())
    row = RiskDecision(case_id=case.id, config_version=cfg_row.version if cfg_row else 0, outcome=result.outcome,
                       reason_codes=result.reason_codes, checks=[c.__dict__ for c in result.checks],
                       sizing=jsonable(result.sizing), inputs=inputs_json, inputs_hash=sha256(inputs_json),
                       code_version=f"{code_version()}+risk-engine-{result.engine_version}")
    s.add(row)
    s.flush()
    audit.record(s, actor, "risk.decision", "risk_decision", row.id,
                 {"case_id": str(case.id), "outcome": result.outcome, "reason_codes": result.reason_codes,
                  "config_version": row.config_version})
    return row


def recompute(inputs: dict[str, Any]) -> RiskResult:
    """Re-run the engine on stored inputs (for reconstruction/replay)."""
    from datetime import datetime

    from meridian.risk.engine import OpenPositionView

    d = dict(inputs)
    d["now"] = datetime.fromisoformat(d["now"])
    if d.get("contract_expiry"):
        d["contract_expiry"] = datetime.fromisoformat(d["contract_expiry"])
    for k in ("equity", "contract_size", "size_increment", "min_size", "entry", "stop", "spread_bps",
              "realized_today", "realized_week", "atr_pct"):
        if d.get(k) is not None:
            d[k] = Decimal(str(d[k]))
    d["open_positions"] = [OpenPositionView(asset=p["asset"], correlation_group=p["correlation_group"],
                                            direction=p["direction"], risk_usd=Decimal(str(p["risk_usd"])),
                                            capital_used_usd=Decimal(str(p["capital_used_usd"])) if p.get("capital_used_usd") is not None else None,
                                            notional_usd=Decimal(str(p.get("notional_usd", 0))))
                           for p in d.get("open_positions", [])]
    return evaluate(RiskInputs(**d))
