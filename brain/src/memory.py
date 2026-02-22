"""Memory store — embed, store, retrieve, mutate memories with Beta-distributed weights."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np

import asyncpg
from google import genai

from .config import EMBED_DIMENSIONS, EMBED_MODEL, MEMORY_TYPE_PREFIXES, NOVELTY_THRESHOLD, WEIGHT_CENTER_SQL, RetryConfig
from .relevance import update_co_access

logger = logging.getLogger("brain.memory")


class MemoryStore:
    def __init__(self, pool: asyncpg.Pool, retry_config: RetryConfig | None = None):
        self.pool = pool
        self.retry_config = retry_config or RetryConfig()
        self.safety = None  # SafetyMonitor, wired in Phase 7
        self._flashrank_model = None

        api_key = os.environ.get("GOOGLE_API_KEY")
        if api_key:
            self.genai_client = genai.Client(api_key=api_key)
            logger.info("Gemini embedding client initialized.")
        else:
            self.genai_client = None
            logger.warning("No GOOGLE_API_KEY — embedding disabled.")

    # ── Embedding ──────────────────────────────────────────────────────

    async def embed(
        self,
        text: str,
        task_type: str = "RETRIEVAL_DOCUMENT",
        title: str | None = None,
    ) -> list[float]:
        """Embed text via Gemini with retry."""
        if not self.genai_client:
            raise RuntimeError("Embedding unavailable: no GOOGLE_API_KEY.")

        cfg = self.retry_config
        last_err = None
        for attempt in range(cfg.max_retries):
            try:
                kwargs: dict[str, Any] = {
                    "model": EMBED_MODEL,
                    "contents": text,
                    "config": {
                        "task_type": task_type,
                        "output_dimensionality": EMBED_DIMENSIONS,
                    },
                }
                if title:
                    kwargs["config"]["title"] = title
                result = self.genai_client.models.embed_content(**kwargs)
                return result.embeddings[0].values
            except Exception as e:
                last_err = e
                delay = min(cfg.base_delay * (2**attempt), cfg.max_delay)
                logger.warning("Embed attempt %d failed: %s (retry in %.1fs)", attempt + 1, e, delay)
                await asyncio.sleep(delay)
        raise RuntimeError(f"Embedding failed after {cfg.max_retries} attempts: {last_err}")

    async def embed_batch(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
        title: str | None = None,
    ) -> list[list[float]]:
        """True batch embed via Gemini batch API, chunks of 100."""
        if not self.genai_client:
            raise RuntimeError("Embedding unavailable: no GOOGLE_API_KEY.")
        if not texts:
            return []

        cfg = self.retry_config
        results: list[list[float]] = []
        for i in range(0, len(texts), 100):
            chunk = texts[i : i + 100]
            last_err = None
            for attempt in range(cfg.max_retries):
                try:
                    kwargs: dict[str, Any] = {
                        "model": EMBED_MODEL,
                        "contents": chunk,
                        "config": {
                            "task_type": task_type,
                            "output_dimensionality": EMBED_DIMENSIONS,
                        },
                    }
                    if title:
                        kwargs["config"]["title"] = title
                    result = self.genai_client.models.embed_content(**kwargs)
                    results.extend(emb.values for emb in result.embeddings)
                    break
                except Exception as e:
                    last_err = e
                    delay = min(cfg.base_delay * (2**attempt), cfg.max_delay)
                    logger.warning(
                        "Batch embed attempt %d failed (%d texts): %s (retry in %.1fs)",
                        attempt + 1, len(chunk), e, delay,
                    )
                    await asyncio.sleep(delay)
            else:
                raise RuntimeError(
                    f"Batch embedding failed after {cfg.max_retries} attempts: {last_err}"
                )
        logger.debug("Batch embedded %d texts in %d API calls", len(texts), -(-len(texts) // 100))
        return results

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def prefixed_content(content: str, memory_type: str) -> str:
        prefix = MEMORY_TYPE_PREFIXES.get(memory_type, "")
        return prefix + content

    @staticmethod
    def _gen_id(prefix: str = "mem") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    # ── Store ──────────────────────────────────────────────────────────

    async def store_memory(
        self,
        content: str,
        agent_id: str,
        memory_type: str = "semantic",
        source: str | None = None,
        tags: list[str] | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
        evidence_count: int = 0,
        metadata: dict | None = None,
        source_tag: str | None = None,
        initial_alpha: float | None = None,
        initial_beta: float | None = None,
        memory_group_id: str | None = None,
        remind_at: "datetime | None" = None,
        protect_until: "datetime | None" = None,
    ) -> str:
        mem_id = self._gen_id()
        prefixed = self.prefixed_content(content, memory_type)
        embedding = await self.embed(prefixed)
        now = datetime.now(timezone.utc)

        await self.pool.execute(
            """
            INSERT INTO memories (
                id, agent_id, content, type, embedding, created_at, updated_at,
                source, tags, confidence, importance, evidence_count, metadata, source_tag,
                depth_weight_alpha, depth_weight_beta, memory_group_id, remind_at,
                protect_until
            ) VALUES (
                $1, $2, $3, $4, $5::halfvec, $6, $7,
                $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18,
                $19
            )
            """,
            mem_id,
            agent_id,
            content,
            memory_type,
            str(embedding),
            now,
            now,
            source,
            tags or [],
            confidence,
            importance,
            evidence_count,
            metadata or {},
            source_tag or "external_user",
            initial_alpha if initial_alpha is not None else 1.0,
            initial_beta if initial_beta is not None else 4.0,
            memory_group_id,
            remind_at,
            protect_until,
        )
        logger.info("Stored memory %s (type=%s, agent=%s, group=%s)", mem_id, memory_type, agent_id, memory_group_id)
        return mem_id

    async def store_insight(
        self,
        content: str,
        agent_id: str,
        source_memory_ids: list[str],
        importance: float = 0.8,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        insight_level: int = 1,
    ) -> str:
        """Store a consolidation insight and link to source memories.

        Inherits weighted-average alpha/beta from source memories (heavy sources
        skew more). Does NOT demote source importance — sources keep their weight
        until the insight proves itself through retrieval.
        """
        # Compute inherited weights and group_id from sources
        initial_alpha = 1.0
        initial_beta = 4.0
        inherited_group_id = None
        if source_memory_ids:
            rows = await self.pool.fetch(
                f"""
                SELECT depth_weight_alpha, depth_weight_beta,
                       {WEIGHT_CENTER_SQL} AS center,
                       memory_group_id
                FROM memories
                WHERE id = ANY($1) AND agent_id = $2
                """,
                source_memory_ids,
                agent_id,
            )
            if rows:
                # Weighted average by center (heavy sources skew more)
                total_weight = sum(float(r["center"]) for r in rows)
                if total_weight > 0:
                    initial_alpha = sum(
                        float(r["depth_weight_alpha"]) * float(r["center"]) for r in rows
                    ) / total_weight
                    initial_beta = sum(
                        float(r["depth_weight_beta"]) * float(r["center"]) for r in rows
                    ) / total_weight

                # D-018c: inherit group_id when ALL sources share the same one
                group_ids = {r["memory_group_id"] for r in rows}
                if len(group_ids) == 1:
                    only_id = group_ids.pop()
                    if only_id is not None:
                        inherited_group_id = only_id

        # Merge insight_level into metadata for query filtering
        meta = dict(metadata) if metadata else {}
        meta["insight_level"] = insight_level

        mem_id = await self.store_memory(
            content,
            agent_id,
            memory_type="reflection",
            source="consolidation",
            tags=tags,
            importance=importance,
            metadata=meta,
            initial_alpha=initial_alpha,
            initial_beta=initial_beta,
            memory_group_id=inherited_group_id,
        )
        # Set insight_level column directly (for DB-level filtering)
        try:
            await self.pool.execute(
                "UPDATE memories SET insight_level = $1 WHERE id = $2",
                insight_level, mem_id,
            )
        except Exception:
            logger.warning("Failed to set insight_level=%d on %s", insight_level, mem_id)
        for src_id in source_memory_ids:
            await self.pool.execute(
                """
                INSERT INTO memory_supersedes (insight_id, source_id, agent_id)
                VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                """,
                mem_id,
                src_id,
                agent_id,
            )
        return mem_id

    # ── Retrieve ───────────────────────────────────────────────────────

    async def get_memory(self, memory_id: str, agent_id: str) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM memories WHERE id = $1 AND agent_id = $2 AND NOT archived",
            memory_id,
            agent_id,
        )
        return dict(row) if row else None

    async def get_random_memory(self, agent_id: str) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM memories WHERE agent_id = $1 AND NOT archived ORDER BY RANDOM() LIMIT 1",
            agent_id,
        )
        return dict(row) if row else None

    async def memory_count(self, agent_id: str) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND NOT archived",
            agent_id,
        )

    async def delete_memory(self, memory_id: str, agent_id: str) -> bool:
        result = await self.pool.execute(
            "DELETE FROM memories WHERE id = $1 AND agent_id = $2",
            memory_id,
            agent_id,
        )
        return result == "DELETE 1"

    # ── Evidence chain ─────────────────────────────────────────────────

    async def why_do_i_believe(self, memory_id: str, agent_id: str) -> list[dict]:
        rows = await self.pool.fetch(
            """
            WITH RECURSIVE evidence_chain AS (
                SELECT s.source_id, 1 AS depth
                FROM memory_supersedes s
                WHERE s.insight_id = $1 AND s.agent_id = $2
                UNION ALL
                SELECT s.source_id, ec.depth + 1
                FROM memory_supersedes s
                JOIN evidence_chain ec ON s.insight_id = ec.source_id
                WHERE ec.depth < 5 AND s.agent_id = $2
            )
            SELECT DISTINCT m.id, m.content, m.type, m.confidence, m.importance,
                   m.created_at, m.source, m.tags, ec.depth
            FROM evidence_chain ec JOIN memories m ON m.id = ec.source_id
            WHERE m.agent_id = $2
            ORDER BY ec.depth, m.created_at
            """,
            memory_id,
            agent_id,
        )
        return [dict(r) for r in rows]

    async def get_insights_for(self, source_memory_id: str, agent_id: str) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT m.id, m.content, m.importance, m.evidence_count, m.created_at
            FROM memory_supersedes s JOIN memories m ON m.id = s.insight_id
            WHERE s.source_id = $1 AND s.agent_id = $2
            ORDER BY m.importance DESC
            """,
            source_memory_id,
            agent_id,
        )
        return [dict(r) for r in rows]

    # ── Search ─────────────────────────────────────────────────────────

    async def search_similar(
        self,
        query: str,
        agent_id: str,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> list[dict]:
        """Pure vector similarity search."""
        query_vec = await self.embed(query, task_type="RETRIEVAL_QUERY")
        rows = await self.pool.fetch(
            """
            SELECT id, content, type, confidence, importance, access_count,
                   last_accessed, tags, source, created_at,
                   1 - (embedding <=> $1::halfvec) AS similarity
            FROM memories
            WHERE agent_id = $2 AND NOT archived
              AND 1 - (embedding <=> $1::halfvec) > $3
            ORDER BY embedding <=> $1::halfvec
            LIMIT $4
            """,
            str(query_vec),
            agent_id,
            min_similarity,
            top_k,
        )
        return [dict(r) for r in rows]

    async def search_hybrid(
        self,
        query: str,
        agent_id: str,
        top_k: int = 20,
        mutate: bool = True,
        reinforce_top_k: int = 5,
    ) -> list[dict]:
        """Dense + sparse fusion with RRF + weight + recency."""
        query_vec = await self.embed(query, task_type="RETRIEVAL_QUERY")

        rows = await self.pool.fetch(
            f"""
            WITH dense AS (
                SELECT id, content, type, confidence, importance, access_count,
                       last_accessed, tags, source, created_at, embedding,
                       depth_weight_alpha, depth_weight_beta,
                       ROW_NUMBER() OVER (ORDER BY embedding <=> $1::halfvec) AS dense_rank
                FROM memories
                WHERE agent_id = $2 AND NOT archived
                ORDER BY embedding <=> $1::halfvec
                LIMIT 50
            ),
            sparse AS (
                SELECT id,
                       ROW_NUMBER() OVER (ORDER BY ts_rank_cd(content_tsv, query) DESC) AS sparse_rank
                FROM memories, websearch_to_tsquery('english', $3) query
                WHERE agent_id = $2 AND NOT archived AND content_tsv @@ query
                ORDER BY ts_rank_cd(content_tsv, query) DESC
                LIMIT 50
            ),
            combined AS (
                SELECT
                    COALESCE(d.id, s.id) AS id,
                    d.content, d.type, d.confidence, d.importance, d.access_count,
                    d.last_accessed, d.tags, d.source, d.created_at, d.embedding,
                    d.depth_weight_alpha, d.depth_weight_beta,
                    1.0 / (60 + COALESCE(d.dense_rank, 999)) AS rrf_dense,
                    1.0 / (60 + COALESCE(s.sparse_rank, 999)) AS rrf_sparse
                FROM dense d FULL OUTER JOIN sparse s ON d.id = s.id
            )
            SELECT
                id, content, type, confidence, importance, access_count,
                last_accessed, tags, source, created_at,
                depth_weight_alpha, depth_weight_beta,
                rrf_dense, rrf_sparse,
                EXP(-0.693 * EXTRACT(EPOCH FROM (NOW() - created_at)) / 604800.0) AS recency_score,
                0.5 * (rrf_dense + rrf_sparse)
                  + 0.3 * EXP(-0.693 * EXTRACT(EPOCH FROM (NOW() - created_at)) / 604800.0)
                  + 0.2 * ({WEIGHT_CENTER_SQL}) AS weighted_score
            FROM combined
            WHERE content IS NOT NULL
            ORDER BY weighted_score DESC
            LIMIT $4
            """,
            str(query_vec),
            agent_id,
            query,
            top_k,
        )
        results = [dict(r) for r in rows]

        if mutate and results:
            retrieved_ids = [r["id"] for r in results[:reinforce_top_k]]
            near_miss_ids = [r["id"] for r in results[reinforce_top_k:]]
            await self.apply_retrieval_mutation(
                retrieved_ids, agent_id, near_miss_ids=near_miss_ids
            )
            await update_co_access(self.pool, retrieved_ids, agent_id)

        return results

    async def search_reranked(
        self,
        query: str,
        agent_id: str,
        top_k: int = 5,
        hybrid_top_k: int = 20,
    ) -> list[dict]:
        """Hybrid search + FlashRank reranking."""
        candidates = await self.search_hybrid(
            query, agent_id, top_k=hybrid_top_k, mutate=False
        )
        if not candidates:
            return []

        if self._flashrank_model is None:
            from flashrank import Ranker
            self._flashrank_model = Ranker(model_name="ms-marco-MiniLM-L-12-v2")
            logger.info("FlashRank model loaded.")

        from flashrank import RerankRequest
        passages = [{"id": c["id"], "text": c["content"], "meta": c} for c in candidates]
        rerank_req = RerankRequest(query=query, passages=passages)
        reranked = self._flashrank_model.rerank(rerank_req)

        results = []
        for item in reranked[:top_k]:
            meta = item.metadata if hasattr(item, 'metadata') else item.get("meta", item.get("metadata", {}))
            if isinstance(meta, dict) and "weighted_score" in meta:
                weighted = meta["weighted_score"]
            else:
                weighted = 0.0
            rerank_score = item.score if hasattr(item, 'score') else item.get("score", 0.0)
            final_score = 0.6 * rerank_score + 0.4 * weighted
            result = dict(meta) if isinstance(meta, dict) else {}
            result["rerank_score"] = rerank_score
            result["final_score"] = final_score
            results.append(result)

        results.sort(key=lambda x: x.get("final_score", 0), reverse=True)

        # Apply mutation post-reranking
        if results:
            retrieved_ids = [r["id"] for r in results]
            await self.apply_retrieval_mutation(retrieved_ids, agent_id)
            await update_co_access(self.pool, retrieved_ids, agent_id)

        return results

    # ── Identity scoring (D-015) ─────────────────────────────────────

    async def score_identity_wxs(
        self,
        query_vec: list[float],
        agent_id: str,
        top_n: int = 20,
    ) -> list[dict]:
        """Score identity memories by weight_center × cosine_sim (D-015).

        Computes injection_score entirely in SQL. Excludes immutables
        (handled by Track 0). Returns candidates ranked by injection_score.
        """
        rows = await self.pool.fetch(
            f"""
            SELECT id, content, depth_weight_alpha, depth_weight_beta, metadata,
                   ({WEIGHT_CENTER_SQL})
                     * (1 - (embedding <=> $1::halfvec)) AS injection_score
            FROM memories
            WHERE agent_id = $2
              AND NOT archived
              AND embedding IS NOT NULL
              AND immutable = false
            ORDER BY injection_score DESC
            LIMIT $3
            """,
            str(query_vec),
            agent_id,
            top_n,
        )
        return [dict(r) for r in rows]

    async def get_identity_embeddings(
        self, agent_id: str, top_n: int = 20,
    ) -> list[tuple] | None:
        """Top-N memories by weight center — identity signal for gate & gut.

        D-005/D-030/D-032: No threshold — weighted average handles signal.
        Returns list[(content, center, ndarray)] or None.
        """
        rows = await self.pool.fetch(
            f"""
            SELECT content,
                   {WEIGHT_CENTER_SQL} AS center,
                   embedding::float4[] AS embedding_arr
            FROM memories
            WHERE agent_id = $1 AND NOT archived AND embedding IS NOT NULL
            ORDER BY {WEIGHT_CENTER_SQL} DESC
            LIMIT $2
            """,
            agent_id,
            top_n,
        )
        if not rows:
            return None
        result = []
        for r in rows:
            emb = np.array(r["embedding_arr"], dtype=np.float32)
            result.append((r["content"], r["center"], emb))
        return result or None

    # ── Mutation ───────────────────────────────────────────────────────

    async def apply_retrieval_mutation(
        self,
        retrieved_ids: list[str],
        agent_id: str,
        near_miss_ids: list[str] | None = None,
        vector_scores: dict[str, float] | None = None,
    ) -> None:
        """Retrieval-induced strengthening/weakening of memory weights."""
        now = datetime.now(timezone.utc)

        if not retrieved_ids:
            return

        # 1. Batch update access_count + last_accessed + timestamps (CQ-002)
        await self.pool.execute(
            """
            UPDATE memories
            SET access_count = access_count + 1,
                last_accessed = $1,
                access_timestamps = array_append(
                    COALESCE(access_timestamps, ARRAY[]::timestamptz[]), $1
                ),
                updated_at = $1
            WHERE id = ANY($2) AND agent_id = $3 AND NOT immutable AND NOT archived
            """,
            now,
            retrieved_ids,
            agent_id,
        )

        # 2. Alpha boost
        needs_per_memory = bool(vector_scores) or bool(self.safety)
        if needs_per_memory:
            # Batch-fetch memories for safety/novelty-bonus checks (CQ-002)
            rows = await self.pool.fetch(
                """
                SELECT id, access_count, depth_weight_alpha, depth_weight_beta
                FROM memories
                WHERE id = ANY($1) AND agent_id = $2 AND NOT immutable AND NOT archived
                """,
                retrieved_ids,
                agent_id,
            )
            mem_map = {r["id"]: dict(r) for r in rows}

            for mem_id in retrieved_ids:
                mem = mem_map.get(mem_id)
                if not mem:
                    continue

                gain = 0.1
                if vector_scores and vector_scores.get(mem_id, 0) > 0.9:
                    if mem.get("access_count", 0) == 0:
                        gain = 0.2

                if self.safety:
                    allowed, adj_alpha, _adj_beta, _reasons = self.safety.check_weight_change(
                        mem_id, mem["depth_weight_alpha"], mem["depth_weight_beta"],
                        delta_alpha=gain,
                    )
                    if not allowed:
                        continue
                    gain = adj_alpha

                await self.pool.execute(
                    """
                    UPDATE memories
                    SET depth_weight_alpha = depth_weight_alpha + $2,
                        updated_at = $1
                    WHERE id = $3 AND agent_id = $4 AND NOT immutable AND NOT archived
                    """,
                    now,
                    gain,
                    mem_id,
                    agent_id,
                )
        else:
            # No safety, no vector_scores — uniform +0.1 batch (CQ-002)
            await self.pool.execute(
                """
                UPDATE memories
                SET depth_weight_alpha = depth_weight_alpha + 0.1,
                    updated_at = $1
                WHERE id = ANY($2) AND agent_id = $3 AND NOT immutable AND NOT archived
                """,
                now,
                retrieved_ids,
                agent_id,
            )

        # 3. Near-miss beta bump — single batch (CQ-003)
        if near_miss_ids:
            await self.pool.execute(
                """
                UPDATE memories
                SET depth_weight_beta = depth_weight_beta + 0.05,
                    updated_at = $1
                WHERE id = ANY($2) AND agent_id = $3 AND NOT immutable AND NOT archived
                """,
                now,
                near_miss_ids,
                agent_id,
            )

    async def touch_memory(
        self,
        memory_id: str,
        agent_id: str,
    ) -> None:
        """Refresh last_accessed without incrementing access_count.

        Used by gate novelty check — prevents decay for 24h
        but doesn't count as a real retrieval.
        D-018c: If the memory belongs to a group, refreshes all group members.
        """
        now = datetime.now(timezone.utc)

        # Check if memory belongs to a group
        group_id = await self.pool.fetchval(
            "SELECT memory_group_id FROM memories WHERE id = $1 AND agent_id = $2 AND NOT archived",
            memory_id,
            agent_id,
        )

        if group_id:
            # Group-wide touch: refresh all siblings
            await self.pool.execute(
                """
                UPDATE memories
                SET last_accessed = $1, updated_at = $1
                WHERE memory_group_id = $2 AND agent_id = $3 AND NOT immutable AND NOT archived
                """,
                now,
                group_id,
                agent_id,
            )
        else:
            # Standalone memory: touch only this one
            await self.pool.execute(
                """
                UPDATE memories
                SET last_accessed = $1, updated_at = $1
                WHERE id = $2 AND agent_id = $3 AND NOT immutable AND NOT archived
                """,
                now,
                memory_id,
                agent_id,
            )

    # ── Scratch buffer ─────────────────────────────────────────────────

    async def buffer_scratch(
        self,
        content: str,
        agent_id: str,
        source: str | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        scratch_id = self._gen_id("scratch")
        await self.pool.execute(
            """
            INSERT INTO scratch_buffer (id, agent_id, content, source, tags, metadata, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '24 hours')
            """,
            scratch_id,
            agent_id,
            content,
            source,
            tags or [],
            metadata or {},
        )
        return scratch_id

    async def cleanup_expired_scratch(self, agent_id: str) -> int:
        result = await self.pool.execute(
            "DELETE FROM scratch_buffer WHERE agent_id = $1 AND expires_at < NOW()",
            agent_id,
        )
        count = int(result.split()[-1]) if result else 0
        return count

    # ── Novelty / staleness ────────────────────────────────────────────

    async def check_novelty(
        self,
        content: str,
        agent_id: str,
        threshold: float = NOVELTY_THRESHOLD,
        embedding: list[float] | None = None,
    ) -> tuple[bool, float, str | None]:
        """Check if content is novel compared to existing memories.

        Returns (is_novel, max_similarity, most_similar_id).
        If embedding is provided, skips the internal embed() call.
        """
        vec = embedding if embedding is not None else await self.embed(content, task_type="SEMANTIC_SIMILARITY")
        row = await self.pool.fetchrow(
            """
            SELECT id, 1 - (embedding <=> $1::halfvec) AS similarity
            FROM memories
            WHERE agent_id = $2 AND NOT archived
            ORDER BY embedding <=> $1::halfvec
            LIMIT 1
            """,
            str(vec),
            agent_id,
        )
        if not row:
            return True, 0.0, None
        max_sim = row["similarity"]
        return max_sim < threshold, max_sim, row["id"]

    async def get_stale_memories(
        self,
        agent_id: str,
        stale_days: int = 90,
        min_access_count: int = 3,
    ) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT id, content, importance, access_count, last_accessed,
                   depth_weight_alpha, depth_weight_beta
            FROM memories
            WHERE agent_id = $1
              AND NOT archived
              AND (last_accessed IS NULL OR last_accessed < NOW() - INTERVAL '1 day' * $2)
              AND access_count < $3
              AND importance > 0.05
              AND (protect_until IS NULL OR protect_until < NOW())
            ORDER BY importance ASC
            """,
            agent_id,
            stale_days,
            min_access_count,
        )
        return [dict(r) for r in rows]

    async def decay_memories(
        self,
        memory_ids: list[str],
        agent_id: str,
        factor: float = 0.5,
    ) -> None:
        await self.pool.execute(
            "UPDATE memories SET importance = importance * $1 WHERE id = ANY($2) AND agent_id = $3 AND NOT archived",
            factor,
            memory_ids,
            agent_id,
        )

    async def avg_depth_weight_center(
        self,
        agent_id: str,
    ) -> float:
        query = f"""
            SELECT AVG({WEIGHT_CENTER_SQL})
            FROM memories WHERE agent_id = $1 AND NOT archived
        """
        result = await self.pool.fetchval(query, agent_id)
        return float(result) if result else 0.0

    # ── Corrections ────────────────────────────────────────────────────

    async def search_corrections(
        self,
        query_embedding: list[float],
        agent_id: str,
        top_k: int = 3,
    ) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT id, content, confidence, metadata, created_at,
                   1 - (embedding <=> $1::halfvec) AS similarity
            FROM memories
            WHERE agent_id = $2 AND NOT archived AND type = 'correction'
            ORDER BY embedding <=> $1::halfvec
            LIMIT $3
            """,
            str(query_embedding),
            agent_id,
            top_k,
        )
        return [dict(r) for r in rows]

    async def store_correction(
        self,
        trigger: str,
        original_reasoning: str,
        correction: str,
        agent_id: str,
        context: str | None = None,
        confidence: float = 0.8,
    ) -> str:
        content = f"Trigger: {trigger}\nOriginal: {original_reasoning}\nCorrection: {correction}"
        metadata = {"trigger": trigger, "original": original_reasoning, "correction": correction}
        if context:
            metadata["context"] = context
        return await self.store_memory(
            content,
            agent_id,
            memory_type="correction",
            confidence=confidence,
            metadata=metadata,
        )

    # ── Dedup helpers ──────────────────────────────────────────────────

    async def archive_memory(self, memory_id: str, agent_id: str, reason: dict) -> bool:
        """Soft-delete a memory by setting archived=True with a reason."""
        result = await self.pool.execute(
            """
            UPDATE memories
            SET archived = TRUE, archived_reason = $3::jsonb, updated_at = NOW()
            WHERE id = $1 AND agent_id = $2 AND NOT archived
            """,
            memory_id,
            agent_id,
            reason,
        )
        return result == "UPDATE 1"

    async def transfer_weights(self, from_id: str, to_id: str, agent_id: str) -> None:
        """Transfer alpha+beta from one memory to another (preserves distribution shape)."""
        from_mem = await self.pool.fetchrow(
            "SELECT depth_weight_alpha, depth_weight_beta FROM memories WHERE id = $1 AND agent_id = $2",
            from_id,
            agent_id,
        )
        if not from_mem:
            return
        await self.pool.execute(
            """
            UPDATE memories
            SET depth_weight_alpha = depth_weight_alpha + $2,
                depth_weight_beta = depth_weight_beta + $3,
                updated_at = NOW()
            WHERE id = $1 AND agent_id = $4 AND NOT archived
            """,
            to_id,
            from_mem["depth_weight_alpha"],
            from_mem["depth_weight_beta"],
            agent_id,
        )

    async def execute_dedup_verdict(self, agent_id: str, verdict: dict) -> str | None:
        """Execute a dedup verdict: archive loser, transfer weights, handle synthesis.

        Returns survivor_id or None if verdict was 'distinct'.
        BUG-003 fix: when synthesis requested but text missing, falls back to
        picking the memory with higher weight_center as survivor.
        """
        if verdict.get("verdict") != "redundant":
            return None

        survivor_label = verdict.get("survivor")

        if survivor_label in ("A", "B"):
            # Simple case: one survivor, one loser
            survivor_id = verdict["survivor_id"]
            loser_id = verdict["loser_id"]
            await self.transfer_weights(loser_id, survivor_id, agent_id)
            await self.archive_memory(loser_id, agent_id, {
                "dedup": True,
                "survivor_id": survivor_id,
                "reason": verdict.get("reason", ""),
            })
            return survivor_id

        if survivor_label == "synthesize" and verdict.get("synthesis"):
            # Synthesis: create new memory, transfer weights from both, archive both
            new_id = await self.store_memory(
                content=verdict["synthesis"],
                agent_id=agent_id,
                source="dedup_synthesis",
                source_tag="consolidation",
            )
            orig_a = verdict["mem_a_id"]
            orig_b = verdict["mem_b_id"]
            await self.transfer_weights(orig_a, new_id, agent_id)
            await self.transfer_weights(orig_b, new_id, agent_id)
            for orig_id in (orig_a, orig_b):
                await self.archive_memory(orig_id, agent_id, {
                    "dedup": True,
                    "survivor_id": new_id,
                    "reason": "synthesized replacement",
                })
            return new_id

        # BUG-003 fallback: synthesis requested but no text (or unknown label).
        # Pick the memory with higher weight_center as survivor.
        mem_a_id = verdict.get("mem_a_id")
        mem_b_id = verdict.get("mem_b_id")
        if mem_a_id and mem_b_id:
            rows = await self.pool.fetch(
                f"""
                SELECT id, {WEIGHT_CENTER_SQL} AS center
                FROM memories
                WHERE id = ANY($1) AND agent_id = $2 AND NOT archived
                """,
                [mem_a_id, mem_b_id],
                agent_id,
            )
            if len(rows) == 2:
                centers = {r["id"]: r["center"] for r in rows}
                if centers.get(mem_a_id, 0) >= centers.get(mem_b_id, 0):
                    survivor_id, loser_id = mem_a_id, mem_b_id
                else:
                    survivor_id, loser_id = mem_b_id, mem_a_id
                await self.transfer_weights(loser_id, survivor_id, agent_id)
                await self.archive_memory(loser_id, agent_id, {
                    "dedup": True,
                    "survivor_id": survivor_id,
                    "reason": verdict.get("reason", "") + " (synthesis fallback: higher weight)",
                })
                logger.info(
                    "Dedup synthesis fallback: %s survives over %s [%s]",
                    survivor_id, loser_id, agent_id,
                )
                return survivor_id

        return None
