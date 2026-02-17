"""Database connection pool and schema migration."""

import logging
import os
from pathlib import Path

import asyncpg

logger = logging.getLogger("brain.db")

_pool: asyncpg.Pool | None = None

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it if needed."""
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def init_pool() -> asyncpg.Pool:
    """Create the connection pool and run schema migration."""
    global _pool
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://agent:agent_secret@localhost:5432/agent_memory",
    )
    _pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    logger.info("Database pool created.")

    await _run_schema(_pool)
    return _pool


async def _run_schema(pool: asyncpg.Pool) -> None:
    """Apply schema.sql idempotently (CREATE IF NOT EXISTS)."""
    schema_sql = SCHEMA_PATH.read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema_sql)
    logger.info("Schema migration applied.")


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed.")
