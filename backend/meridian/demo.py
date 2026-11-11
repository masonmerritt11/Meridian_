"""End-to-end demonstration on SIMULATED data (clearly labeled). Runs the full pipeline synchronously:
discovery -> ingest -> trend -> case -> macro -> challenge -> risk -> committee memo."""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select

from meridian.core.config import Settings
from meridian.db.models import CommitteeMemo
from meridian.db.session import session_scope
from meridian.marketdata.simulated import SimulatedProvider
from meridian.runtime import Runtime, make_context


def run_demo(settings: Settings, macro: Any = None) -> dict[str, Any]:
    if settings.market_data_mode != "simulated":
        raise RuntimeError("demo requires MARKET_DATA_MODE=simulated")
    from datetime import UTC, datetime, timedelta

    from meridian.marketdata.service import DEMO_ANCHOR_KEY, demo_anchor, set_freshness
    with session_scope() as s:
        anchor = demo_anchor(s)
        if anchor is None:
            now = datetime.now(UTC)
            anchor = datetime.fromtimestamp(int(now.timestamp()) // 3600 * 3600, UTC) - timedelta(hours=1)
            set_freshness(s, DEMO_ANCHOR_KEY, anchor, "FRESH", "SIMULATED", "demo breakout anchor (simulated scenario)", now)
    ctx = make_context(settings, market=SimulatedProvider(scenario="demo", demo_end=anchor), macro=macro)
    rt = Runtime.build(settings, ctx)
    steps = {}
    for wid in ("data.instrument_discovery", "macro.calendar_analyst", "data.market_ingest"):
        try:
            steps[wid] = rt.run_scheduled(wid).model_dump()
        except Exception as e:  # noqa: BLE001 — e.g. macro feed unreachable: pipeline continues with UNAVAILABLE state
            steps[wid] = {"error": repr(e)}
    for wid, w in rt.workers.items():  # analysts publish views before cases are researched
        if w.spec.schedule_seconds and not w.slow and wid.split(".")[0] in ("trend", "stat_arb", "vol", "liquidity", "macro") \
                and wid not in ("macro.calendar_analyst",):
            try:
                rt.run_scheduled(wid)
            except Exception as e:  # noqa: BLE001
                steps[wid] = {"error": repr(e)}
    steps["events_handled"] = rt.drain()
    steps["position_monitor"] = rt.run_scheduled("risk.position_monitor").model_dump()
    with session_scope() as s:
        memos = s.execute(select(CommitteeMemo).order_by(desc(CommitteeMemo.created_at)).limit(10)).scalars()
        steps["memos"] = [{"id": str(m.id), "case_id": str(m.case_id), "headline": m.memo["headline"],
                           "recommendation": m.recommendation} for m in memos]
    steps["label"] = "SIMULATED DATA — demonstration only, not market information"
    return steps
