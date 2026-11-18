"""Coinbase account sync end to end with a fake (read-only) account."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from meridian.brokerage.sync import AccountSyncWorker, sync_account
from meridian.db.models import AccountSyncState, Instrument, Position
from meridian.db.session import session_scope
from meridian.runtime import make_context
from meridian.workers.base import execute
from test_flow import FakeMacro, client, login

pytestmark = pytest.mark.integration
GOL = "GOL-25NOV26-CDE"


class FakeAccount:
    def __init__(self):
        self.pos: list[dict] = []
        self.orders: list[dict] = []
        self.all_fills: list[dict] = []
        self.perms = {"can_view": True, "can_trade": False, "can_transfer": False}

    def assert_read_only(self):
        if self.perms["can_trade"]:
            from meridian.brokerage.coinbase_account import KeyNotReadOnly
            raise KeyNotReadOnly("this API key can trade or transfer; MERIDIAN only accepts a View-only key")
        return self.perms

    def positions(self):
        return self.pos

    def open_orders(self):
        return self.orders

    def fills(self, start_iso):
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        return [f for f in self.all_fills if datetime.fromisoformat(f["trade_time"]) >= start]

    def fill(self, fid, side, price, size, at, fee="1.20"):
        self.all_fills.append({"entry_id": fid, "product_id": GOL, "side": side, "price": str(price), "size": str(size),
                               "commission": fee, "trade_time": at.isoformat(), "sequence_timestamp": at.isoformat()})


@pytest.fixture()
def gold(clean_env):
    with session_scope() as s:
        s.add(Instrument(id=GOL, asset="GOLD", venue="coinbase", product_type="FUTURE", display_name="Gold Nov 26",
                         quote_currency="USD", contract_size=D(1), price_increment=D("0.5"), source="COINBASE", status="ACTIVE"))
    return clean_env


def _open(s):  # noqa: ANN001
    return list(s.execute(select(Position).where(Position.status == "OPEN")).scalars())


def test_full_lifecycle(gold):
    acct = FakeAccount()
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    # 1) open 2 LONG with a stop order and a take-profit order on Coinbase
    acct.fill("f1", "BUY", 4050, 2, t0)
    acct.pos = [{"product_id": GOL, "side": "LONG", "number_of_contracts": "2", "avg_entry_price": "4050"}]
    acct.orders = [{"product_id": GOL, "side": "SELL", "order_configuration": {"stop_limit_stop_limit_gtc": {"stop_price": "4015", "limit_price": "4010"}}},
                   {"product_id": GOL, "side": "SELL", "order_configuration": {"limit_limit_gtc": {"limit_price": "4120"}}}]
    with session_scope() as s:
        rep = sync_account(s, acct, now=t0 + timedelta(minutes=1))
        [p] = _open(s)
        assert rep.opened and p.source == "COINBASE" and p.quantity == 2 and p.stop_price == D(4015) and p.target_price == D(4120)
        assert p.fees == D("1.20")
    # 2) add 1 more at 4060 and move the stop up
    acct.fill("f2", "BUY", 4060, 1, t0 + timedelta(minutes=5))
    acct.pos[0].update(number_of_contracts="3", avg_entry_price="4053.3333")
    acct.orders[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"] = "4030"
    with session_scope() as s:
        rep = sync_account(s, acct, now=t0 + timedelta(minutes=6))
        [p] = _open(s)
        assert p.quantity == 3 and p.stop_price == D(4030) and rep.resized and p.fees == D("2.40")
    # 3) sell 1 at 4100: partial close booked at the real fill price
    acct.fill("f3", "SELL", 4100, 1, t0 + timedelta(minutes=10))
    acct.pos[0]["number_of_contracts"] = "2"
    with session_scope() as s:
        sync_account(s, acct, now=t0 + timedelta(minutes=11))
        [p] = _open(s)
        closed = list(s.execute(select(Position).where(Position.status == "CLOSED")).scalars())
        assert p.quantity == 2 and len(closed) == 1 and closed[0].exit_price == D(4100) and closed[0].realized_pnl > 0
    # 4) position gone but the fill isn't visible yet -> wait, then close at the real fill
    acct.pos = []
    with session_scope() as s:
        rep = sync_account(s, acct, now=t0 + timedelta(minutes=20))
        assert len(_open(s)) == 1 and any("waiting" in w for w in rep.warnings)
        assert s.get(AccountSyncState, "coinbase").pending
    acct.fill("f4", "SELL", 4110, 2, t0 + timedelta(minutes=19))
    with session_scope() as s:
        rep = sync_account(s, acct, now=t0 + timedelta(minutes=21))
        assert not _open(s) and rep.closed
        last = s.execute(select(Position).where(Position.status == "CLOSED").order_by(Position.closed_at.desc())).scalars().first()
        assert last.exit_price == D(4110)
        # (4110 - 4053.3333) * 2 contracts, minus fees
        assert D("100") < last.realized_pnl < D("115")
        assert not s.get(AccountSyncState, "coinbase").pending


def test_no_stop_order_and_flip_and_trade_key_refused(gold):
    acct = FakeAccount()
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    acct.fill("a", "SELL", 4000, 1, t0)
    acct.pos = [{"product_id": GOL, "side": "SHORT", "number_of_contracts": "1", "avg_entry_price": "4000"}]
    with session_scope() as s:
        rep = sync_account(s, acct, now=t0 + timedelta(minutes=1))
        [p] = _open(s)
        assert p.direction == "SHORT" and p.stop_price > p.entry_price and "no stop order" in p.note and rep.warnings
    acct.fill("b", "BUY", 3990, 1, t0 + timedelta(minutes=3))
    acct.fill("c", "BUY", 3990, 1, t0 + timedelta(minutes=3, seconds=1))
    acct.pos = [{"product_id": GOL, "side": "LONG", "number_of_contracts": "1", "avg_entry_price": "3990"}]
    with session_scope() as s:
        sync_account(s, acct, now=t0 + timedelta(minutes=4))
        [p] = _open(s)
        assert p.direction == "LONG"
    acct.perms["can_trade"] = True
    ctx = make_context(gold, macro=FakeMacro())
    ctx.services["account_reader"] = acct
    out = execute(AccountSyncWorker(ctx), trigger="MANUAL")
    assert out.status == "ERROR" and "View-only" in out.summary
    c = client()
    login(c)
    st = c.get("/api/overview").json()["account_sync"]
    assert st["last_error"] and "View-only" in st["last_error"]


def test_idle_without_key(gold):
    ctx = make_context(gold, macro=FakeMacro())
    assert execute(AccountSyncWorker(ctx), trigger="MANUAL").status == "IDLE"
