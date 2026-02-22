"""Context assembly -- dynamic injection with identity, safety, and situational memories.

Builds a structured system prompt from:
  Track 0: immutable safety memories (always present)
  Active identity: w×s scored memories (D-015 — weight_center × cosine_sim)
  Track 1: situational memories (hybrid search within budget)

D-005: Identity is the weights. No L0/L1 layers — identity emerges from
high-weight memories in the unified table.
D-015/DJ-005: injection_score = weight_center × cosine_sim. No floor.
D-017/DJ-006: Identity hash feature-flagged dormant.
D-018a: Adaptive context shift threshold — P75 of last 200 shift values.
"""

import hashlib
import logging

from .activation import cosine_similarity
from .config import PRIORITY_IMPORTANCE_THRESHOLD, WEIGHT_CENTER_SQL

logger = logging.getLogger("brain.context")

# Token budget allocation
BUDGET_IMMUTABLE_SAFETY = 100
BUDGET_IDENTITY_AVG = 1500
BUDGET_IDENTITY_MAX = 3000
BUDGET_SITUATIONAL = 2000
BUDGET_COGNITIVE_STATE = 200
BUDGET_ATTENTION_FIELD = 500
BUDGET_OUTPUT_BUFFER = 4000

IDENTITY_TOP_N = 20

# Identity hash feature flag (D-017/DJ-006: dormant)
IDENTITY_HASH_ENABLED = False

# Identity render constants
IDENTITY_HASH_TOP_N = 10
IDENTITY_FULL_TOP_N = 30

# Adaptive context shift (D-018a)
ADAPTIVE_SHIFT_BUFFER_SIZE = 200
ADAPTIVE_SHIFT_DEFAULT = 0.5  # Bootstrap threshold when < 200 values

# Identity cache — per-agent, invalidated when context_shift >= adaptive threshold
_identity_cache: dict[str, list[dict]] = {}  # agent_id -> cached identity_candidates


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


async def _get_adaptive_threshold(pool, agent_id: str) -> float:
    """P75 of last 200 context_shift values. Returns bootstrap default if < 200 values."""
    row = await pool.fetchrow(
        """
        SELECT count(*) AS cnt,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY shift_value) AS p75
        FROM (
            SELECT shift_value
            FROM context_shift_buffer
            WHERE agent_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        ) sub
        """,
        agent_id,
        ADAPTIVE_SHIFT_BUFFER_SIZE,
    )
    if row["cnt"] < ADAPTIVE_SHIFT_BUFFER_SIZE:
        return ADAPTIVE_SHIFT_DEFAULT
    return float(row["p75"])


async def _record_context_shift(pool, agent_id: str, shift_value: float) -> None:
    """Append shift_value to the ring buffer and trim to 200 entries."""
    try:
        await pool.execute(
            "INSERT INTO context_shift_buffer (agent_id, shift_value) VALUES ($1, $2)",
            agent_id, shift_value,
        )
        # Trim: keep only the most recent ADAPTIVE_SHIFT_BUFFER_SIZE rows
        await pool.execute(
            """
            DELETE FROM context_shift_buffer
            WHERE agent_id = $1
              AND id NOT IN (
                  SELECT id FROM context_shift_buffer
                  WHERE agent_id = $1
                  ORDER BY created_at DESC
                  LIMIT $2
              )
            """,
            agent_id,
            ADAPTIVE_SHIFT_BUFFER_SIZE,
        )
    except Exception:
        logger.warning("Context shift buffer write failed for %s", agent_id)


async def assemble_context(
    memory_store,
    agent_id: str,
    attention_embedding=None,
    previous_attention_embedding=None,
    cognitive_state_report: str = "",
    conversation: list[dict] | None = None,
    total_budget: int = 131_072,
    query_text: str = "",
) -> dict:
    """Assemble full context for an agent's LLM call.

    Returns dict with: parts, used_tokens, conversation_budget,
    identity_token_count, context_shift, inertia.
    """
    used_tokens = 0
    injected_memory_ids: list[str] = []
    parts: dict = {
        "immutable": [],
        "identity_hash": "",
        "identity_memories": [],
        "situational": [],
        "cognitive_state": cognitive_state_report,
    }

    # Track 0: immutable safety (always injected)
    immutable = await _get_immutable_memories(memory_store, agent_id)
    for mem in immutable:
        parts["immutable"].append(mem["content"])
        used_tokens += _estimate_tokens(mem["content"])

    # Identity hash (D-017: feature-flagged dormant)
    if IDENTITY_HASH_ENABLED:
        identity_hash = await render_identity_hash(memory_store, agent_id)
        parts["identity_hash"] = identity_hash
        used_tokens += _estimate_tokens(identity_hash)

    # Context shift (D-018a: computed early for identity cache gating)
    context_shift = 1.0
    if attention_embedding is not None and previous_attention_embedding is not None:
        context_shift = 1.0 - cosine_similarity(
            attention_embedding, previous_attention_embedding
        )
    adaptive_threshold = await _get_adaptive_threshold(
        memory_store.pool, agent_id
    )

    # Active identity: w×s scored memories (D-015) with caching (D-018a)
    identity_tokens = 0
    identity_candidates = []
    cache_hit = False
    if query_text:
        # Check identity cache — reuse if shift < adaptive threshold
        if (
            context_shift < adaptive_threshold
            and agent_id in _identity_cache
        ):
            identity_candidates = _identity_cache[agent_id]
            cache_hit = True
            logger.debug(
                "Identity cache hit for %s (shift=%.3f < threshold=%.3f)",
                agent_id, context_shift, adaptive_threshold,
            )
        else:
            try:
                query_vec = await memory_store.embed(
                    query_text, task_type="RETRIEVAL_QUERY"
                )
                identity_candidates = await memory_store.score_identity_wxs(
                    query_vec, agent_id, IDENTITY_TOP_N
                )
            except RuntimeError:
                identity_candidates = []
            # Cache the result
            _identity_cache[agent_id] = identity_candidates
            logger.debug(
                "Identity recomputed for %s (shift=%.3f >= threshold=%.3f)",
                agent_id, context_shift, adaptive_threshold,
            )
        for mem in identity_candidates:
            if identity_tokens >= BUDGET_IDENTITY_MAX:
                break
            content = _annotate_chunk(mem)
            tokens = _estimate_tokens(content)
            if identity_tokens + tokens <= BUDGET_IDENTITY_MAX:
                parts["identity_memories"].append(content)
                identity_tokens += tokens
                injected_memory_ids.append(mem["id"])
    used_tokens += identity_tokens

    # Log injection decisions (D-018d) — non-blocking
    if identity_candidates:
        try:
            injected_set = set(injected_memory_ids)
            query_hash = hashlib.sha256(query_text.encode()).hexdigest()[:16]
            log_rows = []
            for mem in identity_candidates:
                alpha = mem["depth_weight_alpha"]
                beta = mem["depth_weight_beta"]
                wc = alpha / (alpha + beta) if (alpha + beta) > 0 else 0.0
                cs = mem["injection_score"] / wc if wc > 0 else 0.0
                log_rows.append((
                    agent_id, mem["id"], wc, cs,
                    mem["injection_score"], mem["id"] in injected_set,
                    query_hash,
                ))
            await memory_store.pool.executemany(
                """INSERT INTO injection_logs
                   (agent_id, memory_id, weight_center, cosine_sim,
                    injection_score, was_injected, query_hash)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                log_rows,
            )
        except Exception:
            logger.warning("Injection logging failed for %s", agent_id)

    # Cognitive state
    if cognitive_state_report:
        used_tokens += _estimate_tokens(cognitive_state_report)

    # Priority injection bypass (D-033): force-inject high-importance memories
    # (e.g., fired reminders with importance=1.0) regardless of w×s scoring.
    # One-shot: importance reset to 0.5 after injection so it doesn't repeat.
    priority_tokens = 0
    try:
        priority_rows = await memory_store.pool.fetch(
            """
            SELECT id, content, type, metadata, importance,
                   depth_weight_alpha, depth_weight_beta
            FROM memories
            WHERE agent_id = $1 AND NOT archived
              AND importance >= $2
              AND id != ALL($3::text[])
            ORDER BY importance DESC
            LIMIT 5
            """,
            agent_id,
            PRIORITY_IMPORTANCE_THRESHOLD,
            injected_memory_ids or [""],
        )
        priority_ids = []
        for row in priority_rows:
            mem = dict(row)
            content = _annotate_chunk(mem)
            tokens = _estimate_tokens(content)
            parts["situational"].append(content)
            priority_tokens += tokens
            injected_memory_ids.append(mem["id"])
            priority_ids.append(mem["id"])
            logger.info(
                "Priority injection: %s (importance=%.2f) [%s]",
                mem["id"], mem["importance"], agent_id,
            )
        # One-shot: reset importance so these don't force-inject every turn
        if priority_ids:
            await memory_store.pool.execute(
                """
                UPDATE memories SET importance = 0.5
                WHERE id = ANY($1::text[]) AND agent_id = $2
                """,
                priority_ids,
                agent_id,
            )
    except Exception:
        logger.warning("Priority injection query failed for %s", agent_id)
    used_tokens += priority_tokens

    # Track 1: situational (competition-based)
    situational_budget = min(
        BUDGET_SITUATIONAL - priority_tokens,
        total_budget - used_tokens - BUDGET_OUTPUT_BUFFER,
    )
    if situational_budget > 0 and query_text:
        situational = await _get_situational_memories(
            memory_store, agent_id, query_text, situational_budget,
            exclude_ids=set(injected_memory_ids),
        )
        for mem in situational:
            content = mem.get("compressed") or mem["content"]
            content = _annotate_chunk(mem, content)
            parts["situational"].append(content)
            if mem.get("id"):
                injected_memory_ids.append(mem["id"])
    used_tokens += sum(_estimate_tokens(s) for s in parts["situational"]) - priority_tokens

    # Context inertia (D-018a: adaptive threshold replaces hardcoded 0.7)
    # context_shift already computed above for identity cache gating
    inertia = 0.05 if context_shift > adaptive_threshold else 0.3

    # Record shift value to ring buffer (non-blocking)
    await _record_context_shift(memory_store.pool, agent_id, context_shift)

    conversation_budget = total_budget - used_tokens - BUDGET_OUTPUT_BUFFER

    return {
        "parts": parts,
        "used_tokens": used_tokens,
        "conversation_budget": max(0, conversation_budget),
        "identity_token_count": identity_tokens,
        "context_shift": context_shift,
        "inertia": inertia,
        "injected_memory_ids": injected_memory_ids,
    }


def render_system_prompt(context: dict) -> str:
    """Render assembled context into a system prompt string."""
    sections: list[str] = []

    if context["parts"]["immutable"]:
        sections.append("[SAFETY BOUNDARIES]")
        sections.extend(context["parts"]["immutable"])
        sections.append("")

    if context["parts"]["identity_hash"]:
        sections.append("[IDENTITY]")
        sections.append(context["parts"]["identity_hash"])
        sections.append("")

    if context["parts"]["identity_memories"]:
        sections.append("[ACTIVE IDENTITY]")
        sections.extend(context["parts"]["identity_memories"])
        sections.append("")

    if context["parts"]["situational"]:
        sections.append("[RELEVANT MEMORIES]")
        sections.extend(context["parts"]["situational"])
        sections.append("")

    if context["parts"]["cognitive_state"]:
        sections.append("[COGNITIVE STATE]")
        sections.append(context["parts"]["cognitive_state"])
        sections.append("")

    return "\n".join(sections).strip()


# ── Identity rendering (from unified memory) ────────────────────────


async def render_identity_hash(memory_store, agent_id: str) -> str:
    """Compact identity summary (~100-200 tokens) from top unified memories.

    Replaces LayerStore.render_identity_hash() — identity IS the weights.
    """
    rows = await memory_store.pool.fetch(
        f"""
        SELECT content, type, immutable,
               {WEIGHT_CENTER_SQL} AS center
        FROM memories
        WHERE agent_id = $1
          AND NOT archived
          AND {WEIGHT_CENTER_SQL} > 0.3
        ORDER BY {WEIGHT_CENTER_SQL} DESC
        LIMIT $2
        """,
        agent_id,
        IDENTITY_HASH_TOP_N,
    )
    if not rows:
        return "Identity is bootstrapping. No strong memories yet."

    core_items = []
    boundary_items = []
    for r in rows:
        if r["immutable"]:
            boundary_items.append(r["content"])
        else:
            center = r["center"]
            core_items.append(f"{r['content']} ({center:.2f})")

    parts = []
    if core_items:
        parts.append("Shaped by: " + "; ".join(core_items[:5]))
    if boundary_items:
        parts.append("Boundaries: " + "; ".join(boundary_items))

    return ". ".join(parts) + "." if parts else "Identity is bootstrapping."


async def render_identity_full(memory_store, agent_id: str) -> str:
    """Full identity render (~1-2k tokens) from top unified memories.

    Replaces LayerStore.render_identity_full() — groups by memory type.
    """
    rows = await memory_store.pool.fetch(
        f"""
        SELECT content, type, immutable, confidence, importance,
               depth_weight_alpha, depth_weight_beta,
               {WEIGHT_CENTER_SQL} AS center
        FROM memories
        WHERE agent_id = $1
          AND NOT archived
          AND {WEIGHT_CENTER_SQL} > 0.2
        ORDER BY {WEIGHT_CENTER_SQL} DESC
        LIMIT $2
        """,
        agent_id,
        IDENTITY_FULL_TOP_N,
    )
    if not rows:
        return "Identity is bootstrapping. No values, beliefs, or goals have formed yet."

    sections: list[str] = []

    # Group by type
    immutable_items = [r for r in rows if r["immutable"]]
    by_type: dict[str, list] = {}
    for r in rows:
        if r["immutable"]:
            continue
        t = r["type"]
        by_type.setdefault(t, []).append(r)

    if immutable_items:
        sections.append("## Core Identity")
        for r in immutable_items:
            sections.append(f"- {r['content']}")

    # Render each non-empty type group
    type_labels = {
        "identity": "Identity",
        "semantic": "Knowledge & Beliefs",
        "preference": "Preferences",
        "episodic": "Experiences",
        "procedural": "Skills",
        "reflection": "Insights",
        "narrative": "Self-Narratives",
        "correction": "Corrections",
        "tension": "Tensions",
    }
    for mem_type, label in type_labels.items():
        items = by_type.get(mem_type, [])
        if not items:
            continue
        sections.append(f"\n## {label}")
        for r in items:
            center = r["center"]
            sections.append(f"- {r['content']} (weight: {center:.2f})")

    # Any remaining types not in the label map
    for mem_type, items in by_type.items():
        if mem_type in type_labels:
            continue
        sections.append(f"\n## {mem_type.title()}")
        for r in items:
            center = r["center"]
            sections.append(f"- {r['content']} (weight: {center:.2f})")

    return "\n".join(sections) if sections else "Identity is bootstrapping."


# ── Chunk annotation (D-018c) ────────────────────────────────────────


def _annotate_chunk(mem: dict, content: str | None = None) -> str:
    """Prepend [part N of M] to content if memory is a group chunk."""
    if content is None:
        content = mem.get("content", "")
    meta = mem.get("metadata")
    if isinstance(meta, str):
        # asyncpg may return JSONB as string in some contexts
        import json
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if isinstance(meta, dict) and "group_part" in meta and "group_total" in meta:
        part = meta["group_part"]
        total = meta["group_total"]
        return f"[part {part} of {total}] {content}"
    return content


# ── Private helpers ───────────────────────────────────────────────────


async def _get_immutable_memories(memory_store, agent_id: str) -> list[dict]:
    """Fetch memories marked as immutable (safety boundaries)."""
    rows = await memory_store.pool.fetch(
        "SELECT id, content FROM memories WHERE agent_id = $1 AND NOT archived AND immutable = true",
        agent_id,
    )
    return [dict(r) for r in rows]


async def _get_situational_memories(
    memory_store, agent_id: str, query_text: str, budget: int,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """Fetch situational memories via hybrid search within token budget."""
    if not query_text:
        return []

    candidates = await memory_store.search_hybrid(
        query=query_text, agent_id=agent_id, top_k=10, mutate=False
    )
    if exclude_ids:
        candidates = [m for m in candidates if m.get("id") not in exclude_ids]

    result: list[dict] = []
    used = 0
    for mem in candidates:
        content = mem.get("compressed") or mem.get("content", "")
        tokens = _estimate_tokens(content)
        if used + tokens > budget:
            break
        result.append(mem)
        used += tokens
    return result
