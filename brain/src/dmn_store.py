"""DMN thought queue — in-memory per-agent queue of AttentionCandidates."""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger("brain.dmn_store")

DMN_URGENCY = 0.2


@dataclass
class AttentionCandidate:
    """A single DMN thought ready to be surfaced."""

    thought: str
    channel: str  # "DMN/goal", "DMN/creative", "DMN/identity", "DMN/reflect"
    urgency: float = DMN_URGENCY
    memory_id: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "thought": self.thought,
            "channel": self.channel,
            "urgency": self.urgency,
            "memory_id": self.memory_id,
            "timestamp": self.timestamp,
        }


class ThoughtQueue:
    """Per-agent in-memory queue of AttentionCandidates.

    Thoughts are ephemeral — not persisted. If the service restarts,
    the queue is empty. This is intentional: DMN thoughts are opportunistic.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[AttentionCandidate]] = defaultdict(asyncio.Queue)

    def put_thought(self, agent_id: str, candidate: AttentionCandidate) -> None:
        """Enqueue a thought for an agent."""
        self._queues[agent_id].put_nowait(candidate)
        logger.debug(
            "Thought queued for %s [%s] queue_size=%d",
            agent_id,
            candidate.channel,
            self._queues[agent_id].qsize(),
        )

    def get_thoughts(self, agent_id: str) -> list[AttentionCandidate]:
        """Non-blocking drain: return all pending thoughts for an agent."""
        queue = self._queues.get(agent_id)
        if queue is None:
            return []
        thoughts: list[AttentionCandidate] = []
        while True:
            try:
                thoughts.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return thoughts

    def queue_size(self, agent_id: str) -> int:
        queue = self._queues.get(agent_id)
        return queue.qsize() if queue else 0

    def all_queue_sizes(self) -> dict[str, int]:
        return {aid: q.qsize() for aid, q in self._queues.items() if q.qsize() > 0}
