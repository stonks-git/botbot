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
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import asyncpg
import hdbscan
import numpy as np

from .activation import cosine_similarity
from .config import NOVELTY_THRESHOLD, WEIGHT_CENTER_SQL
from .db import get_agent_ids
from .config import (
    RESEARCH_CONFIRMATION_HOURS,
    RESEARCH_DAILY_LIMIT,
    RESEARCH_DISPLACE_BETA,
    RESEARCH_HOURLY_LIMIT,
    RESEARCH_MIN_WEIGHT,
)
from .llm import retry_llm_call, retry_llm_call_with_search
from .memory import MemoryStore

logger = logging.getLogger("brain.consolidation")

# -- Tier 1 Constants --
DECAY_TICK_INTERVAL = 3600  # 1 hour
CONTRADICTION_SCAN_INTERVAL = 600  # 10 min
PATTERN_DETECT_INTERVAL = 86400  # 1/day (D-021: HDBSCAN replaces greedy, runs daily)
CONSTANT_LOOP_INTERVAL = 30  # main loop sleep
DECAY_NUDGE_AMOUNT = 0.01
DECAY_STALE_HOURS = 24

# -- Tier 2 Constants --
DEEP_INTERVAL_SECONDS = 3600  # 1 hour
DEEP_CHECK_INTERVAL = 60  # check triggers every 60s
MERGE_SIMILARITY_THRESHOLD = NOVELTY_THRESHOLD
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

# -- HDBSCAN Pattern Detection (D-021) --
HDBSCAN_MIN_CLUSTER_SIZE = 3
HDBSCAN_MIN_SAMPLES = 2
PATTERN_MIN_WEIGHT = 0.25
PATTERN_MIN_ACCESS = 2


# -- Helpers --


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


def _hdbscan_cluster(
    items: list[tuple[str, np.ndarray]],
    min_cluster_size: int = HDBSCAN_MIN_CLUSTER_SIZE,
    min_samples: int = HDBSCAN_MIN_SAMPLES,
) -> list[list[str]]:
    """HDBSCAN clustering on embedding vectors (D-021).

    Returns list of clusters (each cluster is a list of memory IDs).
    Noise points (label=-1) are excluded.
    """
    if len(items) < min_cluster_size:
        return []

    ids = [item[0] for item in items]
    embeddings = np.vstack([item[1] for item in items])

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
    )
    labels = clusterer.fit_predict(embeddings)

    # Group by label, exclude noise (-1)
    groups: dict[int, list[str]] = defaultdict(list)
    for idx, label in enumerate(labels):
        if label >= 0:
            groups[label].append(ids[idx])

    return list(groups.values())


# ==================================================================
# Tier 1: Constant Consolidation
# ==================================================================


class ConstantConsolidation:
    """Lightweight scheduled operations running every 30 seconds."""

    def __init__(self, pool: asyncpg.Pool, memory_store: MemoryStore, notification_store=None):
        self.pool = pool
        self.store = memory_store
        self.notification_store = notification_store
        self._last_decay_tick: float = 0.0
        self._last_contradiction_scan: float = 0.0
        self._last_pattern_detect: float = 0.0

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main Tier 1 loop."""
        logger.info("Tier 1 (constant) consolidation started.")
        while not shutdown_event.is_set():
            try:
                agent_ids = await get_agent_ids(self.pool)
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
            f"""
            UPDATE memories
            SET depth_weight_beta = depth_weight_beta + $1,
                updated_at = NOW()
            WHERE agent_id = $2
              AND (last_accessed IS NULL OR last_accessed < $3)
              AND NOT immutable
              AND {WEIGHT_CENTER_SQL} > 0.1
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
                    # D-019: Notify about contradiction
                    if self.notification_store:
                        try:
                            await self.notification_store.enqueue(
                                agent_id=agent_id,
                                content=f"Contradiction detected: {response[:200]}",
                                urgency=0.3,
                                importance=0.6,
                                source="consolidation/contradiction",
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("Contradiction check failed for pair: %s", e)

        await _log_consolidation(
            self.pool,
            agent_id,
            "contradiction_scan",
            {"pairs_checked": len(pairs), "tensions_found": tensions_found},
        )

        # D-016: Queue research for factual contradictions + process queue
        for mem_a, mem_b in pairs:
            await self._maybe_queue_research(agent_id, mem_a, mem_b)
        await self._process_research_queue(agent_id)

        return {"status": "ok", "pairs_checked": len(pairs), "tensions_found": tensions_found}

    # -- Research sessions (D-016/DJ-008) --

    async def _maybe_queue_research(
        self, agent_id: str, mem_a: dict, mem_b: dict,
    ) -> None:
        """Classify a contradiction and queue for research if factual + high confidence."""
        try:
            # Check weight centers — skip if both < RESEARCH_MIN_WEIGHT
            centers = await self.pool.fetch(
                f"""
                SELECT id, {WEIGHT_CENTER_SQL} AS center, source_tag
                FROM memories WHERE id = ANY($1) AND agent_id = $2
                """,
                [mem_a["id"], mem_b["id"]],
                agent_id,
            )
            center_map = {r["id"]: r for r in centers}
            ca = center_map.get(mem_a["id"])
            cb = center_map.get(mem_b["id"])
            if not ca or not cb:
                return
            if float(ca["center"]) < RESEARCH_MIN_WEIGHT and float(cb["center"]) < RESEARCH_MIN_WEIGHT:
                return
            # Skip user-sourced memories (trust user over Google)
            if ca.get("source_tag") == "external_user" or cb.get("source_tag") == "external_user":
                return
            # Rate limit check
            if not await self._check_research_rate_limits(agent_id):
                return
            # Classify
            classification = await self._classify_contradiction(mem_a, mem_b)
            if (
                classification.get("type") == "factual"
                and classification.get("confidence", 0) > 0.7
                and classification.get("research_worthy")
            ):
                await self.pool.execute(
                    """
                    INSERT INTO research_queue
                        (agent_id, tension_id, mem_a_id, mem_b_id, classification)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    agent_id,
                    "",  # tension_id filled later if needed
                    mem_a["id"],
                    mem_b["id"],
                    classification,
                )
                logger.info("Research queued for %s vs %s [%s]", mem_a["id"], mem_b["id"], agent_id)
        except Exception as e:
            logger.warning("Research queue failed: %s", e)

    async def _classify_contradiction(self, mem_a: dict, mem_b: dict) -> dict:
        """LLM classifies contradiction as factual/subjective with confidence."""
        prompt = (
            "Two memories contradict each other:\n"
            f'A: "{mem_a["content"][:500]}"\n'
            f'B: "{mem_b["content"][:500]}"\n\n'
            "Classify: Is this a factual contradiction or a subjective difference?\n"
            "Rate confidence 0.0-1.0. Is web research appropriate to resolve this?\n"
            'Respond as JSON only: {"type": "factual" or "subjective", '
            '"confidence": 0.0-1.0, "research_worthy": true/false, '
            '"research_question": "what to search for"}'
        )
        import json
        try:
            response = await retry_llm_call(prompt, temperature=0.2)
            # Try to extract JSON from response
            text = response.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)
        except (json.JSONDecodeError, RuntimeError) as e:
            logger.warning("Classification parse failed: %s", e)
            return {"type": "unknown", "confidence": 0.0, "research_worthy": False, "research_question": ""}

    async def _check_research_rate_limits(self, agent_id: str) -> bool:
        """Check 1/hour and 24/day limits. Returns True if allowed."""
        row = await self.pool.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 hour') AS hourly,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS daily
            FROM consolidation_log
            WHERE agent_id = $1 AND operation = 'research_session'
            """,
            agent_id,
        )
        return row["hourly"] < RESEARCH_HOURLY_LIMIT and row["daily"] < RESEARCH_DAILY_LIMIT

    async def _process_research_queue(self, agent_id: str) -> None:
        """Process pending and awaiting-confirmation research items."""
        try:
            # 1. Run first research on pending items
            pending = await self.pool.fetch(
                "SELECT * FROM research_queue WHERE agent_id = $1 AND status = 'pending' LIMIT 1",
                agent_id,
            )
            for item in pending:
                await self._run_first_research(agent_id, item)

            # 2. Run confirmation on items researched 24h+ ago
            awaiting = await self.pool.fetch(
                """
                SELECT * FROM research_queue
                WHERE agent_id = $1
                  AND status = 'researched'
                  AND first_researched_at < NOW() - INTERVAL '%d hours'
                LIMIT 1
                """ % RESEARCH_CONFIRMATION_HOURS,
                agent_id,
            )
            for item in awaiting:
                await self._run_confirmation_research(agent_id, item)
        except Exception as e:
            logger.warning("Research queue processing failed: %s", e)

    async def _run_first_research(self, agent_id: str, item: dict) -> None:
        """Execute first search for a queued contradiction."""
        classification = item["classification"]
        question = classification.get("research_question", "")
        if not question:
            await self.pool.execute(
                "UPDATE research_queue SET status = 'expired' WHERE id = $1", item["id"],
            )
            return

        prompt = (
            f"Research this factual question using web search:\n{question}\n\n"
            "Provide a clear, factual answer based on search results. "
            "State which of the following is correct:\n"
            f'A: "{(await self.pool.fetchval("SELECT content FROM memories WHERE id=$1", item["mem_a_id"]) or "")[:300]}"\n'
            f'B: "{(await self.pool.fetchval("SELECT content FROM memories WHERE id=$1", item["mem_b_id"]) or "")[:300]}"\n\n'
            'Respond with: {"verdict": "A" or "B" or "neither", '
            '"explanation": "brief explanation", "confidence": "HIGH/MEDIUM/LOW"}'
        )
        try:
            text, sources, chunk_count = await retry_llm_call_with_search(prompt)
        except RuntimeError:
            logger.warning("First research LLM failed for queue item %d", item["id"])
            return

        # Structural confidence from grounding chunks
        if chunk_count == 0:
            struct_confidence = "UNRESOLVED"
        elif chunk_count == 1:
            struct_confidence = "LOW"
        else:
            struct_confidence = "MEDIUM"

        result = {
            "text": text[:1000],
            "sources": sources[:5],
            "grounding_chunk_count": chunk_count,
            "structural_confidence": struct_confidence,
        }

        # Store research finding memory
        await self.store.store_memory(
            content=f"Research finding: {text[:500]}",
            agent_id=agent_id,
            memory_type="research_finding",
            source="consolidation",
            source_tag="consolidation_research",
            importance=0.6,
            metadata={
                "research_question": question,
                "sources": sources[:5],
                "structural_confidence": struct_confidence,
                "queue_id": item["id"],
            },
        )

        await self.pool.execute(
            """
            UPDATE research_queue
            SET status = 'researched', first_result = $1, first_researched_at = NOW()
            WHERE id = $2
            """,
            result, item["id"],
        )
        await _log_consolidation(self.pool, agent_id, "research_session", {
            "queue_id": item["id"],
            "phase": "first",
            "structural_confidence": struct_confidence,
        })
        logger.info("First research complete for queue %d [%s]: %s", item["id"], agent_id, struct_confidence)
        # D-019: Notify on MEDIUM confidence research findings
        if struct_confidence == "MEDIUM" and self.notification_store:
            try:
                await self.notification_store.enqueue(
                    agent_id=agent_id,
                    content=f"Research finding ({struct_confidence}): {text[:200]}",
                    urgency=0.5,
                    importance=0.8,
                    source="consolidation/research",
                    source_memory_id=item["mem_a_id"],
                )
            except Exception:
                pass

    async def _run_confirmation_research(self, agent_id: str, item: dict) -> None:
        """Execute second (confirmation) search with rephrased prompt."""
        first = item["first_result"] or {}
        classification = item["classification"]
        question = classification.get("research_question", "")

        # Rephrase for independent verification
        prompt = (
            f"Verify this factual claim by searching the web:\n"
            f"Original question: {question}\n"
            f"Previous finding: {first.get('text', '')[:300]}\n\n"
            "Search independently to confirm or refute the previous finding. "
            'Respond with: {"verdict": "confirmed" or "refuted" or "inconclusive", '
            '"explanation": "brief explanation"}'
        )
        try:
            text, sources, chunk_count = await retry_llm_call_with_search(prompt)
        except RuntimeError:
            logger.warning("Confirmation research failed for queue %d", item["id"])
            return

        struct_confidence = "UNRESOLVED" if chunk_count == 0 else ("LOW" if chunk_count == 1 else "MEDIUM")
        result = {
            "text": text[:1000],
            "sources": sources[:5],
            "grounding_chunk_count": chunk_count,
            "structural_confidence": struct_confidence,
        }

        import json
        # Check if both searches agree
        try:
            verdict_text = text.strip()
            if verdict_text.startswith("```"):
                verdict_text = verdict_text.split("```")[1]
                if verdict_text.startswith("json"):
                    verdict_text = verdict_text[4:]
            verdict = json.loads(verdict_text)
        except (json.JSONDecodeError, IndexError):
            verdict = {"verdict": "inconclusive"}

        if verdict.get("verdict") == "confirmed" and struct_confidence == "MEDIUM":
            # Both searches agree — displace the loser via safety
            first_text = first.get("text", "")
            # Determine which memory to displace from first result
            import re
            loser_id = None
            if '"verdict": "A"' in first_text or '"verdict":"A"' in first_text:
                loser_id = item["mem_b_id"]
            elif '"verdict": "B"' in first_text or '"verdict":"B"' in first_text:
                loser_id = item["mem_a_id"]

            if loser_id:
                # Displace through safety-checked weight change
                if self.store.safety:
                    allowed, adj_alpha, adj_beta, reasons = self.store.safety.check_weight_change(
                        0.0, 0.0, RESEARCH_DISPLACE_BETA, 0.0, 0.0,
                    )
                    if allowed:
                        await self.pool.execute(
                            "UPDATE memories SET depth_weight_beta = depth_weight_beta + $1 WHERE id = $2",
                            adj_beta, loser_id,
                        )
                        logger.info("Research displaced memory %s (beta += %.1f)", loser_id, adj_beta)
                else:
                    await self.pool.execute(
                        "UPDATE memories SET depth_weight_beta = depth_weight_beta + $1 WHERE id = $2",
                        RESEARCH_DISPLACE_BETA, loser_id,
                    )

                # Create correction memory
                await self.store.store_memory(
                    content=f"Verified correction: {first.get('text', '')[:300]}",
                    agent_id=agent_id,
                    memory_type="correction",
                    source="consolidation",
                    source_tag="consolidation_research",
                    importance=0.8,
                    metadata={
                        "displaced_memory_id": loser_id,
                        "research_sources": (first.get("sources", []) + sources)[:5],
                        "queue_id": item["id"],
                    },
                )
            # D-019: Notify about confirmed research displacement
            if self.notification_store and loser_id:
                try:
                    await self.notification_store.enqueue(
                        agent_id=agent_id,
                        content=f"Research confirmed: displaced memory {loser_id}",
                        urgency=0.5,
                        importance=0.8,
                        source="consolidation/research_confirmed",
                        source_memory_id=loser_id,
                    )
                except Exception:
                    pass
            status = "confirmed"
        else:
            status = "expired"

        await self.pool.execute(
            "UPDATE research_queue SET status = $1, second_result = $2, second_researched_at = NOW() WHERE id = $3",
            status, result, item["id"],
        )
        await _log_consolidation(self.pool, agent_id, "research_session", {
            "queue_id": item["id"],
            "phase": "confirmation",
            "outcome": status,
        })
        logger.info("Confirmation research for queue %d [%s]: %s", item["id"], agent_id, status)

    # -- Pattern detection --

    async def _pattern_detection(self, agent_id: str) -> dict:
        """HDBSCAN cluster qualifying memories, generate per-cluster insights (D-021)."""
        rows = await self.pool.fetch(
            f"""
            SELECT id, content, type, embedding::float4[] AS embedding_arr,
                   {WEIGHT_CENTER_SQL} AS center
            FROM memories
            WHERE agent_id = $1
              AND embedding IS NOT NULL
              AND {WEIGHT_CENTER_SQL} > $2
              AND access_count >= $3
              AND COALESCE(insight_level, 0) < 2
            ORDER BY created_at DESC LIMIT 200
            """,
            agent_id,
            PATTERN_MIN_WEIGHT,
            PATTERN_MIN_ACCESS,
        )
        if len(rows) < HDBSCAN_MIN_CLUSTER_SIZE:
            return {"status": "ok", "clusters_found": 0, "insights_created": 0}

        items = [
            (r["id"], np.array(r["embedding_arr"], dtype=np.float32))
            for r in rows
        ]
        clusters = _hdbscan_cluster(items)

        significant = [c for c in clusters if len(c) >= HDBSCAN_MIN_CLUSTER_SIZE]
        if not significant:
            return {"status": "ok", "clusters_found": 0, "insights_created": 0}

        # Build id→row lookup
        row_map = {r["id"]: r for r in rows}
        cluster_insights: list[dict] = []
        insights_created = 0

        # Per-cluster LLM analysis
        for cluster_ids in significant:
            cluster_rows = [row_map[cid] for cid in cluster_ids if cid in row_map]
            if not cluster_rows:
                continue

            contents = "\n".join(
                f"- [{r['type']}] {r['content'][:300]}" for r in cluster_rows
            )
            prompt = (
                f"Analyze these {len(cluster_rows)} related memories and extract "
                f"the key pattern or insight:\n{contents}\n\n"
                "Write a single, first-person insight (1-2 sentences) that "
                "captures what this cluster reveals about my experience or beliefs."
            )
            try:
                insight_text = await retry_llm_call(prompt, temperature=0.4)
            except RuntimeError:
                logger.warning("LLM failed for cluster analysis [%s]", agent_id)
                continue

            if not insight_text or len(insight_text) < 20:
                continue

            # Dedup check
            is_novel, sim, existing_id = await self.store.check_novelty(
                insight_text, agent_id, MERGE_SIMILARITY_THRESHOLD,
            )
            if not is_novel and existing_id:
                await self.store.apply_retrieval_mutation([existing_id], agent_id)
                logger.debug("Pattern insight reinforced existing %s", existing_id)
                continue

            # Store as level-1 insight
            mem_id = await self.store.store_insight(
                content=insight_text,
                agent_id=agent_id,
                source_memory_ids=cluster_ids,
                importance=0.8,
                metadata={"phase": "hdbscan_pattern", "cluster_size": len(cluster_ids)},
                insight_level=1,
            )
            insights_created += 1
            cluster_insights.append({
                "content": insight_text,
                "cluster_size": len(cluster_ids),
                "source_ids": cluster_ids,
                "insight_id": mem_id,
            })

            await _log_consolidation(
                self.pool,
                agent_id,
                "pattern_insight",
                {
                    "cluster_size": len(cluster_ids),
                    "member_ids": cluster_ids,
                    "insight_id": mem_id,
                    "insight": insight_text[:200],
                },
            )

        # Cross-cluster meta-insight (1-level recursion: D-021)
        if len(cluster_insights) >= 2:
            insights_text = "\n".join(
                f"- (cluster of {ci['cluster_size']}): {ci['content'][:200]}"
                for ci in cluster_insights
            )
            meta_prompt = (
                f"These are insights extracted from {len(cluster_insights)} "
                f"memory clusters:\n{insights_text}\n\n"
                "Write a single meta-level observation (1-2 sentences) about what "
                "these patterns collectively reveal about my experience."
            )
            try:
                meta_text = await retry_llm_call(meta_prompt, temperature=0.4)
            except RuntimeError:
                meta_text = None

            if meta_text and len(meta_text) >= 20:
                is_novel, sim, existing_id = await self.store.check_novelty(
                    meta_text, agent_id, MERGE_SIMILARITY_THRESHOLD,
                )
                if is_novel:
                    all_source_ids = [
                        ci["insight_id"] for ci in cluster_insights
                    ]
                    meta_id = await self.store.store_insight(
                        content=meta_text,
                        agent_id=agent_id,
                        source_memory_ids=all_source_ids,
                        importance=0.9,
                        metadata={"phase": "hdbscan_meta", "source_insights": len(cluster_insights)},
                        insight_level=2,
                    )
                    insights_created += 1
                    await _log_consolidation(
                        self.pool,
                        agent_id,
                        "meta_insight",
                        {"insight_id": meta_id, "meta_insight": meta_text[:200]},
                    )

        logger.info(
            "Pattern detection [%s]: %d HDBSCAN clusters, %d insights created",
            agent_id, len(significant), insights_created,
        )
        return {
            "status": "ok",
            "clusters_found": len(significant),
            "insights_created": insights_created,
        }


# ==================================================================
# Tier 2: Deep Consolidation
# ==================================================================


class DeepConsolidation:
    """Hourly deep processing cycle, or triggered manually."""

    def __init__(self, pool: asyncpg.Pool, memory_store: MemoryStore, notification_store=None):
        self.pool = pool
        self.store = memory_store
        self.notification_store = notification_store
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
                    agent_ids = await get_agent_ids(self.pool)
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
            f"""
            SELECT id, content, access_count,
                   depth_weight_alpha, depth_weight_beta,
                   {WEIGHT_CENTER_SQL} AS center
            FROM memories
            WHERE agent_id = $1
              AND access_count >= $2
              AND created_at < NOW() - INTERVAL '1 day' * $3
              AND {WEIGHT_CENTER_SQL} < 0.65
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
            f"""
            SELECT id, content, access_count,
                   depth_weight_alpha, depth_weight_beta,
                   {WEIGHT_CENTER_SQL} AS center
            FROM memories
            WHERE agent_id = $1
              AND access_count >= $2
              AND created_at < NOW() - INTERVAL '1 day' * $3
              AND {WEIGHT_CENTER_SQL} BETWEEN 0.65 AND 0.82
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
            f"""
            SELECT {WEIGHT_CENTER_SQL} AS center
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

    def __init__(self, pool: asyncpg.Pool, memory_store: MemoryStore, notification_store=None):
        self.constant = ConstantConsolidation(pool, memory_store, notification_store)
        self.deep = DeepConsolidation(pool, memory_store, notification_store)
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
