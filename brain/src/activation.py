"""ACT-R activation equation: B + S + P + epsilon."""

import math
import random
from datetime import datetime, timezone

import numpy as np

DEFAULT_DECAY_D = 0.5
DEFAULT_NOISE_S = 0.4
DEFAULT_MISMATCH_P = -1.0
DEFAULT_THRESHOLD_TAU = 0.0


def cosine_similarity(a, b) -> float:
    """Cosine similarity between two vectors. Returns 0 if either norm is 0."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def base_level_activation(
    access_timestamps: list[datetime],
    now: datetime | None = None,
    d: float = DEFAULT_DECAY_D,
) -> float:
    """B_i = ln(sum(t_j^{-d})) where t_j = seconds since each access."""
    if not access_timestamps:
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)
    total = 0.0
    for ts in access_timestamps:
        age = (now - ts).total_seconds()
        if age > 0:
            total += age ** (-d)
    if total <= 0:
        return 0.0
    return math.log(total)


def spreading_activation(
    memory_embedding,
    attention_embedding=None,
    layer_embeddings: list[tuple] | None = None,
    context_weight: float = 0.4,
    identity_weight: float = 0.6,
) -> float:
    """S_i = context_weight * cosine(mem, attention) + identity_weight * weighted_avg_cosine(mem, layers)."""
    s = 0.0
    if attention_embedding is not None:
        s += context_weight * max(0.0, cosine_similarity(memory_embedding, attention_embedding))
    if layer_embeddings:
        total_w = 0.0
        weighted_sim = 0.0
        for _text, weight, emb in layer_embeddings:
            sim = max(0.0, cosine_similarity(memory_embedding, emb))
            weighted_sim += weight * sim
            total_w += weight
        if total_w > 0:
            s += identity_weight * (weighted_sim / total_w)
    return min(s, 1.0)


def partial_matching_penalty(
    memory_metadata: dict,
    query_metadata: dict,
    p: float = DEFAULT_MISMATCH_P,
) -> float:
    """P_i = p * total_mismatches (negative contribution)."""
    total = 0.0
    if memory_metadata.get("type") != query_metadata.get("type"):
        total += 0.3
    if memory_metadata.get("source") != query_metadata.get("source"):
        total += 0.2
    mem_tags = set(memory_metadata.get("tags", []) or [])
    q_tags = set(query_metadata.get("tags", []) or [])
    if mem_tags or q_tags:
        union = mem_tags | q_tags
        overlap = len(mem_tags & q_tags) / len(union) if union else 1.0
        total += 0.5 * (1.0 - overlap)
    return p * total


def logistic_noise(s: float = DEFAULT_NOISE_S) -> float:
    """Epsilon = s * ln(p / (1-p)), p uniform clamped 0.001-0.999."""
    p = random.random()
    p = max(0.001, min(0.999, p))
    return s * math.log(p / (1 - p))


def compute_activation(
    memory_embedding,
    access_timestamps: list[datetime],
    attention_embedding=None,
    layer_embeddings: list[tuple] | None = None,
    memory_metadata: dict | None = None,
    query_metadata: dict | None = None,
    now: datetime | None = None,
    d: float = DEFAULT_DECAY_D,
    s: float = DEFAULT_NOISE_S,
    p: float = DEFAULT_MISMATCH_P,
    tau: float = DEFAULT_THRESHOLD_TAU,
) -> tuple[float, dict]:
    """A_i = B_i + S_i + P_i + epsilon_i. Returns (activation, breakdown)."""
    b = base_level_activation(access_timestamps, now=now, d=d)
    s_val = spreading_activation(
        memory_embedding,
        attention_embedding=attention_embedding,
        layer_embeddings=layer_embeddings,
    )
    p_val = partial_matching_penalty(
        memory_metadata or {},
        query_metadata or {},
        p=p,
    )
    eps = logistic_noise(s)
    activation = b + s_val + p_val + eps
    return activation, {
        "base_level": b,
        "spreading": s_val,
        "partial_match": p_val,
        "noise": eps,
        "total": activation,
        "threshold": tau,
        "above_threshold": activation > tau,
    }
