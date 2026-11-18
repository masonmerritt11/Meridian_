"""Reconcile MERIDIAN's position records with the CIO's actual Coinbase futures account (read-only).

Coinbase is the source of truth for what is open. Every minute:
  * a position on Coinbase that MERIDIAN doesn't know -> recorded (entry = Coinbase average entry price);
  * more contracts than recorded -> quantity and average entry updated;
  * fewer contracts -> the closed part is recorded as a closed trade at the actual fill prices;
  * gone from Coinbase -> closed at the actual fill prices (volume-weighted), fees from the fills;
  * side flipped -> old one closed, new one opened.
Stops and targets come from the CIO's resting orders on Coinbase. If there is no stop order, the stop is set where the
loss would equal the per-trade loss limit and the position is flagged. Each new position is linked to the matching live
recommendation (same contract and direction, last 24 hours), if any.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from meridian.core import audit
from meridian.brokerage.coinbase_account import CoinbaseAccountReader
from meridian.bus.bus import publish
from meridian.bus.schemas import make_event
from meridian.cases import lifecycle as lc
from meridian.core.util import jsonable, utcnow
from meridian.db.models import AccountSyncState, Instrument, Position, ResearchCase
from meridian.marketdata import service as md
from meridian.portfolio import service as portfolio
from meridian.risk import service as risk_svc
from meridian.risk.engine import DEFAULT_RISK_CONFIG

ACTOR = "coinbase-sync"


def _d(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class CbPosition:
    product: str
    direction: str
    contracts: Decimal
    avg_entry: Decimal


def parse_positions(raw: list[dict[str, Any]]) -> dict[str, CbPosition]:
    out = {}
    for p in raw:
        qty = _d(p.get("number_of_contracts"))
        entry = _d(p.get("avg_entry_price"))
        side = str(p.get("side") or "").upper()
        if not p.get("product_id") or not qty or qty == 0 or entry is None:
            continue
        d = "SHORT" if ("SHORT" in side or qty < 0) else "LONG"
        out[p["product_id"]] = CbPosition(p["product_id"], d, abs(qty), entry)
    return out


def _find(obj: Any, keys: tuple[str, ...]) -> Decimal | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and _d(v) is not None:
                return _d(v)
            got = _find(v, keys)
            if got is not None:
                return got
    return None


def protective_levels(orders: list[dict[str, Any]], product: str, direction: str) -> tuple[Decimal | None, Decimal | None]:
    """(stop, target) from resting orders on the closing side."""
    closing = "SELL" if direction == "LONG" else "BUY"
    stop = target = None
    for o in orders:
        if o.get("product_id") != product or str(o.get("side", "")).upper() != closing:
            continue
        cfg = o.get("order_configuration") or {}
        sp = _find(cfg, ("stop_price", "stop_trigger_price"))
        lp = _find(cfg, ("limit_price",))
        if sp is not None:
            stop = stop or sp
            if "trigger_bracket" in "".join(cfg.keys()) and lp is not None:
                target = target or lp       # bracket: limit leg is the take-profit
        elif lp is not None:
            target = target or lp
    return stop, target


@dataclass
class Report:
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    resized: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{k} {', '.join(v)}" for k, v in (("opened", self.opened), ("closed", self.closed), ("resized", self.resized),
                                                     ("stops/targets", self.levels)) if v]
        return "; ".join(parts) or "in sync"


def _fid(f: dict[str, Any]) -> str:
    return str(f.get("entry_id") or f.get("trade_id") or f"{f.get('order_id')}:{f.get('trade_time')}")


def _vwap(fills: list[dict[str, Any]]) -> tuple[Decimal | None, Decimal, Decimal]:
    qty = sum((_d(f.get("size")) or Decimal(0) for f in fills), Decimal(0))
    notional = sum(((_d(f.get("price")) or Decimal(0)) * (_d(f.get("size")) or Decimal(0)) for f in fills), Decimal(0))
    fees = sum((_d(f.get("commission")) or Decimal(0) for f in fills), Decimal(0))
    return (notional / qty if qty else None), qty, fees


def _fallback_stop(s: Session, direction: str, entry: Decimal, qty: Decimal, contract_size: Decimal) -> Decimal:
    cfg = {**DEFAULT_RISK_CONFIG, **((risk_svc.active_config(s).config if risk_svc.active_config(s) else {}))}
    eq = portfolio.equity(s) or Decimal(0)
    max_loss = eq * Decimal(str(cfg["max_loss_per_trade_pct"]))
    dist = (max_loss / (qty * contract_size)) if qty and contract_size and max_loss > 0 else entry * Decimal("0.02")
    return entry - dist if direction == "LONG" else entry + dist


def _match_case(s: Session, product: str, direction: str, now: datetime) -> ResearchCase | None:
    return s.execute(select(ResearchCase).where(
        ResearchCase.instrument_id == product, ResearchCase.direction == direction,
        ResearchCase.state.in_(("RECOMMENDED", "EXPIRED")), ResearchCase.created_at >= now - timedelta(hours=24))
        .order_by(desc(ResearchCase.created_at)).limit(1)).scalar()


def _open(s: Session, cp: CbPosition, inst: Instrument, orders: list[dict[str, Any]], fills: list[dict[str, Any]],
          rep: Report, now: datetime, consumed: list[str]) -> Position:
    stop, target = protective_levels(orders, cp.product, cp.direction)
    cs = inst.contract_size or Decimal(1)
    note = "synced from Coinbase"
    if stop is None:
        stop = _fallback_stop(s, cp.direction, cp.avg_entry, cp.contracts, cs)
        note += "; no stop order found on Coinbase, stop assumed at the per-trade loss limit"
        rep.warnings.append(f"{cp.product}: no stop order on Coinbase")
    opening = [f for f in fills if f.get("product_id") == cp.product and str(f.get("side", "")).upper() == ("BUY" if cp.direction == "LONG" else "SELL")]
    _, _, fees = _vwap(opening)
    consumed.extend(_fid(f) for f in opening)
    case = _match_case(s, cp.product, cp.direction, now)
    t_open = min((_ts(f.get("trade_time")) for f in opening if _ts(f.get("trade_time"))), default=now)
    pos = Position(case_id=case.id if case else None, instrument_id=inst.id, direction=cp.direction, quantity=cp.contracts,
                   entry_price=cp.avg_entry, stop_price=stop, target_price=target, contract_size=cs, status="OPEN",
                   recorded_by=ACTOR, note=note, source="COINBASE", fees=fees, opened_at=t_open)
    s.add(pos)
    s.flush()
    if case is not None:
        case.expires_at = None
    publish(s, make_event("position.recorded", {"position_id": pos.id, "status": "OPEN"}, producer=ACTOR))
    audit.record(s, ACTOR, "position.synced_open", "position", pos.id,
                 jsonable({"product": cp.product, "direction": cp.direction, "contracts": cp.contracts, "entry": cp.avg_entry,
                           "stop": stop, "target": target, "case_id": case.id if case else None}))
    rep.opened.append(f"{cp.direction} {cp.contracts} {cp.product}")
    return pos


CLOSE_FILL_GRACE = timedelta(minutes=5)


def _close(s: Session, pos: Position, qty: Decimal, fills: list[dict[str, Any]], rep: Report, now: datetime, why: str,
           st: AccountSyncState | None = None, consumed: list[str] | None = None) -> bool:
    """Close `qty` contracts of `pos` at the actual closing fill prices. Returns False if deferred (fills not visible yet)."""
    closing_side = "SELL" if pos.direction == "LONG" else "BUY"
    used = set(consumed or [])
    closing = [f for f in fills if f.get("product_id") == pos.instrument_id and str(f.get("side", "")).upper() == closing_side
               and (_ts(f.get("trade_time")) or now) >= pos.opened_at and _fid(f) not in used]
    closing.sort(key=lambda f: str(f.get("trade_time")), reverse=True)
    picked, got = [], Decimal(0)
    for f in closing:                       # the most recent closing fills that add up to the closed size
        if got >= qty:
            break
        picked.append(f)
        got += _d(f.get("size")) or Decimal(0)
    px, _, close_fees = _vwap(picked)
    note = why
    pend = dict((st.pending or {}) if st is not None else {})
    key = str(pos.id)
    if px is None and st is not None:
        first = _ts(pend.get(key)) or now
        if now - first < CLOSE_FILL_GRACE:   # Coinbase can show the position gone before the fill is visible
            pend[key] = first.isoformat()
            st.pending = pend
            rep.warnings.append(f"{pos.instrument_id}: waiting for closing fills")
            return False
    pend.pop(key, None)
    if st is not None:
        st.pending = pend
    if consumed is not None:
        consumed.extend(_fid(f) for f in picked)
    if px is None:
        last = md.load_candles(s, pos.instrument_id, "ONE_HOUR", limit=1)
        px = last[-1].close if last else pos.entry_price
        note += "; closing fills not found, exit priced at the latest close"
        rep.warnings.append(f"{pos.instrument_id}: closing fills not found")
    t_close = max((_ts(f.get("trade_time")) for f in closing if _ts(f.get("trade_time"))), default=now)
    open_fees_share = (Decimal(pos.fees or 0) * qty / pos.quantity) if pos.quantity else Decimal(0)
    if qty < pos.quantity:  # partial: book the closed part as its own closed trade
        part = Position(case_id=pos.case_id, instrument_id=pos.instrument_id, direction=pos.direction, quantity=qty,
                        entry_price=pos.entry_price, stop_price=pos.stop_price, target_price=pos.target_price,
                        contract_size=pos.contract_size, status="CLOSED", opened_at=pos.opened_at, closed_at=t_close,
                        exit_price=px, recorded_by=ACTOR, note=f"partial close of {pos.id}: {note}", source="COINBASE",
                        fees=open_fees_share + close_fees)
        part.realized_pnl = portfolio.pnl_for(part, px) - part.fees
        s.add(part)
        pos.quantity -= qty
        pos.fees = Decimal(pos.fees or 0) - open_fees_share
        target = part
    else:
        pos.exit_price, pos.closed_at, pos.status = px, t_close, "CLOSED"
        pos.fees = Decimal(pos.fees or 0) + close_fees
        pos.realized_pnl = portfolio.pnl_for(pos, px) - pos.fees
        pos.note = (pos.note + "\n" + note).strip()
        if pos.case_id:
            c = s.get(ResearchCase, pos.case_id)
            if c and "CLOSED" in lc.TRANSITIONS[c.state]:
                lc.transition(s, c, "CLOSED", "position closed on Coinbase", ACTOR)
        target = pos
    s.flush()
    publish(s, make_event("position.recorded", {"position_id": target.id, "status": "CLOSED"}, producer=ACTOR))
    audit.record(s, ACTOR, "position.synced_close", "position", target.id,
                 jsonable({"contracts": qty, "exit": px, "realized_pnl": target.realized_pnl, "fees": target.fees}))
    rep.closed.append(f"{qty} {pos.instrument_id} @ {px:.4f}")
    return True


def sync_account(s: Session, reader: CoinbaseAccountReader, now: datetime | None = None) -> Report:
    now = now or utcnow()
    rep = Report()
    st = s.get(AccountSyncState, "coinbase")
    if st is None:
        st = AccountSyncState(id="coinbase", seen_fill_ids=[])
        s.add(st)
    st.permissions = reader.assert_read_only()       # fail closed if the key can trade or transfer
    cb = parse_positions(reader.positions())
    orders = reader.open_orders()
    since = st.fills_cursor or (now - timedelta(days=7)).isoformat()
    consumed: list[str] = list(st.seen_fill_ids or [])   # fills already used for an open, add or close
    all_fills = reader.fills(since)
    fills = [f for f in all_fills if _fid(f) not in set(consumed)]
    mine = {p.instrument_id: p for p in s.execute(select(Position).where(Position.status == "OPEN")).scalars()}
    changed = False
    for product in sorted(set(cb) | {k for k in mine if k.endswith("-CDE")}):
        cp, mp = cb.get(product), mine.get(product)
        inst = s.get(Instrument, product)
        if cp and inst is None:
            rep.warnings.append(f"{product}: not a MERIDIAN instrument, ignored")
            continue
        if cp and mp is None:
            _open(s, cp, inst, orders, [f for f in fills if _fid(f) not in set(consumed)], rep, now, consumed)  # type: ignore[arg-type]
            changed = True
        elif mp is not None and cp is None:
            changed |= _close(s, mp, mp.quantity, reader.fills(mp.opened_at.isoformat()), rep, now, "closed on Coinbase", st, consumed)
        elif mp is not None and cp is not None:
            if cp.direction != mp.direction:
                if _close(s, mp, mp.quantity, reader.fills(mp.opened_at.isoformat()), rep, now, "position flipped on Coinbase", st, consumed):
                    _open(s, cp, inst, orders, [f for f in fills if _fid(f) not in set(consumed)], rep, now, consumed)  # type: ignore[arg-type]
                    changed = True
                continue
            if cp.contracts < mp.quantity:
                changed |= _close(s, mp, mp.quantity - cp.contracts, reader.fills(mp.opened_at.isoformat()), rep, now,
                                  "reduced on Coinbase", st, consumed)
            elif cp.contracts > mp.quantity:
                opening = [f for f in fills if f.get("product_id") == product and _fid(f) not in set(consumed)
                           and str(f.get("side", "")).upper() == ("BUY" if cp.direction == "LONG" else "SELL")]
                mp.fees = Decimal(mp.fees or 0) + _vwap(opening)[2]
                consumed.extend(_fid(f) for f in opening)
                mp.quantity, mp.entry_price = cp.contracts, cp.avg_entry
                rep.resized.append(f"{product} to {cp.contracts}")
                audit.record(s, ACTOR, "position.synced_resize", "position", mp.id, jsonable({"contracts": cp.contracts, "entry": cp.avg_entry}))
                changed = True
            stop, target = protective_levels(orders, product, cp.direction)
            if stop is not None and stop != mp.stop_price:
                audit.record(s, ACTOR, "position.stop_recorded", "position", mp.id, {"from": str(mp.stop_price), "to": str(stop)})
                mp.stop_price = stop
                rep.levels.append(f"{product} stop {stop}")
            if target is not None and target != mp.target_price:
                mp.target_price = target
                rep.levels.append(f"{product} target {target}")
    if changed:
        lc.rereview_after_portfolio_change(s, ACTOR)
    stamps = [str(f.get("sequence_timestamp") or f.get("trade_time")) for f in all_fills if f.get("sequence_timestamp") or f.get("trade_time")]
    if stamps:
        st.fills_cursor = max(stamps)
    st.seen_fill_ids = consumed[-2000:]
    st.last_ok, st.last_error = now, None
    return rep


# ------------------------------------------------------------------ worker
from meridian.workers.base import BaseWorker, RunContext, WorkerOutput, WorkerSpec  # noqa: E402


class SyncOutput(WorkerOutput):
    status: str = "OK"


class AccountSyncWorker(BaseWorker):
    spec = WorkerSpec(id="risk.account_sync", department="risk", name="Account Sync",
                      responsibility="Reads the CIO's Coinbase futures account (view-only key) every minute and keeps positions, "
                                     "stops, targets, exits and fees in sync. Cannot place or change orders.",
                      version="1.0.0", output_model=SyncOutput, schedule_seconds=60, max_retries=1,
                      escalation_to="risk.chief_risk_officer", default_config={})

    def run(self, s: Session, rc: RunContext) -> SyncOutput:
        st = self.ctx.settings
        reader = self.ctx.services.get("account_reader")
        if reader is None:
            if not (st.coinbase_api_key_name and st.coinbase_api_key_secret):
                return SyncOutput(summary="IDLE: no read-only Coinbase key configured", status="IDLE")
            try:
                reader = CoinbaseAccountReader(st.coinbase_api_key_name, st.coinbase_api_key_secret)
            except Exception as e:  # noqa: BLE001 — never echo the secret
                return self._fail(s, f"API key could not be loaded: {type(e).__name__}")
            self.ctx.services["account_reader"] = reader
        try:
            rep = sync_account(s, reader)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            return self._fail(s, str(e)[:300])
        rc.metrics.update({"opened": rep.opened, "closed": rep.closed, "resized": rep.resized, "levels": rep.levels,
                           "warnings": rep.warnings})
        return SyncOutput(summary=rep.summary() + (f" ({'; '.join(rep.warnings)})" if rep.warnings else ""))

    def _fail(self, s: Session, msg: str) -> SyncOutput:
        st = s.get(AccountSyncState, "coinbase") or AccountSyncState(id="coinbase", seen_fill_ids=[])
        st.last_error = msg
        s.merge(st)
        s.commit()
        return SyncOutput(summary=f"ERROR: {msg}", status="ERROR")
