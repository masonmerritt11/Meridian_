from __future__ import annotations

from decimal import Decimal

from meridian.portfolio.constructor import Candidate, build_plan
from meridian.risk.engine import DEFAULT_RISK_CONFIG

CFG = {**DEFAULT_RISK_CONFIG, "max_total_open_risk_pct": "0.60"}


def cand(asset, score, qty, rpu, cpu, **kw):
    base = dict(case_id=f"c-{asset}", memo_id="m", asset=asset, instrument_id=f"{asset}-X", direction="LONG",
                score=Decimal(str(score)), reward_risk=Decimal("1.5"), entry="100", stop="95", target="110",
                suggested_qty=Decimal(qty), risk_per_unit=Decimal(str(rpu)),
                capital_per_unit=Decimal(str(cpu)) if cpu is not None else None,
                size_increment=Decimal(1), min_size=Decimal(1), risk_outcome="ALLOW", data_source="COINBASE")
    base.update(kw)
    return Candidate(**base)


def plan(cands, **kw):
    args = dict(equity=Decimal(700), open_risk=Decimal(0), open_capital=Decimal(0), open_capital_unknown=False,
                held_assets={}, cfg=CFG)
    args.update(kw)
    return build_plan(cands, **args)


def test_best_ranked_first_and_capital_never_exceeded():
    p = plan([cand("ETH", 20, 7, 10, 60), cand("BTC", 30, 7, 10, 150), cand("GOLD", 10, 2, 35, 400)])
    assert [a["asset"] for a in p["allocations"]] == ["BTC", "ETH"]
    btc, eth = p["allocations"]
    assert btc["quantity"] == "4" and btc["reduced_from"] == "7"  # $600 of $700 capital
    assert eth["quantity"] == "1"  # only $100 left
    assert Decimal(p["totals"]["planned_capital_usd"]) <= 700
    assert any(sk["asset"] == "GOLD" and sk["reason"] == "NO_ROOM" for sk in p["skipped"])


def test_each_position_risk_at_most_70_and_total_risk_room():
    p = plan([cand("BTC", 30, 7, 10, 10), cand("ETH", 20, 7, 10, 10)], cfg={**CFG, "max_total_open_risk_pct": "0.10"})
    assert len(p["allocations"]) == 1 and Decimal(p["allocations"][0]["risk_usd"]) <= 70
    assert p["skipped"][0]["reason"] == "NO_ROOM"


def test_one_position_per_asset_and_blocks_skipped():
    p = plan([cand("GOLD", 30, 2, 35, 50), cand("BTC", 20, 1, 10, 50, risk_outcome="BLOCK")], held_assets={"GOLD": 1})
    assert p["allocations"] == []
    assert {sk["reason"] for sk in p["skipped"]} == {"ASSET_HELD", "RISK_BLOCK"}


def test_unknown_capital_is_flagged_not_hidden():
    p = plan([cand("GOLD", 30, 2, 35, None)])
    assert p["allocations"][0]["capital_usd"] is None and p["totals"]["capital_unverified"] is True
