"""Consolidation engine -- background memory processing ("sleep cycle").

Tier 1 (constant): decay nudge, contradiction scan, pattern detection.
Tier 2 (deep/hourly): merge+insight, promote patterns, decay+reconsolidate,
                       entropy tuning, contextual retrieval.

D-005: No L0/L1 writes. Promotion reinforces weights directly in the unified table.
"""

import asyncio
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import numpy as np

from .activation import cosine_similarity
from .llm import retry_llm_call
from .memory import MemoryStore

logger = logging.getLogger("brain.consolidation")

# -- Tier 1 Constants --
DECAY_TICK_INTERVAL = 3600  # 1 hour
CONTRADICTION_SCAN_INTERVAL = 600  # 10 min
PATTERN_DETECT_INTERVAL = 900  # 15 min
CONSTANT_LOOP_INTERVAL = 30  # main loop sleep
DECAY_NUDGE_AMOUNT = 0.01
DECAY_STALE_HOURS = 24

# -- Tier 2 Constants --
DEEP_INTERVAL_SECONDS = 3600  # 1 hour
DEEP_CHECK_INTERVAL = 60  # check triggers every 60s
MERGE_SIMILARITY_THRESHOLD = 0.85
INSIGHT_QUESTION_COUNT = 3
INSIGHT_PER_QUESTION = 5
PROMOTE_GOAL_MIN_COUNT = 5
PROMOTE_GOAL_MIN_DAYS = 14
PROMOTE_GOAL_REINFORCE = 2.0
PROMOTE_IDENTITY_MIN_COUNT = 10
PROMOTE_IDENTITY_MIN_DAYS = 30
PROMOTE_IDENTITY_REINFORCE = 5.0
DECAY_STALE_DAYS = 90
DECAY_MIN_ACCESS = 3
DECAY_CONTRADICT_AMOUNT = 1.0


# -- Helpers --


async def _get_agent_ids(pool: asyncpg.Pool) -> list[str]:
    """Get all distinct agent IDs that have memories."""
    rows = await pool.fetch("SELECT DISTINCT agent_id FROM memories")
    return [r["agent_id"] for r in rows]


async def _log_consolidation(
    pool: asyncpg.Pool,
    agent_id: str,
    operation: str,
    details: dict,
) -> None:
    """Write to the consolidation_log table."""
    # Pass dict directly — asyncpg's default JSONB codec calls json.dumps()
    # internally. Pre-serializing with json.dumps() caused double-serialization
    # (CQ-014): DB stored JSON string literals instead of objects, making
    # details->>'key' queries return NULL and isinstance(details, dict) fail.
    await pool.execute(
        """
        INSERT INTO consolidation_log (agent_id, operation, details)
        VALUES ($1, $2, $3)
        """,
        agent_id,
        operation,
        details,
    )


def _greedy_cluster(
    items: list[tuple[str, np.ndarray]],
    threshold: float,
) -> list[list[str]]:
    """Greedy cosine-similarity clustering.

    Args:
        items: list of (id, embedding_ndarray)
        threshold: cosine similarity threshold for cluster membership

    Returns list of clusters (each cluster is a list of ids).
    """
    clustered: set[int] = set()
    clusters: list[list[str]] = []

    for i in range(len(items)):
        if i in clustered:
            continue
        cluster_ids = [items[i][0]]
        clustered.add(i)
        for j in range(i + 1, len(items)):
            if j in clustered:
                continue
            sim = cosine_similarity(items[i][1], items[j][1])
            if sim >= threshold:
                cluster_ids.append(items[j][0])
                clustered.add(j)
        clusters.append(cluster_ids)

    return clusters


# ==================================================================
# Tier 1: Constant Consolidation
# ==================================================================


class ConstantConsolidation:
    """Lightweight scheduled operations running every 30 seconds."""

    def __init__(self, pool: asyncpg.Pool, memory_store: MemoryStore):
        self.pool = pool
        self.store = memory_store
        self._last_decay_tick: float = 0.0
        self._last_contradiction_scan: float = 0.0
        self._last_pattern_detect: float = 0.0

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main Tier 1 loop."""
        logger.info("Tier 1 (constant) consolidation started.")
        while not shutdown_event.is_set():
            try:
                agent_ids = await _get_agent_ids(self.pool)
                now = time.time()

                for agent_id in agent_ids:
                    if now - self._last_decay_tick >= DECAY_TICK_INTERVAL:
                        await self._safe_run(self._decay_tick, agent_id)

                    if now - self._last_contradiction_scan >= CONTRADICTION_SCAN_INTERVAL:
                        await self._safe_run(self._contradiction_scan, agent_id)

                    if now - self._last_pattern_detect >= PATTERN_DETECT_INTERVAL:
                        await self._safe_run(self._pattern_detection, agent_id)

                # Update timestamps after processing all agents
                if now - self._last_decay_tick >= DECAY_TICK_INTERVAL:
                    self._last_decay_tick = now
                if now - self._last_contradiction_scan >= CONTRADICTION_SCAN_INTERVAL:
                    self._last_contradiction_scan = now
                if now - self._last_pattern_detect >= PATTERN_DETECT_INTERVAL:
                    self._last_pattern_detect = now

            except Exception as e:
                logger.error("Tier 1 loop error: %s", e)

            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=CONSTANT_LOOP_INTERVAL
                )
                break  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # normal: timeout means keep looping

        logger.info("Tier 1 (constant) consolidation stopped.")

    @staticmethod
    async def _safe_run(coro_fn, agent_id: str) -> dict:
        """Run an operation with error isolation."""
        try:
            return await coro_fn(agent_id)
        except Exception as e:
            logger.error("%s failed for %s: %s", coro_fn.__name__, agent_id, e)
            return {"status": "error", "error": str(e)}

    # -- Decay tick --

    async def _decay_tick(self, agent_id: str) -> dict:
        """Nudge beta +0.01 for stale memories (24h+ not accessed)."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=DECAY_STALE_HOURS)
        result = await self.pool.execute(
            """
            UPDATE memories
            SET depth_weight_beta = depth_weight_beta + $1,
                updated_at = NOW()
            WHERE agent_id = $2
              AND (last_accessed IS NULL OR last_accessed < $3)
              AND NOT immutable
              AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.1
            """,
            DECAY_NUDGE_AMOUNT,
            agent_id,
            cutoff,
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info("Decay tick [%s]: nudged %d memories", agent_id, count)
            await _log_consolidation(
                self.pool,
                agent_id,
                "decay_tick",
                {"nudge": DECAY_NUDGE_AMOUNT, "stale_hours": DECAY_STALE_HOURS, "affected": count},
            )
        return {"status": "ok", "affected": count}

    # -- Contradiction scan --

    async def _contradiction_scan(self, agent_id: str) -> dict:
        """Check random pairs of recent memories for contradictions."""
        rows = await self.pool.fetch(
            """
            SELECT id, content FROM memories
            WHERE agent_id = $1
              AND created_at > NOW() - INTERVAL '24 hours'
              AND type NOT IN ('tension', 'correction')
            ORDER BY created_at DESC LIMIT 10
            """,
            agent_id,
        )
        if len(rows) < 2:
            return {"status": "ok", "pairs_checked": 0, "tensions_found": 0}

        # Build all possible pairs, sample up to 2
        pairs = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                pairs.append((rows[i], rows[j]))
        pairs = random.sample(pairs, min(2, len(pairs)))

        tensions_found = 0
        for mem_a, mem_b in pairs:
            try:
                prompt = (
                    "Do these two memories contradict each other?\n"
                    "If yes, briefly describe the contradiction in one sentence.\n"
                    "If no, reply exactly 'NO'.\n\n"
                    f"Memory A: {mem_a['content'][:500]}\n\n"
                    f"Memory B: {mem_b['content'][:500]}"
                )
                response = await retry_llm_call(prompt, max_tokens=100, temperature=0.1)

                if response.strip().upper() != "NO":
                    tension_content = f"Tension: {response}"
                    is_novel, sim, existing_id = await self.store.check_novelty(
                        tension_content, agent_id, threshold=MERGE_SIMILARITY_THRESHOLD
                    )
                    if not is_novel and existing_id:
                        await self.store.apply_retrieval_mutation([existing_id], agent_id)
                        logger.info(
                            "Tension reinforced existing %s (sim=%.3f) [%s]",
                            existing_id, sim, agent_id,
                        )
                    else:
                        await self.store.store_memory(
                            content=tension_content,
                            agent_id=agent_id,
                            memory_type="tension",
                            source="consolidation",
                            importance=0.6,
                            metadata={
                                "source_a": mem_a["id"],
                                "source_b": mem_b["id"],
                            },
                        )
                    tensions_found += 1
                    logger.info(
                        "Contradiction found [%s]: %s vs %s",
                        agent_id,
                        mem_a["id"],
                        mem_b["id"],
                    )
            except Exception as e:
                logger.warning("Contradiction check failed for pair: %s", e)

        await _log_consolidation(
            self.pool,
            agent_id,
            "contradiction_scan",
            {"pairs_checked": len(pairs), "tensions_found": tensions_found},
        )
        return {"status": "ok", "pairs_checked": len(pairs), "tensions_found": tensions_found}

    # -- Pattern detection --

    async def _pattern_detection(self, agent_id: str) -> dict:
        """Greedy cluster recent memories by embedding similarity."""
        rows = await self.pool.fetch(
            """
            SELECT id, content, embedding::float4[] AS embedding_arr
            FROM memories
            WHERE agent_id = $1
              AND created_at > NOW() - INTERVAL '7 days'
              AND embedding IS NOT NULL
            ORDER BY created_at DESC LIMIT 50
            """,
            agent_id,
        )
        if len(rows) < 3:
            return {"status": "ok", "clusters_found": 0}

        items = [
            (r["id"], np.array(r["embedding_arr"], dtype=np.float32))
            for r in rows
        ]
        clusters = _greedy_cluster(items, MERGE_SIMILARITY_THRESHOLD)

        significant = [c for c in clusters if len(c) >= 3]
        for cluster_ids in significant:
            # Find representative content
            rep_content = ""
            for r in rows:
                if r["id"] == cluster_ids[0]:
                    rep_content = r["content"][:200]
                    break
            await _log_consolidation(
                self.pool,
                agent_id,
                "pattern_detected",
                {
                    "cluster_size": len(cluster_ids),
                    "member_ids": cluster_ids,
                    "representative": rep_content,
                },
            )

        if significant:
            logger.info(
                "Pattern detection [%s]: %d clusters (3+ members)",
                agent_id,
                len(significant),
            )
        return {"status": "ok", "clusters_found": len(significant)}


# ==================================================================
# Tier 2: Deep Consolidation
# ==================================================================


class DeepConsolidation:
    """Hourly deep processing cycle, or triggered manually."""

    def __init__(self, pool: asyncpg.Pool, memory_store: MemoryStore):
        self.pool = pool
        self.store = memory_store
        self._last_deep: float = 0.0
        self._trigger_agents: set[str] = set()
        self._running: bool = False
        self._current_cycle_id: str | None = None

    def trigger(self, agent_id: str) -> None:
        """Queue an agent for immediate deep consolidation."""
        self._trigger_agents.add(agent_id)

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main Tier 2 loop."""
        logger.info("Tier 2 (deep) consolidation started.")
        while not shutdown_event.is_set():
            try:
                now = time.time()

                # Check for manual triggers
                if self._trigger_agents:
                    triggered = list(self._trigger_agents)
                    self._trigger_agents.clear()
                    for agent_id in triggered:
                        await self._safe_deep_cycle(agent_id)

                # Check for hourly schedule
                elif now - self._last_deep >= DEEP_INTERVAL_SECONDS:
                    agent_ids = await _get_agent_ids(self.pool)
                    for agent_id in agent_ids:
                        await self._safe_deep_cycle(agent_id)
                    self._last_deep = now

            except Exception as e:
                logger.error("Tier 2 loop error: %s", e)

            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=DEEP_CHECK_INTERVAL
                )
                break
            except asyncio.TimeoutError:
                pass

        logger.info("Tier 2 (deep) consolidation stopped.")

    async def _safe_deep_cycle(self, agent_id: str) -> dict:
        """Run deep cycle with error isolation."""
        if self._running:
            logger.warning("Deep cycle already running, skipping %s", agent_id)
            return {"status": "skipped", "reason": "already_running"}
        self._running = True
        try:
            return await self._deep_cycle(agent_id)
        except Exception as e:
            logger.error("Deep cycle failed for %s: %s", agent_id, e)
            return {"status": "error", "error": str(e)}
        finally:
            self._running = False

    async def _deep_cycle(self, agent_id: str) -> dict:
        """Full deep consolidation sequence."""
        logger.info("Deep consolidation starting for %s", agent_id)
        results: dict = {}

        # Enable Phase B safety for consolidation cycle
        cycle_id = f"deep_{agent_id}_{int(time.time())}"
        self._current_cycle_id = cycle_id
        safety = self.store.safety
        if safety:
            safety.enable_phase_b()

        try:
            results["merge_insight"] = await self._safe_step(
                self._merge_and_insight, agent_id
            )
            results["promote"] = await self._safe_step(
                self._promote_patterns, agent_id
            )
            results["decay_reconsolidate"] = await self._safe_step(
                self._decay_and_reconsolidate, agent_id
            )
            results["tune"] = await self._safe_step(
                self._tune_parameters, agent_id
            )
            results["contextual"] = await self._safe_step(
                self._contextual_retrieval, agent_id
            )
        finally:
            self._current_cycle_id = None
            if safety:
                safety.end_consolidation_cycle(cycle_id)

        await _log_consolidation(self.pool, agent_id, "deep_cycle", results)
        logger.info("Deep consolidation complete for %s", agent_id)
        return results

    @staticmethod
    async def _safe_step(coro_fn, agent_id: str) -> dict:
        """Run a deep cycle step with error isolation."""
        try:
            return await coro_fn(agent_id)
        except Exception as e:
            logger.error("%s failed for %s: %s", coro_fn.__name__, agent_id, e)
            return {"status": "error", "error": str(e)}

    # -- Merge and Insight --

    async def _merge_and_insight(self, agent_id: str) -> dict:
        """Generate questions from recent memories, extract insights, cluster narratives."""
        # Fetch recent memories
        rows = await self.pool.fetch(
            """
            SELECT id, content FROM memories
            WHERE agent_id = $1
              AND created_at > NOW() - INTERVAL '7 days'
              AND type NOT IN ('tension')
            ORDER BY created_at DESC LIMIT 30
            """,
            agent_id,
        )
        if len(rows) < 5:
            return {"status": "ok", "reason": "insufficient_memories", "count": len(rows)}

        # Step 1: Generate questions
        memory_text = "\n".join(
            f"{i+1}. {r['content'][:200]}" for i, r in enumerate(rows)
        )
        questions_prompt = (
            f"Given these recent memories from an AI agent, what are the {INSIGHT_QUESTION_COUNT} "
            "most salient high-level questions that emerge?\n"
            "Return ONLY the questions, one per line.\n\n"
            f"{memory_text}"
        )
        questions_response = await retry_llm_call(
            questions_prompt, max_tokens=500, temperature=0.3
        )
        questions = [
            q.strip().lstrip("0123456789.-) ")
            for q in questions_response.split("\n")
            if q.strip()
        ][:INSIGHT_QUESTION_COUNT]

        if not questions:
            return {"status": "ok", "reason": "no_questions_generated"}

        # Step 2: Extract insights per question
        insights_stored = 0
        insights_skipped = 0

        for question in questions:
            try:
                similar = await self.store.search_similar(
                    question, agent_id, top_k=INSIGHT_PER_QUESTION
                )
                if not similar:
                    continue

                context_text = "\n".join(
                    f"- {m['content'][:200]}" for m in similar
                )
                source_ids = [m["id"] for m in similar]

                insight_prompt = (
                    f"Question: {question}\n"
                    "Based on these memories, provide up to 5 high-level insights that "
                    "answer or address this question. Each insight should be a single "
                    "sentence. Return ONLY the insights, one per line.\n\n"
                    f"{context_text}"
                )
                insight_response = await retry_llm_call(
                    insight_prompt, max_tokens=500, temperature=0.3
                )
                insights = [
                    line.strip().lstrip("0123456789.-) ")
                    for line in insight_response.split("\n")
                    if line.strip()
                ]

                for insight in insights:
                    if not insight or len(insight) < 10:
                        continue
                    is_novel, sim, existing_id = await self.store.check_novelty(
                        insight, agent_id, threshold=MERGE_SIMILARITY_THRESHOLD
                    )
                    if is_novel:
                        await self.store.store_insight(
                            content=insight,
                            agent_id=agent_id,
                            source_memory_ids=source_ids,
                            importance=0.8,
                            metadata={"question": question, "phase": "merge_and_insight"},
                        )
                        insights_stored += 1
                    elif existing_id:
                        await self.store.apply_retrieval_mutation([existing_id], agent_id)
                        logger.info(
                            "Insight reinforced existing %s (sim=%.3f) [%s]",
                            existing_id, sim, agent_id,
                        )
                        insights_skipped += 1
                    else:
                        insights_skipped += 1
            except Exception as e:
                logger.warning("Insight extraction failed for question: %s", e)

        # Step 3: Cluster narratives from existing reflections
        narratives_generated = await self._cluster_narratives(agent_id)

        await _log_consolidation(
            self.pool,
            agent_id,
            "merge_and_insight",
            {
                "questions": questions,
                "insights_stored": insights_stored,
                "insights_skipped": insights_skipped,
                "narratives_generated": narratives_generated,
            },
        )
        return {
            "status": "ok",
            "questions": len(questions),
            "insights_stored": insights_stored,
            "insights_skipped": insights_skipped,
            "narratives": narratives_generated,
        }

    async def _cluster_narratives(self, agent_id: str) -> int:
        """Cluster reflection memories and generate narratives."""
        rows = await self.pool.fetch(
            """
            SELECT id, content, embedding::float4[] AS embedding_arr
            FROM memories
            WHERE agent_id = $1
              AND type = 'reflection'
              AND embedding IS NOT NULL
            ORDER BY created_at DESC LIMIT 30
            """,
            agent_id,
        )
        if len(rows) < 3:
            return 0

        items = [
            (r["id"], np.array(r["embedding_arr"], dtype=np.float32))
            for r in rows
        ]
        clusters = _greedy_cluster(items, MERGE_SIMILARITY_THRESHOLD)
        significant = [c for c in clusters if len(c) >= 3]

        narratives = 0
        for cluster_ids in significant:
            try:
                # Build cluster text
                cluster_contents = []
                for r in rows:
                    if r["id"] in cluster_ids:
                        cluster_contents.append(r["content"][:200])
                cluster_text = "\n".join(
                    f"{i+1}. {c}" for i, c in enumerate(cluster_contents)
                )

                prompt = (
                    "These memories form a cluster of related experiences/beliefs:\n"
                    f"{cluster_text}\n"
                    "Write a brief causal narrative (1-2 sentences) in first person "
                    "that explains WHY this pattern exists. Start with 'I came to...' "
                    "or 'I value...' or similar."
                )
                narrative = await retry_llm_call(
                    prompt, max_tokens=200, temperature=0.4
                )
                if narrative and len(narrative) > 10:
                    is_novel, sim, existing_id = await self.store.check_novelty(
                        narrative, agent_id, threshold=MERGE_SIMILARITY_THRESHOLD
                    )
                    if not is_novel and existing_id:
                        await self.store.apply_retrieval_mutation([existing_id], agent_id)
                        logger.info(
                            "Narrative reinforced existing %s (sim=%.3f) [%s]",
                            existing_id, sim, agent_id,
                        )
                    else:
                        await self.store.store_memory(
                            content=narrative,
                            agent_id=agent_id,
                            memory_type="narrative",
                            source="consolidation",
                            importance=0.7,
                            metadata={"cluster_member_ids": cluster_ids},
                        )
                    narratives += 1
            except Exception as e:
                logger.warning("Narrative generation failed: %s", e)

        return narratives

    # -- Promote Patterns (D-005 simplified) --

    async def _promote_patterns(self, agent_id: str) -> dict:
        """Promote frequently accessed memories by reinforcing weights directly."""
        goals_promoted = 0
        goals_blocked = 0
        identities_promoted = 0
        identities_blocked = 0
        safety = self.store.safety

        # Goal promotion: 5+ access, 14+ days old, center < 0.65
        goal_candidates = await self.pool.fetch(
            """
            SELECT id, content, access_count,
                   depth_weight_alpha, depth_weight_beta,
                   depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) AS center
            FROM memories
            WHERE agent_id = $1
              AND access_count >= $2
              AND created_at < NOW() - INTERVAL '1 day' * $3
              AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) < 0.65
              AND NOT immutable
            """,
            agent_id,
            PROMOTE_GOAL_MIN_COUNT,
            PROMOTE_GOAL_MIN_DAYS,
        )
        for row in goal_candidates:
            gain = PROMOTE_GOAL_REINFORCE
            if safety:
                allowed, adj_alpha, _adj_beta, _reasons = safety.check_weight_change(
                    row["id"],
                    float(row["depth_weight_alpha"]),
                    float(row["depth_weight_beta"]),
                    delta_alpha=gain,
                    cycle_id=self._current_cycle_id,
                )
                if not allowed:
                    goals_blocked += 1
                    continue
                gain = adj_alpha
            await self.pool.execute(
                """
                UPDATE memories
                SET depth_weight_alpha = depth_weight_alpha + $1,
                    updated_at = NOW()
                WHERE id = $2 AND agent_id = $3
                """,
                gain,
                row["id"],
                agent_id,
            )
            goals_promoted += 1

        # Identity promotion: 10+ access, 30+ days, center 0.65-0.82
        identity_candidates = await self.pool.fetch(
            """
            SELECT id, content, access_count,
                   depth_weight_alpha, depth_weight_beta,
                   depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) AS center
            FROM memories
            WHERE agent_id = $1
              AND access_count >= $2
              AND created_at < NOW() - INTERVAL '1 day' * $3
              AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) BETWEEN 0.65 AND 0.82
              AND NOT immutable
            """,
            agent_id,
            PROMOTE_IDENTITY_MIN_COUNT,
            PROMOTE_IDENTITY_MIN_DAYS,
        )
        for row in identity_candidates:
            gain = PROMOTE_IDENTITY_REINFORCE
            if safety:
                allowed, adj_alpha, _adj_beta, _reasons = safety.check_weight_change(
                    row["id"],
                    float(row["depth_weight_alpha"]),
                    float(row["depth_weight_beta"]),
                    delta_alpha=gain,
                    cycle_id=self._current_cycle_id,
                )
                if not allowed:
                    identities_blocked += 1
                    continue
                gain = adj_alpha
            await self.pool.execute(
                """
                UPDATE memories
                SET depth_weight_alpha = depth_weight_alpha + $1,
                    updated_at = NOW()
                WHERE id = $2 AND agent_id = $3
                """,
                gain,
                row["id"],
                agent_id,
            )
            identities_promoted += 1

        if goals_promoted or identities_promoted or goals_blocked or identities_blocked:
            logger.info(
                "Promote [%s]: %d goals (%d blocked), %d identities (%d blocked)",
                agent_id,
                goals_promoted,
                goals_blocked,
                identities_promoted,
                identities_blocked,
            )
            await _log_consolidation(
                self.pool,
                agent_id,
                "promote_patterns",
                {
                    "goals_promoted": goals_promoted,
                    "goals_blocked": goals_blocked,
                    "identities_promoted": identities_promoted,
                    "identities_blocked": identities_blocked,
                },
            )
        return {
            "status": "ok",
            "goals_promoted": goals_promoted,
            "goals_blocked": goals_blocked,
            "identities_promoted": identities_promoted,
            "identities_blocked": identities_blocked,
        }

    # -- Decay and Reconsolidate --

    async def _decay_and_reconsolidate(self, agent_id: str) -> dict:
        """Deep decay for truly stale memories + revalidate existing insights."""
        # Decay stale memories (beta += 1.0) — per-row safety check
        stale = await self.store.get_stale_memories(
            agent_id,
            stale_days=DECAY_STALE_DAYS,
            min_access_count=DECAY_MIN_ACCESS,
        )
        safety = self.store.safety
        decayed_ids = []
        decay_blocked = 0
        for m in stale:
            if safety:
                allowed, _adj_alpha, adj_beta, _reasons = safety.check_weight_change(
                    m["id"],
                    float(m["depth_weight_alpha"]),
                    float(m["depth_weight_beta"]),
                    delta_beta=DECAY_CONTRADICT_AMOUNT,
                    cycle_id=self._current_cycle_id,
                )
                if not allowed:
                    decay_blocked += 1
                    continue
            decayed_ids.append(m["id"])
        if decayed_ids:
            await self.pool.execute(
                """
                UPDATE memories
                SET depth_weight_beta = depth_weight_beta + $1,
                    updated_at = NOW()
                WHERE id = ANY($2) AND agent_id = $3
                """,
                DECAY_CONTRADICT_AMOUNT,
                decayed_ids,
                agent_id,
            )
            logger.info(
                "Deep decay [%s]: %d memories (%d blocked)",
                agent_id, len(decayed_ids), decay_blocked,
            )

        # Revalidate existing insights
        insights_revalidated = 0
        insights_updated = 0

        insight_rows = await self.pool.fetch(
            """
            SELECT id, content, depth_weight_alpha, depth_weight_beta
            FROM memories
            WHERE agent_id = $1
              AND type = 'reflection'
              AND source = 'consolidation'
            ORDER BY created_at DESC LIMIT 10
            """,
            agent_id,
        )
        for insight_row in insight_rows:
            try:
                sources = await self.store.why_do_i_believe(
                    insight_row["id"], agent_id
                )
                if not sources:
                    continue

                source_text = "\n".join(
                    f"- {s['content'][:200]}" for s in sources[:5]
                )
                prompt = (
                    f"Original insight: {insight_row['content'][:300]}\n"
                    f"Current source evidence:\n{source_text}\n\n"
                    "Does this insight still hold? If it needs updating, provide the updated insight.\n"
                    "If it still holds as-is, reply exactly 'UNCHANGED'."
                )
                response = await retry_llm_call(
                    prompt, max_tokens=200, temperature=0.2
                )
                insights_revalidated += 1

                if response.strip().upper() != "UNCHANGED":
                    is_novel, sim, existing_id = await self.store.check_novelty(
                        response, agent_id, threshold=MERGE_SIMILARITY_THRESHOLD
                    )
                    if not is_novel and existing_id:
                        await self.store.apply_retrieval_mutation([existing_id], agent_id)
                        logger.info(
                            "Revalidation reinforced existing %s (sim=%.3f) [%s]",
                            existing_id, sim, agent_id,
                        )
                    else:
                        # Store updated insight linked to same sources
                        source_ids = [s["id"] for s in sources]
                        await self.store.store_insight(
                            content=response,
                            agent_id=agent_id,
                            source_memory_ids=source_ids,
                            importance=0.8,
                            metadata={"phase": "revalidation", "replaces": insight_row["id"]},
                        )
                    # Weaken old insight (superseded regardless)
                    weaken_allowed = True
                    if safety:
                        weaken_allowed, _, _, _ = safety.check_weight_change(
                            insight_row["id"],
                            float(insight_row["depth_weight_alpha"]),
                            float(insight_row["depth_weight_beta"]),
                            delta_beta=DECAY_CONTRADICT_AMOUNT,
                            cycle_id=self._current_cycle_id,
                        )
                    if weaken_allowed:
                        await self.pool.execute(
                            """
                            UPDATE memories
                            SET depth_weight_beta = depth_weight_beta + $1,
                                updated_at = NOW()
                            WHERE id = $2 AND agent_id = $3
                            """,
                            DECAY_CONTRADICT_AMOUNT,
                            insight_row["id"],
                            agent_id,
                        )
                    insights_updated += 1
            except Exception as e:
                logger.warning("Insight revalidation failed: %s", e)

        await _log_consolidation(
            self.pool,
            agent_id,
            "decay_and_reconsolidate",
            {
                "stale_decayed": len(decayed_ids),
                "stale_blocked": decay_blocked,
                "insights_revalidated": insights_revalidated,
                "insights_updated": insights_updated,
            },
        )
        return {
            "status": "ok",
            "stale_decayed": len(decayed_ids),
            "stale_blocked": decay_blocked,
            "insights_revalidated": insights_revalidated,
            "insights_updated": insights_updated,
        }

    # -- Tune Parameters --

    async def _tune_parameters(self, agent_id: str) -> dict:
        """Entropy check on weight distribution (log only, Phase 7 adds enforcement)."""
        rows = await self.pool.fetch(
            """
            SELECT depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) AS center
            FROM memories
            WHERE agent_id = $1 AND NOT immutable
            """,
            agent_id,
        )
        if not rows:
            return {"status": "ok", "reason": "no_memories"}

        centers = [float(r["center"]) for r in rows]
        centers_arr = np.array(centers)

        # Shannon entropy over 20 bins
        counts, _ = np.histogram(centers_arr, bins=20, range=(0.0, 1.0))
        total = counts.sum()
        if total == 0:
            return {"status": "ok", "entropy_bits": 0.0, "memory_count": 0}

        probs = counts / total
        entropy = -sum(p * math.log2(p) for p in probs if p > 0)

        avg_center = float(centers_arr.mean())
        result = {
            "status": "ok",
            "entropy_bits": round(entropy, 3),
            "avg_center": round(avg_center, 4),
            "memory_count": len(centers),
        }

        await _log_consolidation(self.pool, agent_id, "entropy_check", result)
        logger.info(
            "Entropy check [%s]: %.2f bits, avg_center=%.3f, n=%d",
            agent_id,
            entropy,
            avg_center,
            len(centers),
        )
        return result

    # -- Contextual Retrieval --

    async def _contextual_retrieval(self, agent_id: str) -> dict:
        """Generate WHO/WHEN/WHY preambles and re-embed memories."""
        rows = await self.pool.fetch(
            """
            SELECT id, content, type, source, created_at
            FROM memories
            WHERE agent_id = $1
              AND content_contextualized IS NULL
              AND type NOT IN ('tension')
            ORDER BY created_at DESC LIMIT 20
            """,
            agent_id,
        )
        if not rows:
            return {"status": "ok", "contextualized": 0}

        contextualized = 0
        for row in rows:
            try:
                prompt = (
                    f"Memory type: {row['type']}  Source: {row['source']}  "
                    f"Created: {row['created_at']}\n"
                    f"Content: {row['content'][:300]}\n"
                    "Give a short context preamble (WHO, WHEN, WHY) in one sentence."
                )
                preamble = await retry_llm_call(
                    prompt, max_tokens=100, temperature=0.1
                )
                if not preamble or len(preamble) < 5:
                    continue

                full_contextualized = f"{preamble} {row['content']}"

                # Update content_contextualized (tsvector auto-updates via GENERATED ALWAYS)
                await self.pool.execute(
                    """
                    UPDATE memories
                    SET content_contextualized = $1,
                        updated_at = NOW()
                    WHERE id = $2 AND agent_id = $3
                    """,
                    full_contextualized,
                    row["id"],
                    agent_id,
                )

                # Re-embed with contextualized content
                new_embedding = await self.store.embed(
                    full_contextualized, task_type="RETRIEVAL_DOCUMENT"
                )
                await self.pool.execute(
                    """
                    UPDATE memories
                    SET embedding = $1::halfvec,
                        updated_at = NOW()
                    WHERE id = $2 AND agent_id = $3
                    """,
                    str(new_embedding),
                    row["id"],
                    agent_id,
                )
                contextualized += 1
            except Exception as e:
                logger.warning("Contextual retrieval failed for %s: %s", row["id"], e)

        if contextualized:
            await _log_consolidation(
                self.pool,
                agent_id,
                "contextual_retrieval",
                {"contextualized": contextualized, "total_candidates": len(rows)},
            )
            logger.info(
                "Contextual retrieval [%s]: %d/%d memories",
                agent_id,
                contextualized,
                len(rows),
            )
        return {"status": "ok", "contextualized": contextualized}


# ==================================================================
# Engine Wrapper
# ==================================================================


class ConsolidationEngine:
    """Runs Tier 1 (constant) and Tier 2 (deep) consolidation as background tasks."""

    def __init__(self, pool: asyncpg.Pool, memory_store: MemoryStore):
        self.constant = ConstantConsolidation(pool, memory_store)
        self.deep = DeepConsolidation(pool, memory_store)
        self._running: bool = False

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Start both tiers. Blocks until shutdown."""
        self._running = True
        logger.info("Consolidation engine starting.")
        try:
            await asyncio.gather(
                self.constant.run(shutdown_event),
                self.deep.run(shutdown_event),
            )
        finally:
            self._running = False
            logger.info("Consolidation engine stopped.")

    def trigger(self, agent_id: str) -> None:
        """Trigger immediate deep consolidation for an agent."""
        self.deep.trigger(agent_id)

    def status(self) -> dict:
        """Current engine status."""
        return {
            "running": self._running,
            "constant": {
                "last_decay_tick": self.constant._last_decay_tick,
                "last_contradiction_scan": self.constant._last_contradiction_scan,
                "last_pattern_detect": self.constant._last_pattern_detect,
            },
            "deep": {
                "last_deep_cycle": self.deep._last_deep,
                "pending_triggers": list(self.deep._trigger_agents),
                "deep_running": self.deep._running,
            },
        }
