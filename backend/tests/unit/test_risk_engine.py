from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from meridian.risk.engine import DEFAULT_RISK_CONFIG, OpenPositionView, RiskInputs, evaluate

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def make(**kw) -> RiskInputs:
    base = dict(
        now=NOW, config=dict(DEFAULT_RISK_CONFIG, margin_per_contract={"GOLD": "300"}), equity=Decimal("700"),
        asset="GOLD", asset_class="METAL", correlation_group="metals", instrument_status="ACTIVE",
        product_type="FUTURE", contract_size=Decimal("1"), size_increment=Decimal("1"), min_size=Decimal("1"),
        contract_expiry=NOW + timedelta(days=30), direction="LONG", entry=Decimal("3000"), stop=Decimal("2970"),
        candles_fresh=True, spread_bps=Decimal("2"), macro_status="FRESH", macro_events=[], open_positions=[],
        atr_pct=Decimal("0.4"),
    )
    base.update(kw)
    return RiskInputs(**base)


def test_clean_trade_allows_and_sizes_to_10pct():
    r = evaluate(make())
    assert r.outcome == "ALLOW", r.reason_codes
    assert r.sizing["suggested_quantity"] == "2"  # $30 risk/unit, $70 budget -> 2
    assert Decimal(r.sizing["max_loss_usd"]) <= Decimal("70")


def test_one_contract_over_10pct_blocks():
    r = evaluate(make(stop=Decimal("2920")))  # $80/unit > $70
    assert r.outcome == "BLOCK"
    assert "MIN_SIZE_EXCEEDS_LOSS_LIMIT" in r.reason_codes


@pytest.mark.parametrize("field,value,code", [
    ("equity", None, "EQUITY_MISSING"),
    ("candles_fresh", None, "DATA_MISSING"),
    ("candles_fresh", False, "DATA_STALE"),
    ("instrument_status", None, "INSTRUMENT_UNAVAILABLE"),
    ("contract_size", None, "CONTRACT_SIZE_MISSING"),
    ("contract_expiry", None, "CONTRACT_EXPIRY_UNKNOWN"),
    ("stop", None, "INVALID_STOP"),
    ("entry", None, "INVALID_STOP"),
])
def test_missing_essential_information_blocks(field, value, code):
    r = evaluate(make(**{field: value}))
    assert r.outcome == "BLOCK"
    assert code in r.reason_codes


@pytest.mark.parametrize("field,value,code", [
    ("spread_bps", None, "LIQUIDITY_UNKNOWN"),
    ("macro_status", "UNAVAILABLE", "EVENT_DATA_UNAVAILABLE"),
    ("macro_status", None, "EVENT_DATA_UNAVAILABLE"),
    ("atr_pct", None, "VOLATILITY_UNKNOWN"),
])
def test_missing_nonessential_information_never_allows(field, value, code):
    r = evaluate(make(**{field: value}))
    assert r.outcome != "ALLOW"
    assert code in r.reason_codes


def test_unknown_margin_restricts():
    r = evaluate(make(config=dict(DEFAULT_RISK_CONFIG)))
    assert r.outcome == "RESTRICT"
    assert "MARGIN_UNVERIFIED" in r.reason_codes


def test_empty_config_blocks():
    r = evaluate(make(config={}))
    assert r.outcome == "BLOCK" and "CONFIG_MISSING" in r.reason_codes


def test_wrong_side_stop_blocks():
    assert evaluate(make(stop=Decimal("3010"))).outcome == "BLOCK"
    assert evaluate(make(direction="SHORT", stop=Decimal("2990"))).outcome == "BLOCK"


def test_total_open_risk_blocks_when_full():
    pos = OpenPositionView("BTC", "crypto", "LONG", Decimal("70"), Decimal("0"))
    r = evaluate(make(open_positions=[pos]))
    assert r.outcome == "BLOCK" and "TOTAL_RISK_LIMIT" in r.reason_codes


def test_total_open_risk_reduces_size():
    pos = OpenPositionView("BTC", "crypto", "LONG", Decimal("35"), Decimal("0"))
    r = evaluate(make(open_positions=[pos]))
    assert r.outcome == "RESTRICT" and r.sizing["suggested_quantity"] == "1"


def test_correlated_exposure():
    cfg = dict(DEFAULT_RISK_CONFIG, margin_per_contract={"GOLD": "300"}, max_total_open_risk_pct="0.5",
               max_correlated_risk_pct="0.05")
    r = evaluate(make(config=cfg, open_positions=[OpenPositionView("SILVER", "metals", "LONG", Decimal("10"), Decimal("0"))]))
    assert r.outcome == "BLOCK" and "CORRELATED_EXPOSURE_LIMIT" in r.reason_codes
    # opposite direction is not correlated exposure
    r2 = evaluate(make(config=cfg, open_positions=[OpenPositionView("SILVER", "metals", "SHORT", Decimal("10"), Decimal("0"))]))
    assert "CORRELATED_EXPOSURE_LIMIT" not in r2.reason_codes


def test_margin_capacity_limit():
    cfg = dict(DEFAULT_RISK_CONFIG, margin_per_contract={"GOLD": "800"})
    r = evaluate(make(config=cfg))
    assert r.outcome == "BLOCK" and "MAX_EXPOSURE_LIMIT" in r.reason_codes


def test_gross_notional_ceiling():
    cfg = dict(DEFAULT_RISK_CONFIG, margin_per_contract={"GOLD": "100"}, max_gross_notional_multiple="4")
    r = evaluate(make(config=cfg))  # one unit = $3000 notional > 4 x 700 = 2800
    assert r.outcome == "BLOCK" and "GROSS_NOTIONAL_LIMIT" in r.reason_codes


def test_event_blackout_blocks_and_upcoming_restricts():
    ev = {"title": "FOMC Statement", "country": "USD", "impact": "High", "time_confidence": "EXACT"}
    r = evaluate(make(macro_events=[{**ev, "event_time": (NOW + timedelta(minutes=30)).isoformat()}]))
    assert r.outcome == "BLOCK" and "EVENT_BLACKOUT" in r.reason_codes
    r2 = evaluate(make(macro_events=[{**ev, "event_time": (NOW + timedelta(hours=6)).isoformat()}]))
    assert r2.outcome == "RESTRICT" and "EVENT_UPCOMING" in r2.reason_codes
    low = {**ev, "impact": "Low", "event_time": (NOW + timedelta(minutes=10)).isoformat()}
    assert "EVENT_BLACKOUT" not in evaluate(make(macro_events=[low])).reason_codes


def test_oil_keyword_events():
    ev = {"title": "Crude Oil Inventories", "country": "USD", "impact": "Medium", "time_confidence": "EXACT",
          "event_time": (NOW + timedelta(minutes=20)).isoformat()}
    assert "EVENT_BLACKOUT" in evaluate(make(asset="OIL", asset_class="ENERGY", correlation_group="energy",
                                             macro_events=[ev], config=dict(DEFAULT_RISK_CONFIG))).reason_codes
    assert "EVENT_BLACKOUT" not in evaluate(make(macro_events=[ev])).reason_codes


def test_loss_limits():
    assert "DAILY_LOSS_LIMIT" in evaluate(make(realized_today=Decimal("-70"))).reason_codes
    assert "WEEKLY_LOSS_LIMIT" in evaluate(make(realized_week=Decimal("-140"))).reason_codes


def test_volatility_and_expiry_and_spread():
    assert "VOLATILITY_EXTREME" in evaluate(make(atr_pct=Decimal("2.5"))).reason_codes
    assert "VOLATILITY_ELEVATED" in evaluate(make(atr_pct=Decimal("1.2"))).reason_codes
    assert "CONTRACT_NEAR_EXPIRY" in evaluate(make(contract_expiry=NOW + timedelta(days=1))).reason_codes
    assert "SPREAD_TOO_WIDE" in evaluate(make(spread_bps=Decimal("40"))).reason_codes


def test_deterministic():
    a, b = evaluate(make()), evaluate(make())
    assert (a.outcome, a.reason_codes, a.sizing) == (b.outcome, b.reason_codes, b.sizing)


def test_one_position_per_asset():
    held = OpenPositionView("GOLD", "metals", "LONG", Decimal("10"), Decimal("0"))
    r = evaluate(make(open_positions=[held]))
    assert r.outcome == "BLOCK" and "ASSET_POSITION_LIMIT" in r.reason_codes
    other = OpenPositionView("BTC", "crypto", "LONG", Decimal("10"), Decimal("0"))
    assert "ASSET_POSITION_LIMIT" not in evaluate(make(open_positions=[other])).reason_codes


def test_exchange_margin_rate_used_when_not_configured():
    r = evaluate(make(config=dict(DEFAULT_RISK_CONFIG), margin_rate=Decimal("0.2")))  # 20% of $3000 = $600/contract
    assert any(c.code == "MARGIN_FROM_EXCHANGE" for c in r.checks)
    assert "SIZE_REDUCED_EXPOSURE" in r.reason_codes  # risk allowed 2, capital allows 1
    assert r.sizing["capital_per_unit_usd"] == "600" and r.sizing["suggested_quantity"] == "1"  # $700 capital fits one
    assert "MARGIN_UNVERIFIED" not in r.reason_codes
