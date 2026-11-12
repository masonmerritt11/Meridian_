from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from meridian.bus.schemas import make_event
from meridian.cases.lifecycle import TERMINAL, TRANSITIONS
from meridian.committee.logic import decide
from meridian.marketdata.coinbase import CoinbasePublicProvider, ForbiddenRequest, ReadOnlyPublicClient, select_product
from meridian.marketdata.simulated import SimulatedProvider
from meridian.marketdata.types import ASSETS, CandleDTO
from meridian.marketdata.validation import validate_candles
from meridian.registry.catalog import DEPARTMENTS, ROLES
from meridian.risk.position_monitor import MonitorInputs, advise
from meridian.workers.indicators import TrendParams, atr, ema, evaluate_trend
from meridian.workers.macro_provider import parse_fair_economy

FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)


# ---------------------------------------------------------------- organization
def test_catalog_has_11_departments_59_roles():
    assert len(DEPARTMENTS) == 11 and len(ROLES) == 59
    counts = {}
    for _, d, *_ in ROLES:
        counts[d] = counts.get(d, 0) + 1
    assert counts == {"trend": 7, "stat_arb": 6, "global_macro": 7, "liquidity": 5, "volatility": 5, "predictions": 4,
                      "portfolio": 5, "risk": 6, "data_ops": 6, "executive": 5, "research": 3}
    assert len({r[0] for r in ROLES}) == 59


# ---------------------------------------------------------------- no execution
def test_no_order_execution_code_exists():
    root = Path(__file__).resolve().parents[2] / "meridian"
    banned = re.compile(r"(/orders\b|create_order|place_order|cancel_order|edit_order|submit_order|/brokerage/orders)", re.I)
    hits = [f"{p}:{i}" for p in root.rglob("*.py") for i, line in enumerate(p.read_text().splitlines(), 1)
            if banned.search(line) and "banned" not in line]
    assert hits == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v3/brokerage/orders"), ("GET", "/api/v3/brokerage/orders/historical/batch"),
    ("GET", "/api/v3/brokerage/accounts"), ("DELETE", "/api/v3/brokerage/market/products"),
    ("GET", "/api/v3/brokerage/products"),  # authenticated variant is also excluded
])
def test_client_rejects_non_public_or_non_get(method, path):
    with pytest.raises(ForbiddenRequest):
        ReadOnlyPublicClient.check_allowed(method, path)


def test_client_allows_public_market_paths():
    for p in ("/api/v3/brokerage/market/products", "/api/v3/brokerage/market/products/BTC-USD/candles",
              "/api/v3/brokerage/market/product_book"):
        ReadOnlyPublicClient.check_allowed("GET", p)


# ---------------------------------------------------------------- coinbase adapter (fixtures from published schema)
def _fixture_transport():
    products = json.loads((FIX / "coinbase_products.json").read_text())
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET" and "authorization" not in {k.lower() for k in req.headers}
        if req.url.path == "/api/v3/brokerage/market/products":
            pt = req.url.params.get("product_type")
            return httpx.Response(200, json={"products": [p for p in products if p["product_type"] == pt],
                                             "num_products": 0, "pagination": {"has_next": False}})
        if req.url.path.endswith("/candles"):
            start = int(req.url.params["start"])
            return httpx.Response(200, json={"candles": [
                {"start": str(start + 3600 * i), "low": "99", "high": "101", "open": "100", "close": "100.5", "volume": "10"}
                for i in range(3)]})
        if req.url.path == "/api/v3/brokerage/market/product_book":
            return httpx.Response(200, json={"pricebook": {"product_id": req.url.params["product_id"],
                                                           "bids": [{"price": "100.0", "size": "2"}],
                                                           "asks": [{"price": "100.1", "size": "3"}],
                                                           "time": "2026-11-10T15:00:00Z"}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_coinbase_discovery_resolves_universe():
    p = CoinbasePublicProvider(ReadOnlyPublicClient(transport=_fixture_transport(), rate_per_s=100))
    specs = {s.asset: s for s in p.discover()}
    assert set(specs) == set(ASSETS)
    assert specs["GOLD"].product_type == "FUTURE" and specs["GOLD"].contract_size == Decimal("1")
    assert specs["BTC"].id == "BIP-20DEC30-CDE"  # perpetual-style preferred for crypto
    assert specs["XRP"].id == "XRP-USD"  # no XRP future in fixture -> spot fallback
    assert specs["OIL"].status == "UNAVAILABLE"  # nothing in fixture: explicit unavailable state, nothing fabricated
    b = p.book(specs["GOLD"])
    assert b.spread_bps == Decimal("10.00")


def test_coinbase_rate_limit_retry():
    calls = {"n": 0}
    def handler(req):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"products": [], "pagination": {}})
    c = ReadOnlyPublicClient(transport=httpx.MockTransport(handler), rate_per_s=100)
    c.get("/api/v3/brokerage/market/products")
    assert c.rate_limited == 1 and calls["n"] == 2


def test_select_product_skips_near_expiry():
    gold = ASSETS["GOLD"]
    prods = [{"product_id": "GOL-A", "product_type": "FUTURE", "status": "online",
              "future_product_details": {"contract_root_unit": "GOLD", "contract_expiry": (NOW + timedelta(days=1)).isoformat()}},
             {"product_id": "GOL-B", "product_type": "FUTURE", "status": "online",
              "future_product_details": {"contract_root_unit": "GOLD", "contract_expiry": (NOW + timedelta(days=30)).isoformat()}}]
    assert select_product(gold, prods, NOW)["product_id"] == "GOL-B"


# ---------------------------------------------------------------- validation
def test_validation_drops_incomplete_and_bad_bars():
    t = datetime(2026, 11, 10, 12, tzinfo=UTC)
    good = CandleDTO(t, Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5"), Decimal(1))
    bad_ohlc = CandleDTO(t + timedelta(hours=1), Decimal(1), Decimal("0.9"), Decimal("0.5"), Decimal("1.5"), Decimal(1))
    misaligned = CandleDTO(t + timedelta(minutes=7), Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5"), Decimal(1))
    forming = CandleDTO(datetime(2026, 11, 10, 15, tzinfo=UTC), Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5"), Decimal(1))
    rep = validate_candles([good, bad_ohlc, misaligned, forming, good], "ONE_HOUR", NOW)
    assert rep.accepted == [good] and rep.dropped_incomplete == 1 and rep.rejected == 3


# ---------------------------------------------------------------- indicators / trend
def test_ema_atr_basic():
    assert ema([1, 2, 3, 4], 2)[-1] == pytest.approx(3.5, rel=1e-6)
    assert atr([2, 2, 2], [1, 1, 1], [1.5, 1.5, 1.5], 2)[-1] == pytest.approx(1.0)


def test_trend_detects_breakout_and_rounds_stops_to_tick():
    bars = []
    for i in range(80):
        c = 100 + i * 0.5 + (3 if i == 79 else 0)
        bars.append({"ts": i, "open": c - 0.2, "high": c + 0.3, "low": c - 0.5, "close": c})
    sig = evaluate_trend(bars, Decimal("0.25"), TrendParams())
    assert sig.direction == "LONG" and sig.stop < sig.entry < sig.target
    assert (sig.stop / Decimal("0.25")) % 1 == 0
    flat = [{"ts": i, "open": 100, "high": 100.5, "low": 99.5, "close": 100} for i in range(80)]
    assert evaluate_trend(flat, Decimal("0.25"), TrendParams()).direction is None


def test_simulated_provider_is_deterministic_and_labeled():
    now = datetime(2026, 11, 10, 15, 10, tzinfo=UTC)
    p = SimulatedProvider(now=now)
    inst = next(i for i in p.discover() if i.asset == "GOLD")
    assert inst.id.startswith("SIM-") and inst.source == "SIMULATED"
    a = p.candles(inst, "ONE_HOUR", now - timedelta(hours=50), now)
    b = SimulatedProvider(now=now).candles(inst, "ONE_HOUR", now - timedelta(hours=50), now)
    assert a == b and a[-1].ts == datetime(2026, 11, 10, 14, tzinfo=UTC)  # last CLOSED bar only
    assert validate_candles(a, "ONE_HOUR", now, inst).rejected == 0


# ---------------------------------------------------------------- macro
def test_macro_parse_never_guesses_timezone():
    evs = parse_fair_economy([
        {"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
        {"title": "Naive", "country": "USD", "date": "2026-11-12T08:30:00", "impact": "High"},
        {"title": "", "country": "USD", "date": "2026-11-12T08:30:00-05:00"},
    ])
    assert len(evs) == 2
    assert evs[0].event_time == datetime(2026, 11, 12, 13, 30, tzinfo=UTC) and evs[0].time_confidence == "EXACT"
    assert evs[1].event_time is None and evs[1].time_confidence == "UNKNOWN"


# ---------------------------------------------------------------- lifecycle / committee / monitor
def test_lifecycle_graph():
    assert all(not TRANSITIONS[t] for t in TERMINAL)
    for st in ("CHALLENGED", "RISK_REVIEW", "COMMITTEE_REVIEW", "DEFERRED", "BLOCKED", "RECOMMENDED"):
        assert "RESEARCHING" in TRANSITIONS[st]  # back to research on new evidence
    assert "RECOMMENDED" in TRANSITIONS["COMMITTEE_REVIEW"] and "RECOMMENDED" not in TRANSITIONS["RISK_REVIEW"]


def _committee_inputs(outcome="ALLOW", stances=("SUPPORTS",), strength=0.6):
    return {"case": {"id": "c", "instrument_id": "SIM-GOLD", "asset": "GOLD", "direction": "LONG",
                     "hypothesis": {"strength": strength, "entry_reference": "4000", "stop": "3970", "target": "4045"}},
            "evidence": [{"kind": "CHALLENGE", "stance": s, "summary": f"{s} x", "producer": "p"} for s in stances],
            "risk": {"outcome": outcome, "reason_codes": [], "checks": [], "sizing": {"suggested_quantity": "2", "max_loss_usd": "60.00"}},
            "data_source": "SIMULATED", "macro_events": []}


def test_committee_cannot_recommend_on_block():
    for stances in (("SUPPORTS",) * 5, ()):
        assert decide(_committee_inputs("BLOCK", stances, strength=1.0))["recommendation"] == "BLOCKED"


def test_committee_preserves_dissent_and_uncertainty():
    m = decide(_committee_inputs("RESTRICT", ("SUPPORTS", "OPPOSES", "OPPOSES")))
    assert m["recommendation"] == "DEFER" and len(m["dissent"]) == 2
    assert any("SIMULATED" in u for u in m["uncertainty"])
    ok = decide(_committee_inputs())
    assert ok["recommendation"] == "RECOMMEND" and ok["trade"]["reward_risk"] == "1.50"
    assert decide(_committee_inputs(strength=0.1))["recommendation"] == "REJECT"


def _mon(**kw):
    base = dict(now=NOW, direction="LONG", entry=Decimal(100), stop=Decimal(95), target=Decimal(110), quantity=Decimal(1),
                contract_size=Decimal(10), equity=Decimal(700), last_price=Decimal(102), last_low=Decimal(101),
                last_high=Decimal(103), data_fresh=True, ema_fast=101.0, ema_slow=99.0, contract_expiry=NOW + timedelta(days=30),
                min_days_to_expiry=3, max_loss_pct=Decimal("0.10"), blackout_event=None)
    base.update(kw)
    return advise(MonitorInputs(**base))


def test_position_monitor():
    assert _mon()[0] == "HOLD"
    assert _mon(last_low=Decimal(94))[1] == ["STOP_BREACHED"]
    assert "TARGET_REACHED" in _mon(last_price=Decimal(111), last_high=Decimal(111))[1]
    assert "TREND_INVALIDATED" in _mon(ema_fast=98.0)[1]
    assert _mon(data_fresh=False)[0] == "REVIEW"
    assert _mon(last_price=None)[1] == ["DATA_MISSING"]
    assert _mon(blackout_event="FOMC")[0] == "REVIEW"
    assert "CONTRACT_EXPIRING" in _mon(contract_expiry=NOW + timedelta(days=1))[1]
    short = _mon(direction="SHORT", stop=Decimal(105), target=Decimal(90), last_high=Decimal(106), ema_fast=98.0)
    assert short[0] == "EXIT" and "STOP_BREACHED" in short[1]


# ---------------------------------------------------------------- event schemas
def test_event_schema_validation():
    env = make_event("case.researched", {"case_id": "00000000-0000-0000-0000-000000000001", "state": "RESEARCHING"}, producer="t")
    assert env.schema_version == 1 and env.correlation_id and env.occurred_at.tzinfo is not None
    child = make_event("case.challenged", {"case_id": "00000000-0000-0000-0000-000000000001", "state": "X"}, producer="t", causation=env)
    assert child.correlation_id == env.correlation_id and child.causation_id == env.event_id
    with pytest.raises(ValueError):
        make_event("order.placed", {}, producer="t")
    with pytest.raises(Exception):
        make_event("case.researched", {"state": "missing case id"}, producer="t")


def test_margin_rate_takes_higher_of_intraday_overnight():
    from meridian.marketdata.types import margin_rate
    raw = {"future_product_details": {"intraday_margin_rate": {"long_margin_rate": "0.10", "short_margin_rate": "0.12"},
                                      "overnight_margin_rate": {"long_margin_rate": "0.25", "short_margin_rate": "0.27"}}}
    assert margin_rate(raw, "LONG") == Decimal("0.25") and margin_rate(raw, "SHORT") == Decimal("0.27")
    assert margin_rate({}, "LONG") is None


def test_missed_opportunity_rules():
    from decimal import Decimal as D

    from meridian.cases.staleness import missed_reason
    # SHORT: entry 100, stop 104, target 92
    assert missed_reason("SHORT", D(100), D(104), D(92), D(99)) is None
    assert missed_reason("SHORT", D(100), D(104), D(92), D(95))[0] == "MISSED_ENTRY_PASSED"   # 62% of the way
    assert missed_reason("SHORT", D(100), D(104), D(92), D(97), low=D(91.5))[0] == "MISSED_TARGET_REACHED"
    assert missed_reason("SHORT", D(100), D(104), D(92), D(101), high=D(104.2))[0] == "STOP_HIT_BEFORE_ENTRY"
    # LONG mirrored
    assert missed_reason("LONG", D(100), D(96), D(108), D(101)) is None
    assert missed_reason("LONG", D(100), D(96), D(108), D(105))[0] == "MISSED_ENTRY_PASSED"
    assert missed_reason("LONG", D(100), D(96), D(108), D(102), high=D(108))[0] == "MISSED_TARGET_REACHED"
    assert missed_reason("LONG", D(100), D(96), D(108), None) is None
