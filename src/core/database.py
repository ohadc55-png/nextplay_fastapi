"""Async SQLAlchemy engine, session factory, and base classes.

Replaces backend/db_connection.py (psycopg2 + custom SQLite emulator) with
SQLAlchemy 2.0 async + asyncpg (prod) / aiosqlite (dev). The Flask app's
PgConnectionWrapper trick (auto-converting `?` to `%s`, auto-appending
`RETURNING id`) is no longer needed — SQLAlchemy handles dialect differences
natively.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import TypeDecorator, Text, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.config import settings


# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------

_is_sqlite = settings.database_url_async.startswith("sqlite")

# Pool args differ by driver — SQLite has no pool concept in async mode.
_engine_kwargs: dict[str, Any] = {
    "echo": False,
    "future": True,
}
if not _is_sqlite:
    _engine_kwargs["pool_size"] = settings.DB_POOL_MAX
    _engine_kwargs["max_overflow"] = max(0, settings.DB_POOL_MAX // 2)
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(settings.database_url_async, **_engine_kwargs)


# Apply SQLite pragmas on every new connection (matches Flask db_connection.py:202-207).
if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped AsyncSession. Auto-rollback on unhandled errors;
    the caller (service / router) is responsible for `await session.commit()`."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Custom JSON column type
# ---------------------------------------------------------------------------

class JSONText(TypeDecorator):
    """Portable JSON column stored as TEXT.

    Used for legacy columns that the Flask app currently writes as JSON-encoded
    strings (json.dumps + json.loads): player_metrics.metrics_json,
    entities.attributes_json, session_summaries.topics_json /
    agents_used_json, plays.plays_json, social_accounts.provider_data,
    memories.embedding_json. Works identically on Postgres + SQLite so dev/prod
    parity is preserved.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, _dialect):  # noqa: ANN001
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: Any, _dialect):  # noqa: ANN001
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Declarative base + mixins
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Project-wide declarative base for all SQLAlchemy ORM models."""


class TimestampMixin:
    """Adds `created_at` / `updated_at` columns matching Flask schema.

    Apply only to tables that already have these columns in the live schema.
    Don't add timestamps to tables that don't currently have them — preserve
    exact schema parity for the Alembic baseline.
    """

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


__all__ = [
    "Base",
    "TimestampMixin",
    "JSONText",
    "engine",
    "AsyncSessionLocal",
    "get_db",
]
