"""DMN Idle Loop — spontaneous self-prompts during agent downtime.

4 sampling channels (neglected 35%, tension 20%, temporal 20%, introspective 25%)
produce thoughts that are queued as AttentionCandidates and surfaced by the plugin.
Rumination threads provide continuity across heartbeats.
"""

import asyncio
import collections
import logging
import random
import time
from typing import Callable

import asyncpg

from .dmn_store import AttentionCandidate, DMN_URGENCY, ThoughtQueue
from .llm import retry_llm_call
from .memory import MemoryStore
from .relevance import spread_activation
from .rumination import RuminationManager

logger = logging.getLogger("brain.idle")

# ── Constants ────────────────────────────────────────────────────────────

# Sampling channel biases (must sum to 1.0)
BIAS_NEGLECTED = 0.35
BIAS_TENSION = 0.20
BIAS_TEMPORAL = 0.20
BIAS_INTROSPECTION = 0.25

# Interval tiers (seconds)
INTERVAL_POST_TASK = 60       # < 10 min idle
INTERVAL_IDLE_10MIN = 300     # 10-60 min idle
INTERVAL_IDLE_1HOUR = 900     # 1-4 hours idle
INTERVAL_IDLE_4HOURS = 1800   # 4+ hours idle

# Loop sleep — poll at the fastest tier, skip agents whose interval hasn't elapsed
LOOP_SLEEP = 30


# ── Helpers ──────────────────────────────────────────────────────────────


async def _get_agent_ids(pool: asyncpg.Pool) -> list[str]:
    """Get all distinct agent IDs that have memories."""
    rows = await pool.fetch("SELECT DISTINCT agent_id FROM memories")
    return [r["agent_id"] for r in rows]


# ── IdleLoop ─────────────────────────────────────────────────────────────


class IdleLoop:
    """DMN idle loop — generates spontaneous thoughts when agents are inactive."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        memory_store: MemoryStore,
        thought_queue: ThoughtQueue,
        gut_getter: Callable,
    ):
        self.pool = pool
        self.memory_store = memory_store
        self.thought_queue = thought_queue
        self._gut_getter = gut_getter  # Callable[[str], GutFeeling] — injected to avoid circular import

        self._rumination: dict[str, RuminationManager] = {}
        self._recent_topics: dict[str, collections.deque] = {}
        self.last_activity: dict[str, float] = {}
        self._last_heartbeat: dict[str, float] = {}
        self._heartbeat_count: dict[str, int] = {}
        self._running = False

    # ── Main loop ────────────────────────────────────────────────────

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main DMN loop. Blocks until shutdown."""
        self._running = True
        logger.info("DMN idle loop started.")

        while not shutdown_event.is_set():
            try:
                agent_ids = await _get_agent_ids(self.pool)

                for agent_id in agent_ids:
                    interval = self._agent_interval(agent_id)
                    last_hb = self._last_heartbeat.get(agent_id, 0.0)
                    if time.time() - last_hb >= interval:
                        await self._safe_run(self._heartbeat, agent_id)
                        self._last_heartbeat[agent_id] = time.time()

            except Exception as e:
                logger.error("DMN loop error: %s", e)

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=LOOP_SLEEP)
                break  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # normal: keep looping

        self._running = False
        # Persist all rumination states on shutdown
        for rm in self._rumination.values():
            try:
                rm.save()
            except Exception:
                pass
        logger.info("DMN idle loop stopped.")

    def notify_activity(self, agent_id: str) -> None:
        """Called when an agent receives user input — resets idle timer."""
        self.last_activity[agent_id] = time.time()

    # ── Interval logic ───────────────────────────────────────────────

    def _agent_interval(self, agent_id: str) -> float:
        """Compute heartbeat interval based on how long the agent has been idle."""
        last = self.last_activity.get(agent_id)
        if last is None:
            return INTERVAL_POST_TASK

        idle_seconds = time.time() - last
        if idle_seconds < 600:       # < 10 min
            return INTERVAL_POST_TASK
        elif idle_seconds < 3600:    # 10-60 min
            return INTERVAL_IDLE_10MIN
        elif idle_seconds < 14400:   # 1-4 hours
            return INTERVAL_IDLE_1HOUR
        else:                        # 4+ hours
            return INTERVAL_IDLE_4HOURS

    # ── Error isolation ──────────────────────────────────────────────

    @staticmethod
    async def _safe_run(coro_fn, agent_id: str) -> dict:
        """Run an operation with error isolation."""
        try:
            return await coro_fn(agent_id)
        except Exception as e:
            logger.error("%s failed for %s: %s", coro_fn.__name__, agent_id, e)
            return {"status": "error", "error": str(e)}

    # ── Rumination management ────────────────────────────────────────

    def _get_rumination(self, agent_id: str) -> RuminationManager:
        """Lazy-load rumination manager from disk."""
        if agent_id not in self._rumination:
            self._rumination[agent_id] = RuminationManager.load(agent_id)
        return self._rumination[agent_id]

    # ── Heartbeat ────────────────────────────────────────────────────

    async def _heartbeat(self, agent_id: str) -> dict:
        """One DMN heartbeat: continue or start a rumination thread."""
        rm = self._get_rumination(agent_id)

        if rm.has_active_thread():
            thread = rm.active_thread

            # Check random pop
            if thread.should_random_pop():
                rm.end_thread("random_pop")
                memory = await self._sample_memory(agent_id)
                if memory:
                    await self._start_new_thread(agent_id, memory)
                rm.save()
            else:
                await self._continue_thread(agent_id)
        else:
            # No active thread — sample and start
            memory = await self._sample_memory(agent_id)
            if memory:
                await self._start_new_thread(agent_id, memory)

        self._heartbeat_count[agent_id] = self._heartbeat_count.get(agent_id, 0) + 1
        return {"status": "ok", "heartbeat": self._heartbeat_count[agent_id]}

    # ── Sampling channels ────────────────────────────────────────────

    async def _sample_memory(self, agent_id: str) -> dict | None:
        """Roll a channel and sample a memory from it."""
        roll = random.random()

        if roll < BIAS_NEGLECTED:
            return await self._sample_neglected(agent_id)
        elif roll < BIAS_NEGLECTED + BIAS_TENSION:
            return await self._sample_tension(agent_id)
        elif roll < BIAS_NEGLECTED + BIAS_TENSION + BIAS_TEMPORAL:
            return await self._sample_temporal(agent_id)
        else:
            return await self._sample_introspective(agent_id)

    async def _sample_neglected(self, agent_id: str) -> dict | None:
        """High-weight memories not accessed in 7+ days."""
        row = await self.pool.fetchrow(
            """
            SELECT id, content, type
            FROM memories
            WHERE agent_id = $1
              AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.5
              AND (last_accessed IS NULL OR last_accessed < NOW() - INTERVAL '7 days')
            ORDER BY RANDOM() LIMIT 1
            """,
            agent_id,
        )
        if row:
            logger.debug("Neglected channel sampled memory %s for %s", row["id"], agent_id)
            return dict(row)
        return None

    async def _sample_tension(self, agent_id: str) -> dict | None:
        """High-weight seed + moderately similar partner of different type."""
        seed = await self.pool.fetchrow(
            """
            SELECT id, content, type, embedding::float4[] AS emb_arr
            FROM memories
            WHERE agent_id = $1
              AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.5
              AND embedding IS NOT NULL
            ORDER BY RANDOM() LIMIT 1
            """,
            agent_id,
        )
        if not seed:
            return None

        # Format embedding for halfvec cast
        emb_str = "[" + ",".join(str(x) for x in seed["emb_arr"]) + "]"

        partner = await self.pool.fetchrow(
            """
            SELECT id, content, type
            FROM memories
            WHERE agent_id = $1
              AND type != $2
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> $3::halfvec) BETWEEN 0.3 AND 0.7
            ORDER BY RANDOM() LIMIT 1
            """,
            agent_id,
            seed["type"],
            emb_str,
        )

        if partner:
            logger.debug(
                "Tension channel: seed=%s partner=%s for %s",
                seed["id"],
                partner["id"],
                agent_id,
            )
            return {
                "id": seed["id"],
                "content": f"{seed['content'][:250]} — tension with — {partner['content'][:250]}",
                "type": seed["type"],
                "partner_id": partner["id"],
            }

        # No partner found — return seed alone
        logger.debug("Tension channel: seed only %s for %s", seed["id"], agent_id)
        return dict(seed)

    async def _sample_temporal(self, agent_id: str) -> dict | None:
        """Old memories (30+ days) for creative reconnection."""
        row = await self.pool.fetchrow(
            """
            SELECT id, content, type
            FROM memories
            WHERE agent_id = $1
              AND created_at < NOW() - INTERVAL '30 days'
            ORDER BY RANDOM() LIMIT 1
            """,
            agent_id,
        )
        if row:
            logger.debug("Temporal channel sampled memory %s for %s", row["id"], agent_id)
            return dict(row)
        return None

    async def _sample_introspective(self, agent_id: str) -> dict | None:
        """High-weight reflective memories (reflection, narrative, preference, tension)."""
        row = await self.pool.fetchrow(
            """
            SELECT id, content, type
            FROM memories
            WHERE agent_id = $1
              AND type IN ('reflection', 'narrative', 'preference', 'tension')
              AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.6
            ORDER BY RANDOM() LIMIT 1
            """,
            agent_id,
        )
        if row:
            logger.debug("Introspective channel sampled memory %s for %s", row["id"], agent_id)
            return dict(row)
        return None

    # ── Output channel classification ────────────────────────────────

    async def _classify_channel(
        self,
        memory_content: str,
        memory_type: str,
        memory_id: str,
        agent_id: str,
    ) -> str:
        """Classify a sampled memory into a DMN output channel."""
        # 1. Goal connection: check keyword overlap with high-weight goal-like memories
        goal_rows = await self.pool.fetch(
            """
            SELECT content FROM memories
            WHERE agent_id = $1
              AND type IN ('narrative', 'reflection')
              AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.6
            ORDER BY depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) DESC
            LIMIT 5
            """,
            agent_id,
        )

        memory_words = set(memory_content.lower().split())
        for row in goal_rows:
            goal_words = set(row["content"].lower().split())
            if len(goal_words & memory_words) >= 3:
                return "DMN/goal"

        # 2. Creative: check spread activation via co-access network
        try:
            activated = await spread_activation(
                self.pool, [memory_id], agent_id, hops=2,
            )
            if len(activated) > 0:
                return "DMN/creative"
        except Exception:
            pass  # co-access table may be empty

        # 3. Identity: reflective types
        if memory_type in ("reflection", "narrative", "tension"):
            return "DMN/identity"

        # 4. Default
        return "DMN/reflect"

    # ── Thread lifecycle ─────────────────────────────────────────────

    async def _start_new_thread(self, agent_id: str, memory: dict) -> None:
        """Create a new rumination thread from a sampled memory."""
        rm = self._get_rumination(agent_id)

        topic = memory["content"][:200]
        seed_id = memory["id"]

        rm.start_thread(topic, seed_id, memory["content"])

        # Generate initial thought via LLM
        prompt = (
            f"You are quietly reflecting on this memory:\n"
            f"{memory['content'][:500]}\n\n"
            f"What does this make you think about? Explore one angle briefly (2-3 sentences)."
        )
        thought = await retry_llm_call(prompt, max_tokens=200, temperature=0.6)

        rm.continue_thread(thought, 0.0)
        rm.save()

        channel = await self._classify_channel(
            memory["content"], memory.get("type", ""), seed_id, agent_id,
        )
        await self._queue_thought(agent_id, thought, channel, seed_id)

    async def _continue_thread(self, agent_id: str) -> None:
        """Continue the active rumination thread with LLM."""
        rm = self._get_rumination(agent_id)
        if not rm.has_active_thread():
            return

        thread_prompt = rm.active_thread.render_for_prompt()
        thought = await retry_llm_call(thread_prompt, max_tokens=200, temperature=0.5)

        # Check for self-resolution
        if "THREAD_RESOLVED" in thought:
            rm.end_thread("self_resolved")
            rm.save()
            return

        gut = self._gut_getter(agent_id)
        rm.continue_thread(thought, gut.emotional_charge)
        rm.save()

        channel = await self._classify_channel(
            rm.active_thread.seed_content,
            "",
            rm.active_thread.seed_memory_id,
            agent_id,
        )
        await self._queue_thought(agent_id, thought, channel, rm.active_thread.seed_memory_id)

    # ── Thought queuing ──────────────────────────────────────────────

    async def _queue_thought(
        self,
        agent_id: str,
        thought: str,
        channel: str,
        memory_id: str | None,
    ) -> None:
        """Create an AttentionCandidate and enqueue it (if not repetitive)."""
        if self._is_repetitive(agent_id, thought):
            logger.debug("DMN thought filtered as repetitive for %s", agent_id)
            return

        candidate = AttentionCandidate(
            thought=thought,
            channel=channel,
            urgency=DMN_URGENCY,
            memory_id=memory_id,
        )
        self.thought_queue.put_thought(agent_id, candidate)

        # Track for repetition detection
        if agent_id not in self._recent_topics:
            self._recent_topics[agent_id] = collections.deque(maxlen=5)
        self._recent_topics[agent_id].append(thought[:50])

        logger.info(
            "DMN thought queued [%s] channel=%s queue_size=%d",
            agent_id,
            channel,
            self.thought_queue.queue_size(agent_id),
        )

    def _is_repetitive(self, agent_id: str, thought: str) -> bool:
        """Check if thought[:50] matches 2+ of last 5 recent topics."""
        recent = self._recent_topics.get(agent_id)
        if not recent:
            return False
        prefix = thought[:50]
        matches = sum(1 for t in recent if t == prefix)
        return matches >= 2

    # ── Status ───────────────────────────────────────────────────────

    def status(self) -> dict:
        """Status report for the API."""
        active_threads: dict[str, dict] = {}
        for agent_id, rm in self._rumination.items():
            if rm.has_active_thread():
                t = rm.active_thread
                active_threads[agent_id] = {
                    "topic": t.topic[:100],
                    "cycle_count": t.cycle_count,
                    "started_at": t.started_at,
                }

        return {
            "running": self._running,
            "heartbeat_counts": dict(self._heartbeat_count),
            "queue_sizes": self.thought_queue.all_queue_sizes(),
            "active_threads": active_threads,
        }
