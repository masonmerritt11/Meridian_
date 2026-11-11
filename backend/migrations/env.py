from alembic import context
from sqlalchemy import engine_from_config, pool

from meridian.core.config import get_settings
from meridian.db.models import Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
config.set_main_option("sqlalchemy.url", url)
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ANN201
    # Hand-written indexes/triggers from 0002 are not in the ORM metadata; never autogenerate drops for them.
    if type_ == "index" and reflected and compare_to is None:
        return False
    return True


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
