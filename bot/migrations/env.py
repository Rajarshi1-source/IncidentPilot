"""Alembic environment.

The database URL comes from ``settings``, never from ``alembic.ini``. That is
what lets the same migration set run against a Testcontainer, docker compose and
production without an edit -- and it keeps the connection string out of a file
that gets committed.

Migrations run synchronously: Alembic's async support exists, but the migration
path is a single serial process by definition and the async wrapper buys nothing
except a harder stack trace when a DDL statement fails.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from incidentpilot.config.settings import settings
from incidentpilot.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """psycopg3's sync driver, from the same URL the app uses async."""
    return settings.database_url.get_secret_value().replace(
        "postgresql+psycopg://", "postgresql+psycopg://"
    )


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    """Keep TimescaleDB's internal schemas out of autogenerate.

    Hypertables create chunk tables under ``_timescaledb_internal``. Without
    this filter, autogenerate proposes dropping every chunk on every run, which
    is a spectacular way to lose data.
    """
    return not (type_ == "table" and name and name.startswith("_"))


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _sync_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
