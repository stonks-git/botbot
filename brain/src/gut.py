"""Two-centroid emotional model — the agent's gut feeling.

Subconscious centroid: weighted average of identity memory embeddings (D-005).
Attention centroid: EMA of recently observed message embeddings.
Delta between them = emotional charge (divergence) + alignment (congruence).

D-005 adaptation: No L0/L1 layers. Subconscious centroid is computed from
top-N identity memories in the unified table, weighted by Beta weight center.
"""

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger("brain.gut")

# EMA decay: halflife of 10 embeddings
# decay = exp(-ln(2) / HALFLIFE) ≈ 0.9330
ATTENTION_HALFLIFE = 10
ATTENTION_DECAY = math.exp(-0.693 / ATTENTION_HALFLIFE)

# State persistence path (docker volume: ./brain-state:/app/state:rw)
STATE_DIR = Path("/app/state")


@dataclass
class GutDelta:
    """Snapshot of the emotional delta at a point in time."""

    delta: np.ndarray  # difference vector (attention - subconscious)
    magnitude: float  # L2 norm of delta
    direction: np.ndarray  # unit vector (delta / magnitude)
    context: str  # what triggered this computation
    timestamp: float  # time.time()
    outcome_id: str | None = None  # for PCA linkage (Phase 7)


class GutFeeling:
    """Two-centroid emotional model.

    Subconscious centroid: identity-weighted average of top memory embeddings.
      Updated during context assembly (when identity data is already loaded).

    Attention centroid: EMA of recently observed message embeddings.
      Updated on every attention update (user message, context assembly, etc.)

    The delta between them drives emotional_charge (divergence intensity)
    and emotional_alignment (identity congruence).
    """

    def __init__(self, agent_id: str, dimensions: int = 3072):
        self.agent_id = agent_id
        self.dimensions = dimensions
        self.subconscious_centroid: np.ndarray | None = None
        self.attention_centroid: np.ndarray | None = None
        self._previous_attention: np.ndarray | None = None
        self._attention_count: int = 0
        self._last_delta: GutDelta | None = None
        self._delta_log: list[dict] = []  # recent deltas (metadata only), max 50

    def update_subconscious(
        self,
        identity_embeddings: list[tuple] | None,
    ) -> np.ndarray | None:
        """Recompute subconscious centroid from identity memory embeddings.

        Args:
            identity_embeddings: list of (content, weight_center, ndarray)
                from _get_identity_embeddings(). The weight_center (0-1)
                serves as the importance weight for each embedding.

        Returns the new centroid or None if no embeddings available.
        """
        if not identity_embeddings:
            return None

        total_weight = 0.0
        weighted_sum = np.zeros(self.dimensions, dtype=np.float64)

        for _content, center, emb in identity_embeddings:
            w = float(center)
            weighted_sum += w * np.asarray(emb, dtype=np.float64)
            total_weight += w

        if total_weight > 0:
            self.subconscious_centroid = (weighted_sum / total_weight).astype(
                np.float32
            )
        return self.subconscious_centroid

    def update_attention(self, embedding: list | np.ndarray) -> np.ndarray:
        """Update attention centroid via EMA with new observation.

        Args:
            embedding: the embedding of the content being attended to.

        Returns the updated attention centroid.
        """
        emb = np.asarray(embedding, dtype=np.float32)

        # Save previous for context inertia
        if self.attention_centroid is not None:
            self._previous_attention = self.attention_centroid.copy()

        if self.attention_centroid is None:
            self.attention_centroid = emb
        else:
            self.attention_centroid = (
                ATTENTION_DECAY * self.attention_centroid
                + (1 - ATTENTION_DECAY) * emb
            )

        self._attention_count += 1
        return self.attention_centroid

    def compute_delta(self, context: str = "") -> GutDelta | None:
        """Compute the emotional delta between attention and subconscious.

        Returns None if either centroid is missing.
        """
        if self.subconscious_centroid is None or self.attention_centroid is None:
            return None

        delta = (
            self.attention_centroid.astype(np.float64)
            - self.subconscious_centroid.astype(np.float64)
        )
        magnitude = float(np.linalg.norm(delta))

        if magnitude > 0:
            direction = (delta / magnitude).astype(np.float32)
        else:
            direction = np.zeros(self.dimensions, dtype=np.float32)

        gut_delta = GutDelta(
            delta=delta.astype(np.float32),
            magnitude=magnitude,
            direction=direction,
            context=context,
            timestamp=time.time(),
        )

        self._last_delta = gut_delta

        # Log metadata (not full vectors)
        self._delta_log.append(
            {
                "magnitude": magnitude,
                "charge": self.emotional_charge,
                "alignment": self.emotional_alignment,
                "context": context[:100],
                "timestamp": gut_delta.timestamp,
            }
        )
        if len(self._delta_log) > 50:
            self._delta_log = self._delta_log[-50:]

        return gut_delta

    @property
    def emotional_charge(self) -> float:
        """0 = calm/aligned, 1 = intense divergence from identity."""
        if self._last_delta is None:
            return 0.0
        return min(1.0, self._last_delta.magnitude / 2.0)

    @property
    def emotional_alignment(self) -> float:
        """1 = fully aligned with identity, 0 = fully divergent."""
        if self._last_delta is None:
            return 1.0
        return max(0.0, 1.0 - self._last_delta.magnitude / 2.0)

    @property
    def previous_attention_centroid(self) -> np.ndarray | None:
        """Previous attention centroid, for context inertia calculation."""
        return self._previous_attention

    def gut_summary(self) -> str:
        """One-line summary for context injection."""
        if self._last_delta is None:
            return "Gut: no signal yet (waiting for attention data)"

        charge = self.emotional_charge
        alignment = self.emotional_alignment
        mag = self._last_delta.magnitude

        if charge < 0.2:
            intensity = "calm"
        elif charge < 0.5:
            intensity = "mild"
        elif charge < 0.8:
            intensity = "high"
        else:
            intensity = "intense"

        if alignment > 0.7:
            direction = "aligned with identity"
        elif alignment > 0.4:
            direction = "drifting from identity"
        else:
            direction = "divergent from identity"

        return f"Gut: {intensity} intensity, {direction} (charge={charge:.2f}, mag={mag:.2f})"

    def link_outcome(self, outcome_id: str, last_n: int = 1) -> None:
        """Forward-link recent deltas for PCA analysis (Phase 7)."""
        for entry in self._delta_log[-last_n:]:
            entry["outcome_id"] = outcome_id

    # ── Persistence ──────────────────────────────────────────────────

    def _state_path(self) -> Path:
        """Path to persist gut state for this agent."""
        agent_dir = STATE_DIR / self.agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        return agent_dir / "gut_state.json"

    def save(self) -> None:
        """Persist gut state to disk."""
        state: dict = {
            "agent_id": self.agent_id,
            "dimensions": self.dimensions,
            "attention_count": self._attention_count,
            "delta_log": self._delta_log[-50:],
        }
        if self.attention_centroid is not None:
            state["attention_centroid"] = self.attention_centroid.tolist()
        if self.subconscious_centroid is not None:
            state["subconscious_centroid"] = self.subconscious_centroid.tolist()
        if self._previous_attention is not None:
            state["previous_attention"] = self._previous_attention.tolist()
        if self._last_delta is not None:
            state["last_delta"] = {
                "magnitude": self._last_delta.magnitude,
                "context": self._last_delta.context,
                "timestamp": self._last_delta.timestamp,
            }

        path = self._state_path()
        path.write_text(json.dumps(state))
        logger.debug("Gut state saved for %s", self.agent_id)

    @classmethod
    def load(cls, agent_id: str, dimensions: int = 3072) -> "GutFeeling":
        """Load gut state from disk, or return fresh instance."""
        gut = cls(agent_id, dimensions)
        path = gut._state_path()

        if not path.exists():
            logger.info("No gut state for %s, starting fresh", agent_id)
            return gut

        try:
            state = json.loads(path.read_text())
            gut._attention_count = state.get("attention_count", 0)
            gut._delta_log = state.get("delta_log", [])

            if "attention_centroid" in state:
                gut.attention_centroid = np.array(
                    state["attention_centroid"], dtype=np.float32
                )
            if "subconscious_centroid" in state:
                gut.subconscious_centroid = np.array(
                    state["subconscious_centroid"], dtype=np.float32
                )
            if "previous_attention" in state:
                gut._previous_attention = np.array(
                    state["previous_attention"], dtype=np.float32
                )
            # Reconstruct last_delta from centroids if available
            if (
                "last_delta" in state
                and gut.attention_centroid is not None
                and gut.subconscious_centroid is not None
            ):
                ld = state["last_delta"]
                delta_vec = (
                    gut.attention_centroid.astype(np.float64)
                    - gut.subconscious_centroid.astype(np.float64)
                )
                mag = float(np.linalg.norm(delta_vec))
                direction = (
                    (delta_vec / mag).astype(np.float32)
                    if mag > 0
                    else np.zeros(dimensions, dtype=np.float32)
                )
                gut._last_delta = GutDelta(
                    delta=delta_vec.astype(np.float32),
                    magnitude=mag,
                    direction=direction,
                    context=ld.get("context", "restored"),
                    timestamp=ld.get("timestamp", time.time()),
                )

            logger.info(
                "Gut state loaded for %s (attention_count=%d)",
                agent_id,
                gut._attention_count,
            )
        except Exception as e:
            logger.warning("Failed to load gut state for %s: %s", agent_id, e)

        return gut
