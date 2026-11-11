"""Analyst framework: scheduled workers that publish assessments (label, directional score, summary, data)
per asset or for MARKET / PORTFOLIO / SYSTEM. Assessments are attached to research cases as evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from meridian.core.util import jsonable, utcnow
from meridian.db.models import AnalystAssessment, Instrument
from meridian.marketdata import service as md
from meridian.marketdata.types import ASSETS
from meridian.workers.base import BaseWorker, RunContext, WorkerOutput


@dataclass
class Assessment:
    subject: str
    label: str
    summary: str
    score: float | None = None  # -1..1 directional bias, + = bullish
    confidence: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    stance: str | None = None  # direction-agnostic stance override: SUPPORTS / OPPOSES (e.g. choppy regime)


class AnalystOutput(WorkerOutput):
    assessments: int = 0
    labels: dict[str, str] = {}


class MarketContext:
    """Loads active instruments and their recent closed candles once per run."""

    def __init__(self, s: Session, granularity: str, bars: int = 600):
        self.s, self.gran = s, granularity
        self.instruments: dict[str, Instrument] = {i.asset: i for i in md.active_instruments(s) if i.asset in ASSETS}
        self._bars: dict[str, list[Any]] = {}
        self._n = bars

    def bars(self, asset: str) -> list[Any]:
        if asset not in self._bars:
            inst = self.instruments.get(asset)
            self._bars[asset] = md.load_candles(self.s, inst.id, self.gran, limit=self._n) if inst else []
        return self._bars[asset]

    def closes(self, asset: str) -> list[float]:
        return [float(b.close) for b in self.bars(asset)]

    def highs(self, asset: str) -> list[float]:
        return [float(b.high) for b in self.bars(asset)]

    def lows(self, asset: str) -> list[float]:
        return [float(b.low) for b in self.bars(asset)]

    def source(self, asset: str | None = None) -> str:
        if asset and asset in self.instruments:
            return self.instruments[asset].source
        srcs = {i.source for i in self.instruments.values()}
        return srcs.pop() if len(srcs) == 1 else "MIXED"


class AnalystWorker(BaseWorker):
    min_bars = 60

    def analyze(self, s: Session, ctx: MarketContext, rc: RunContext) -> list[Assessment]:
        raise NotImplementedError

    def per_asset(self, ctx: MarketContext, fn) -> list[Assessment]:  # noqa: ANN001
        out = []
        for asset in ctx.instruments:
            if len(ctx.bars(asset)) >= self.min_bars:
                a = fn(asset)
                if a is not None:
                    out.append(a)
        return out

    def run(self, s: Session, rc: RunContext) -> AnalystOutput:
        ctx = MarketContext(s, self.ctx.settings.candle_granularity)
        items = self.analyze(s, ctx, rc)
        for a in items:
            s.add(AnalystAssessment(worker_id=self.spec.id, subject=a.subject[:32], label=a.label[:48],
                                    score=round(a.score, 4) if a.score is not None else None,
                                    confidence=round(a.confidence, 4) if a.confidence is not None else None,
                                    summary=a.summary[:2000], data=jsonable({**a.data, "stance": a.stance}),
                                    data_source=ctx.source(a.subject if a.subject in ASSETS else None)))
        rc.metrics["assessments"] = len(items)
        labels = {a.subject: a.label for a in items}
        return AnalystOutput(summary="; ".join(f"{k}: {v}" for k, v in list(labels.items())[:8]) or "nothing to assess",
                             assessments=len(items), labels=labels)


def latest(s: Session, worker_id: str, subject: str, max_age: timedelta) -> AnalystAssessment | None:
    return s.execute(select(AnalystAssessment).where(AnalystAssessment.worker_id == worker_id,
                                                     AnalystAssessment.subject == subject,
                                                     AnalystAssessment.created_at >= utcnow() - max_age)
                     .order_by(desc(AnalystAssessment.id)).limit(1)).scalar()


def latest_by_worker(s: Session, subject: str, max_age: timedelta) -> list[AnalystAssessment]:
    rows = s.execute(select(AnalystAssessment).where(AnalystAssessment.subject == subject,
                                                     AnalystAssessment.created_at >= utcnow() - max_age)
                     .order_by(desc(AnalystAssessment.id))).scalars()
    seen: dict[str, AnalystAssessment] = {}
    for r in rows:
        seen.setdefault(r.worker_id, r)
    return list(seen.values())
