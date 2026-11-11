"""Trading Psychology & Philosophy Research department.

Two researchers read continuously (one topic per run) and a Research Director writes a daily synthesis.
They LEARN and RECOMMEND only: notes and proposals go to the Learning Journal for the CIO. Nothing they
produce changes strategies, risk limits, sizing or recommendations automatically.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from meridian.core import audit
from meridian.core.util import utcnow
from meridian.db.models import ResearchNote, ResearchRecommendation, ResearchTopic
from meridian.research.llm import LLMClient, LLMUnavailable, complete_json, extract_json, fetch_arxiv
from meridian.workers.base import BaseWorker, RunContext, WorkerOutput, WorkerSpec

COMPONENTS = ("trend", "stat_arb", "macro", "volatility", "liquidity", "risk", "portfolio", "committee",
              "cio_process", "research", "other")

SYSTEM_CONTEXT = """You are a researcher in MERIDIAN, an advisory-only trading research firm run for one human CIO.
MERIDIAN watches six Coinbase Derivatives futures: gold, silver, crude oil, bitcoin, ether and XRP, on hourly bars.
Account: about $700. Hard rules you must respect and never propose loosening: maximum $70 loss per position,
one position per asset, a deterministic risk engine whose vetoes are final, and the human CIO places every trade.
Current strategy: EMA(20/50) trend filter + 20-bar breakout, 2xATR stop, 3xATR target; a committee ranks ideas
by evidence for and against; a portfolio constructor allocates capital best-first.
The CIO's goal for this department: not to 'beat the market' with cleverness, but to learn to think unlike the crowd -
second-level, contrarian where justified, disciplined - and to find ideas that fit this system.

Rules for your work:
- Read real sources. Prefer peer-reviewed papers, working papers (SSRN, arXiv, NBER), books by recognized
  practitioners, and reputable long-form articles. Avoid SEO listicles and marketing.
- Summarize in your own words. Never copy long passages; at most one short quote (under 15 words) per source.
- Only cite sources you actually opened or that appeared in your search results in this session.
- Separate what the evidence shows from opinion; say plainly when evidence is weak or mixed.
- Never invent statistics. If you report a number, it must come from a source you cite.
- Recommendations must be concrete, testable, and compatible with the hard rules above."""

OUTPUT_SPEC = """Finish with ONE JSON object in a ```json code block, exactly this shape:
{"title": "short title of what you learned",
 "summary": "120-200 words, your own words",
 "key_ideas": ["3-6 crisp ideas"],
 "evidence_quality": "how strong/replicated the evidence is, in 1-3 sentences",
 "applies_to_meridian": "how this could fit or challenge MERIDIAN, 2-4 sentences",
 "contrarian_angle": "what the crowd usually gets wrong here, 1-3 sentences",
 "sources": [{"title": "", "url": "", "author": "", "year": "", "kind": "paper|book|article|talk"}],
 "recommendation": null or {"title": "", "proposal": "what to change or try", "rationale": "why",
    "component": "one of: trend, stat_arb, macro, volatility, liquidity, risk, portfolio, committee, cio_process, research, other",
    "how_to_test": "how MERIDIAN could test it before adopting it", "confidence": "LOW|MEDIUM|HIGH"},
 "next_topics": ["0-3 follow-up topics worth reading next"]}"""

SEEDS: dict[str, list[str]] = {
    "research.psychology": [
        "Second-level thinking and contrarian judgment (Howard Marks) applied to trading decisions",
        "Overreaction and underreaction in markets: De Bondt & Thaler and later evidence",
        "The disposition effect: why traders sell winners early and hold losers (Shefrin & Statman, Odean)",
        "Overconfidence and excessive trading: Barber & Odean evidence",
        "Herding and information cascades in commodity and crypto markets",
        "Investor sentiment extremes as contrarian signals: evidence for and against",
        "Prospect theory and loss aversion: implications for stop-loss discipline",
        "Probabilistic thinking and process over outcome (Mark Douglas, Annie Duke 'Thinking in Bets')",
        "Tilt, revenge trading and the case for rule-based circuit breakers",
        "Reflexivity (George Soros) and boom-bust dynamics in crypto",
        "Narrative economics (Robert Shiller): how stories move commodity and crypto prices",
        "Decision journals and pre-mortems for discretionary traders",
        "Recency bias and trend extrapolation by retail traders",
        "Crowded trades and positioning data (COT reports, futures funding, open interest) as contrarian context",
        "Fear of missing out (FOMO) in crypto rallies: behavioral evidence",
    ],
    "research.philosophy": [
        "Time-series momentum across futures (Moskowitz, Ooi & Pedersen 2012) and its fit for gold, oil and crypto",
        "Trend following philosophy and its long-run evidence (Hurst, Ooi & Pedersen 'A Century of Evidence')",
        "Volatility targeting and position sizing by ATR: evidence and pitfalls",
        "Kelly criterion and fractional Kelly for small accounts",
        "Carry and term structure in commodity futures: contango, backwardation and roll yield",
        "Gold/silver ratio mean reversion: evidence and practical trading rules",
        "Antifragility and barbell thinking (Nassim Taleb) for a small trading account",
        "Asymmetric payoff design: cutting losers fast and letting winners run, with evidence",
        "The Turtle Traders' rules: what held up and what did not",
        "Common principles of Market Wizards interviewees (Schwager) and which are evidence-based",
        "Regime detection: when trend following fails (choppy ranges) and filters that help",
        "Basis and funding-rate trades in crypto futures: risks for small accounts",
        "Ed Thorp's approach to edge, sizing and risk of ruin",
        "Ray Dalio's principles on diversification across uncorrelated return streams",
        "Mean reversion versus momentum by time horizon in commodities and crypto",
    ],
}

WORKER_CAPS = {"research.psychology": Decimal("1.10"), "research.philosophy": Decimal("1.10"),
               "research.director": Decimal("0.25")}


class SourceModel(BaseModel):
    title: str = ""
    url: str = ""
    author: str = ""
    year: str = ""
    kind: str = ""


class RecModel(BaseModel):
    title: str
    proposal: str
    rationale: str
    component: str = "other"
    how_to_test: str = ""
    confidence: str = "LOW"


class NoteModel(BaseModel):
    title: str
    summary: str
    key_ideas: list[str] = Field(default_factory=list)
    evidence_quality: str = ""
    applies_to_meridian: str = ""
    contrarian_angle: str = ""
    sources: list[SourceModel] = Field(default_factory=list)
    recommendation: RecModel | None = None
    next_topics: list[str] = Field(default_factory=list)


class ResearchOutput(WorkerOutput):
    status: str = "UNAVAILABLE"
    topic: str | None = None
    note_id: str | None = None
    sources_read: int = 0
    recommendation: str | None = None
    cost_usd: str = "0"


def ensure_seeds(s: Session) -> None:
    for wid, topics in SEEDS.items():
        existing = {t.topic.lower() for t in s.execute(select(ResearchTopic).where(ResearchTopic.worker_id == wid)).scalars()}
        for i, t in enumerate(topics):
            if t.lower() not in existing:
                s.add(ResearchTopic(worker_id=wid, topic=t, origin="SEED", priority=100 + i))
    s.flush()


def _norm_url(u: str) -> str:
    return u.strip().rstrip("/").split("#")[0].replace("http://", "https://")


def validate_sources(claimed: list[SourceModel], retrieved: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Keep only sources the model actually retrieved this session (prevents fabricated citations)."""
    got = {_norm_url(r["url"]): r for r in retrieved}
    out = []
    for c in claimed:
        r = got.get(_norm_url(c.url))
        if r is not None:
            out.append({**c.model_dump(), "verified": True, "opened": r.get("fetched") == "true"})
    if not out:  # model forgot to list them; fall back to what it opened
        out = [{"title": r.get("title", ""), "url": r["url"], "author": "", "year": "", "kind": "article",
                "verified": True, "opened": r.get("fetched") == "true"} for r in retrieved if r.get("fetched") == "true"][:6]
    return out


class ResearcherWorker(BaseWorker):
    """Shared logic for the psychology and philosophy researchers."""
    slow = True
    mandate: str = ""

    def __init__(self, ctx):  # noqa: ANN001
        super().__init__(ctx)
        self.llm: LLMClient = ctx.services.get("llm") or LLMClient(ctx.settings)

    def run(self, s: Session, rc: RunContext) -> ResearchOutput:
        wid = self.spec.id
        ensure_seeds(s)
        if not self.llm.enabled:
            return ResearchOutput(summary="UNAVAILABLE: ANTHROPIC_API_KEY not set", status="UNAVAILABLE")
        topic = s.execute(select(ResearchTopic).where(ResearchTopic.worker_id == wid, ResearchTopic.status == "QUEUED")
                          .order_by(ResearchTopic.priority, ResearchTopic.id).limit(1)).scalar()
        if topic is None:
            return ResearchOutput(summary="topic queue empty", status="IDLE")
        rc.ref("research_topic", str(topic.id))
        seen = [n.sources for n in s.execute(select(ResearchNote).where(ResearchNote.worker_id == wid)
                                             .order_by(desc(ResearchNote.created_at)).limit(40)).scalars()]
        seen_urls = sorted({src.get("url") for lst in seen for src in lst if src.get("url")})[:60]
        prompt = (f"{self.mandate}\n\nTopic for this session: {topic.topic}\n\n"
                  "Search for 2-4 strong sources on this topic and open the single most important one with web_fetch "
                  "(prefer an HTML article or abstract page over a long PDF). Keep your working notes brief: your final "
                  "message must be the JSON note described below, and nothing after it.\n"
                  + (f"Already read (do not repeat): {seen_urls}\n" if seen_urls else "")
                  + "\n" + OUTPUT_SPEC)
        try:
            if LLMClient.web_tools_disabled:  # account has no web tools: read arXiv abstracts instead
                papers = fetch_arxiv(topic.topic.split(":")[0][:120], max_results=4)
                obj, res = complete_json(
                    self.llm, s, worker_id=wid, system=SYSTEM_CONTEXT, spec=OUTPUT_SPEC, max_tokens=6000,
                    worker_cap=WORKER_CAPS.get(wid),
                    prompt=f"{self.mandate}\n\nTopic: {topic.topic}\n\nYou cannot browse. Learn from these paper abstracts "
                           f"(and only cite these):\n{papers}\n\n{OUTPUT_SPEC}")
                res.retrieved = [{"url": p["url"], "title": p["title"], "fetched": "true"} for p in papers]
            else:
                obj, res = complete_json(self.llm, s, worker_id=wid, system=SYSTEM_CONTEXT, prompt=prompt, spec=OUTPUT_SPEC,
                                         max_tokens=6000, web=True, max_searches=3, max_fetches=1,
                                         worker_cap=WORKER_CAPS.get(wid), reserve=Decimal("0.35"))
        except LLMUnavailable as e:
            rc.metrics["unavailable"] = 1
            return ResearchOutput(summary=f"UNAVAILABLE: {e}"[:300], status="UNAVAILABLE", topic=topic.topic)
        except ValueError as e:  # even the repair produced no JSON
            topic.status, topic.done_at = "FAILED", utcnow()
            rc.metrics["parse_failed"] = 1
            return ResearchOutput(summary=f"note could not be parsed: {e}"[:300], status="PARSE_FAILED", topic=topic.topic)
        try:
            note = NoteModel.model_validate(obj)
        except ValidationError as e:
            topic.status, topic.done_at = "FAILED", utcnow()  # do not pay to retry the same failure forever
            rc.metrics["parse_failed"] = 1
            return ResearchOutput(summary=f"note did not match the format: {e}"[:300], status="PARSE_FAILED",
                                  topic=topic.topic, cost_usd=str(res.cost_usd))
        sources = validate_sources(note.sources, res.retrieved)
        row = ResearchNote(worker_id=wid, topic=topic.topic, title=note.title[:300], summary=note.summary,
                           key_ideas=note.key_ideas[:8], evidence_quality=note.evidence_quality,
                           applies_to_meridian=note.applies_to_meridian, contrarian_angle=note.contrarian_angle,
                           sources=sources, run_id=rc.run_id, cost_usd=res.cost_usd)
        s.add(row)
        s.flush()
        rec_title = None
        if note.recommendation is not None:
            r = note.recommendation
            s.add(ResearchRecommendation(worker_id=wid, note_id=row.id, title=r.title[:300], proposal=r.proposal,
                                         rationale=r.rationale, component=r.component if r.component in COMPONENTS else "other",
                                         how_to_test=r.how_to_test,
                                         confidence=r.confidence.upper() if r.confidence.upper() in ("LOW", "MEDIUM", "HIGH") else "LOW"))
            rec_title = r.title
        topic.status, topic.done_at = "DONE", utcnow()
        queued = {t.topic.lower() for t in s.execute(select(ResearchTopic).where(ResearchTopic.worker_id == wid)).scalars()}
        for nt in note.next_topics[:3]:
            if nt and nt.lower() not in queued:
                s.add(ResearchTopic(worker_id=wid, topic=nt[:500], origin="SUGGESTED", priority=150))
        audit.record(s, f"{wid}@{self.spec.version}", "research.note", "research_note", row.id,
                     {"topic": topic.topic, "sources": len(sources), "recommendation": rec_title,
                      "cost_usd": str(res.cost_usd)})
        rc.metrics.update({"sources": len(sources), "cost_usd": float(res.cost_usd),
                           "input_tokens": res.input_tokens, "output_tokens": res.output_tokens})
        return ResearchOutput(summary=f"learned: {note.title}"[:300], status="OK", topic=topic.topic,
                              note_id=str(row.id), sources_read=len(sources), recommendation=rec_title,
                              cost_usd=str(res.cost_usd))


class PsychologyResearcher(ResearcherWorker):
    spec = WorkerSpec(id="research.psychology", department="research", name="Trading Psychology Researcher",
                      responsibility="Reads trading-psychology research and writes learning notes + recommendations.",
                      version="1.0.0", output_model=ResearchOutput, schedule_seconds=3 * 3600, max_retries=1,
                      escalation_to="research.director")
    mandate = ("Your field is trading psychology and behavioral finance: biases, crowd behavior, discipline, "
               "and how to think differently from the crowd without fooling yourself.")


class PhilosophyResearcher(ResearcherWorker):
    spec = WorkerSpec(id="research.philosophy", department="research", name="Trading Philosophy Researcher",
                      responsibility="Reads trading philosophies and strategy research; finds ideas that fit MERIDIAN.",
                      version="1.0.0", output_model=ResearchOutput, schedule_seconds=3 * 3600, max_retries=1,
                      escalation_to="research.director")
    mandate = ("Your field is trading philosophy and strategy research: schools of thought, systematic strategies "
               "and the evidence behind them, judged by how well they could fit MERIDIAN's instruments and rules.")


class DirectorOutput(WorkerOutput):
    status: str = "UNAVAILABLE"
    notes_reviewed: int = 0


class ResearchDirector(BaseWorker):
    slow = True
    spec = WorkerSpec(id="research.director", department="research", name="Research Director",
                      responsibility="Daily synthesis of what the department learned; ranks open recommendations.",
                      version="1.0.0", output_model=DirectorOutput, schedule_seconds=6 * 3600, max_retries=1,
                      escalation_to="cio")

    def __init__(self, ctx):  # noqa: ANN001
        super().__init__(ctx)
        self.llm: LLMClient = ctx.services.get("llm") or LLMClient(ctx.settings)

    def run(self, s: Session, rc: RunContext) -> DirectorOutput:
        if not self.llm.enabled:
            return DirectorOutput(summary="UNAVAILABLE: ANTHROPIC_API_KEY not set")
        since = utcnow() - timedelta(hours=24)
        notes = list(s.execute(select(ResearchNote).where(ResearchNote.kind == "NOTE", ResearchNote.created_at >= since)
                               .order_by(ResearchNote.created_at)).scalars())
        if not notes:
            return DirectorOutput(summary="no new notes in the last 24h", status="IDLE")
        recs = list(s.execute(select(ResearchRecommendation).where(ResearchRecommendation.status == "PROPOSED")).scalars())
        digest = [{"by": n.worker_id, "title": n.title, "summary": n.summary, "ideas": n.key_ideas,
                   "contrarian": n.contrarian_angle, "evidence": n.evidence_quality} for n in notes]
        prompt = ("Write today's synthesis for the CIO from these research notes. Be honest about weak evidence.\n"
                  f"Notes: {digest}\nOpen recommendations: {[r.title for r in recs]}\n\n"
                  'Return ONE JSON object in a ```json block: {"title": "What we learned: <theme>", '
                  '"summary": "150-250 words", "key_ideas": ["3-6 takeaways"], '
                  '"evidence_quality": "overall evidence assessment", "applies_to_meridian": "what to consider doing", '
                  '"contrarian_angle": "the most non-consensus insight today", '
                  '"top_recommendations": ["up to 3 titles from the open list, best first"]}')
        try:
            out, res = complete_json(self.llm, s, worker_id=self.spec.id, system=SYSTEM_CONTEXT, prompt=prompt,
                                     spec="the JSON object described above", max_tokens=3000,
                                     worker_cap=WORKER_CAPS[self.spec.id], reserve=Decimal("0.05"))
        except (LLMUnavailable, ValueError) as e:
            return DirectorOutput(summary=f"UNAVAILABLE: {e}"[:300])
        s.add(ResearchNote(worker_id=self.spec.id, topic="daily synthesis", title=str(out.get("title", "Daily synthesis"))[:300],
                           summary=str(out.get("summary", "")), key_ideas=list(out.get("key_ideas", []))[:8],
                           evidence_quality=str(out.get("evidence_quality", "")),
                           applies_to_meridian=str(out.get("applies_to_meridian", "")),
                           contrarian_angle=str(out.get("contrarian_angle", "")),
                           sources=[{"title": n.title, "url": f"note:{n.id}", "kind": "note", "verified": True} for n in notes],
                           kind="SYNTHESIS", run_id=rc.run_id, cost_usd=res.cost_usd))
        rc.metrics["notes_reviewed"] = len(notes)
        return DirectorOutput(summary=f"synthesized {len(notes)} notes", status="OK", notes_reviewed=len(notes))


RESEARCH_WORKERS = [PsychologyResearcher, PhilosophyResearcher, ResearchDirector]
