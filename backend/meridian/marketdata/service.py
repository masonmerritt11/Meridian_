from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from meridian.core.config import Settings
from meridian.core.util import jsonable, utcnow
from meridian.db.models import BookSnapshot, Candle, DataFreshness, Instrument
from meridian.marketdata.coinbase import CoinbasePublicProvider
from meridian.marketdata.simulated import SimulatedProvider
from meridian.marketdata.types import GRANULARITY_SECONDS, BookDTO, CandleDTO, InstrumentSpec, MarketDataProvider
from meridian.marketdata.validation import ValidationReport, is_fresh, validate_candles, validate_instrument


DEMO_ANCHOR_KEY = "simulated:demo_anchor"


def demo_anchor(s: Session) -> datetime | None:
    row = s.get(DataFreshness, DEMO_ANCHOR_KEY)
    return row.last_data_ts if row else None


def build_provider(settings: Settings, scenario: str | None = None) -> MarketDataProvider:
    if settings.market_data_mode == "coinbase":
        overrides = json.loads(settings.product_overrides) if settings.product_overrides else {}
        return CoinbasePublicProvider(overrides=overrides)
    from meridian.db.session import session_scope
    try:
        with session_scope() as s:
            anchor = demo_anchor(s)
    except Exception:  # noqa: BLE001
        anchor = None
    # Keep the simulated series continuous with any demo that already ran.
    return SimulatedProvider(scenario="demo" if anchor else scenario, demo_end=anchor)


def upsert_instruments(s: Session, specs: list[InstrumentSpec], now: datetime) -> list[Instrument]:
    rows = []
    # Instruments from the other mode / no longer selected are marked UNAVAILABLE, not deleted.
    selected = {sp.id for sp in specs}
    for row in s.execute(select(Instrument)).scalars():
        if row.id not in selected and row.status == "ACTIVE":
            row.status, row.status_detail = "UNAVAILABLE", "No longer selected by discovery"
    for sp in specs:
        problems = validate_instrument(sp, now) if sp.status == "ACTIVE" else []
        status, detail = sp.status, sp.status_detail
        if "CONTRACT_EXPIRED" in problems:
            status, detail = "EXPIRED", "Contract expired"
        elif any(p.endswith("_MISSING") for p in problems):
            status, detail = "UNAVAILABLE", ",".join(problems)
        row = s.get(Instrument, sp.id) or Instrument(id=sp.id)
        row.asset, row.venue, row.product_type, row.display_name = sp.asset, sp.venue, sp.product_type, sp.display_name
        row.quote_currency, row.price_increment, row.size_increment = sp.quote_currency, sp.price_increment, sp.size_increment
        row.min_size, row.contract_size, row.contract_expiry = sp.min_size, sp.contract_size, sp.contract_expiry
        row.source, row.status, row.status_detail, row.raw = sp.source, status, detail, jsonable(sp.raw)
        row.updated_at = now
        s.add(row)
        rows.append(row)
    s.flush()
    return rows


def to_spec(row: Instrument) -> InstrumentSpec:
    return InstrumentSpec(id=row.id, asset=row.asset, venue=row.venue, product_type=row.product_type,  # type: ignore[arg-type]
                          display_name=row.display_name, quote_currency=row.quote_currency,
                          price_increment=row.price_increment, size_increment=row.size_increment, min_size=row.min_size,
                          contract_size=row.contract_size, contract_expiry=row.contract_expiry, source=row.source,  # type: ignore[arg-type]
                          status=row.status, status_detail=row.status_detail, raw=row.raw)  # type: ignore[arg-type]


def active_instruments(s: Session) -> list[Instrument]:
    return list(s.execute(select(Instrument).where(Instrument.status == "ACTIVE").order_by(Instrument.asset)).scalars())


def store_candles(s: Session, inst: Instrument, granularity: str, candles: list[CandleDTO], now: datetime) -> ValidationReport:
    rep = validate_candles(candles, granularity, now, to_spec(inst))
    for c in rep.accepted:
        stmt = pg_insert(Candle).values(instrument_id=inst.id, granularity=granularity, ts=c.ts, open=c.open, high=c.high,
                                        low=c.low, close=c.close, volume=c.volume, source=inst.source, ingested_at=now)
        # Closed bars are immutable once stored, so recorded inputs stay reconstructable.
        s.execute(stmt.on_conflict_do_nothing(index_elements=["instrument_id", "granularity", "ts"]))
    last = s.execute(select(Candle.ts).where(Candle.instrument_id == inst.id, Candle.granularity == granularity)
                     .order_by(Candle.ts.desc()).limit(1)).scalar()
    fresh = is_fresh(last, granularity, now)
    hard_issues = [i for i in rep.issues if "WARN" not in i]
    set_freshness(s, f"candles:{inst.id}:{granularity}", last, "FRESH" if fresh else "STALE", inst.source,
                  f"accepted={len(rep.accepted)} rejected={len(hard_issues)} incomplete_dropped={rep.dropped_incomplete}"
                  + (f" issues={hard_issues[:3]}" if hard_issues else ""), now)
    return rep


def store_book(s: Session, inst: Instrument, book: BookDTO | None, now: datetime) -> None:
    if book is None:
        set_freshness(s, f"book:{inst.id}", None, "UNAVAILABLE", inst.source, "order book unavailable", now)
        return
    s.add(BookSnapshot(instrument_id=inst.id, ts=book.ts, best_bid=book.best_bid, best_ask=book.best_ask,
                       spread_bps=book.spread_bps, bid_depth=book.bid_depth, ask_depth=book.ask_depth, source=inst.source))
    set_freshness(s, f"book:{inst.id}", book.ts, "FRESH", inst.source, f"spread_bps={book.spread_bps}", now)


def set_freshness(s: Session, dataset: str, last_ts: datetime | None, status: str, source: str, detail: str, now: datetime) -> None:
    stmt = pg_insert(DataFreshness).values(dataset=dataset, last_data_ts=last_ts, last_checked=now, status=status,
                                           source=source, detail=detail)
    s.execute(stmt.on_conflict_do_update(index_elements=["dataset"], set_={"last_data_ts": last_ts, "last_checked": now,
                                                                        "status": status, "source": source, "detail": detail}))


def load_candles(s: Session, instrument_id: str, granularity: str, limit: int = 300, until: datetime | None = None) -> list[Candle]:
    q = select(Candle).where(Candle.instrument_id == instrument_id, Candle.granularity == granularity)
    if until is not None:
        q = q.where(Candle.ts <= until)
    rows = list(s.execute(q.order_by(Candle.ts.desc()).limit(limit)).scalars())
    return rows[::-1]


def latest_book(s: Session, instrument_id: str) -> BookSnapshot | None:
    return s.execute(select(BookSnapshot).where(BookSnapshot.instrument_id == instrument_id)
                     .order_by(BookSnapshot.ts.desc()).limit(1)).scalar()


def recheck_freshness(s: Session, granularity: str, now: datetime | None = None) -> list[DataFreshness]:
    now = now or utcnow()
    rows = list(s.execute(select(DataFreshness)).scalars())
    for r in rows:
        if r.dataset.startswith("candles:") and r.status != "UNAVAILABLE":
            r.status = "FRESH" if is_fresh(r.last_data_ts, granularity, now) else "STALE"
            r.last_checked = now
        elif r.dataset.startswith("book:") and r.last_data_ts is not None:
            r.status = "FRESH" if now - r.last_data_ts < timedelta(minutes=30) else "STALE"
            r.last_checked = now
    return rows


def window_for(granularity: str, bars: int, now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(seconds=GRANULARITY_SECONDS[granularity] * bars), now


def dec(x: float | Decimal | str) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))
