"""Live exit watch: stop/target judged on the live order book within seconds; sticky once hit; stale book falls back."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from meridian.core.util import utcnow
from meridian.db.models import BookSnapshot, Instrument, Position, PositionAdvisory
from meridian.db.session import session_scope
from meridian.notify import push
from meridian.workers.impl import evaluate_position, fast_exit_check, monitor_context

pytestmark = pytest.mark.integration


def _setup(s, direction="LONG"):
    s.add(Instrument(id="GOL-LIVE-CDE", asset="GOLD", venue="TEST", product_type="FUTURE", display_name="Gold",
                     quote_currency="USD", source="SIMULATED", status="ACTIVE"))
    s.flush()
    pos = Position(instrument_id="GOL-LIVE-CDE", direction=direction, quantity=Decimal(1), entry_price=Decimal(100),
                   stop_price=Decimal(95) if direction == "LONG" else Decimal(105), target_price=Decimal(110) if direction == "LONG" else Decimal(90),
                   contract_size=Decimal(1), status="OPEN", recorded_by="cio")
    s.add(pos); s.flush()
    return pos


def _book(s, bid, ask, age=timedelta(seconds=2)):
    s.add(BookSnapshot(instrument_id="GOL-LIVE-CDE", ts=utcnow() - age, best_bid=Decimal(bid), best_ask=Decimal(ask), source="SIMULATED"))
    s.flush()


def test_live_stop_flags_exit_immediately_and_sticks(clean_env):
    with session_scope() as s:
        pos = _setup(s)
        now = utcnow(); ctx = monitor_context(s, "ONE_HOUR", now)
        _book(s, "97.0", "97.2")
        assert fast_exit_check(s, ctx, now) == []                       # above the stop: nothing
        _book(s, "94.8", "95.1")                                         # bid (what a long sells at) through the stop
        hits = fast_exit_check(s, ctx, utcnow())
        assert len(hits) == 1 and "STOP_BREACHED" in hits[0][1]
        adv = s.execute(select(PositionAdvisory).where(PositionAdvisory.position_id == pos.id)).scalars().all()
        assert len(adv) == 1 and adv[0].action == "EXIT" and adv[0].detail["price_source"] == "LIVE_BOOK"
        assert fast_exit_check(s, ctx, utcnow()) == []                   # not written twice
        _book(s, "97.5", "97.7")                                         # price bounces back above the stop
        action, codes, detail = evaluate_position(s, pos, ctx, utcnow())
        assert action == "EXIT" and "STOP_BREACHED" in codes             # sticky: stays EXIT
        s.commit()
        assert len(push.pending_alerts(s)) == 1                           # one phone alert


def test_short_uses_ask_and_target(clean_env):
    with session_scope() as s:
        pos = _setup(s, "SHORT")
        now = utcnow(); ctx = monitor_context(s, "ONE_HOUR", now)
        _book(s, "89.8", "90.1")                                         # ask still above the 90 target: no exit yet
        assert fast_exit_check(s, ctx, now) == []
        _book(s, "89.7", "89.9")
        hits = fast_exit_check(s, ctx, utcnow())
        assert hits and "TARGET_REACHED" in hits[0][1]


def test_stale_book_not_used(clean_env):
    with session_scope() as s:
        pos = _setup(s)
        now = utcnow(); ctx = monitor_context(s, "ONE_HOUR", now)
        _book(s, "90", "90.2", age=timedelta(minutes=5))                 # old snapshot
        assert fast_exit_check(s, ctx, now) == []
        action, codes, detail = evaluate_position(s, pos, ctx, now)
        assert detail["price_source"] == "HOURLY_CLOSE" and action == "REVIEW"   # no fresh data -> REVIEW, not a guess
