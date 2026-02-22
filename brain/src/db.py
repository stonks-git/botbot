"""Database connection pool and schema migration."""

import json
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
    async def _init_connection(conn):
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
        await conn.set_type_codec(
            "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    _pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10, init=_init_connection)
    logger.info("Database pool created.")

    await _run_schema(_pool)
    return _pool


async def _run_schema(pool: asyncpg.Pool) -> None:
    """Apply schema.sql idempotently (CREATE IF NOT EXISTS)."""
    schema_sql = SCHEMA_PATH.read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema_sql)
    logger.info("Schema migration applied.")


async def get_agent_ids(pool: asyncpg.Pool) -> list[str]:
    """Get all distinct agent IDs that have memories."""
    rows = await pool.fetch("SELECT DISTINCT agent_id FROM memories WHERE NOT archived")
    return [r["agent_id"] for r in rows]


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed.")
