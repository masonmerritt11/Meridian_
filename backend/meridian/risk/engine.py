"""Deterministic risk engine. Pure function of its inputs — no I/O, no randomness, no language model.

Contract:
  * Every check returns ALLOW, RESTRICT or BLOCK with a machine-readable reason code.
  * Overall outcome is the most severe check outcome.
  * Missing *essential* information returns BLOCK. Missing non-essential information returns RESTRICT.
    The engine never returns ALLOW when information is missing.
  * Any unexpected exception inside a check is converted to BLOCK (fail closed).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

ENGINE_VERSION = "1.0.0"
SEVERITY = {"ALLOW": 0, "RESTRICT": 1, "BLOCK": 2}

DEFAULT_RISK_CONFIG: dict[str, Any] = {
    "max_loss_per_trade_pct": "0.10",      # CIO instruction: a trade stops out at 10% of total capital
    "max_total_open_risk_pct": "0.10",     # all open trades together can lose at most 10% (conservative default)
    "max_correlated_risk_pct": "0.10",     # same-direction risk within metals / energy / crypto
    "max_capital_usage_pct": "1.00",       # CIO: may deploy all capital into positions
    "max_gross_notional_multiple": "10",   # total contract notional <= 10x equity (leverage ceiling)
    "daily_loss_limit_pct": "0.10",
    "weekly_loss_limit_pct": "0.20",
    "min_days_to_expiry": 3,
    "event_blackout_minutes_before": 60,
    "event_blackout_minutes_after": 30,
    "event_upcoming_hours": 24,
    "event_impacts": ["High"],
    "event_countries": ["USD"],
    "asset_event_keywords": {"OIL": ["crude", "oil", "opec"]},
    "max_spread_bps": "25",
    "volatility_restrict_atr_pct": {"METAL": "1.0", "ENERGY": "1.5", "CRYPTO": "2.5"},
    "volatility_block_atr_pct": {"METAL": "2.0", "ENERGY": "3.0", "CRYPTO": "5.0"},
    "max_positions_per_asset": 1,          # account too small to stack positions in one asset
    "margin_per_contract": {},             # USD per contract by asset, entered by the CIO from the broker
    "recommendation_ttl_hours": 12,
}

ESSENTIAL_CONFIG_KEYS = ("max_loss_per_trade_pct", "max_total_open_risk_pct", "max_correlated_risk_pct",
                         "max_capital_usage_pct", "max_gross_notional_multiple", "daily_loss_limit_pct",
                         "weekly_loss_limit_pct")


@dataclass
class OpenPositionView:
    asset: str
    correlation_group: str
    direction: str
    risk_usd: Decimal
    capital_used_usd: Decimal | None
    notional_usd: Decimal = Decimal(0)


@dataclass
class RiskInputs:
    now: datetime
    config: dict[str, Any]
    equity: Decimal | None
    asset: str
    asset_class: str
    correlation_group: str
    instrument_status: str | None
    product_type: str | None
    contract_size: Decimal | None
    size_increment: Decimal | None
    min_size: Decimal | None
    contract_expiry: datetime | None
    direction: str
    entry: Decimal | None
    stop: Decimal | None
    candles_fresh: bool | None
    spread_bps: Decimal | None
    macro_status: str | None
    macro_events: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[OpenPositionView] = field(default_factory=list)
    realized_today: Decimal = Decimal(0)
    realized_week: Decimal = Decimal(0)
    atr_pct: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class Check:
    check: str
    outcome: str
    code: str
    detail: str


@dataclass
class RiskResult:
    outcome: str
    reason_codes: list[str]
    checks: list[Check]
    sizing: dict[str, Any]
    engine_version: str = ENGINE_VERSION


def _s(x: Decimal) -> str:
    from meridian.core.util import dstr
    return dstr(x)


def _d(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _floor(q: Decimal, inc: Decimal) -> Decimal:
    return (q / inc).to_integral_value(rounding=ROUND_FLOOR) * inc


def evaluate(inp: RiskInputs) -> RiskResult:
    checks: list[Check] = []
    sizing: dict[str, Any] = {}

    def add(check: str, outcome: str, code: str, detail: str) -> None:
        checks.append(Check(check, outcome, code, detail))

    cfg = {**DEFAULT_RISK_CONFIG, **(inp.config or {})}
    missing_cfg = [k for k in ESSENTIAL_CONFIG_KEYS if (inp.config or {}).get(k) in (None, "")]
    if missing_cfg:
        add("CONFIG", "BLOCK", "CONFIG_MISSING", f"risk config missing {missing_cfg}")

    # ---- essential inputs
    if inp.equity is None or inp.equity <= 0:
        add("CAPITAL", "BLOCK", "EQUITY_MISSING", "account equity unknown or non-positive")
    if inp.instrument_status != "ACTIVE":
        add("INSTRUMENT", "BLOCK", "INSTRUMENT_UNAVAILABLE", f"instrument status {inp.instrument_status}")
    if inp.candles_fresh is None:
        add("DATA_FRESHNESS", "BLOCK", "DATA_MISSING", "no market data freshness record")
    elif not inp.candles_fresh:
        add("DATA_FRESHNESS", "BLOCK", "DATA_STALE", "market data is stale")
    else:
        add("DATA_FRESHNESS", "ALLOW", "DATA_FRESH", "market data fresh")
    if inp.entry is None or inp.stop is None or inp.entry <= 0 or inp.stop <= 0:
        add("STOP", "BLOCK", "INVALID_STOP", "entry/stop missing")
    elif (inp.direction == "LONG" and inp.stop >= inp.entry) or (inp.direction == "SHORT" and inp.stop <= inp.entry):
        add("STOP", "BLOCK", "INVALID_STOP", "stop on wrong side of entry")
    if inp.contract_size is None or inp.contract_size <= 0:
        add("CONTRACT", "BLOCK", "CONTRACT_SIZE_MISSING", "contract size unknown")

    held = sum(1 for p in inp.open_positions if p.asset == inp.asset)
    if held >= int(cfg["max_positions_per_asset"]):
        add("ASSET_CONCENTRATION", "BLOCK", "ASSET_POSITION_LIMIT",
            f"{held} open {inp.asset} position(s); limit {cfg['max_positions_per_asset']} per asset")
    else:
        add("ASSET_CONCENTRATION", "ALLOW", "ASSET_NOT_HELD", f"no open {inp.asset} position")

    blocked_essential = any(c.outcome == "BLOCK" for c in checks)
    qty: Decimal | None = None
    if not blocked_essential:
        try:
            qty = _size(inp, cfg, add, sizing)
        except Exception as exc:  # noqa: BLE001 — fail closed
            add("SIZING", "BLOCK", "ENGINE_ERROR", repr(exc))
            qty = None

    for fn in (_expiry, _liquidity, _events, _loss_limits, _volatility):
        try:
            fn(inp, cfg, add)
        except Exception as exc:  # noqa: BLE001 — fail closed
            add(fn.__name__.strip("_").upper(), "BLOCK", "ENGINE_ERROR", repr(exc))

    outcome = max((c.outcome for c in checks), key=lambda o: SEVERITY[o], default="BLOCK")
    if qty is None or qty <= 0:
        outcome = "BLOCK"
        if not any(c.outcome == "BLOCK" for c in checks):
            add("SIZING", "BLOCK", "NO_SIZE", "no permissible size")
    sizing["suggested_quantity"] = _s(qty) if qty and outcome != "BLOCK" else "0"
    if qty and outcome != "BLOCK":
        sizing["max_loss_usd"] = str((qty * _d(sizing["risk_per_unit_usd"])).quantize(Decimal("0.01")))
    codes = sorted({c.code for c in checks if c.outcome != "ALLOW"})
    return RiskResult(outcome=outcome, reason_codes=codes, checks=checks, sizing=sizing)


def _size(inp: RiskInputs, cfg: dict[str, Any], add: Any, sizing: dict[str, Any]) -> Decimal | None:
    eq = _d(inp.equity)
    inc = inp.size_increment or Decimal(1)
    min_size = inp.min_size or inc
    risk_unit = abs(_d(inp.entry) - _d(inp.stop)) * _d(inp.contract_size)
    budget = eq * _d(cfg["max_loss_per_trade_pct"])
    sizing.update({"equity_usd": _s(eq), "risk_per_unit_usd": _s(risk_unit.quantize(Decimal("0.0001"))),
                   "per_trade_budget_usd": str(budget.quantize(Decimal("0.01"))), "size_increment": _s(inc)})
    qty = _floor(budget / risk_unit, inc)
    if qty < min_size:
        add("MAX_LOSS", "BLOCK", "MIN_SIZE_EXCEEDS_LOSS_LIMIT",
            f"one {_s(min_size)} unit risks ${risk_unit:.2f} > per-trade limit ${budget:.2f}")
        return None
    add("MAX_LOSS", "ALLOW", "WITHIN_LOSS_LIMIT", f"{_s(qty)} units risk ${qty * risk_unit:.2f} <= ${budget:.2f}")

    open_risk = sum((p.risk_usd for p in inp.open_positions), Decimal(0))
    room = eq * _d(cfg["max_total_open_risk_pct"]) - open_risk
    qty = _cap(qty, room, risk_unit, inc, min_size, "TOTAL_OPEN_RISK", "TOTAL_RISK_LIMIT", add,
               f"open risk ${open_risk:.2f}, room ${max(room, Decimal(0)):.2f}")
    if qty is None:
        return None

    corr_risk = sum((p.risk_usd for p in inp.open_positions
                     if p.correlation_group == inp.correlation_group and p.direction == inp.direction), Decimal(0))
    room_c = eq * _d(cfg["max_correlated_risk_pct"]) - corr_risk
    qty = _cap(qty, room_c, risk_unit, inc, min_size, "CORRELATED_EXPOSURE", "CORRELATED_EXPOSURE_LIMIT", add,
               f"{inp.correlation_group} {inp.direction} risk ${corr_risk:.2f}, room ${max(room_c, Decimal(0)):.2f}")
    if qty is None:
        return None

    # capital usage (margin for futures, notional for spot)
    cap_limit = eq * _d(cfg["max_capital_usage_pct"])
    used_known = [p.capital_used_usd for p in inp.open_positions if p.capital_used_usd is not None]
    unknown_open = any(p.capital_used_usd is None for p in inp.open_positions)
    if inp.product_type == "FUTURE":
        m = (cfg.get("margin_per_contract") or {}).get(inp.asset)
        if m in (None, ""):
            add("OVERNIGHT_MARGIN", "RESTRICT", "MARGIN_UNVERIFIED",
                "margin per contract not configured; confirm with broker before acting")
            per_unit_cap = None
        else:
            per_unit_cap = _d(m)
    else:
        per_unit_cap = _d(inp.entry) * _d(inp.contract_size)
    if per_unit_cap is not None:
        room_cap = cap_limit - sum(used_known, Decimal(0))
        max_by_cap = _floor(room_cap / per_unit_cap, inc) if per_unit_cap > 0 else qty
        sizing["capital_per_unit_usd"] = _s(per_unit_cap)
        if max_by_cap < min_size:
            add("MAX_EXPOSURE", "BLOCK", "MAX_EXPOSURE_LIMIT", f"capital room ${room_cap:.2f} < one unit ${per_unit_cap:.2f}")
            return None
        if max_by_cap < qty:
            add("MAX_EXPOSURE", "RESTRICT", "SIZE_REDUCED_EXPOSURE", f"reduced {_s(qty)} -> {_s(max_by_cap)} by capital usage")
            qty = max_by_cap
        else:
            add("MAX_EXPOSURE", "ALLOW", "WITHIN_EXPOSURE", f"capital used ${qty * per_unit_cap:.2f} <= ${room_cap:.2f}")
        if unknown_open:
            add("MAX_EXPOSURE", "RESTRICT", "OPEN_EXPOSURE_UNVERIFIED", "capital usage of an open position unknown")
    # gross notional ceiling (applies to futures and spot)
    unit_notional = _d(inp.entry) * _d(inp.contract_size)
    open_notional = sum((p.notional_usd for p in inp.open_positions), Decimal(0))
    room_n = eq * _d(cfg["max_gross_notional_multiple"]) - open_notional
    max_by_n = _floor(room_n / unit_notional, inc) if room_n > 0 else Decimal(0)
    if max_by_n < min_size:
        add("GROSS_NOTIONAL", "BLOCK", "GROSS_NOTIONAL_LIMIT",
            f"one unit notional ${unit_notional:.2f} exceeds remaining ceiling ${max(room_n, Decimal(0)):.2f}")
        return None
    if max_by_n < qty:
        add("GROSS_NOTIONAL", "RESTRICT", "SIZE_REDUCED_GROSS_NOTIONAL", f"reduced {_s(qty)} -> {_s(max_by_n)} by notional ceiling")
        qty = max_by_n
    else:
        add("GROSS_NOTIONAL", "ALLOW", "WITHIN_GROSS_NOTIONAL", f"notional ${qty * unit_notional:.2f} <= ${room_n:.2f}")
    sizing["notional_usd"] = str((qty * unit_notional).quantize(Decimal("0.01")))
    sizing["notional_multiple_of_equity"] = str((qty * unit_notional / eq).quantize(Decimal("0.01")))
    return qty


def _cap(qty: Decimal, room: Decimal, risk_unit: Decimal, inc: Decimal, min_size: Decimal, check: str, code: str,
         add: Any, detail: str) -> Decimal | None:
    max_q = _floor(room / risk_unit, inc) if room > 0 else Decimal(0)
    if max_q < min_size:
        add(check, "BLOCK", code, detail)
        return None
    if max_q < qty:
        add(check, "RESTRICT", f"SIZE_REDUCED_{check}", f"{detail}; reduced {_s(qty)} -> {_s(max_q)}")
        return max_q
    add(check, "ALLOW", f"WITHIN_{check}", detail)
    return qty


def _expiry(inp: RiskInputs, cfg: dict[str, Any], add: Any) -> None:
    if inp.product_type != "FUTURE":
        add("CONTRACT_EXPIRY", "ALLOW", "NOT_EXPIRING", "spot product")
        return
    if inp.contract_expiry is None:
        add("CONTRACT_EXPIRY", "BLOCK", "CONTRACT_EXPIRY_UNKNOWN", "futures expiry unknown")
    elif inp.contract_expiry - inp.now < timedelta(days=int(cfg["min_days_to_expiry"])):
        add("CONTRACT_EXPIRY", "BLOCK", "CONTRACT_NEAR_EXPIRY", f"expires {inp.contract_expiry.isoformat()}")
    else:
        add("CONTRACT_EXPIRY", "ALLOW", "EXPIRY_OK", f"expires {inp.contract_expiry.isoformat()}")


def _liquidity(inp: RiskInputs, cfg: dict[str, Any], add: Any) -> None:
    mx = _d(cfg["max_spread_bps"])
    if inp.spread_bps is None:
        add("LIQUIDITY", "RESTRICT", "LIQUIDITY_UNKNOWN", "order book unavailable")
    elif inp.spread_bps > mx:
        add("LIQUIDITY", "BLOCK", "SPREAD_TOO_WIDE", f"spread {_s(inp.spread_bps)}bps > {mx}bps")
    elif inp.spread_bps > mx / 2:
        add("LIQUIDITY", "RESTRICT", "WIDE_SPREAD", f"spread {_s(inp.spread_bps)}bps")
    else:
        add("LIQUIDITY", "ALLOW", "SPREAD_OK", f"spread {_s(inp.spread_bps)}bps")


def relevant_events(events: list[dict[str, Any]], asset: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    impacts = set(cfg["event_impacts"])
    countries = set(cfg["event_countries"])
    kws = [k.lower() for k in (cfg.get("asset_event_keywords") or {}).get(asset, [])]
    out = []
    for e in events:
        title = str(e.get("title", "")).lower()
        if (e.get("impact") in impacts and e.get("country") in countries) or (kws and any(k in title for k in kws)):
            out.append(e)
    return out


def _events(inp: RiskInputs, cfg: dict[str, Any], add: Any) -> None:
    if inp.macro_status != "FRESH":
        add("ECONOMIC_EVENTS", "RESTRICT", "EVENT_DATA_UNAVAILABLE", f"macro calendar status {inp.macro_status}")
    before = timedelta(minutes=int(cfg["event_blackout_minutes_before"]))
    after = timedelta(minutes=int(cfg["event_blackout_minutes_after"]))
    upcoming = timedelta(hours=int(cfg["event_upcoming_hours"]))
    flagged = False
    for e in relevant_events(inp.macro_events, inp.asset, cfg):
        t = e.get("event_time")
        t = datetime.fromisoformat(t) if isinstance(t, str) else t
        if t is None:
            continue
        if e.get("time_confidence") == "DATE_ONLY":
            if abs(t - inp.now) < timedelta(hours=24):
                add("ECONOMIC_EVENTS", "RESTRICT", "EVENT_TIME_UNCERTAIN", f"{e['title']} on {t.date()} (time unknown)")
                flagged = True
            continue
        if t - before <= inp.now <= t + after:
            add("ECONOMIC_EVENTS", "BLOCK", "EVENT_BLACKOUT", f"{e['title']} at {t.isoformat()}")
            flagged = True
        elif inp.now < t <= inp.now + upcoming:
            add("ECONOMIC_EVENTS", "RESTRICT", "EVENT_UPCOMING", f"{e['title']} at {t.isoformat()}")
            flagged = True
    if not flagged and inp.macro_status == "FRESH":
        add("ECONOMIC_EVENTS", "ALLOW", "NO_EVENT_CONFLICT", "no relevant events in window")


def _loss_limits(inp: RiskInputs, cfg: dict[str, Any], add: Any) -> None:
    if inp.equity is None or inp.equity <= 0:
        return
    eq = _d(inp.equity)
    dl = -eq * _d(cfg["daily_loss_limit_pct"])
    wl = -eq * _d(cfg["weekly_loss_limit_pct"])
    if inp.realized_today <= dl:
        add("LOSS_LIMITS", "BLOCK", "DAILY_LOSS_LIMIT", f"realized today ${inp.realized_today:.2f} <= ${dl:.2f}")
    elif inp.realized_week <= wl:
        add("LOSS_LIMITS", "BLOCK", "WEEKLY_LOSS_LIMIT", f"realized week ${inp.realized_week:.2f} <= ${wl:.2f}")
    else:
        add("LOSS_LIMITS", "ALLOW", "WITHIN_LOSS_LIMITS", f"today ${inp.realized_today:.2f}, week ${inp.realized_week:.2f}")


def _volatility(inp: RiskInputs, cfg: dict[str, Any], add: Any) -> None:
    if inp.atr_pct is None:
        add("VOLATILITY", "RESTRICT", "VOLATILITY_UNKNOWN", "ATR unavailable")
        return
    r = _d(cfg["volatility_restrict_atr_pct"][inp.asset_class])
    b = _d(cfg["volatility_block_atr_pct"][inp.asset_class])
    if inp.atr_pct >= b:
        add("VOLATILITY", "BLOCK", "VOLATILITY_EXTREME", f"ATR {_s(inp.atr_pct)}% >= {b}%")
    elif inp.atr_pct >= r:
        add("VOLATILITY", "RESTRICT", "VOLATILITY_ELEVATED", f"ATR {_s(inp.atr_pct)}% >= {r}%")
    else:
        add("VOLATILITY", "ALLOW", "VOLATILITY_NORMAL", f"ATR {_s(inp.atr_pct)}%")
