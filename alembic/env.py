"""Alembic environment for NEXTPLAY FastAPI (async).

Reads the database URL from src.core.config.settings at runtime so it
matches the same logic as the running app (asyncpg in prod, aiosqlite in dev).

Migrations call AsyncEngine.run_sync(do_run_migrations) to bridge async
SQLAlchemy with Alembic's sync migration API.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.config import settings
from src.core.database import Base

# Import all model modules here so Base.metadata sees them.
# This list will grow as Phase 1 lands models. For Phase 0 it is intentionally empty —
# `alembic check` should run cleanly against a no-table baseline.
# Example (Phase 1):
#   import src.models.users  # noqa: F401
#   import src.models.teams  # noqa: F401


config = context.config

# Override sqlalchemy.url from settings so we never hard-code a URL.
config.set_main_option("sqlalchemy.url", settings.database_url_async)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no connection, just emits SQL)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
