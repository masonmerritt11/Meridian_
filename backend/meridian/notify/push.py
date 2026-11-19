"""Web Push (phone notifications).

What is sent: only two kinds of alert, to staff logins only —
  * a new trade recommendation (once per research case), and
  * an EXIT advisory on an open position (once each time a position flips into EXIT).
Prediction Desk alerts are deliberately NOT pushed. Members never receive trade alerts.

The signing (VAPID) key is generated on first use and stored in app_secrets; it is never returned by the API.
Delivery goes through the browser vendor's free push service (Apple / Google / Mozilla).
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptography.hazmat.primitives import serialization
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from meridian.core.util import utcnow
from meridian.db.models import (
    AppSecret,
    CaseTransition,
    CommitteeMemo,
    Instrument,
    Position,
    PositionAdvisory,
    PushSent,
    PushSubscription,
    ResearchCase,
)

log = logging.getLogger(__name__)
VAPID_NAME = "vapid_private_pem"
LOOKBACK = timedelta(minutes=30)  # never alert on anything older than this (e.g. right after a deploy)
MAX_FAILURES = 10


# ---------------------------------------------------------------- keys
def _vapid(s: Session):  # noqa: ANN202
    from py_vapid import Vapid02

    row = s.get(AppSecret, VAPID_NAME)
    if row is None:
        v = Vapid02()
        v.generate_keys()
        pem = v.private_pem().decode()
        s.execute(insert(AppSecret).values(name=VAPID_NAME, value=pem).on_conflict_do_nothing())
        s.flush()
        row = s.get(AppSecret, VAPID_NAME)
    return Vapid02.from_pem(row.value.encode())


def public_key(s: Session) -> str:
    """applicationServerKey for the browser: base64url of the uncompressed P-256 point."""
    raw = _vapid(s).public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------------------------------------------------------------- sending
@dataclass
class Alert:
    key: str
    title: str
    body: str
    url: str
    tag: str
    ttl: int = 3600


def _payload(a: Alert) -> str:
    return json.dumps({"title": a.title, "body": a.body, "url": a.url, "tag": a.tag})


def deliver(s: Session, a: Alert, contact: str, usernames: list[str] | None = None) -> int:
    """Send to every subscription (optionally only some users). Returns the number delivered."""
    from pywebpush import WebPushException, webpush

    q = select(PushSubscription)
    if usernames is not None:
        q = q.where(PushSubscription.username.in_(usernames))
    subs = list(s.execute(q).scalars())
    if not subs:
        return 0
    vapid = _vapid(s)
    ok = 0
    for sub in subs:
        try:
            webpush({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}, _payload(a),
                    vapid_private_key=vapid, vapid_claims={"sub": contact}, ttl=a.ttl,
                    headers={"Urgency": "high"}, timeout=10)
            sub.last_ok_at, sub.failures = utcnow(), 0
            ok += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):  # the phone unsubscribed / app removed
                s.delete(sub)
            else:
                sub.failures += 1
                log.warning("push failed", extra={"status": code, "subscription": sub.id})
                if sub.failures >= MAX_FAILURES:
                    s.delete(sub)
        except Exception as e:  # noqa: BLE001 — network trouble; try again next alert
            sub.failures += 1
            log.warning("push error", extra={"error": repr(e)[:200], "subscription": sub.id})
    return ok


# ---------------------------------------------------------------- what to alert on
def _num(v: Any) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError):
        return str(v)
    d = d.normalize()
    return f"{d:,f}" if abs(d) >= 1000 else format(d, "f")


def _human(code: str) -> str:
    return code.replace("_", " ").lower()


def pending_alerts(s: Session) -> list[Alert]:
    now = utcnow()
    since = now - LOOKBACK
    sent = set(s.execute(select(PushSent.key).where(PushSent.created_at >= now - timedelta(days=3))).scalars())
    out: list[Alert] = []

    # New recommendations: once per case, only while the case is still RECOMMENDED.
    for t in s.execute(select(CaseTransition).where(CaseTransition.to_state == "RECOMMENDED",
                                                    CaseTransition.created_at >= since)
                       .order_by(CaseTransition.id)).scalars():
        key = f"rec:{t.case_id}"
        if key in sent:
            continue
        case = s.get(ResearchCase, t.case_id)
        if case is None or case.state != "RECOMMENDED":
            continue
        memo = s.execute(select(CommitteeMemo).where(CommitteeMemo.case_id == case.id)
                         .order_by(CommitteeMemo.created_at.desc()).limit(1)).scalar()
        tr = (memo.memo or {}).get("trade", {}) if memo else {}
        qty = tr.get("suggested_quantity")
        body = (f"{case.direction} {qty} x {case.instrument_id} near {_num(tr.get('entry_reference'))} · "
                f"stop {_num(tr.get('stop'))} · target {_num(tr.get('target'))}")
        if tr.get("max_loss_usd") is not None:
            body += f" · max loss ${_num(tr.get('max_loss_usd'))}"
        out.append(Alert(key=key, title=f"New recommendation: {case.direction} {case.asset}", body=body,
                         url="/", tag=key, ttl=3600))
        sent.add(key)

    # EXIT advisories: once each time a position moves into EXIT (the hourly repeats are skipped).
    for adv in s.execute(select(PositionAdvisory).where(PositionAdvisory.action == "EXIT",
                                                        PositionAdvisory.created_at >= since)
                         .order_by(PositionAdvisory.id)).scalars():
        key = f"exit:{adv.id}"
        if key in sent:
            continue
        prev = s.execute(select(PositionAdvisory.action).where(PositionAdvisory.position_id == adv.position_id,
                                                               PositionAdvisory.id < adv.id)
                         .order_by(PositionAdvisory.id.desc()).limit(1)).scalar()
        if prev == "EXIT":
            continue
        pos = s.get(Position, adv.position_id)
        if pos is None or pos.status != "OPEN":
            continue
        inst = s.get(Instrument, pos.instrument_id)
        asset = inst.asset if inst else pos.instrument_id
        why = ", ".join(_human(c) for c in adv.reason_codes) or "exit conditions met"
        d = adv.detail or {}
        body = f"{pos.direction} {_num(pos.quantity)} x {pos.instrument_id}: {why}."
        if d.get("last_price") is not None:
            body += f" Last {_num(d['last_price'])}"
            if d.get("unrealized_pnl_usd") is not None:
                body += f", open P&L ${_num(d['unrealized_pnl_usd'])}"
            body += "."
        out.append(Alert(key=key, title=f"Exit advisory: {pos.direction} {asset}", body=body,
                         url="/risk/", tag=f"exit:{pos.id}", ttl=1800))
        sent.add(key)
    return out


def run_once(s: Session, contact: str) -> int:
    """Find new alerts, send them, record them. Returns alerts processed."""
    alerts = pending_alerts(s)
    for a in alerts:
        n = deliver(s, a, contact)
        s.execute(insert(PushSent).values(key=a.key, title=a.title, delivered=n).on_conflict_do_nothing())
        s.commit()
    return len(alerts)
