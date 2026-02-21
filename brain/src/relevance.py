"""5-component Dirichlet-blended hybrid relevance scoring."""

import logging
import math
import random
from datetime import datetime, timezone

import numpy as np

from .activation import cosine_similarity

logger = logging.getLogger(__name__)

COLD_START_ALPHA = {
    "semantic": 12.0,
    "coactivation": 1.0,
    "noise": 0.5,
    "emotional": 0.5,
    "recency": 3.0,
}
TARGET_ALPHA = {
    "semantic": 8.0,
    "coactivation": 5.0,
    "noise": 0.5,
    "emotional": 3.0,
    "recency": 2.0,
}
RECENCY_HALF_LIFE_SECONDS = 604800  # 7 days


def compute_semantic_similarity(memory_embedding, attention_embedding) -> float:
    if attention_embedding is None:
        return 0.0
    return max(0.0, cosine_similarity(memory_embedding, attention_embedding))


def compute_coactivation(
    memory_id: str,
    active_memory_ids: list[str] | None,
    co_access_scores: dict | None,
) -> float:
    if not active_memory_ids or not co_access_scores:
        return 0.0
    max_score = 0.0
    for other_id in active_memory_ids:
        key = tuple(sorted([memory_id, other_id]))
        score = co_access_scores.get(key, 0.0)
        if score > max_score:
            max_score = score
    return min(1.0, max_score)


def compute_noise() -> float:
    return random.random()


def compute_emotional_alignment(gut_alignment: float | None = None) -> float:
    if gut_alignment is None:
        return 0.5
    return max(0.0, min(1.0, gut_alignment))


def compute_recency(last_accessed: datetime | None) -> float:
    if last_accessed is None:
        return 0.0
    now = datetime.now(timezone.utc)
    age_seconds = (now - last_accessed).total_seconds()
    if age_seconds <= 0:
        return 1.0
    return math.exp(-0.693 * age_seconds / RECENCY_HALF_LIFE_SECONDS)


def sample_blend_weights(
    memory_count: int = 0,
    alpha_override: dict | None = None,
) -> dict[str, float]:
    """Sample blend weights from Dirichlet. Cold-start is semantic-heavy."""
    if alpha_override:
        alphas = alpha_override
    elif memory_count < 100:
        alphas = COLD_START_ALPHA
    else:
        t = min(1.0, (memory_count - 100) / 900)
        alphas = {}
        for k in COLD_START_ALPHA:
            alphas[k] = COLD_START_ALPHA[k] + t * (TARGET_ALPHA[k] - COLD_START_ALPHA[k])
        alphas = alphas
    alpha_values = [alphas[k] for k in ["semantic", "coactivation", "noise", "emotional", "recency"]]
    samples = np.random.dirichlet(alpha_values)
    keys = ["semantic", "coactivation", "noise", "emotional", "recency"]
    return dict(zip(keys, samples.tolist()))


def compute_hybrid_relevance(
    memory_embedding,
    memory_id: str,
    last_accessed: datetime | None,
    attention_embedding=None,
    active_memory_ids: list[str] | None = None,
    co_access_scores: dict | None = None,
    gut_alignment: float | None = None,
    blend_weights: dict[str, float] | None = None,
    memory_count: int = 0,
) -> tuple[float, dict]:
    """Compute 5-component blended relevance score."""
    if blend_weights is None:
        blend_weights = sample_blend_weights(memory_count)

    components = {
        "semantic": compute_semantic_similarity(memory_embedding, attention_embedding),
        "coactivation": compute_coactivation(memory_id, active_memory_ids, co_access_scores),
        "noise": compute_noise(),
        "emotional": compute_emotional_alignment(gut_alignment),
        "recency": compute_recency(last_accessed),
    }

    score = sum(blend_weights[k] * components[k] for k in components)
    breakdown = {
        "components": components,
        "weights": blend_weights,
        "score": score,
    }
    return score, breakdown


async def spread_activation(
    pool,
    seed_ids: list[str],
    agent_id: str,
    hops: int = 1,
    top_k_per_hop: int = 3,
) -> dict[str, float]:
    """Spread activation through co-access network (Hebbian). Returns {memory_id: score}."""
    decay_per_hop = [1.0, 0.3, 0.1]
    activated: dict[str, float] = {}
    frontier = set(seed_ids)

    for hop in range(min(hops + 1, len(decay_per_hop))):
        if not frontier:
            break
        decay = decay_per_hop[hop]
        frontier_list = list(frontier)

        rows = await pool.fetch(
            """
            SELECT memory_id_a, memory_id_b, co_access_count
            FROM memory_co_access
            WHERE agent_id = $1 AND (memory_id_a = ANY($2) OR memory_id_b = ANY($2))
            ORDER BY co_access_count DESC
            """,
            agent_id,
            frontier_list,
        )

        next_frontier = set()
        for row in rows:
            a, b, count = row["memory_id_a"], row["memory_id_b"], row["co_access_count"]
            neighbor = b if a in frontier else a
            normalized = min(1.0, count / 20.0)
            score = decay * normalized
            if neighbor not in activated or score > activated[neighbor]:
                activated[neighbor] = score
                next_frontier.add(neighbor)

        frontier = next_frontier

    return activated


async def update_co_access(pool, memory_ids: list[str], agent_id: str) -> None:
    """Update co-access counts for retrieved memory pairs (Hebbian learning)."""
    if len(memory_ids) < 2:
        return
    pairs = []
    for i, mid_a in enumerate(memory_ids):
        for mid_b in memory_ids[i + 1 : min(i + 6, len(memory_ids))]:
            a, b = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
            pairs.append((a, b))

    if not pairs:
        return

    # Batch UPSERT all pairs in one round-trip (CQ-004)
    a_ids = [p[0] for p in pairs]
    b_ids = [p[1] for p in pairs]
    await pool.execute(
        """
        INSERT INTO memory_co_access (memory_id_a, memory_id_b, agent_id, co_access_count, last_co_accessed)
        SELECT unnest($1::text[]), unnest($2::text[]), $3, 1, NOW()
        ON CONFLICT (memory_id_a, memory_id_b)
        DO UPDATE SET co_access_count = memory_co_access.co_access_count + 1,
                     last_co_accessed = NOW()
        """,
        a_ids,
        b_ids,
        agent_id,
    )
