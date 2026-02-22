"""Entry/Exit gates — ACT-R based filtering for what's worth remembering.

Entry gate: stochastic filter on incoming content (mechanical, short → skip).
Exit gate: 3x3 decision matrix (relevance × novelty) → persist/reinforce/buffer/drop.
"""

import logging
import math
import random
import re
from dataclasses import dataclass, field

from .activation import spreading_activation
from .config import NOVELTY_THRESHOLD

logger = logging.getLogger("brain.gate")


# ── Semantic chunking (D-018b) ────────────────────────────────────────

MAX_CHUNK_TOKENS = 300


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def semantic_chunk(text: str, max_tokens: int = MAX_CHUNK_TOKENS) -> list[str]:
    """Split text into semantically coherent chunks, each ≤ max_tokens.

    Algorithm: split by paragraph (\\n\\n), then sentence boundaries.
    Greedily merge segments into chunks under the token limit.
    Single sentences exceeding max_tokens are kept whole (no mid-sentence split).
    """
    if _estimate_tokens(text) <= max_tokens:
        return [text]

    # Split into paragraphs first
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # Break paragraphs into sentences
    segments: list[str] = []
    for para in paragraphs:
        # Split on sentence boundaries (. ! ?) followed by space or end
        sentences = re.split(r'(?<=[.!?])\s+', para)
        sentences = [s.strip() for s in sentences if s.strip()]
        if sentences:
            segments.extend(sentences)
        else:
            segments.append(para)

    # Greedily merge segments into chunks
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for seg in segments:
        seg_tokens = _estimate_tokens(seg)
        # +1 for the space when joining with existing segments
        added_tokens = seg_tokens + (1 if current else 0)
        if current and current_tokens + added_tokens > max_tokens:
            chunks.append(" ".join(current))
            current = [seg]
            current_tokens = seg_tokens
        else:
            current.append(seg)
            current_tokens += added_tokens

    if current:
        chunks.append(" ".join(current))

    return chunks if chunks else [text]

# ── Decision constants ────────────────────────────────────────────────

PERSIST_HIGH = "persist_high"
PERSIST_FLAG = "persist_flag"  # Core + Contradicting (max priority)
PERSIST = "persist"
REINFORCE = "reinforce"
BUFFER = "buffer"
SKIP = "skip"
DROP = "drop"


# ── Entry Gate ────────────────────────────────────────────────────────


@dataclass
class EntryGateConfig:
    min_content_length: int = 10
    short_content_skip_rate: float = 0.95
    mechanical_skip_rate: float = 0.90
    base_buffer_rate: float = 0.99
    mechanical_prefixes: list[str] = field(
        default_factory=lambda: ["/", "[tool:", "[system:", "[error:", "```"]
    )


class EntryGate:
    """Stochastic filter on raw incoming content."""

    def __init__(self, config: EntryGateConfig | None = None):
        self.config = config or EntryGateConfig()

    def evaluate(
        self,
        content: str,
        source: str = "unknown",
        source_tag: str = "external_user",
    ) -> tuple[bool, dict]:
        """Returns (should_buffer, metadata)."""
        cfg = self.config
        content_len = len(content.strip())

        # Short content: 95% skip
        if content_len < cfg.min_content_length:
            should_skip = random.random() < cfg.short_content_skip_rate
            return not should_skip, {
                "reason": "short_content",
                "decision": SKIP if should_skip else BUFFER,
                "skip_rate": cfg.short_content_skip_rate,
                "content_length": content_len,
            }

        # Mechanical content: 90% skip
        stripped = content.strip()
        for prefix in cfg.mechanical_prefixes:
            if stripped.startswith(prefix):
                should_skip = random.random() < cfg.mechanical_skip_rate
                return not should_skip, {
                    "reason": "mechanical",
                    "decision": SKIP if should_skip else BUFFER,
                    "skip_rate": cfg.mechanical_skip_rate,
                    "matched_prefix": prefix,
                }

        # Normal content: 99% buffer (1% skip = noise floor)
        should_buffer = random.random() < cfg.base_buffer_rate
        return should_buffer, {
            "reason": "normal",
            "decision": BUFFER if should_buffer else SKIP,
            "buffer_rate": cfg.base_buffer_rate,
        }


# ── Exit Gate ─────────────────────────────────────────────────────────

NEGATION_MARKERS = [
    "not", "dont", "doesn't", "doesnt", "isnt", "wasnt", "wont", "cant",
    "never", "no longer", "stopped", "changed", "actually", "instead",
    "wrong", "incorrect", "mistaken", "however", "but actually",
    "on the contrary", "opposite", "disagree", "unlike", "different from",
]


def detect_contradiction_negation(new_content: str, existing_content: str) -> float:
    """Detect negation asymmetry between new and existing content.

    Returns a score 0-1 where higher = more likely contradiction.
    """
    new_lower = new_content.lower()
    existing_lower = existing_content.lower()

    asymmetry_count = 0
    for marker in NEGATION_MARKERS:
        in_new = marker in new_lower
        in_existing = marker in existing_lower
        if in_new != in_existing:
            asymmetry_count += 1

    return min(1.0, asymmetry_count * 0.15)


@dataclass
class ExitGateConfig:
    core_threshold: float = 0.6
    peripheral_threshold: float = 0.3
    confirming_sim: float = NOVELTY_THRESHOLD
    novel_sim: float = 0.6
    contradiction_sim: float = 0.7
    drop_noise_floor: float = 0.02
    emotional_charge_bonus: float = 0.15
    emotional_charge_threshold: float = 0.3
    hunger_max_boost: float = 2.5
    hunger_decay_constant: float = 10.0


# 3x3 decision matrix: (relevance_axis, novelty_axis) -> (decision, base_score)
_DECISION_MATRIX: dict[tuple[str, str], tuple[str, float]] = {
    # Core relevance
    ("core", "confirming"):    (REINFORCE, 0.50),
    ("core", "novel"):         (PERSIST, NOVELTY_THRESHOLD),
    ("core", "contradicting"): (PERSIST_FLAG, 0.95),
    # Peripheral relevance
    ("peripheral", "confirming"):    (SKIP, 0.15),
    ("peripheral", "novel"):         (BUFFER, 0.40),
    ("peripheral", "contradicting"): (PERSIST, 0.70),
    # Irrelevant
    ("irrelevant", "confirming"):    (DROP, 0.05),
    ("irrelevant", "novel"):         (DROP, 0.05),
    ("irrelevant", "contradicting"): (DROP, 0.05),
}


class ExitGate:
    """3x3 decision matrix: relevance (core/peripheral/irrelevant) x novelty (confirming/novel/contradicting)."""

    def __init__(self, config: ExitGateConfig | None = None):
        self.config = config or ExitGateConfig()

    def _hunger_boost(self, memory_count: int) -> float:
        """Exponential decay from max_boost to 1.0 based on memory count.

        Newborn agents (0 memories) get maximum boost so everything gets stored.
        As memories accumulate, boost tapers to 1.0 (normal gate behavior).
        """
        cfg = self.config
        return 1.0 + (cfg.hunger_max_boost - 1.0) * math.exp(
            -memory_count / cfg.hunger_decay_constant
        )

    async def evaluate(
        self,
        content: str,
        agent_id: str,
        memory_store,
        layer_embeddings: list[tuple] | None = None,
        attention_embedding=None,
        emotional_charge: float = 0.0,
        source_tag: str = "external_user",
        memory_count: int = 0,
    ) -> tuple[str, float, dict]:
        """Evaluate content through the 3x3 matrix.

        Returns (decision, score, metadata).

        Integration points for later phases:
          - layer_embeddings: Phase 3 (top-N identity memory embeddings from DB)
            → enables core/peripheral/irrelevant relevance classification
          - attention_embedding: Phase 4 (GutFeeling.attention_centroid)
            → adds attention-based component to spreading_activation
          - emotional_charge: Phase 4 (GutFeeling.emotional_charge)
            → adds +0.15 bonus when charge >= 0.3
        """
        cfg = self.config

        # 1. Embed content
        content_embedding = await memory_store.embed(
            content, task_type="SEMANTIC_SIMILARITY"
        )

        # 2. Relevance axis: spreading_activation → classify
        has_context = attention_embedding is not None or bool(layer_embeddings)
        s_i = spreading_activation(
            content_embedding,
            attention_embedding=attention_embedding,
            layer_embeddings=layer_embeddings,
        )

        if not has_context:
            # No layers or attention yet (pre-Phase 3/4): default to peripheral
            # so the novelty axis drives decisions (novel→buffer, contradicting→persist)
            relevance_axis = "peripheral"
            s_i = 0.35  # midpoint of peripheral range
        elif s_i >= cfg.core_threshold:
            relevance_axis = "core"
        elif s_i >= cfg.peripheral_threshold:
            relevance_axis = "peripheral"
        else:
            relevance_axis = "irrelevant"

        # 3. Novelty axis: check_novelty + contradiction detection
        #    Reuse content_embedding (same SEMANTIC_SIMILARITY task type) to avoid re-embed (CQ-010)
        is_novel, max_similarity, most_similar_id = await memory_store.check_novelty(
            content, agent_id, threshold=cfg.confirming_sim,
            embedding=content_embedding,
        )

        # Lightweight touch: refresh last_accessed to prevent decay (no access_count change)
        if most_similar_id:
            try:
                await memory_store.touch_memory(most_similar_id, agent_id)
            except Exception:
                pass  # Non-critical, don't fail the gate

        # Check for contradiction if similarity is in the right range
        #   Fetch by ID instead of re-embedding + re-searching (CQ-010)
        contradiction_score = 0.0
        if max_similarity >= cfg.contradiction_sim and most_similar_id:
            existing = await memory_store.get_memory(most_similar_id, agent_id)
            if existing:
                contradiction_score = detect_contradiction_negation(
                    content, existing["content"]
                )

        # Classify novelty
        if contradiction_score >= 0.3 and max_similarity >= cfg.contradiction_sim:
            novelty_axis = "contradicting"
        elif max_similarity >= cfg.confirming_sim:
            novelty_axis = "confirming"
        elif max_similarity < cfg.novel_sim:
            novelty_axis = "novel"
        else:
            # Middle ground (0.6-0.85 similarity, no contradiction) → novel
            novelty_axis = "novel"

        # 4. Matrix lookup
        decision, base_score = _DECISION_MATRIX[(relevance_axis, novelty_axis)]

        # 5. Score modulation: base_score * (0.5 + 0.5 * s_i) * hunger + emotional bonus
        hunger = self._hunger_boost(memory_count)
        score = base_score * (0.5 + 0.5 * s_i) * hunger
        if emotional_charge >= cfg.emotional_charge_threshold:
            score += cfg.emotional_charge_bonus

        # 6. Hunger promotion: buffer → persist when score is boosted above threshold
        original_decision = decision
        if decision == BUFFER and hunger > 1.05 and score >= 0.5:
            decision = PERSIST
            logger.info("Hunger promotion: BUFFER → PERSIST (hunger=%.2f, score=%.3f)", hunger, score)

        # 7. Noise floor: 2% chance DROP → BUFFER
        if decision == DROP and random.random() < cfg.drop_noise_floor:
            decision = BUFFER
            logger.debug("Noise floor override: DROP → BUFFER")

        metadata = {
            "relevance_axis": relevance_axis,
            "novelty_axis": novelty_axis,
            "spreading_activation": round(s_i, 4),
            "max_similarity": round(max_similarity, 4),
            "most_similar_id": most_similar_id,
            "contradiction_score": round(contradiction_score, 4),
            "base_score": base_score,
            "emotional_charge": round(emotional_charge, 4),
            "hunger_boost": round(hunger, 4),
            "memory_count": memory_count,
            "matrix_decision": original_decision,
            "final_decision": decision,
            "score": round(score, 4),
        }

        logger.info(
            "Exit gate [%s]: %s×%s → %s (score=%.3f, s_i=%.3f, sim=%.3f, hunger=%.2f, memories=%d)",
            agent_id, relevance_axis, novelty_axis, decision, score, s_i, max_similarity, hunger, memory_count,
        )

        return decision, score, metadata
