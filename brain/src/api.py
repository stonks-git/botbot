"""Brain Service — FastAPI application."""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_pool, close_pool, get_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("brain.api")

_start_time: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect DB + apply schema. Shutdown: close pool."""
    global _start_time
    _start_time = time.time()
    await init_pool()
    logger.info("Brain service started.")
    yield
    await close_pool()
    logger.info("Brain service stopped.")


app = FastAPI(title="Brain Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    pool = await get_pool()
    async with pool.acquire() as conn:
        memory_count = await conn.fetchval(
            "SELECT COUNT(*) FROM memories"
        )
        agent_count = await conn.fetchval(
            "SELECT COUNT(DISTINCT agent_id) FROM memories"
        )
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "memory_count": memory_count,
        "agent_count": agent_count,
    }
