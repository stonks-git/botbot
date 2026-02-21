"""Rumination threads — persistent thought continuity for the DMN idle loop."""

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("brain.rumination")

STATE_DIR = Path("/app/state")

MAX_THREAD_CYCLES = 50
MIN_GUT_DELTA_TO_CONTINUE = 0.1
RANDOM_POP_BASE_PROBABILITY = 0.10
RANDOM_POP_AGE_FACTOR = 0.02  # +2% per cycle, capped at 0.5
MAX_HISTORY = 10
MAX_COMPLETED = 20


@dataclass
class RuminationThread:
    """A single persistent thread of thought."""

    topic: str
    seed_memory_id: str
    seed_content: str
    history: list[dict] = field(default_factory=list)  # [{cycle, summary, ts}]
    started_at: float = field(default_factory=time.time)
    cycle_count: int = 0
    last_gut_magnitude: float = 0.0
    resolved: bool = False
    resolution_reason: str = ""

    def should_random_pop(self) -> bool:
        """Probabilistic thread termination — increases with age."""
        probability = min(0.5, RANDOM_POP_BASE_PROBABILITY + (self.cycle_count * RANDOM_POP_AGE_FACTOR))
        return random.random() < probability

    def render_for_prompt(self) -> str:
        """Format thread for LLM continuation prompt."""
        lines = [
            f"[DMN RUMINATION THREAD -- cycle {self.cycle_count}]",
            f"Topic: {self.topic}",
        ]
        if self.history:
            lines.append("Previous thoughts:")
            for entry in self.history[-5:]:
                lines.append(f"  - Cycle {entry['cycle']}: {entry['summary']}")
        lines.append("")
        lines.append("Continue this thread. Explore a new angle or deeper layer.")
        lines.append("If this feels resolved, say THREAD_RESOLVED.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "seed_memory_id": self.seed_memory_id,
            "seed_content": self.seed_content,
            "history": self.history,
            "started_at": self.started_at,
            "cycle_count": self.cycle_count,
            "last_gut_magnitude": self.last_gut_magnitude,
            "resolved": self.resolved,
            "resolution_reason": self.resolution_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RuminationThread":
        return cls(
            topic=d["topic"],
            seed_memory_id=d["seed_memory_id"],
            seed_content=d["seed_content"],
            history=d.get("history", []),
            started_at=d.get("started_at", time.time()),
            cycle_count=d.get("cycle_count", 0),
            last_gut_magnitude=d.get("last_gut_magnitude", 0.0),
            resolved=d.get("resolved", False),
            resolution_reason=d.get("resolution_reason", ""),
        )


class RuminationManager:
    """Manages the active rumination thread and completed thread archive for one agent."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.active_thread: RuminationThread | None = None
        self.completed_threads: list[dict] = []  # last MAX_COMPLETED

    def has_active_thread(self) -> bool:
        return self.active_thread is not None and not self.active_thread.resolved

    def start_thread(self, topic: str, seed_memory_id: str, seed_content: str) -> None:
        """Start a new rumination thread. Archives existing thread if any."""
        if self.has_active_thread():
            self.end_thread("superseded")
        self.active_thread = RuminationThread(
            topic=topic,
            seed_memory_id=seed_memory_id,
            seed_content=seed_content,
        )
        logger.info("Rumination thread started for %s: %s", self.agent_id, topic[:80])

    def end_thread(self, reason: str) -> None:
        """End the active thread and archive it."""
        if self.active_thread is None:
            return
        self.active_thread.resolved = True
        self.active_thread.resolution_reason = reason
        self.completed_threads.append({
            "topic": self.active_thread.topic,
            "seed_memory_id": self.active_thread.seed_memory_id,
            "cycles": self.active_thread.cycle_count,
            "reason": reason,
            "started_at": self.active_thread.started_at,
            "ended_at": time.time(),
        })
        self.completed_threads = self.completed_threads[-MAX_COMPLETED:]
        logger.info(
            "Rumination thread ended for %s: %s (reason=%s, cycles=%d)",
            self.agent_id,
            self.active_thread.topic[:60],
            reason,
            self.active_thread.cycle_count,
        )
        self.active_thread = None

    def continue_thread(self, llm_summary: str, gut_magnitude: float) -> None:
        """Append a new cycle to the active thread. Checks terminal conditions."""
        if self.active_thread is None:
            return
        t = self.active_thread
        t.cycle_count += 1
        t.last_gut_magnitude = gut_magnitude
        t.history.append({
            "cycle": t.cycle_count,
            "summary": llm_summary[:500],
            "ts": time.time(),
        })
        if len(t.history) > MAX_HISTORY:
            t.history = t.history[-MAX_HISTORY:]

        # Terminal conditions
        if t.cycle_count >= MAX_THREAD_CYCLES:
            self.end_thread("max_cycles")
        elif gut_magnitude < MIN_GUT_DELTA_TO_CONTINUE and t.cycle_count > 3:
            self.end_thread("gut_flat")

    def render_for_prompt(self) -> str | None:
        """Render the active thread for LLM, or None if no active thread."""
        if not self.has_active_thread():
            return None
        return self.active_thread.render_for_prompt()

    # ── Persistence ──────────────────────────────────────────────────────

    def _state_path(self) -> Path:
        agent_dir = STATE_DIR / self.agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        return agent_dir / "rumination_state.json"

    def save(self) -> None:
        """Persist rumination state to disk."""
        state: dict = {
            "agent_id": self.agent_id,
            "completed_threads": self.completed_threads,
        }
        if self.active_thread is not None:
            state["active_thread"] = self.active_thread.to_dict()
        path = self._state_path()
        path.write_text(json.dumps(state))
        logger.debug("Rumination state saved for %s", self.agent_id)

    @classmethod
    def load(cls, agent_id: str) -> "RuminationManager":
        """Load rumination state from disk, or return fresh instance."""
        rm = cls(agent_id)
        path = rm._state_path()

        if not path.exists():
            logger.info("No rumination state for %s, starting fresh", agent_id)
            return rm

        try:
            state = json.loads(path.read_text())
            rm.completed_threads = state.get("completed_threads", [])
            if "active_thread" in state:
                rm.active_thread = RuminationThread.from_dict(state["active_thread"])
        except Exception as e:
            logger.warning("Failed to load rumination state for %s: %s", agent_id, e)

        return rm
