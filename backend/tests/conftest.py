from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://meridian:meridian@localhost:5432/meridian_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("MERIDIAN_ENV", "test")
os.environ.setdefault("MARKET_DATA_MODE", "simulated")
os.environ.setdefault("CIO_USERNAME", "cio")
os.environ.setdefault("CIO_PASSWORD", "test-password-123")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-000")
os.environ.setdefault("STREAM_PREFIX", "meridiantest")
os.environ.setdefault("RUN_WORKERS", "false")

import pytest  # noqa: E402


def _services_up() -> bool:
    try:
        import redis
        from sqlalchemy import create_engine, text

        with create_engine(os.environ["DATABASE_URL"]).connect() as c:
            c.execute(text("SELECT 1"))
        redis.Redis.from_url(os.environ["REDIS_URL"]).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


SERVICES = _services_up()


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    if SERVICES:
        return
    skip = pytest.mark.skip(reason="PostgreSQL/Redis not reachable (see docs/LOCAL_DEVELOPMENT.md)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture()
def clean_env():
    """Fresh schema + empty Redis for each integration test."""
    import redis
    from sqlalchemy import text

    from meridian.bootstrap import bootstrap
    from meridian.core.config import get_settings
    from meridian.db.session import get_engine

    st = get_settings()
    with get_engine().begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    redis.Redis.from_url(st.redis_url).flushdb()
    bootstrap(st)
    yield st
