"""Minimal Anthropic Messages API client for MERIDIAN's language-model workers.

Guarantees:
  * No key -> LLMUnavailable (workers record an explicit UNAVAILABLE state; nothing is invented).
  * A hard daily spend cap (LLM_DAILY_BUDGET_USD, default $3) plus per-worker caps, checked before every call.
  * Every call is recorded in llm_usage (tokens, searches, estimated cost, errors).
  * Only sources the model actually retrieved through the web tools are returned as citable; anything the
    model cites that it did not retrieve is dropped by the caller.
Language-model output is research commentary only. It never sets prices, sizes, stops or risk decisions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from meridian.core.config import Settings
from meridian.core.logging import get_logger
from meridian.core.util import utcnow
from meridian.db.models import LlmUsage

log = get_logger("meridian.llm")
API_URL = "https://api.anthropic.com/v1/messages"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch"}
WEB_FETCH_BETA = "web-fetch-2025-09-10"


class LLMUnavailable(Exception):
    pass


class BudgetExceeded(LLMUnavailable):
    pass


@dataclass
class LLMResult:
    text: str
    retrieved: list[dict[str, str]] = field(default_factory=list)  # [{url, title}] actually retrieved by tools
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0
    cost_usd: Decimal = Decimal(0)
    used_web: bool = False


def spent_today(s: Session, worker_id: str | None = None) -> Decimal:
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(LlmUsage.created_at >= since)
    if worker_id:
        q = q.where(LlmUsage.worker_id == worker_id)
    return Decimal(str(s.execute(q).scalar() or 0))


def spent_since(s: Session, hours: int) -> Decimal:
    q = select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(LlmUsage.created_at >= utcnow() - timedelta(hours=hours))
    return Decimal(str(s.execute(q).scalar() or 0))


def extract_json(text: str) -> dict[str, Any]:
    """Take the last JSON object in the model's answer (it may think aloud before it)."""
    m = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = m or []
    if not candidates:
        depth, begin = 0, None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    begin = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and begin is not None:
                    candidates.append(text[begin:i + 1])
    for c in reversed(candidates):
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("model answer contained no JSON object")


class LLMClient:
    web_tools_disabled = False  # flipped if the account rejects server tools

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.st = settings
        self._http = httpx.Client(timeout=300.0, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.st.anthropic_api_key)

    def _cost(self, inp: int, out: int, searches: int) -> Decimal:
        return (Decimal(inp) * Decimal(str(self.st.llm_price_input_per_mtok)) / Decimal(1_000_000)
                + Decimal(out) * Decimal(str(self.st.llm_price_output_per_mtok)) / Decimal(1_000_000)
                + Decimal(searches) * Decimal(str(self.st.llm_price_per_web_search))).quantize(Decimal("0.000001"))

    def check_budget(self, s: Session, worker_id: str, worker_cap: Decimal | None, reserve: Decimal) -> None:
        total = spent_today(s)
        cap = Decimal(str(self.st.llm_daily_budget_usd))
        if total + reserve > cap:
            raise BudgetExceeded(f"daily LLM budget ${cap} reached (spent ${total:.2f})")
        if worker_cap is not None and spent_today(s, worker_id) + reserve > worker_cap:
            raise BudgetExceeded(f"{worker_id} daily share ${worker_cap} reached")

    def complete(self, s: Session, *, worker_id: str, system: str, prompt: str, max_tokens: int = 3000,
                 web: bool = False, max_searches: int = 4, max_fetches: int = 3,
                 worker_cap: Decimal | None = None, reserve: Decimal = Decimal("0.15")) -> LLMResult:
        if not self.enabled:
            raise LLMUnavailable("ANTHROPIC_API_KEY not set")
        self.check_budget(s, worker_id, worker_cap, reserve)
        use_web = web and not LLMClient.web_tools_disabled
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        result = LLMResult(text="", used_web=use_web)
        try:
            for _ in range(4):  # handle pause_turn continuations for long server-tool runs
                body: dict[str, Any] = {"model": self.st.llm_model, "max_tokens": max_tokens, "system": system,
                                        "messages": messages}
                headers = {"x-api-key": self.st.anthropic_api_key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"}
                if use_web:
                    body["tools"] = [{**WEB_SEARCH_TOOL, "max_uses": max_searches}]
                    if max_fetches > 0:
                        body["tools"].append({**WEB_FETCH_TOOL, "max_uses": max_fetches})
                        headers["anthropic-beta"] = WEB_FETCH_BETA
                r = self._http.post(API_URL, headers=headers, json=body)
                if r.status_code == 400 and use_web and "tool" in r.text.lower():
                    log.warning(f"web tools rejected by API; continuing without them: {r.text[:300]}")
                    LLMClient.web_tools_disabled = True
                    use_web = result.used_web = False
                    continue
                if r.status_code != 200:
                    raise LLMUnavailable(f"Anthropic API HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                self._accumulate(result, data)
                if data.get("stop_reason") == "pause_turn":
                    messages = [messages[0], {"role": "assistant", "content": data["content"]}]
                    continue
                break
            result.cost_usd = self._cost(result.input_tokens, result.output_tokens, result.web_searches)
            self._record(s, worker_id, result, None)
            log.info(f"llm call ok: {worker_id} ${result.cost_usd} in={result.input_tokens} out={result.output_tokens} "
                     f"searches={result.web_searches} fetches={result.web_fetches} sources={len(result.retrieved)} web={result.used_web}")
            return result
        except Exception as e:  # noqa: BLE001
            result.cost_usd = self._cost(result.input_tokens, result.output_tokens, result.web_searches)
            self._record(s, worker_id, result, repr(e)[:500])
            log.warning(f"llm call failed: {worker_id}: {e!r}"[:500])
            if isinstance(e, LLMUnavailable):
                raise
            raise LLMUnavailable(repr(e)) from e

    @staticmethod
    def _accumulate(res: LLMResult, data: dict[str, Any]) -> None:
        u = data.get("usage") or {}
        res.input_tokens += int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0) or 0) \
            + int(u.get("cache_read_input_tokens", 0) or 0)
        res.output_tokens += int(u.get("output_tokens", 0))
        stu = u.get("server_tool_use") or {}
        res.web_searches += int(stu.get("web_search_requests", 0) or 0)
        res.web_fetches += int(stu.get("web_fetch_requests", 0) or 0)
        texts = []
        seen = {d["url"] for d in res.retrieved}
        for block in data.get("content") or []:
            t = block.get("type")
            if t == "text":
                texts.append(block.get("text", ""))
            elif t == "web_search_tool_result" and isinstance(block.get("content"), list):
                for item in block["content"]:
                    if item.get("url") and item["url"] not in seen:
                        res.retrieved.append({"url": item["url"], "title": item.get("title", "")})
                        seen.add(item["url"])
            elif t == "web_fetch_tool_result" and isinstance(block.get("content"), dict):
                c = block["content"]
                if c.get("type") == "web_fetch_result" and c.get("url") and c["url"] not in seen:
                    title = ((c.get("content") or {}).get("title")) or ""
                    res.retrieved.append({"url": c["url"], "title": title, "fetched": "true"})
                    seen.add(c["url"])
        if texts:
            res.text = (res.text + "\n" + "\n".join(texts)).strip()

    def _record(self, s: Session, worker_id: str, r: LLMResult, err: str | None) -> None:
        """Spend is committed in its own transaction so it counts against the cap even if the run later fails."""
        own = Session(bind=s.get_bind())
        try:
            own.add(LlmUsage(worker_id=worker_id, model=self.st.llm_model, input_tokens=r.input_tokens,
                             output_tokens=r.output_tokens, web_searches=r.web_searches, web_fetches=r.web_fetches,
                             cost_usd=r.cost_usd, ok=err is None, error=err))
            own.commit()
        finally:
            own.close()


def fetch_arxiv(query: str, max_results: int = 4, transport: httpx.BaseTransport | None = None) -> list[dict[str, str]]:
    """Fallback source when web tools are unavailable: arXiv abstracts (open access, Atom feed)."""
    import xml.etree.ElementTree as ET
    r = httpx.get("https://export.arxiv.org/api/query", timeout=30.0,
                  params={"search_query": f"all:{query}", "max_results": max_results, "sortBy": "relevance"},
                  transport=transport) if transport is None else httpx.Client(transport=transport).get(
        "https://export.arxiv.org/api/query", params={"search_query": f"all:{query}", "max_results": max_results})
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        out.append({"url": (e.findtext("a:id", default="", namespaces=ns) or "").strip(),
                    "title": " ".join((e.findtext("a:title", default="", namespaces=ns) or "").split()),
                    "abstract": " ".join((e.findtext("a:summary", default="", namespaces=ns) or "").split())[:2500],
                    "authors": ", ".join(a.findtext("a:name", default="", namespaces=ns) for a in e.findall("a:author", ns))[:200],
                    "year": (e.findtext("a:published", default="", namespaces=ns) or "")[:4]})
    return out
