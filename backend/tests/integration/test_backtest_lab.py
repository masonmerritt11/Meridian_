"""Backtest lab end to end with a fake hourly feed (no network)."""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from meridian.backtest.workers import BtHistorian, BtStrategyLab, BtTradeTester, live_trend_config
from meridian.db.models import BtCandle, BtRun, RecSim, StrategyProposal, WorkerDefinition
from meridian.db.session import session_scope
from meridian.runtime import make_context
from meridian.workers.base import execute
from test_flow import FakeMacro, client, login, run_demo

pytestmark = pytest.mark.integration


class FakeHourly:
    def __init__(self):
        self.calls = 0

    def hourly(self, product, start, end):  # noqa: ANN001
        self.calls += 1
        rng = random.Random(hash(product) % 1000)
        out, t = [], start.replace(minute=0, second=0, microsecond=0)
        base = 100 + (hash(product) % 50)
        while t < end:
            k = int(t.timestamp() // 3600)
            px = base * (1 + 0.05 * __import__("math").sin(k / 90.0)) * (1 + 0.002 * ((k * 7919) % 13 - 6) / 6)
            out.append((t, px * 0.999, px * 1.004, px * 0.996, px))
            t += timedelta(hours=1)
        return out if rng.random() >= 0 else []


def test_history_loader_and_lab_run(clean_env):
    ctx = make_context(clean_env, macro=FakeMacro())
    ctx.services["bt_source"] = FakeHourly()
    with session_scope() as s:
        w = s.get(WorkerDefinition, "bt.historian")
        w.config = {**w.config, "history_days": 60, "max_requests": 60}
    out = execute(BtHistorian(ctx), trigger="MANUAL")
    assert out.bars_added > 4 * 1000
    with session_scope() as s:
        assert s.execute(select(func.count(func.distinct(BtCandle.product)))).scalar() >= 4
    lab = execute(BtStrategyLab(ctx), trigger="MANUAL")
    assert lab.tested > 30
    with session_scope() as s:
        run = s.execute(select(BtRun)).scalar_one()
        assert run.result["baseline"]["all"]["n"] >= 0 and run.result["variants"] and run.result["coverage"]


def test_accept_and_revert_a_proposal(clean_env):
    with session_scope() as s:
        _, ver, cfg = live_trend_config(s)
        p = StrategyProposal(key="stop_atr=2.5", title="Stop at 2.5x ATR (now 2.0x)", overrides={"stop_atr": 2.5},
                             base_config=cfg, base_config_version=ver, evidence={"passed": True})
        other = StrategyProposal(key="htf_align=True", title="x", overrides={"htf_align": True}, base_config=cfg,
                                 base_config_version=ver, evidence={})
        s.add_all([p, other])
        s.flush()
        pid, oid = p.id, other.id
    c = client()
    login(c)
    assert c.post(f"/api/backtest/proposals/{pid}/decision", json={"status": "ACCEPTED", "password": "wrong"}).status_code == 403
    r = c.post(f"/api/backtest/proposals/{pid}/decision", json={"status": "ACCEPTED", "password": "test-password-123"})
    assert r.status_code == 200, r.text
    with session_scope() as s:
        params, ver2, _ = live_trend_config(s)
        assert params.stop_atr == 2.5 and ver2 == ver + 1
        assert s.get(StrategyProposal, oid).status == "SUPERSEDED"
    ov = c.get("/api/backtest/overview").json()
    assert ov["live_config"]["stop_atr"] == 2.5
    r = c.post(f"/api/backtest/proposals/{pid}/revert", json={"password": "test-password-123"})
    assert r.status_code == 200
    with session_scope() as s:
        params, _, _ = live_trend_config(s)
        assert params.stop_atr == 2.0 and s.get(StrategyProposal, pid).status == "REVERTED"


def test_trade_tester_paper_tests_every_recommendation(clean_env):
    run_demo(clean_env, macro=FakeMacro())
    ctx = make_context(clean_env, macro=FakeMacro())
    out = execute(BtTradeTester(ctx), trigger="MANUAL")
    assert out.tested >= 1
    with session_scope() as s:
        rows = list(s.execute(select(RecSim)).scalars())
        assert rows and all(r.status in ("PENDING", "NOT_FILLED", "FILLED") for r in rows)
    c = client()
    login(c)
    summ = c.get("/api/backtest/recommendations").json()
    assert set(summ["groups"]) == {"RECOMMEND", "BLOCKED", "REJECT", "DEFER"} and summ["rows"]


def test_loader_skips_bad_markets_and_demo_instruments(clean_env):
    from meridian.backtest.workers import products
    from meridian.db.models import Instrument

    class Flaky(FakeHourly):
        def hourly(self, product, start, end):  # noqa: ANN001
            if product == "ETH-USD":
                raise RuntimeError("boom")
            return super().hourly(product, start, end)

    with session_scope() as s:
        for iid, src in (("SIM-OIL", "SIMULATED"), ("NOL-19NOV26-CDE", "COINBASE")):
            s.add(Instrument(id=iid, asset="OIL", venue="x", product_type="FUTURE", display_name=iid, quote_currency="USD",
                             source=src, status="ACTIVE"))
    with session_scope() as s:
        ps = [p for _, p in products(s)]
        assert "SIM-OIL" not in ps and "NOL-19NOV26-CDE" in ps
        w = s.get(WorkerDefinition, "bt.historian")
        w.config = {**w.config, "history_days": 20, "max_requests": 40}
    ctx = make_context(clean_env, macro=FakeMacro())
    ctx.services["bt_source"] = Flaky()
    out = execute(BtHistorian(ctx), trigger="MANUAL")
    assert out.bars_added > 0 and "ETH-USD" in out.summary
