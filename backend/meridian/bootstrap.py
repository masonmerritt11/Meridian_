"""Idempotent bootstrap: migrations, registry seed, default risk config, starting capital, CIO account."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from meridian.api.auth import hash_password
from meridian.core import audit
from meridian.core.config import Settings
from meridian.core.logging import get_logger
from meridian.db.models import CapitalEvent, User
from meridian.db.session import session_scope
from meridian.registry.service import seed_registry
from meridian.risk.service import ensure_default_config
from meridian.workers.impl import ALL_WORKERS

log = get_logger("meridian.bootstrap")
def _backend_dir() -> Path:
    for cand in (Path(os.environ.get("MERIDIAN_BACKEND_DIR", "")), Path.cwd(), Path(__file__).resolve().parent.parent):
        if str(cand) and (cand / "alembic.ini").exists():
            return cand
    raise RuntimeError("alembic.ini not found; run from the backend directory or set MERIDIAN_BACKEND_DIR")


BACKEND_DIR = _backend_dir()


def migrate(url: str) -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def bootstrap(settings: Settings, run_migrations: bool = True) -> None:
    settings.assert_safe_for_production()
    if run_migrations:
        migrate(settings.database_url)
    with session_scope() as s:
        seed_registry(s, ALL_WORKERS)
        ensure_default_config(s)
        if not s.execute(select(func.count()).select_from(CapitalEvent)).scalar():
            amt = Decimal(os.environ.get("STARTING_CAPITAL", "700"))
            s.add(CapitalEvent(kind="DEPOSIT", amount=amt, note="initial capital", username="system"))
            audit.record(s, "system", "capital.deposit", "capital", "initial", {"amount": str(amt)})
        if settings.market_data_mode == "simulated":
            from datetime import UTC, datetime, timedelta

            from meridian.marketdata.service import DEMO_ANCHOR_KEY, demo_anchor, set_freshness
            if demo_anchor(s) is None:  # fix the simulated scenario before any worker ingests
                now = datetime.now(UTC)
                anchor = datetime.fromtimestamp(int(now.timestamp()) // 3600 * 3600, UTC) - timedelta(hours=1)
                set_freshness(s, DEMO_ANCHOR_KEY, anchor, "FRESH", "SIMULATED", "demo breakout anchor (simulated scenario)", now)
        if settings.cio_username and settings.cio_password:
            if s.execute(select(User).where(User.username == settings.cio_username)).scalar() is None:
                s.add(User(username=settings.cio_username, password_hash=hash_password(settings.cio_password), role="CIO"))
                audit.record(s, "system", "user.created", "user", settings.cio_username, {"role": "CIO"})
        elif not s.execute(select(func.count()).select_from(User)).scalar():
            log.warning("no users exist and CIO_USERNAME/CIO_PASSWORD not set; dashboard login impossible")
    log.info("bootstrap complete")
