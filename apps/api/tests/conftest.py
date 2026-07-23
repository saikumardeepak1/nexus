"""Shared pytest fixtures for integration tests.

Tests run against a real Postgres database (the one named by
DATABASE_URL / app.core.config.settings, see README.md) with migrations
already applied via `alembic upgrade head`. Each test runs inside its own
outer transaction that is rolled back afterward (using a savepoint for any
commit() the test code issues), so tests never leak rows into each other
regardless of execution order.

The engine is created fresh per test (rather than reusing the app's
module-level singleton from app.core.db) and disposed afterward, because
pytest-asyncio gives each test its own event loop and an asyncpg connection
pool cannot be reused across event loops.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()
            session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                await outer_transaction.rollback()
    finally:
        await engine.dispose()
