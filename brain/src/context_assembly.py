"""Context assembly -- dynamic injection with identity, safety, and situational memories.

Builds a structured system prompt from:
  Track 0: immutable safety memories (always present)
  Identity hash: compact summary from top unified memories (always present)
  Track 2: stochastic identity memories (Beta-sampled from top-N by depth_weight)
  Track 1: situational memories (hybrid search within budget)

D-005: Identity is the weights. No L0/L1 layers — identity emerges from
high-weight memories in the unified table.
"""

import logging

from .activation import cosine_similarity
from .stochastic import StochasticWeight

logger = logging.getLogger("brain.context")

# Token budget allocation
BUDGET_IMMUTABLE_SAFETY = 100
BUDGET_IDENTITY_AVG = 1500
BUDGET_IDENTITY_MAX = 3000
BUDGET_SITUATIONAL = 2000
BUDGET_COGNITIVE_STATE = 200
BUDGET_ATTENTION_FIELD = 500
BUDGET_OUTPUT_BUFFER = 4000

IDENTITY_THRESHOLD = 0.6
IDENTITY_TOP_N = 20

# Identity render constants
IDENTITY_HASH_TOP_N = 10
IDENTITY_FULL_TOP_N = 30


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


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

    # Identity hash (compact, always present — rendered from top DB memories)
    identity_hash = await render_identity_hash(memory_store, agent_id)
    parts["identity_hash"] = identity_hash
    used_tokens += _estimate_tokens(identity_hash)

    # Track 2: stochastic identity injection
    identity_tokens = 0
    identity_memories = await _get_top_identity_memories(
        memory_store, agent_id, IDENTITY_TOP_N
    )
    for mem in identity_memories:
        if identity_tokens >= BUDGET_IDENTITY_MAX:
            break
        weight = StochasticWeight(
            alpha=mem.get("depth_weight_alpha", 1.0),
            beta=mem.get("depth_weight_beta", 4.0),
        )
        if weight.observe() > IDENTITY_THRESHOLD or mem.get("immutable", False):
            content = mem["content"]
            tokens = _estimate_tokens(content)
            if identity_tokens + tokens <= BUDGET_IDENTITY_MAX:
                parts["identity_memories"].append(content)
                identity_tokens += tokens
    used_tokens += identity_tokens

    # Cognitive state
    if cognitive_state_report:
        used_tokens += _estimate_tokens(cognitive_state_report)

    # Track 1: situational (competition-based)
    situational_budget = min(
        BUDGET_SITUATIONAL, total_budget - used_tokens - BUDGET_OUTPUT_BUFFER
    )
    if situational_budget > 0 and query_text:
        situational = await _get_situational_memories(
            memory_store, agent_id, query_text, situational_budget
        )
        for mem in situational:
            parts["situational"].append(mem.get("compressed") or mem["content"])
    used_tokens += sum(_estimate_tokens(s) for s in parts["situational"])

    # Context inertia (Phase 4 wires attention embeddings)
    context_shift = 1.0
    if attention_embedding is not None and previous_attention_embedding is not None:
        context_shift = 1.0 - cosine_similarity(
            attention_embedding, previous_attention_embedding
        )
    inertia = 0.05 if context_shift > 0.7 else 0.3

    conversation_budget = total_budget - used_tokens - BUDGET_OUTPUT_BUFFER

    return {
        "parts": parts,
        "used_tokens": used_tokens,
        "conversation_budget": max(0, conversation_budget),
        "identity_token_count": identity_tokens,
        "context_shift": context_shift,
        "inertia": inertia,
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
        sections.append("[IDENTITY -- active beliefs/values this cycle]")
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


def adaptive_fifo_prune(
    conversation: list[dict],
    budget: int,
    intensity: float = 0.5,
) -> tuple[list[dict], list[dict]]:
    """Intensity-adaptive context pruning. Returns (kept, pruned)."""
    if not conversation:
        return [], []

    if intensity > 0.7:
        effective_budget = int(budget * 0.9)
    elif intensity < 0.3:
        effective_budget = int(budget * 0.35)
    else:
        effective_budget = budget

    total = sum(_estimate_tokens(m.get("content", "")) + 4 for m in conversation)
    if total <= effective_budget:
        return conversation, []

    kept: list[dict] = []
    pruned: list[dict] = []
    running = 0
    for msg in reversed(conversation):
        msg_tokens = _estimate_tokens(msg.get("content", "")) + 4
        if running + msg_tokens <= effective_budget:
            kept.insert(0, msg)
            running += msg_tokens
        else:
            pruned.insert(0, msg)
    return kept, pruned


# ── Identity rendering (from unified memory) ────────────────────────


async def render_identity_hash(memory_store, agent_id: str) -> str:
    """Compact identity summary (~100-200 tokens) from top unified memories.

    Replaces LayerStore.render_identity_hash() — identity IS the weights.
    """
    rows = await memory_store.pool.fetch(
        """
        SELECT content, type, immutable,
               depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) AS center
        FROM memories
        WHERE agent_id = $1
          AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.3
        ORDER BY depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) DESC
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
        """
        SELECT content, type, immutable, confidence, importance,
               depth_weight_alpha, depth_weight_beta,
               depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) AS center
        FROM memories
        WHERE agent_id = $1
          AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.2
        ORDER BY depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) DESC
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


# ── Private helpers ───────────────────────────────────────────────────


async def _get_immutable_memories(memory_store, agent_id: str) -> list[dict]:
    """Fetch memories marked as immutable (safety boundaries)."""
    rows = await memory_store.pool.fetch(
        "SELECT id, content FROM memories WHERE agent_id = $1 AND immutable = true",
        agent_id,
    )
    return [dict(r) for r in rows]


async def _get_top_identity_memories(
    memory_store, agent_id: str, top_n: int
) -> list[dict]:
    """Fetch top-N memories by depth_weight center (> 0.3)."""
    rows = await memory_store.pool.fetch(
        """
        SELECT id, content, depth_weight_alpha, depth_weight_beta, immutable
        FROM memories
        WHERE agent_id = $1
          AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.3
        ORDER BY depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) DESC
        LIMIT $2
        """,
        agent_id,
        top_n,
    )
    return [dict(r) for r in rows]


async def _get_situational_memories(
    memory_store, agent_id: str, query_text: str, budget: int
) -> list[dict]:
    """Fetch situational memories via hybrid search within token budget."""
    if not query_text:
        return []

    candidates = await memory_store.search_hybrid(
        query=query_text, agent_id=agent_id, top_k=10, mutate=False
    )

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
