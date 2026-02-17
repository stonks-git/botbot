# KB-01: Architecture

## Overview

BotBot integrates the intuitive-AI cognitive architecture into OpenClaw via a Python sidecar brain service.

**Stack**: Python 3.12 (brain) + Node.js/TypeScript (OpenClaw) + PostgreSQL 17 + pgvector

**Architecture**: Three Docker containers — `postgres` (pgvector), `brain` (FastAPI on :8400), `openclaw` (Node.js). Brain and OpenClaw communicate over HTTP on a shared Docker network. OpenClaw's `memory-brain` plugin hooks into the message lifecycle to inject/capture memories.

**Brain subsystems** (ported from intuitive-AI with agent_id namespacing):
- Memory store — unified table, Beta(alpha,beta) weight distributions, halfvec(3072) embeddings
- Entry/Exit gates — ACT-R activation-based filtering
- Identity layers — L0 (identity) + L1 (goals) as JSON files per agent
- Gut feeling — two-centroid emotional model (subconscious vs attention)
- Consolidation — background Tier 1 (constant) + Tier 2 (hourly deep) processing
- DMN/Idle loop — spontaneous self-prompts with rumination threads
- Safety monitor — hard ceilings, diminishing returns, circuit breakers
- Bootstrap readiness — 10 milestones tracking agent maturation

**Embedding model**: `gemini-embedding-001` at 3072 dimensions (max Matryoshka resolution)

**Key files**: `brain/src/schema.sql` (DB schema), `brain/src/api.py` (HTTP API), `docker-compose.yml` (orchestration)

See `KB/blueprints/v0.1_brain_integration_plan.md` for the full 9-phase implementation plan.

## Phase 1: Memory Core (implemented)

**Brain Python modules** (ported from intuitive-AI with agent_id namespacing):
- `brain/src/config.py` — RetryConfig, EMBED_MODEL/DIMENSIONS, MEMORY_TYPE_PREFIXES
- `brain/src/stochastic.py` — StochasticWeight: Beta(alpha,beta) with observe/reinforce/contradict
- `brain/src/activation.py` — ACT-R: B+S+P+epsilon, cosine_similarity, base_level_activation
- `brain/src/relevance.py` — 5-component Dirichlet relevance, co-access (Hebbian), spread_activation
- `brain/src/memory.py` — MemoryStore: embed (Gemini), store, search_similar/hybrid/reranked, retrieval mutation

**API endpoints** (`brain/src/api.py`):
- `POST /memory/store` — embed content via Gemini → store with Beta(1,4) initial weight
- `POST /memory/retrieve` — hybrid (dense+sparse+RRF) or reranked (+ FlashRank), triggers retrieval-induced mutation (alpha += 0.1 per access)
- `GET /memory/{id}?agent_id=X` — single memory with Beta weights
- `DELETE /memory/{id}?agent_id=X` — hard delete

**OpenClaw plugin** (`openclaw/extensions/memory-brain/`):
- Tools: memory_recall, memory_store, memory_forget (HTTP to brain :8400)
- Hook `before_agent_start`: auto-recall → inject via prependContext
- Hook `agent_end`: auto-capture user messages (max 3/session, deduplicated)
- Graceful degradation: if brain unreachable, plugin logs warning, OpenClaw works without memory

**Retrieval pipeline**: query → embed(RETRIEVAL_QUERY) → dense CTE (pgvector top 50) + sparse CTE (tsvector top 50) → FULL OUTER JOIN + RRF (k=60) → weighted_score = 0.5*RRF + 0.3*recency(7d halflife) + 0.2*depth_center → FlashRank rerank → final = 0.6*rerank + 0.4*weighted

**Retrieval-induced mutation**: top-k hits get alpha += 0.1 (dormant with score>0.9 get 0.2). Near-misses get beta += 0.05 (except immutable). Safety.check_weight_change() called if safety monitor wired (Phase 7).

**API request/response models** (Pydantic, in `brain/src/api.py`):
```
POST /memory/store
  Request:  { agent_id, content, memory_type="semantic", source?, tags=[], confidence=0.5, importance=0.5, metadata?, source_tag? }
  Response: { id, agent_id, status="stored" }

POST /memory/retrieve
  Request:  { agent_id, query, top_k=5, mode="reranked"|"hybrid"|"similar" }
  Response: { agent_id, query, count, memories: [{ id, content, type, confidence, importance, access_count, tags, source?, created_at, score }] }

GET /memory/{memory_id}?agent_id=X
  Response: { id, agent_id, content, type, confidence, importance, access_count, tags, source?, created_at, depth_weight_alpha, depth_weight_beta }

DELETE /memory/{memory_id}?agent_id=X
  Response: { id, deleted: true }
```

**MemoryStore method inventory** (`brain/src/memory.py`):

| Method | Phase 1 API | Used by later phase |
|--------|------------|---------------------|
| `embed(text, task_type, title)` | store/retrieve | all |
| `embed_batch(texts, ...)` | — | consolidation (P5) |
| `store_memory(content, agent_id, ...)` | POST /store | all |
| `store_insight(content, agent_id, source_ids, ...)` | — | consolidation (P5) |
| `get_memory(id, agent_id)` | GET /{id} | all |
| `get_random_memory(agent_id)` | — | DMN (P6) |
| `memory_count(agent_id)` | /health | bootstrap (P8) |
| `delete_memory(id, agent_id)` | DELETE /{id} | — |
| `why_do_i_believe(id, agent_id)` | — | consolidation (P5) |
| `get_insights_for(src_id, agent_id)` | — | consolidation (P5) |
| `search_similar(query, agent_id, ...)` | POST /retrieve (mode=similar) | gate (P2) |
| `search_hybrid(query, agent_id, ...)` | POST /retrieve (mode=hybrid) | context assembly (P3) |
| `search_reranked(query, agent_id, ...)` | POST /retrieve (mode=reranked) | — |
| `apply_retrieval_mutation(ids, agent_id, ...)` | auto via search | — |
| `buffer_scratch(content, agent_id, ...)` | — | gate (P2) |
| `flush_scratch(agent_id, ...)` | — | gate (P2) |
| `cleanup_expired_scratch(agent_id)` | — | gate (P2) |
| `check_novelty(content, agent_id, threshold)` | — | gate (P2) |
| `get_stale_memories(agent_id, ...)` | — | consolidation (P5) |
| `decay_memories(ids, agent_id, factor)` | — | consolidation (P5) |
| `avg_depth_weight_center(agent_id)` | — | consolidation (P5) |
| `store_correction(trigger, ..., agent_id)` | — | consolidation (P5) |
| `search_corrections(embedding, agent_id)` | — | consolidation (P5) |

**Config constants** (`brain/src/config.py`):
- `EMBED_MODEL = "gemini-embedding-001"`, `EMBED_DIMENSIONS = 3072`
- `RetryConfig(max_retries=3, base_delay=1.0, max_delay=30.0)`
- `MEMORY_TYPE_PREFIXES` — 8 types: episodic, semantic, procedural, preference, reflection, correction, narrative, tension

**Cross-module dependency graph** (Phase 1 only):
```
config.py       ← standalone
stochastic.py   ← standalone
activation.py   ← numpy
relevance.py    ← activation
memory.py       ← config, relevance, db (pool)
api.py          ← memory, db
```

**Plugin config** (`openclaw/extensions/memory-brain/`):
- `BRAIN_URL` env or `brainUrl` config (default: `http://brain:8400`)
- `BRAIN_AGENT_ID` env or `agentId` config (default: `"default"`)
- `autoRecall` (default: true), `autoCapture` (default: true)
- `recallLimit` (default: 5), `captureMaxChars` (default: 500)
- Skip prefixes for auto-capture: `/`, `[tool:`, `[system:`, `[error:`, `` ``` ``
- Auto-detect memory types: preference (contains "I prefer"/"I like"/"I want"), procedural ("how to"/"step"/"instructions"), episodic ("I remember"/"yesterday"/"last time"), default: semantic

## Decision Journal

> The Decision Journal tracks **why** decisions changed and what was learned.
> Not a duplicate of roadmap.json — roadmap records what was decided, the Decision Journal records the evolution and lessons.
>
> **Scan headers first.** Expand an entry only if current task touches that tag domain.
> Tags must exist in `charter.json` `project.tag_taxonomy`.

<!--
### DJ-001 [tag] D-XXX superseded by D-YYY

- **Was:** (original decision, one line)
- **Now:** (replacement decision, one line)
- **Why:** (what evidence/reasoning caused the change)
- **Lesson:** (reusable takeaway — the point of this whole system)

RULES:
- One entry per superseded/amended decision. Not for new decisions (those go in roadmap.json).
- Tag in brackets = greppable domain (must be in charter.json tag_taxonomy).
- Keep entries to 4 lines max. Link to evidence, don't paste it.
- DJ numbering is sequential, never reused.
- When adding a DJ entry, also update the decision's status to "superseded" in roadmap.json.
- Add a devlog entry with event: "dj_entry".
-->
