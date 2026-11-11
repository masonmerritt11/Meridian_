"""Research department (mocked Anthropic API) and analyst workers on simulated data."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from meridian.core.config import Settings
from meridian.db.models import (
    AnalystAssessment,
    CaseEvidence,
    LlmUsage,
    ResearchNote,
    ResearchRecommendation,
    ResearchTopic,
    WorkerRun,
)
from meridian.db.session import session_scope
from meridian.demo import run_demo
from meridian.research.llm import BudgetExceeded, LLMClient
from meridian.research.workers import PsychologyResearcher
from meridian.runtime import Runtime, make_context
from meridian.workers.base import execute
from test_flow import FakeMacro, client, login

pytestmark = pytest.mark.integration

NOTE = {"title": "Disposition effect", "summary": "Traders sell winners too early and hold losers. " * 5,
        "key_ideas": ["a", "b", "c"], "evidence_quality": "Replicated across markets.",
        "applies_to_meridian": "Pre-commit exits.", "contrarian_angle": "The crowd anchors to entry.",
        "sources": [{"title": "Odean 1998", "url": "https://papers.ssrn.com/odean", "author": "Odean", "year": "1998", "kind": "paper"},
                    {"title": "Invented", "url": "https://not-retrieved.example/x", "author": "", "year": "", "kind": "article"}],
        "recommendation": {"title": "Log exit reasons", "proposal": "Require an exit reason when closing.",
                           "rationale": "Counters disposition effect.", "component": "cio_process",
                           "how_to_test": "Compare hold times before/after.", "confidence": "MEDIUM"},
        "next_topics": ["Regret aversion in futures traders"]}


def anthropic_mock(calls: list, note: dict = NOTE, status: int = 200):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["x-api-key"] == "test-key"
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        content = [
            {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "s1",
             "content": [{"type": "web_search_result", "url": "https://papers.ssrn.com/odean", "title": "Odean 1998"}]},
            {"type": "text", "text": "Here is my note:\n```json\n" + json.dumps(note) + "\n```"}]
        return httpx.Response(200, json={"content": content, "stop_reason": "end_turn",
                                          "usage": {"input_tokens": 40_000, "output_tokens": 2_000,
                                                    "server_tool_use": {"web_search_requests": 3}}})
    return httpx.MockTransport(handler)


def research_ctx(st: Settings, transport: httpx.MockTransport, budget: float = 3.0):
    st2 = st.model_copy(update={"anthropic_api_key": "test-key", "llm_daily_budget_usd": budget})
    ctx = make_context(st2, macro=FakeMacro())
    ctx.services["llm"] = LLMClient(st2, transport=transport)
    return ctx


def test_researcher_learns_records_spend_and_drops_fabricated_sources(clean_env):
    calls: list = []
    w = PsychologyResearcher(research_ctx(clean_env, anthropic_mock(calls)))
    out = execute(w)
    assert out.status == "OK" and out.sources_read == 1
    tools = {t["name"] for t in calls[0]["tools"]}
    assert tools == {"web_search", "web_fetch"}
    with session_scope() as s:
        note = s.execute(select(ResearchNote)).scalar_one()
        assert [x["url"] for x in note.sources] == ["https://papers.ssrn.com/odean"]  # invented URL dropped
        rec = s.execute(select(ResearchRecommendation)).scalar_one()
        assert rec.status == "PROPOSED" and rec.component == "cio_process"
        assert s.execute(select(ResearchTopic).where(ResearchTopic.origin == "SUGGESTED")).scalar_one()
        u = s.execute(select(LlmUsage)).scalar_one()
        assert u.cost_usd == Decimal("0.13")  # 40k*$2/M + 2k*$10/M + 3*$0.01
    c = client()
    login(c)
    j = c.get("/api/research/journal").json()
    assert j["notes"][0]["title"] == "Disposition effect" and j["spend"]["today_usd"] == "0.13"
    r = c.post(f"/api/research/recommendations/{j['recommendations'][0]['id']}/decision",
               json={"status": "ACCEPTED", "note": "try it"})
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"


def test_budget_cap_blocks_calls_and_failures_still_count(clean_env):
    calls: list = []
    ctx = research_ctx(clean_env, anthropic_mock(calls), budget=0.50)
    w = PsychologyResearcher(ctx)
    assert execute(w).status == "OK"  # spends $0.13
    assert execute(w).status == "UNAVAILABLE"  # next call would exceed $0.50 (reserve $0.40)
    assert len(calls) == 1
    with session_scope() as s:
        with pytest.raises(BudgetExceeded):
            ctx.services["llm"].check_budget(s, "x", None, Decimal("0.40"))
    # API errors are recorded as failed usage rows
    bad = PsychologyResearcher(research_ctx(clean_env, anthropic_mock([], status=500)))
    assert execute(bad).status == "UNAVAILABLE"
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(LlmUsage).where(LlmUsage.ok.is_(False))).scalar() == 1


def test_no_key_is_explicitly_unavailable(clean_env):
    w = PsychologyResearcher(make_context(clean_env.model_copy(update={"anthropic_api_key": ""}), macro=FakeMacro()))
    assert execute(w).status == "UNAVAILABLE"


def test_all_analysts_run_and_attach_evidence(clean_env):
    run_demo(clean_env, macro=FakeMacro())
    rt = Runtime.build(clean_env, make_context(clean_env, macro=FakeMacro()))
    for wid, w in rt.workers.items():
        if w.spec.schedule_seconds and not w.slow:
            rt.run_scheduled(wid)
    with session_scope() as s:
        failed = s.execute(select(WorkerRun.worker_id, WorkerRun.error).where(WorkerRun.status == "FAILED")).all()
        assert failed == [], failed
        workers = {r[0] for r in s.execute(select(AnalystAssessment.worker_id).distinct())}
        for must in ("trend.multi_timeframe", "vol.regime", "portfolio.sizer", "risk.compliance", "data.reliability",
                     "exec.cio_liaison", "stat_arb.pairs", "macro.regime", "portfolio.attribution"):
            assert must in workers, must
        comp = s.execute(select(AnalystAssessment).where(AnalystAssessment.worker_id == "risk.compliance")).scalars().first()
        assert comp.label == "PASS", comp.summary
        kinds = {e.kind for e in s.execute(select(CaseEvidence)).scalars()}
        assert {"TREND", "VOLATILITY"} <= kinds
    c = client()
    login(c)
    d = c.get("/api/workers/vol.regime/detail").json()
    assert d["worker"]["id"] == "vol.regime" and d["latest_outputs"] and d["stats"]["runs_24h"] >= 1
