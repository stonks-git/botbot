"""Brain Service — FastAPI application."""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .db import close_pool, get_pool, init_pool
from .memory import MemoryStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("brain.api")

_start_time: float = 0.0
_memory_store: MemoryStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect DB + init MemoryStore. Shutdown: close pool."""
    global _start_time, _memory_store
    _start_time = time.time()
    pool = await init_pool()
    _memory_store = MemoryStore(pool)
    logger.info("Brain service started.")
    yield
    _memory_store = None
    await close_pool()
    logger.info("Brain service stopped.")


app = FastAPI(title="Brain Service", version="0.1.0", lifespan=lifespan)


def _store() -> MemoryStore:
    if _memory_store is None:
        raise RuntimeError("MemoryStore not initialized.")
    return _memory_store


# ── Health ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    pool = await get_pool()
    async with pool.acquire() as conn:
        memory_count = await conn.fetchval("SELECT COUNT(*) FROM memories")
        agent_count = await conn.fetchval(
            "SELECT COUNT(DISTINCT agent_id) FROM memories"
        )
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "memory_count": memory_count,
        "agent_count": agent_count,
    }


# ── Request/Response models ───────────────────────────────────────────


class StoreRequest(BaseModel):
    agent_id: str
    content: str
    memory_type: str = "semantic"
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    importance: float = 0.5
    metadata: dict | None = None
    source_tag: str | None = None


class StoreResponse(BaseModel):
    id: str
    agent_id: str
    status: str = "stored"


class RetrieveRequest(BaseModel):
    agent_id: str
    query: str
    top_k: int = 5
    mode: str = "reranked"  # "similar", "hybrid", "reranked"


class MemoryResponse(BaseModel):
    id: str
    content: str
    type: str
    confidence: float
    importance: float
    access_count: int
    tags: list[str]
    source: str | None = None
    created_at: str
    score: float | None = None


class DeleteResponse(BaseModel):
    id: str
    deleted: bool


# ── Endpoints ──────────────────────────────────────────────────────────


@app.post("/memory/store", response_model=StoreResponse)
async def store_memory(req: StoreRequest):
    store = _store()
    try:
        mem_id = await store.store_memory(
            content=req.content,
            agent_id=req.agent_id,
            memory_type=req.memory_type,
            source=req.source,
            tags=req.tags,
            confidence=req.confidence,
            importance=req.importance,
            metadata=req.metadata,
            source_tag=req.source_tag,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return StoreResponse(id=mem_id, agent_id=req.agent_id)


@app.post("/memory/retrieve")
async def retrieve_memories(req: RetrieveRequest):
    store = _store()
    try:
        if req.mode == "similar":
            results = await store.search_similar(
                req.query, req.agent_id, top_k=req.top_k
            )
        elif req.mode == "hybrid":
            results = await store.search_hybrid(
                req.query, req.agent_id, top_k=req.top_k
            )
        else:
            results = await store.search_reranked(
                req.query, req.agent_id, top_k=req.top_k
            )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    memories = []
    for r in results:
        score = (
            r.get("final_score")
            or r.get("weighted_score")
            or r.get("similarity")
            or 0.0
        )
        memories.append(
            MemoryResponse(
                id=r["id"],
                content=r["content"],
                type=r["type"],
                confidence=r.get("confidence", 0.5),
                importance=r.get("importance", 0.5),
                access_count=r.get("access_count", 0),
                tags=r.get("tags", []),
                source=r.get("source"),
                created_at=str(r.get("created_at", "")),
                score=float(score),
            )
        )
    return {"agent_id": req.agent_id, "query": req.query, "count": len(memories), "memories": memories}


@app.get("/memory/{memory_id}")
async def get_memory(
    memory_id: str,
    agent_id: str = Query(..., description="Agent ID"),
):
    store = _store()
    mem = await store.get_memory(memory_id, agent_id)
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {
        "id": mem["id"],
        "agent_id": mem["agent_id"],
        "content": mem["content"],
        "type": mem["type"],
        "confidence": mem["confidence"],
        "importance": mem["importance"],
        "access_count": mem["access_count"],
        "tags": list(mem.get("tags", [])),
        "source": mem.get("source"),
        "created_at": str(mem["created_at"]),
        "depth_weight_alpha": mem["depth_weight_alpha"],
        "depth_weight_beta": mem["depth_weight_beta"],
    }


@app.delete("/memory/{memory_id}", response_model=DeleteResponse)
async def delete_memory(
    memory_id: str,
    agent_id: str = Query(..., description="Agent ID"),
):
    store = _store()
    deleted = await store.delete_memory(memory_id, agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return DeleteResponse(id=memory_id, deleted=True)
