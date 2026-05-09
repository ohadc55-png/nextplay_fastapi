"""Shared pytest fixtures.

Each test gets a fresh in-memory SQLite database. The schema is created
directly from `Base.metadata` (skipping Alembic for speed); the goal of unit
tests is to exercise repository / service logic, not migrations. Migration
correctness is verified separately via `alembic upgrade head` smoke checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Importing src.models registers every ORM class with Base.metadata.
import src.models  # noqa: F401
from src.core.database import Base


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """In-memory aiosqlite engine. StaticPool keeps the single connection
    alive across the whole test, so `:memory:` is shared between session
    operations (otherwise each new connection would see an empty DB)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A short-lived session bound to the per-test engine. Rollback at end so
    test bodies can `flush()` freely without polluting other tests."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
