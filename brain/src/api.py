"""Brain Service — FastAPI application."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .config import NOTIFICATION_MAX_PASSIVE_PER_CONTEXT, WEIGHT_CENTER_SQL
from .config import DEDUP_SIMILARITY_THRESHOLD
from .config import DELIBERATE_INITIAL_ALPHA, DELIBERATE_INITIAL_BETA, DELIBERATE_SOURCE_TAG
from .consolidation import ConsolidationEngine, dedup_pair
from .context_assembly import assemble_context, render_identity_full, render_identity_hash, render_system_prompt
from .db import close_pool, get_pool, init_pool
from .dmn_store import ThoughtQueue
from .gate import DROP, BUFFER, PERSIST, PERSIST_FLAG, PERSIST_HIGH, REINFORCE, SKIP, EntryGate, ExitGate, semantic_chunk, _estimate_tokens as _gate_estimate_tokens
from .gut import GutFeeling
from .idle import IdleLoop
from .memory import MemoryStore
from .notification import NotificationStore, DeliveryWorker
from .bootstrap import BootstrapReadiness
from .safety import SafetyMonitor, get_audit_log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("brain.api")

_start_time: float = 0.0
_memory_store: MemoryStore | None = None
_entry_gate: EntryGate | None = None
_exit_gate: ExitGate | None = None
_gut_feelings: dict[str, GutFeeling] = {}
_consolidation_engine: ConsolidationEngine | None = None
_consolidation_shutdown: asyncio.Event | None = None
_idle_loop: IdleLoop | None = None
_thought_queue: ThoughtQueue | None = None
_idle_shutdown: asyncio.Event | None = None
_safety_monitor: SafetyMonitor | None = None
_bootstrap: BootstrapReadiness | None = None
_notification_store: NotificationStore | None = None
_delivery_worker: DeliveryWorker | None = None
_delivery_shutdown: asyncio.Event | None = None


def _get_gut(agent_id: str) -> GutFeeling:
    """Get or load the GutFeeling instance for an agent."""
    if agent_id not in _gut_feelings:
        _gut_feelings[agent_id] = GutFeeling.load(agent_id)
    return _gut_feelings[agent_id]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect DB + init MemoryStore + start consolidation + DMN. Shutdown: stop all."""
    global _start_time, _memory_store, _entry_gate, _exit_gate
    global _consolidation_engine, _consolidation_shutdown
    global _idle_loop, _thought_queue, _idle_shutdown
    global _safety_monitor, _bootstrap
    global _notification_store, _delivery_worker, _delivery_shutdown
    _start_time = time.time()
    pool = await init_pool()
    _memory_store = MemoryStore(pool)
    _safety_monitor = SafetyMonitor()
    _memory_store.safety = _safety_monitor
    _entry_gate = EntryGate()
    _exit_gate = ExitGate()
    _bootstrap = BootstrapReadiness(pool)
    _notification_store = NotificationStore(pool)

    # Start consolidation engine as background task
    _consolidation_shutdown = asyncio.Event()
    _consolidation_engine = ConsolidationEngine(pool, _memory_store, _notification_store)
    consolidation_task = asyncio.create_task(
        _consolidation_engine.run(_consolidation_shutdown)
    )

    # Start DMN idle loop as background task
    _thought_queue = ThoughtQueue()
    _idle_shutdown = asyncio.Event()
    _idle_loop = IdleLoop(pool, _memory_store, _thought_queue, _get_gut, _notification_store)
    idle_task = asyncio.create_task(_idle_loop.run(_idle_shutdown))

    # Start notification delivery worker
    _delivery_shutdown = asyncio.Event()
    _delivery_worker = DeliveryWorker(pool, _notification_store)
    delivery_task = asyncio.create_task(_delivery_worker.run(_delivery_shutdown))
    logger.info("Brain service started (consolidation + DMN + notifications active).")

    yield

    # Shutdown notification delivery
    if _delivery_shutdown:
        _delivery_shutdown.set()
    delivery_task.cancel()
    try:
        await delivery_task
    except asyncio.CancelledError:
        pass

    # Shutdown DMN
    if _idle_shutdown:
        _idle_shutdown.set()
    idle_task.cancel()
    try:
        await idle_task
    except asyncio.CancelledError:
        pass

    # Shutdown consolidation
    if _consolidation_shutdown:
        _consolidation_shutdown.set()
    consolidation_task.cancel()
    try:
        await consolidation_task
    except asyncio.CancelledError:
        pass

    _memory_store = None
    _entry_gate = None
    _exit_gate = None
    _consolidation_engine = None
    _idle_loop = None
    _thought_queue = None
    _safety_monitor = None
    _bootstrap = None
    _notification_store = None
    _delivery_worker = None
    await close_pool()
    logger.info("Brain service stopped.")


app = FastAPI(title="Brain Service", version="0.7.0", lifespan=lifespan)


def _store() -> MemoryStore:
    if _memory_store is None:
        raise RuntimeError("MemoryStore not initialized.")
    return _memory_store


async def _get_identity_embeddings(agent_id: str, top_n: int = 20):
    """Delegate to MemoryStore.get_identity_embeddings (D-030 move)."""
    return await _store().get_identity_embeddings(agent_id, top_n)


# ── Health ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    pool = await get_pool()
    async with pool.acquire() as conn:
        memory_count = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE NOT archived")
        agent_count = await conn.fetchval(
            "SELECT COUNT(DISTINCT agent_id) FROM memories WHERE NOT archived"
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


class GateRequest(BaseModel):
    agent_id: str
    content: str
    source: str | None = None
    source_tag: str = "external_user"
    remind_at: str | None = None  # ISO 8601 datetime for scheduled reminders
    protect_until: str | None = None  # ISO 8601 datetime for decay protection (D-031)


class GateResponse(BaseModel):
    decision: str
    score: float
    memory_id: str | None = None
    scratch_id: str | None = None
    entry_gate: dict = Field(default_factory=dict)
    exit_gate: dict = Field(default_factory=dict)


class ContextAssembleRequest(BaseModel):
    agent_id: str
    query_text: str = ""
    conversation: list[dict] = Field(default_factory=list)
    total_budget: int = 131_072


class ContextAssembleResponse(BaseModel):
    system_prompt: str
    used_tokens: int
    conversation_budget: int
    identity_token_count: int
    context_shift: float
    inertia: float


class IdentityResponse(BaseModel):
    agent_id: str
    identity: str


class IdentityHashResponse(BaseModel):
    agent_id: str
    hash: str


class AttentionUpdateRequest(BaseModel):
    agent_id: str
    content: str


class AttentionUpdateResponse(BaseModel):
    agent_id: str
    emotional_charge: float
    emotional_alignment: float
    gut_summary: str
    attention_count: int


class GutStateResponse(BaseModel):
    agent_id: str
    emotional_charge: float
    emotional_alignment: float
    gut_summary: str
    attention_count: int
    has_subconscious: bool
    has_attention: bool
    recent_deltas: list[dict] = Field(default_factory=list)


class ConsolidationStatusResponse(BaseModel):
    running: bool
    constant: dict = Field(default_factory=dict)
    deep: dict = Field(default_factory=dict)


class ConsolidationTriggerRequest(BaseModel):
    agent_id: str


class ConsolidationTriggerResponse(BaseModel):
    agent_id: str
    triggered: bool
    message: str


class DedupSweepRequest(BaseModel):
    agent_id: str
    similarity_threshold: float = Field(default=0.75, ge=0.5, le=1.0)
    dry_run: bool = Field(default=True, description="If true, report pairs but don't execute verdicts")
    limit: int = Field(default=500, ge=1, le=2000, description="Max pairs to process")


class DedupSweepResponse(BaseModel):
    agent_id: str
    pairs_found: int
    pairs_processed: int
    redundant: int
    archived: int
    distinct: int
    errors: int
    dry_run: bool


class DMNThoughtResponse(BaseModel):
    agent_id: str
    thoughts: list[dict] = Field(default_factory=list)
    count: int


class DMNStatusResponse(BaseModel):
    running: bool
    heartbeat_counts: dict = Field(default_factory=dict)
    queue_sizes: dict = Field(default_factory=dict)
    active_threads: dict = Field(default_factory=dict)


class DMNActivityRequest(BaseModel):
    agent_id: str


class DMNActivityResponse(BaseModel):
    agent_id: str
    acknowledged: bool
    idle_seconds: float


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


# ── Gate ──────────────────────────────────────────────────────────────


@app.post("/memory/gate", response_model=GateResponse)
async def gate_memory(req: GateRequest):
    """Entry gate -> scratch buffer -> exit gate -> persist/reinforce/buffer/drop."""
    store = _store()
    if _entry_gate is None or _exit_gate is None:
        raise HTTPException(status_code=503, detail="Gates not initialized.")

    # 1. Entry gate: stochastic filter
    should_buffer, entry_meta = _entry_gate.evaluate(
        req.content, source=req.source or "unknown", source_tag=req.source_tag,
    )
    if not should_buffer:
        return GateResponse(
            decision=entry_meta["decision"],
            score=0.0,
            entry_gate=entry_meta,
        )

    # 2. Buffer to scratch
    try:
        scratch_id = await store.buffer_scratch(
            req.content, req.agent_id,
            source=req.source,
            tags=[],
            metadata={"source_tag": req.source_tag},
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Scratch buffer failed: {e}")

    # 3. Exit gate: relevance from top-N identity memories (D-005: unified memory)
    try:
        layer_embeddings = await _get_identity_embeddings(req.agent_id)
    except Exception as e:
        logger.warning("Identity embeddings unavailable for %s: %s", req.agent_id, e)
        layer_embeddings = None

    # Phase 4: load GutFeeling, pass attention_centroid + emotional_charge
    gut = _get_gut(req.agent_id)
    mem_count = await store.memory_count(req.agent_id)
    try:
        decision, score, exit_meta = await _exit_gate.evaluate(
            req.content,
            req.agent_id,
            store,
            layer_embeddings=layer_embeddings,
            attention_embedding=gut.attention_centroid,
            emotional_charge=gut.emotional_charge,
            source_tag=req.source_tag,
            memory_count=mem_count,
        )
    except RuntimeError as e:
        # Embedding failure -> keep in scratch buffer, don't lose data
        logger.warning("Exit gate embedding failed, keeping in scratch: %s", e)
        return GateResponse(
            decision=BUFFER,
            score=0.0,
            scratch_id=scratch_id,
            entry_gate=entry_meta,
            exit_gate={"error": str(e)},
        )

    memory_id = None

    # 4. Act on decision
    if decision in (PERSIST, PERSIST_HIGH, PERSIST_FLAG):
        # Promote to long-term memory (with semantic chunking for long content)
        try:
            importance = min(0.9, 0.5 + score * 0.4)

            # D-029: deliberate memories get higher initial weights
            extra_kw: dict = {}
            if req.source_tag == DELIBERATE_SOURCE_TAG:
                extra_kw["initial_alpha"] = DELIBERATE_INITIAL_ALPHA
                extra_kw["initial_beta"] = DELIBERATE_INITIAL_BETA
            if req.remind_at:
                extra_kw["remind_at"] = datetime.fromisoformat(req.remind_at)
            if req.protect_until:
                extra_kw["protect_until"] = datetime.fromisoformat(req.protect_until)

            content_tokens = _gate_estimate_tokens(req.content)

            if content_tokens > 300:
                # D-018b: chunk long content, link via memory_group_id
                chunks = semantic_chunk(req.content)
                group_id = MemoryStore._gen_id("grp")
                total = len(chunks)
                first_id = None
                for idx, chunk_text in enumerate(chunks, 1):
                    chunk_meta = {
                        "gate_decision": decision,
                        "gate_score": score,
                        "group_part": idx,
                        "group_total": total,
                    }
                    # remind_at + protect_until only on first chunk
                    chunk_extra = {**extra_kw}
                    if idx > 1:
                        chunk_extra.pop("remind_at", None)
                        chunk_extra.pop("protect_until", None)
                    mid = await store.store_memory(
                        content=chunk_text,
                        agent_id=req.agent_id,
                        source=req.source,
                        source_tag=req.source_tag,
                        importance=importance,
                        metadata=chunk_meta,
                        memory_group_id=group_id,
                        **chunk_extra,
                    )
                    if first_id is None:
                        first_id = mid
                memory_id = first_id
                logger.info(
                    "Chunked %d tokens into %d memories (group=%s) [%s]",
                    content_tokens, total, group_id, req.agent_id,
                )
            else:
                # Single memory, no group
                memory_id = await store.store_memory(
                    content=req.content,
                    agent_id=req.agent_id,
                    source=req.source,
                    source_tag=req.source_tag,
                    importance=importance,
                    metadata={"gate_decision": decision, "gate_score": score},
                    **extra_kw,
                )
            # Remove from scratch
            await store.pool.execute(
                "DELETE FROM scratch_buffer WHERE id = $1 AND agent_id = $2",
                scratch_id, req.agent_id,
            )
            scratch_id = None
        except RuntimeError as e:
            logger.error("Failed to persist gated memory: %s", e)
            decision = BUFFER  # Fall back to scratch

        # Triggered dedup: only for non-chunked single memories with high similarity
        if (
            memory_id is not None
            and content_tokens <= 300  # skip chunked memories
            and exit_meta.get("max_similarity", 0) >= DEDUP_SIMILARITY_THRESHOLD
            and exit_meta.get("most_similar_id")
            and exit_meta.get("most_similar_id") != memory_id
        ):
            try:
                verdict = await dedup_pair(
                    store.pool, store, req.agent_id,
                    memory_id, exit_meta["most_similar_id"],
                )
                if verdict and verdict["verdict"] == "redundant":
                    survivor_id = await store.execute_dedup_verdict(
                        req.agent_id, verdict,
                    )
                    if survivor_id and survivor_id != memory_id:
                        memory_id = survivor_id
                    exit_meta["dedup_verdict"] = verdict["verdict"]
                    exit_meta["dedup_survivor"] = survivor_id
                    logger.info(
                        "Dedup at gate: verdict=%s, survivor=%s [%s]",
                        verdict["verdict"], survivor_id, req.agent_id,
                    )
                elif verdict:
                    exit_meta["dedup_verdict"] = verdict["verdict"]
            except Exception:
                logger.warning("Dedup failed at gate (non-fatal) [%s]", req.agent_id, exc_info=True)

    elif decision == REINFORCE:
        # Find most similar memory and reinforce it
        try:
            similar = await store.search_similar(
                req.content, req.agent_id, top_k=1, min_similarity=0.7,
            )
            if similar:
                memory_id = similar[0]["id"]
                await store.apply_retrieval_mutation(
                    [memory_id], req.agent_id,
                )
            # Remove from scratch either way
            await store.pool.execute(
                "DELETE FROM scratch_buffer WHERE id = $1 AND agent_id = $2",
                scratch_id, req.agent_id,
            )
            scratch_id = None
        except RuntimeError as e:
            logger.error("Failed to reinforce: %s", e)

    elif decision in (DROP, SKIP):
        # Clean up scratch
        await store.pool.execute(
            "DELETE FROM scratch_buffer WHERE id = $1 AND agent_id = $2",
            scratch_id, req.agent_id,
        )
        scratch_id = None

    # BUFFER: leave in scratch (24h TTL, consolidation may pick it up)

    return GateResponse(
        decision=decision,
        score=score,
        memory_id=memory_id,
        scratch_id=scratch_id,
        entry_gate=entry_meta,
        exit_gate=exit_meta,
    )


# ── Context Assembly ─────────────────────────────────────────────────


@app.post("/context/assemble", response_model=ContextAssembleResponse)
async def context_assemble(req: ContextAssembleRequest):
    """Assemble full context (identity + memories + gut) for an agent."""
    store = _store()
    gut = _get_gut(req.agent_id)

    # Phase 4: update attention centroid from query
    if req.query_text:
        try:
            query_embedding = await store.embed(
                req.query_text, task_type="SEMANTIC_SIMILARITY"
            )
            gut.update_attention(query_embedding)
        except RuntimeError as e:
            logger.warning("Attention embedding failed for %s: %s", req.agent_id, e)

    # Phase 4: update subconscious from current identity
    try:
        identity_embs = await _get_identity_embeddings(req.agent_id)
        gut.update_subconscious(identity_embs)
    except Exception as e:
        logger.warning("Subconscious update failed for %s: %s", req.agent_id, e)

    # Compute emotional delta
    gut.compute_delta(context=req.query_text[:100] if req.query_text else "")

    # Build cognitive state with passive notifications (D-019)
    cognitive_state = gut.gut_summary()
    if _notification_store:
        try:
            pending_notifs = await _notification_store.get_pending_passive(
                req.agent_id, limit=NOTIFICATION_MAX_PASSIVE_PER_CONTEXT,
            )
            if pending_notifs:
                notif_lines = [f"- [{n['source']}] {n['content']}" for n in pending_notifs]
                cognitive_state += "\n[Pending Notifications]\n" + "\n".join(notif_lines)
                for n in pending_notifs:
                    await _notification_store.mark_delivered(n["id"])
        except Exception as e:
            logger.warning("Passive notification fetch failed for %s: %s", req.agent_id, e)

    context = await assemble_context(
        memory_store=store,
        agent_id=req.agent_id,
        attention_embedding=gut.attention_centroid,
        previous_attention_embedding=gut.previous_attention_centroid,
        cognitive_state_report=cognitive_state,
        query_text=req.query_text,
        conversation=req.conversation,
        total_budget=req.total_budget,
    )

    # Mutate memories that were actually injected into context
    injected_ids = context.get("injected_memory_ids", [])
    if injected_ids:
        try:
            await store.apply_retrieval_mutation(injected_ids, req.agent_id)
        except Exception as e:
            logger.warning("Context injection mutation failed for %s: %s", req.agent_id, e)

    system_prompt = render_system_prompt(context)

    # Persist gut state
    try:
        gut.save()
    except Exception as e:
        logger.warning("Gut state save failed for %s: %s", req.agent_id, e)

    return ContextAssembleResponse(
        system_prompt=system_prompt,
        used_tokens=context["used_tokens"],
        conversation_budget=context["conversation_budget"],
        identity_token_count=context["identity_token_count"],
        context_shift=context["context_shift"],
        inertia=context["inertia"],
    )


# ── Identity ─────────────────────────────────────────────────────────


@app.get("/identity/{agent_id}", response_model=IdentityResponse)
async def get_identity(agent_id: str):
    """Full identity render from top unified memories (D-005)."""
    store = _store()
    identity = await render_identity_full(store, agent_id)
    return IdentityResponse(agent_id=agent_id, identity=identity)


@app.get("/identity/{agent_id}/hash", response_model=IdentityHashResponse)
async def get_identity_hash(agent_id: str):
    """Compact identity hash (~100-200 tokens) from top unified memories (D-005)."""
    store = _store()
    hash_text = await render_identity_hash(store, agent_id)
    return IdentityHashResponse(agent_id=agent_id, hash=hash_text)


# ── Gut Feeling ──────────────────────────────────────────────────────


@app.post("/context/attention", response_model=AttentionUpdateResponse)
async def update_attention(req: AttentionUpdateRequest):
    """Update attention centroid with new content embedding (standalone)."""
    store = _store()
    gut = _get_gut(req.agent_id)

    try:
        embedding = await store.embed(req.content, task_type="SEMANTIC_SIMILARITY")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"Embedding failed: {e}")

    gut.update_attention(embedding)

    # Also refresh subconscious from current identity
    try:
        identity_embs = await _get_identity_embeddings(req.agent_id)
        gut.update_subconscious(identity_embs)
    except Exception as e:
        logger.warning("Subconscious update failed for %s: %s", req.agent_id, e)

    gut.compute_delta(context=req.content[:100])

    try:
        gut.save()
    except Exception as e:
        logger.warning("Gut state save failed for %s: %s", req.agent_id, e)

    return AttentionUpdateResponse(
        agent_id=req.agent_id,
        emotional_charge=gut.emotional_charge,
        emotional_alignment=gut.emotional_alignment,
        gut_summary=gut.gut_summary(),
        attention_count=gut._attention_count,
    )


@app.get("/gut/{agent_id}", response_model=GutStateResponse)
async def get_gut_state(agent_id: str):
    """Get current gut feeling state for an agent."""
    gut = _get_gut(agent_id)
    return GutStateResponse(
        agent_id=agent_id,
        emotional_charge=gut.emotional_charge,
        emotional_alignment=gut.emotional_alignment,
        gut_summary=gut.gut_summary(),
        attention_count=gut._attention_count,
        has_subconscious=gut.subconscious_centroid is not None,
        has_attention=gut.attention_centroid is not None,
        recent_deltas=gut._delta_log[-10:],
    )


# ── Consolidation ────────────────────────────────────────────────────


@app.get("/consolidation/status", response_model=ConsolidationStatusResponse)
async def consolidation_status():
    """Get consolidation engine status."""
    if _consolidation_engine is None:
        return ConsolidationStatusResponse(running=False)
    return ConsolidationStatusResponse(**_consolidation_engine.status())


@app.post("/consolidation/trigger", response_model=ConsolidationTriggerResponse)
async def consolidation_trigger(req: ConsolidationTriggerRequest):
    """Trigger an immediate deep consolidation cycle for an agent."""
    if _consolidation_engine is None:
        raise HTTPException(status_code=503, detail="Consolidation engine not running.")
    _consolidation_engine.trigger(req.agent_id)
    return ConsolidationTriggerResponse(
        agent_id=req.agent_id,
        triggered=True,
        message=f"Deep consolidation triggered for agent {req.agent_id}.",
    )


@app.post("/consolidation/dedup-sweep", response_model=DedupSweepResponse)
async def consolidation_dedup_sweep(req: DedupSweepRequest):
    """Retroactive batch dedup sweep: find high-similarity pairs via pgvector,
    run LLM arbitration on each, execute verdicts.

    Use dry_run=true (default) to preview without archiving.
    """
    store = _store()
    pool = store.pool

    # Find high-similarity pairs using pgvector cosine distance self-join.
    # (1 - cosine_distance) >= threshold  →  cosine_distance <= (1 - threshold)
    max_distance = 1.0 - req.similarity_threshold
    pairs = await pool.fetch(
        """
        SELECT a.id AS id_a, b.id AS id_b,
               1 - (a.embedding <=> b.embedding) AS sim
        FROM memories a
        JOIN memories b ON a.id < b.id
            AND a.agent_id = b.agent_id
            AND (a.embedding <=> b.embedding) <= $2
        WHERE a.agent_id = $1
          AND NOT a.archived AND NOT b.archived
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
        ORDER BY (a.embedding <=> b.embedding) ASC
        LIMIT $3
        """,
        req.agent_id,
        max_distance,
        req.limit,
    )

    pairs_found = len(pairs)
    redundant = 0
    archived = 0
    distinct = 0
    errors = 0

    for pair in pairs:
        try:
            verdict = await dedup_pair(
                pool, store, req.agent_id,
                pair["id_a"], pair["id_b"],
            )
            if verdict is None:
                errors += 1
                continue
            if verdict["verdict"] == "redundant":
                redundant += 1
                if not req.dry_run:
                    survivor_id = await store.execute_dedup_verdict(
                        req.agent_id, verdict,
                    )
                    if survivor_id:
                        archived += 1
            else:
                distinct += 1
        except Exception:
            logger.warning(
                "Dedup sweep error for pair (%s, %s) [%s]",
                pair["id_a"], pair["id_b"], req.agent_id,
                exc_info=True,
            )
            errors += 1

    logger.info(
        "Dedup sweep complete [%s]: %d pairs, %d redundant, %d archived, %d distinct, %d errors (dry_run=%s)",
        req.agent_id, pairs_found, redundant, archived, distinct, errors, req.dry_run,
    )

    return DedupSweepResponse(
        agent_id=req.agent_id,
        pairs_found=pairs_found,
        pairs_processed=pairs_found - errors,
        redundant=redundant,
        archived=archived,
        distinct=distinct,
        errors=errors,
        dry_run=req.dry_run,
    )


# ── DMN / Idle Loop ──────────────────────────────────────────────────


@app.get("/dmn/thoughts", response_model=DMNThoughtResponse)
async def dmn_thoughts(agent_id: str = Query(..., description="Agent ID")):
    """Drain pending DMN thoughts for an agent."""
    if _thought_queue is None:
        raise HTTPException(status_code=503, detail="DMN not initialized.")
    thoughts = _thought_queue.get_thoughts(agent_id)
    return DMNThoughtResponse(
        agent_id=agent_id,
        thoughts=[t.to_dict() for t in thoughts],
        count=len(thoughts),
    )


@app.get("/dmn/status", response_model=DMNStatusResponse)
async def dmn_status():
    """Get DMN idle loop status."""
    if _idle_loop is None:
        return DMNStatusResponse(running=False)
    return DMNStatusResponse(**_idle_loop.status())


@app.post("/dmn/activity", response_model=DMNActivityResponse)
async def dmn_activity(req: DMNActivityRequest):
    """Notify brain of user activity — resets idle timer for DMN."""
    if _idle_loop is None:
        raise HTTPException(status_code=503, detail="DMN not initialized.")
    _idle_loop.notify_activity(req.agent_id)
    idle_secs = time.time() - _idle_loop.last_activity.get(req.agent_id, time.time())
    return DMNActivityResponse(
        agent_id=req.agent_id,
        acknowledged=True,
        idle_seconds=max(0.0, idle_secs),
    )


# ── Monologue (Unified Inner View) ──────────────────────────────────


class MonologueEntry(BaseModel):
    ts: str
    type: str  # "thought", "consolidation", "tension", "narrative", "reflection"
    content: str
    channel: str | None = None
    operation: str | None = None
    source_memory_id: str | None = None
    memory_id: str | None = None
    details: dict | None = None


class RuminationSummary(BaseModel):
    active: dict | None = None
    recent_completed: list[dict] = Field(default_factory=list)


class MonologueResponse(BaseModel):
    agent_id: str
    entries: list[MonologueEntry] = Field(default_factory=list)
    rumination: RuminationSummary = Field(default_factory=RuminationSummary)


@app.get("/monologue/{agent_id}", response_model=MonologueResponse)
async def monologue(agent_id: str, limit: int = Query(default=50, ge=1, le=200)):
    """Unified view of the agent's inner monologue: DMN thoughts,
    consolidation operations, and recent reflection/tension/narrative memories."""
    pool = await get_pool()

    # Query all sources in parallel
    dmn_rows, consol_rows, memory_rows = await asyncio.gather(
        pool.fetch(
            """
            SELECT thought, channel, source_memory_id, created_at
            FROM dmn_log
            WHERE agent_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            agent_id,
            limit,
        ),
        pool.fetch(
            """
            SELECT operation, details, created_at
            FROM consolidation_log
            WHERE agent_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            agent_id,
            limit,
        ),
        pool.fetch(
            """
            SELECT id, content, type, created_at
            FROM memories
            WHERE agent_id = $1
              AND NOT archived
              AND type IN ('tension', 'narrative', 'reflection')
              AND source = 'consolidation'
            ORDER BY created_at DESC LIMIT $2
            """,
            agent_id,
            limit,
        ),
    )

    entries: list[MonologueEntry] = []

    for row in dmn_rows:
        entries.append(MonologueEntry(
            ts=row["created_at"].isoformat(),
            type="thought",
            content=row["thought"],
            channel=row["channel"],
            source_memory_id=row["source_memory_id"],
        ))

    for row in consol_rows:
        details = row["details"] if isinstance(row["details"], dict) else {}
        summary = details.get("summary", row["operation"])
        entries.append(MonologueEntry(
            ts=row["created_at"].isoformat(),
            type="consolidation",
            content=str(summary),
            operation=row["operation"],
            details=details,
        ))

    for row in memory_rows:
        entries.append(MonologueEntry(
            ts=row["created_at"].isoformat(),
            type=row["type"],
            content=row["content"],
            memory_id=row["id"],
        ))

    # Sort all entries by timestamp descending, take top N
    entries.sort(key=lambda e: e.ts, reverse=True)
    entries = entries[:limit]

    # Rumination state
    rumination = RuminationSummary()
    if _idle_loop is not None:
        rm = _idle_loop._rumination.get(agent_id)
        if rm is not None:
            if rm.has_active_thread():
                t = rm.active_thread
                rumination.active = {
                    "topic": t.topic,
                    "cycle_count": t.cycle_count,
                    "seed_memory_id": t.seed_memory_id,
                    "history": [
                        {"cycle": h["cycle"], "summary": h["summary"], "ts": h["ts"]}
                        for h in (t.history[-5:] if t.history else [])
                    ],
                }
            rumination.recent_completed = [
                {
                    "topic": ct.get("topic", "")[:100],
                    "resolution_reason": ct.get("reason", ""),
                    "cycles": ct.get("cycles", 0),
                }
                for ct in (rm.completed_threads[-5:] if rm.completed_threads else [])
            ]

    return MonologueResponse(
        agent_id=agent_id,
        entries=entries,
        rumination=rumination,
    )


# ── Safety Monitor ──────────────────────────────────────────────────


class SafetyStatusResponse(BaseModel):
    phase_a: dict = Field(default_factory=dict)
    phase_b: dict = Field(default_factory=dict)
    phase_c: dict = Field(default_factory=dict)
    audit_log_size: int = 0


class SafetyAuditResponse(BaseModel):
    events: list[dict] = Field(default_factory=list)
    count: int = 0


@app.get("/safety/status", response_model=SafetyStatusResponse)
async def safety_status():
    """Get current safety monitor status."""
    if _safety_monitor is None:
        return SafetyStatusResponse()
    return SafetyStatusResponse(**_safety_monitor.status())


@app.get("/safety/audit", response_model=SafetyAuditResponse)
async def safety_audit(limit: int = Query(default=50, ge=1, le=1000)):
    """Get recent safety audit events."""
    events = get_audit_log()
    recent = events[-limit:]
    return SafetyAuditResponse(events=recent, count=len(events))


# ── Injection Metrics (D-018d) ─────────────────────────────────────


class InjectionMetricsResponse(BaseModel):
    agent_id: str
    days: int
    total_logs: int = 0
    injection_rate: float = 0.0
    score_stats: dict = Field(default_factory=dict)
    top_memories: list[dict] = Field(default_factory=list)


@app.get("/injection/metrics", response_model=InjectionMetricsResponse)
async def injection_metrics(
    agent_id: str = Query(..., description="Agent ID"),
    days: int = Query(default=7, ge=1, le=90),
):
    """Rolling w×s injection stats for empirical formula validation."""
    pool = await get_pool()

    # Stats + percentiles in one query
    stats_row = await pool.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE was_injected) AS injected,
               avg(injection_score) AS avg_score,
               percentile_cont(ARRAY[0.5, 0.75, 0.95])
                 WITHIN GROUP (ORDER BY injection_score) AS pcts
        FROM injection_logs
        WHERE agent_id = $1
          AND created_at >= NOW() - make_interval(days => $2)
        """,
        agent_id,
        days,
    )

    total = stats_row["total"] or 0
    injected = stats_row["injected"] or 0
    pcts = stats_row["pcts"] or [0.0, 0.0, 0.0]

    score_stats = {
        "avg": round(float(stats_row["avg_score"] or 0), 4),
        "p50": round(float(pcts[0]), 4),
        "p75": round(float(pcts[1]), 4),
        "p95": round(float(pcts[2]), 4),
    }

    # Top memories by injection count
    top_rows = await pool.fetch(
        """
        SELECT memory_id, count(*) AS inj_count,
               round(avg(injection_score)::numeric, 4) AS avg_score
        FROM injection_logs
        WHERE agent_id = $1
          AND was_injected = true
          AND created_at >= NOW() - make_interval(days => $2)
        GROUP BY memory_id
        ORDER BY inj_count DESC
        LIMIT 10
        """,
        agent_id,
        days,
    )

    return InjectionMetricsResponse(
        agent_id=agent_id,
        days=days,
        total_logs=total,
        injection_rate=round(injected / total, 4) if total > 0 else 0.0,
        score_stats=score_stats,
        top_memories=[
            {"memory_id": r["memory_id"], "count": r["inj_count"],
             "avg_score": float(r["avg_score"])}
            for r in top_rows
        ],
    )


# ── Bootstrap Readiness ─────────────────────────────────────────────


class BootstrapStatusResponse(BaseModel):
    agent_id: str = ""
    milestones: list[dict] = Field(default_factory=list)
    achieved: int = 0
    total: int = 10
    ready: bool = False
    bootstrap_prompt: str | None = None
    status_text: str = ""


@app.get("/bootstrap/status", response_model=BootstrapStatusResponse)
async def bootstrap_status(agent_id: str = Query(..., description="Agent ID")):
    """Check bootstrap readiness milestones for an agent."""
    if _bootstrap is None:
        raise HTTPException(status_code=503, detail="Bootstrap checker not initialized.")
    result = await _bootstrap.check_all(agent_id)
    return BootstrapStatusResponse(**result)


# ── Notifications (D-019) ─────────────────────────────────────────


class NotificationPreferencesRequest(BaseModel):
    agent_id: str
    telegram_chat_id: str | None = None
    telegram_enabled: bool | None = None
    quiet_hours_start: int | None = None
    quiet_hours_end: int | None = None
    urgency_threshold: float | None = None
    importance_threshold: float | None = None
    enabled: bool | None = None


class NotificationStatusResponse(BaseModel):
    delivery_worker_running: bool = False
    store_initialized: bool = False


@app.get("/notifications/pending")
async def notifications_pending(agent_id: str = Query(..., description="Agent ID")):
    """Get pending passive notifications for an agent."""
    if _notification_store is None:
        raise HTTPException(status_code=503, detail="Notification store not initialized.")
    pending = await _notification_store.get_pending_passive(agent_id)
    return {"agent_id": agent_id, "notifications": pending, "count": len(pending)}


@app.post("/notifications/preferences")
async def set_notification_preferences(req: NotificationPreferencesRequest):
    """Set notification preferences for an agent."""
    if _notification_store is None:
        raise HTTPException(status_code=503, detail="Notification store not initialized.")
    kwargs = {k: v for k, v in req.model_dump().items() if k != "agent_id" and v is not None}
    await _notification_store.set_preferences(req.agent_id, **kwargs)
    prefs = await _notification_store.get_preferences(req.agent_id)
    return {"agent_id": req.agent_id, "preferences": prefs}


@app.get("/notifications/preferences/{agent_id}")
async def get_notification_preferences(agent_id: str):
    """Get notification preferences for an agent."""
    if _notification_store is None:
        raise HTTPException(status_code=503, detail="Notification store not initialized.")
    prefs = await _notification_store.get_preferences(agent_id)
    return {"agent_id": agent_id, "preferences": prefs}


@app.get("/notifications/status", response_model=NotificationStatusResponse)
async def notification_status():
    """Get notification system status."""
    return NotificationStatusResponse(
        delivery_worker_running=_delivery_worker.running if _delivery_worker else False,
        store_initialized=_notification_store is not None,
    )
