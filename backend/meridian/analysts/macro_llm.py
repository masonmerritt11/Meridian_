"""Macro desk analysts backed by the language model with web search. Each reads current news in its mandate
and returns a bias per asset (-1..1) with confidence, a short rationale and the sources it actually retrieved.
They never produce prices, sizes or stops. No key / budget exhausted -> explicit UNAVAILABLE, nothing invented."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from meridian.analysts.base import AnalystOutput, AnalystWorker, Assessment, MarketContext
from meridian.analysts.market import spec
from meridian.research.llm import LLMClient, LLMUnavailable, extract_json
from meridian.workers.base import RunContext

SYSTEM = """You are a macro analyst at MERIDIAN, an advisory-only research desk covering six futures: GOLD, SILVER, OIL (WTI crude),
BTC, ETH and XRP. Use web search to read the latest reliable reporting (central banks, statistics agencies, Reuters, AP,
Bloomberg, FT, WSJ, CNBC, official releases). Report only what sources say; never invent figures or dates.
Today's date matters: prefer reporting from the last 7 days. Keep your own words; at most one short quote per source."""

FORMAT = """Return ONE JSON object in a ```json block:
{"summary": "80-150 words on what matters now in your mandate",
 "assets": {"GOLD": {"bias": -1.0 to 1.0, "confidence": 0.0 to 1.0, "why": "one sentence"},
            "SILVER": {...}, "OIL": {...}, "BTC": {...}, "ETH": {...}, "XRP": {...}},
 "sources": [{"title": "", "url": ""}]}
Bias: + means your mandate's news is a tailwind for the price over the next few days, - a headwind. Use 0 with low confidence
when your mandate has no clear bearing on an asset."""


class MacroLLMAnalyst(AnalystWorker):
    slow = True
    mandate = ""
    min_bars = 0

    def __init__(self, ctx):  # noqa: ANN001
        super().__init__(ctx)
        self.llm: LLMClient = ctx.services.get("llm") or LLMClient(ctx.settings)

    def run(self, s: Session, rc: RunContext) -> AnalystOutput:
        if not self.llm.enabled:
            return AnalystOutput(summary="UNAVAILABLE: ANTHROPIC_API_KEY not set")
        return super().run(s, rc)

    def analyze(self, s: Session, ctx: MarketContext, rc: RunContext) -> list[Assessment]:
        try:
            res = self.llm.complete(s, worker_id=self.spec.id, system=SYSTEM, max_tokens=1800, web=True, max_searches=3,
                                    max_fetches=0, worker_cap=Decimal("0.15"), reserve=Decimal("0.06"),
                                    prompt=f"Your mandate: {self.mandate}\n\n{FORMAT}")
            out = extract_json(res.text)
        except (LLMUnavailable, ValueError) as e:
            rc.metrics["unavailable"] = 1
            return [Assessment("MARKET", "UNAVAILABLE", f"{self.spec.name} unavailable: {e}"[:300], None, None, {})]
        retrieved = {r["url"].rstrip("/") for r in res.retrieved}
        sources = [src for src in out.get("sources", []) if str(src.get("url", "")).rstrip("/") in retrieved][:8]
        items = [Assessment("MARKET", "MACRO_VIEW", str(out.get("summary", ""))[:1500], None, None,
                            {"sources": sources, "cost_usd": str(res.cost_usd), "web": res.used_web})]
        assets: dict[str, Any] = out.get("assets") or {}
        for a in ctx.instruments:
            v = assets.get(a) or {}
            try:
                bias = max(-1.0, min(1.0, float(v.get("bias", 0))))
                conf = max(0.0, min(1.0, float(v.get("confidence", 0))))
            except (TypeError, ValueError):
                continue
            if not sources:  # unsupported views get no weight
                conf = min(conf, 0.1)
            label = "TAILWIND" if bias >= 0.2 else "HEADWIND" if bias <= -0.2 else "NEUTRAL"
            items.append(Assessment(a, label, str(v.get("why", ""))[:500], bias * 0.6, conf, {"sources": sources[:3]}))
        rc.metrics["cost_usd"] = float(res.cost_usd)
        return items


def _make(wid: str, name: str, mandate: str) -> type[MacroLLMAnalyst]:
    return type(wid.replace(".", "_").title().replace("_", ""), (MacroLLMAnalyst,), {
        "spec": spec(wid, "global_macro", name, mandate, every=12 * 3600, esc="exec.chief_macro_officer"),
        "mandate": mandate})


CentralBank = _make("macro.central_bank", "Central Bank Analyst",
                    "Federal Reserve and other major central banks: rate path, guidance, balance sheet, speeches this week.")
Inflation = _make("macro.inflation", "Inflation Analyst", "US inflation: CPI, PCE, PPI, inflation expectations and wage data.")
RatesDollar = _make("macro.rates_dollar", "Rates & Dollar Analyst",
                    "US Treasury yields (2y/10y, real yields) and the US dollar index; their usual effect on gold, silver and crypto.")
Energy = _make("macro.energy", "Energy & Commodities Analyst",
               "Crude oil supply and demand: OPEC+, EIA inventories, production, refinery data, shipping disruptions.")
Geopolitical = _make("macro.geopolitical", "Geopolitical Analyst",
                     "Geopolitical and policy risk: conflicts, sanctions, elections, tariffs, crypto regulation.")

MACRO_LLM_WORKERS = [CentralBank, Inflation, RatesDollar, Energy, Geopolitical]
