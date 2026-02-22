# KB-01: Architecture

## Overview

BotBot integrates the intuitive-AI cognitive architecture into OpenClaw via a Python sidecar brain service.

**Stack**: Python 3.12 (brain) + Node.js/TypeScript (OpenClaw) + PostgreSQL 17 + pgvector

**Architecture**: Three Docker containers — `postgres` (pgvector), `brain` (FastAPI on :8400), `openclaw` (Node.js). Brain and OpenClaw communicate over HTTP on a shared Docker network. OpenClaw's `memory-brain` plugin hooks into the message lifecycle to inject/capture memories.

**Brain subsystems** (ported from intuitive-AI with agent_id namespacing):
- Memory store — unified table, Beta(alpha,beta) weight distributions, halfvec(3072) embeddings
- Entry/Exit gates — ACT-R activation-based filtering
- Context assembly + identity — identity emerges from top unified memory weights (D-005), no L0/L1 files
- Gut feeling — two-centroid emotional model (subconscious vs attention)
- Consolidation — background Tier 1 (constant) + Tier 2 (hourly deep) processing
- DMN/Idle loop — spontaneous self-prompts with rumination threads
- Safety monitor — hard ceilings, diminishing returns, circuit breakers
- Bootstrap readiness — 10 milestones tracking agent maturation

**Embedding model**: `gemini-embedding-001` at 3072 dimensions (max Matryoshka resolution)

**LLM model**: `gemini-3-flash-preview` via `google-genai` SDK (D-006: switched from Anthropic Haiku — Max subscription OAuth tokens restricted to Claude Code/Claude.ai only). Single `GOOGLE_API_KEY` for both embeddings and LLM.

**Key files**: `brain/src/schema.sql` (DB schema), `brain/src/api.py` (HTTP API), `brain/src/llm.py` (LLM wrapper), `docker-compose.yml` (orchestration)

**Integration status**: All 19 endpoints verified working via docker compose (2026-02-21). Store→retrieve→gate→assemble cycle, consolidation deep cycle, safety, DMN, bootstrap all operational.

See `KB/blueprints/v0.3_current_state.md` for the current blueprint (done phases + remaining plan).

## Decision Journal

| ID | Tag | Decision | Supersedes | Date |
|----|-----|----------|------------|------|
| DJ-001 | memory | L0/L1 layers discarded for unified memory | — | 2026-02-19 |
| DJ-002 | llm | Anthropic Haiku replaced by Gemini 3 Flash Preview (Max OAuth restricted) | — | 2026-02-21 |
| DJ-003 | gate | Hunger curve: dynamic gate threshold based on memory count. Newborn agents absorb everything, selectivity increases as memories accumulate. | — | 2026-02-21 |
| DJ-005 | identity | D-015 floor dropped: pure weight×cosine_sim. Any floor creates immortal memories. | D-015 original | 2026-02-22 |
| DJ-006 | identity | D-017 core/active split dropped, identity hash dormant. Core tier = immortal by construction. Values are dispositional, not declarative. | D-017 original | 2026-02-22 |
| DJ-007 | consolidation | D-016 revised: agent research sessions with web search instead of internal LLM fact-check. Training data not reliable for verification. | D-016 original | 2026-02-22 |
| DJ-008 | consolidation | D-016 amended: 2-search confirmation replaces single-search auto-displace. Research_queue table with pending→researched→confirmed lifecycle. | D-016 (DJ-007) | 2026-02-22 |

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
- Hook `agent_end`: auto-capture user + assistant messages with pre-gate chunking (max 10 gate calls/turn)
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
| `embed_batch(texts, ...)` | — | consolidation (P5). True batch via Gemini API (CQ-008): 1 API call per 100 texts |
| `store_memory(content, agent_id, ..., memory_group_id=None)` | POST /store | all. D-018c: optional `memory_group_id` links chunks |
| `store_insight(content, agent_id, source_ids, ...)` | — | consolidation (P5). D-018c: inherits `memory_group_id` when all sources share one |
| `get_memory(id, agent_id)` | GET /{id} | all |
| `get_random_memory(agent_id)` | — | DMN (P6) |
| `memory_count(agent_id)` | /health | bootstrap (P8) |
| `delete_memory(id, agent_id)` | DELETE /{id} | — |
| `why_do_i_believe(id, agent_id)` | — | consolidation (P5) |
| `get_insights_for(src_id, agent_id)` | — | consolidation (P5) |
| `search_similar(query, agent_id, ...)` | POST /retrieve (mode=similar) | gate (P2) |
| `search_hybrid(query, agent_id, ...)` | POST /retrieve (mode=hybrid) | context assembly (P3) |
| `search_reranked(query, agent_id, ...)` | POST /retrieve (mode=reranked) | — |
| `apply_retrieval_mutation(ids, agent_id, ...)` | auto via search + context assembly | context assembly (P3), gate (P2) |
| `touch_memory(memory_id, agent_id)` | — | gate (P2) novelty check. D-018c: group-wide — refreshes all siblings when memory has `memory_group_id` |
| `buffer_scratch(content, agent_id, ...)` | — | gate (P2) |
| `cleanup_expired_scratch(agent_id)` | — | gate (P2) |
| `check_novelty(content, agent_id, threshold, embedding=None)` → `(bool, float, str\|None)` | — | gate (P2), consolidation (P5). Optional pre-computed embedding skips internal embed (CQ-010) |
| `get_stale_memories(agent_id, ...)` | — | consolidation (P5) |
| `decay_memories(ids, agent_id, factor)` | — | consolidation (P5) |
| `avg_depth_weight_center(agent_id)` | — | consolidation (P5) |
| `store_correction(trigger, ..., agent_id)` | — | consolidation (P5) |
| `search_corrections(embedding, agent_id)` | — | consolidation (P5) |

**Config constants** (`brain/src/config.py`):
- `EMBED_MODEL = "gemini-embedding-001"`, `EMBED_DIMENSIONS = 3072`
- `RetryConfig(max_retries=3, base_delay=1.0, max_delay=30.0)`
- `MEMORY_TYPE_PREFIXES` — 9 types: episodic, semantic, procedural, preference, reflection, correction, narrative, tension, research_finding
- `NOVELTY_THRESHOLD = 0.85` — shared similarity threshold for novelty/dedup checks (T-B06/CQ-020). Used by gate, memory, consolidation.
- `WEIGHT_CENTER_SQL = "depth_weight_alpha / (depth_weight_alpha + depth_weight_beta)"` — canonical SQL expression for Beta weight center (T-B06/CQ-013). All SQL queries use this via f-string interpolation.
- Research constants (D-016): `RESEARCH_HOURLY_LIMIT=1`, `RESEARCH_DAILY_LIMIT=24`, `RESEARCH_MIN_WEIGHT=0.3`, `RESEARCH_DISPLACE_BETA=5.0`, `RESEARCH_CONFIRMATION_HOURS=24`
- Notification constants (D-019): `TELEGRAM_BOT_TOKEN_ENV`, `NOTIFICATION_DELIVERY_INTERVAL=30`, `NOTIFICATION_EXPIRY_HOURS=24`, `NOTIFICATION_MAX_PASSIVE_PER_CONTEXT=3`

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
- `recallLimit` (default: 5), `captureMaxChars` (default: 500) — now target chunk size, not hard cap
- Skip prefixes for auto-capture: `/`, `[tool:`, `[system:`, `[error:`, `` ``` ``
- Auto-detect memory types: preference (contains "I prefer"/"I like"/"I want"), procedural ("how to"/"step"/"instructions"), episodic ("I remember"/"yesterday"/"last time"), default: semantic
- Auto-capture scope: both user and assistant messages. Source tags: `auto_capture` (user), `self_reflection` (assistant)

## Phase 2: Entry/Exit Gate (implemented)

**Brain module** (`brain/src/gate.py`):
- `EntryGate` — stochastic filter on raw incoming content:
  - Short content (<10 chars): 95% skip
  - Mechanical (prefix: `/`, `[tool:`, `[system:`, `[error:`, `` ``` ``): 90% skip
  - Normal content: 99% buffer (1% skip noise floor)
- `ExitGate` — 3x3 decision matrix (relevance × novelty):
  ```
                   Confirming       Novel            Contradicting
  Core         | Reinforce(0.50) | PERSIST(0.85)  | PERSIST+FLAG(0.95)
  Peripheral   | Skip(0.15)     | Buffer(0.40)   | Persist(0.70)
  Irrelevant   | Drop(0.05)     | Drop+noise     | Drop+noise
  ```
- Relevance axis: `spreading_activation(content_embedding, attention, layers)` → core (≥0.6) / peripheral (≥0.3) / irrelevant
- Novelty axis: `check_novelty(embedding=content_embedding)` → `(is_novel, max_similarity, most_similar_id)` + `detect_contradiction_negation()` → confirming (sim≥0.85) / novel (sim<0.6) / contradicting (sim≥0.7 + negation markers). Single embed call reused for both spreading_activation and novelty check (CQ-010). Contradiction fetches content via `get_memory(most_similar_id)` instead of re-searching.
- **Gate touch** (D-012): after novelty check, calls `touch_memory(most_similar_id)` — refreshes `last_accessed` only (no access_count, no alpha/beta). Prevents decay for 24h on referenced memories. "Stay of execution, not a promotion."
- **Semantic chunking** (D-018b): `semantic_chunk(text, max_tokens=300)` splits by paragraph (`\n\n`) then sentence boundaries (`.`/`!`/`?`), greedily merges under 300-token limit. Single oversized sentences kept whole. Used by gate PERSIST path.
- Score: `base_score * (0.5 + 0.5 * s_i) * hunger_boost + emotional_charge_bonus`
- **Hunger curve** (D-009): dynamic score multiplier based on memory count. Newborn agents (0 memories) get `hunger_max_boost=2.5`, decaying exponentially (`exp(-count/10)`) toward 1.0 as memories accumulate. When hunger-boosted score ≥ 0.5 and hunger > 1.05, BUFFER is promoted to PERSIST. This solves the cold-start problem where newborn agents couldn't form memories because everything scored as peripheral×novel→buffer.
- Noise floor: 2% chance DROP → BUFFER
- Decision constants: PERSIST_HIGH, PERSIST_FLAG, PERSIST, REINFORCE, BUFFER, SKIP, DROP

**API endpoint** (`POST /memory/gate`):
```
POST /memory/gate
  Request:  { agent_id, content, source?, source_tag="external_user" }
  Response: { decision, score, memory_id?, scratch_id?, entry_gate: {...}, exit_gate: {...} }
```
Pipeline: entry gate → scratch buffer → exit gate → act on decision:
- PERSIST/PERSIST_HIGH/PERSIST_FLAG → if content > 300 tokens: `semantic_chunk()` splits into chunks, each stored with shared `memory_group_id` and metadata `{group_part, group_total}`. If ≤ 300 tokens: single `store_memory()`. Importance derived from gate score.
- REINFORCE → find most similar memory + `apply_retrieval_mutation()`
- BUFFER → leave in scratch (24h TTL, consolidation picks up later)

**`apply_retrieval_mutation()` internals** (D-013, batched T-B01): Three batch phases:
1. **access_count** (batch, unconditional): single `UPDATE ... WHERE id = ANY($1) AND NOT immutable` — increments `access_count`, sets `last_accessed`, appends to `access_timestamps`.
2. **alpha boost**: If no safety monitor and no vector_scores: single batch `UPDATE ... WHERE id = ANY($1)` with uniform +0.1. If safety wired: batch-fetches all memories in one query, computes per-memory gain via `check_weight_change()`, per-memory UPDATEs (safety returns different gains).
3. **near-miss beta** (batch): single `UPDATE ... WHERE id = ANY($1) AND NOT immutable` — bumps `depth_weight_beta += 0.05`. No separate immutability check needed.
- **Co-access** (`relevance.py:update_co_access`): batch UPSERT via `unnest($1::text[], $2::text[])` — all pairs in one round-trip.
- DROP/SKIP → clean up scratch

**Plugin update** (`openclaw/extensions/memory-brain/index.ts`):
- `agent_end` hook now calls `POST /memory/gate` instead of direct `POST /memory/store`
- Added `brainGate()` HTTP client function
- Gate decisions logged at debug level, gate counts at info level
- **Captures both user AND assistant messages**: user messages gated with `source_tag="auto_capture"`, assistant messages with `source_tag="self_reflection"` — enables identity formation from agent's own output
- **Pre-gate chunking**: `chunkText(text, captureMaxChars)` splits long messages by paragraph (`\n\n`) then sentence boundaries (`.`/`!`/`?`), greedy-merging into chunks up to `captureMaxChars` (default 500). No more hard cap that drops long messages — chunks instead.
- **Gate call budget**: max 10 gate calls per turn across all messages (user + assistant), replacing the old `slice(0, 3)` hard limit
- `shouldCapture()` does client-side pre-filtering (min length 10, skip mechanical prefixes) before chunking

**Gate wiring** (all parameters now active):

| Param | Source | Phase | Status |
|-------|--------|-------|--------|
| `layer_embeddings` | `_get_identity_embeddings()` — top-N DB memories by weight center | P3 | DONE |
| `attention_embedding` | `GutFeeling.attention_centroid` | P4 | DONE |
| `emotional_charge` | `GutFeeling.emotional_charge` | P4 | DONE |
| `memory_count` | `store.memory_count(agent_id)` — hunger curve input | D-009 | DONE |

**Fallback when no memories (new agent):** `subconscious_centroid=None` → `emotional_charge=0.0` → gate uses no emotional bonus. Identity embeddings return None → relevance defaults to "peripheral". **Hunger curve compensates**: 0 memories → 2.5x score boost → peripheral×novel still gets persisted.

**Cross-module dependency graph** (Phase 1+2):
```
config.py       ← standalone
stochastic.py   ← standalone
activation.py   ← numpy
relevance.py    ← activation
memory.py       ← config, relevance, db (pool)
gate.py         ← activation, memory (embed, check_novelty, get_memory)
api.py          ← memory, gate, db
```

## Phase 3: Context Assembly + Unified Identity (implemented, D-005 rework)

> **D-005: Identity is the weights.** No L0/L1 layers — identity emerges from high-weight memories in the unified table. `layers.py` deleted. See DJ-001 below and `KB/blueprints/v0.2_unified_memory_rework.md`.

**Brain modules:**
- `brain/src/context_assembly.py` — dynamic context injection + identity rendering:
  - `assemble_context(memory_store, agent_id, ...)` — Track 0 (immutable safety) + active identity (D-015: w×s scored) + Track 1 (situational via `search_hybrid(mutate=False)`). Returns `injected_memory_ids` — IDs of non-immutable memories that survived budget trimming and were actually injected.
  - **Active identity (D-015/DJ-005):** Embeds `query_text` with `RETRIEVAL_QUERY` task type, calls `memory_store.score_identity_wxs(query_vec, agent_id)` which computes `injection_score = weight_center × cosine_sim` in SQL via pgvector `<=>`. Memories ranked by injection_score, injected within `BUDGET_IDENTITY_MAX` token budget. No query = no identity injection (relevance is mandatory per DJ-005). Replaces stochastic Beta-sampling. D-018c: chunked memories annotated with `[part N of M]` prefix via `_annotate_chunk()` from metadata `group_part`/`group_total`.
  - **Identity hash (D-017/DJ-006):** Feature-flagged dormant via `IDENTITY_HASH_ENABLED = False`. `render_identity_hash()` skipped during assembly but still callable via API endpoints.
  - **Retrieval mutation** (D-012): after assembly, `api.py` calls `apply_retrieval_mutation(injected_ids)` — increments `access_count` + boosts `depth_weight_alpha` for memories that influenced the agent's output. access_count always increments (factual counter); alpha boost goes through safety check (D-013).
  - Token budgets: identity max 3000, situational 2000, output buffer 4000
  - `render_system_prompt()` → `[SAFETY BOUNDARIES]`, `[IDENTITY]` (hash, dormant), `[ACTIVE IDENTITY]` (w×s memories), `[RELEVANT MEMORIES]`, `[COGNITIVE STATE]`
  - `render_identity_hash(memory_store, agent_id)` — compact ~100-200 tokens from top-10 memories by weight center (used by API only when hash dormant)
  - `render_identity_full(memory_store, agent_id)` — full ~1-2k tokens grouped by memory type from top-30 memories (replaces LayerStore.render_identity_full)
  - **Injection logging (D-018d):** After w×s scoring, every candidate is logged to `injection_logs` table with weight_center, cosine_sim, injection_score, was_injected (budget-accepted or not), query_hash (SHA-256[:16] of query_text). Non-blocking (try/except wrapped). Batch INSERT via `pool.executemany()`.
  - **Adaptive context shift threshold (D-018a, T-P15):** Ring buffer of last 200 context_shift values in `context_shift_buffer` table. Threshold = P75 percentile. Bootstrap default 0.5 when < 200 values. Identity candidates cached per agent (`_identity_cache`), recomputed only when `context_shift >= threshold`. Shift values recorded non-blocking.
  - Context inertia: shift = 1-cosine(current, previous attention). Inertia 5% if shift > adaptive threshold (was hardcoded 0.7), else 30%.
- `brain/src/memory.py` — `score_identity_wxs(query_vec, agent_id, top_n)` — SQL-native w×s identity scoring. Computes `(alpha/(alpha+beta)) * (1 - (embedding <=> query::halfvec))` in DB. Excludes immutables. Returns candidates ranked by injection_score DESC. Includes `metadata` column for chunk annotation.

**Gate wiring — `_get_identity_embeddings()` in `api.py`:**
- Queries top-N memories by weight center (`depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.3`) from DB
- Uses `embedding::float4[]` SQL cast for asyncpg deserialization → `np.array(..., dtype=np.float32)`
- Returns `list[(content, center, ndarray)]` → passed to `ExitGate.evaluate(layer_embeddings=...)`
- Empty DB (new agents): returns `None` → `has_context=False` → still defaults to "peripheral" as before
- No LayerStore cache needed — query is lightweight (indexed, top-20 only)

**API endpoints** (in `brain/src/api.py`):
```
POST /context/assemble
  Request:  { agent_id, query_text="", conversation=[], total_budget=131072 }
  Response: { system_prompt, used_tokens, conversation_budget, identity_token_count, context_shift, inertia }

GET /identity/{agent_id}
  Response: { agent_id, identity: "<full render from top memories>" }

GET /identity/{agent_id}/hash
  Response: { agent_id, hash: "<compact render from top memories>" }

GET /injection/metrics?agent_id=X&days=7
  Response: { agent_id, days, total_logs, injection_rate, score_stats: { avg, p50, p75, p95 }, top_memories: [{ memory_id, count, avg_score }] }
```
Note: No PUT /identity endpoint — identity changes via memory reinforce/contradict, not direct edit.

**Plugin update** (`openclaw/extensions/memory-brain/index.ts`):
- `before_agent_start` hook: calls `POST /context/assemble` (with user prompt as `query_text`), falls back to raw `/memory/retrieve` if assembly fails
- Tool: `introspect` (view identity, full or hash mode) — calls GET /identity
- HTTP clients: `brainAssembleContext()`, `brainGetIdentity()`, `brainGetIdentityHash()`
- Removed: `identity_update` tool, `brainUpdateIdentity()` (D-005: identity changes via weights, not direct edit)

**Agent core metadata** (name, persona, voice):
- Handled via immutable seed memories at agent creation: `memory_type="identity"`, `immutable=true`, high initial weight `Beta(8, 2)` (center ~0.8)
- These always surface in identity render (immutable + high weight), can never be forgotten

## Phase 4: Gut Feeling (implemented)

**Brain module** (`brain/src/gut.py`):
- `GutFeeling` — two-centroid emotional model, D-005 adapted:
  - **Subconscious centroid**: weighted mean of top-N identity memory embeddings from DB, weighted by Beta weight center (alpha/(alpha+beta)). Reuses `_get_identity_embeddings()` data. Replaces original L0/L1/L2 layer weights.
  - **Attention centroid**: EMA of recently observed message embeddings. Decay = `exp(-0.693/10) ≈ 0.933` (halflife 10 embeddings). Updated during `/context/assemble` from `query_text`.
  - **GutDelta**: delta = attention - subconscious, magnitude (L2 norm), direction (unit vector)
  - `emotional_charge` = `min(1.0, magnitude / 2.0)` — 0 calm, 1 intense divergence
  - `emotional_alignment` = `max(0.0, 1.0 - magnitude / 2.0)` — 1 aligned, 0 divergent
  - `gut_summary()` — one-line intensity + direction for context injection
  - Delta log: last 50 entries (metadata only, not full vectors)
- `GutDelta` dataclass: delta vector, magnitude, direction, context, timestamp, outcome_id
- State persistence: `/app/state/{agent_id}/gut_state.json` (centroids + delta log)

**Per-agent management** (`brain/src/api.py`):
- `_gut_feelings: dict[str, GutFeeling]` — in-memory cache per agent_id
- `_get_gut(agent_id)` — loads from disk on first access, returns cached after
- During `/context/assemble`: embeds query_text (SEMANTIC_SIMILARITY) → `update_attention()` → `update_subconscious(identity_embs)` → `compute_delta()` → passes gut summary as `cognitive_state_report` + attention centroids for context inertia
- During `/memory/gate`: reads `gut.attention_centroid` + `gut.emotional_charge` → passed to `ExitGate.evaluate()`

**API endpoints**:
```
POST /context/attention
  Request:  { agent_id, content }
  Response: { agent_id, emotional_charge, emotional_alignment, gut_summary, attention_count }

GET /gut/{agent_id}
  Response: { agent_id, emotional_charge, emotional_alignment, gut_summary, attention_count, has_subconscious, has_attention, recent_deltas }
```

**Context integration**:
- Gut summary rendered in `[COGNITIVE STATE]` section of system prompt
- Context inertia: `shift = 1 - cosine(current_attention, previous_attention)`. Inertia 5% if shift > 0.7 (topic change), else 30% (steady).

**Plugin update** (`openclaw/extensions/memory-brain/index.ts`):
- Tool: `gut_check` — calls `GET /gut/{agent_id}`, returns gut summary + emotional details
- HTTP clients: `brainGetGutState()`, `brainUpdateAttention()`
- No new hooks needed — attention updates happen brain-internally during `/context/assemble`

**Cross-module dependency graph** (Phase 1+2+3+4):
```
config.py            <- standalone
stochastic.py        <- standalone
activation.py        <- numpy
relevance.py         <- activation
memory.py            <- config, relevance, db (pool)
gate.py              <- activation, memory
gut.py               <- activation (cosine_similarity), numpy, json, pathlib
context_assembly.py  <- activation (cosine_similarity)
api.py               <- memory, gate, context_assembly, gut, db, numpy
```

## Phase 5: Consolidation Engine (implemented)

**Brain modules:**
- `brain/src/llm.py` -- Google Gemini client wrapper (D-006):
  - `retry_llm_call(prompt, max_tokens, temperature, model, retry_config)` -- async with exponential backoff retry
  - `llm_call_with_search(prompt, temperature=1.0)` -- Gemini with GoogleSearch grounding tool (D-016). Returns (text, sources, grounding_chunk_count). Temperature 1.0 recommended for Gemini 3 with tools.
  - `retry_llm_call_with_search(prompt, ...)` -- retry wrapper for search-grounded calls
  - Model: `gemini-3-flash-preview` for all consolidation/DMN prompts
  - Uses `google-genai` SDK, reads `GOOGLE_API_KEY` from env

- `brain/src/consolidation.py` -- background memory processing:
  - **Tier 1 (ConstantConsolidation)** -- 30s loop, 3 scheduled operations:
    - `_decay_tick()` every 1 hour (D-022, was 5min): beta += 0.01 for stale memories (24h+ not accessed, non-immutable, center > 0.1). Decay pressure: 0.01 beta/hr (was 0.12).
    - `_contradiction_scan()` every 10min: fetch 10 recent memories, 2 random pairs, LLM contradiction check. D-023: novelty-checked at sim>=0.85 before storing, reinforces existing if duplicate. D-016/DJ-008: after each pair, calls `_maybe_queue_research()` to classify factual contradictions and queue for 2-search confirmation. At end, calls `_process_research_queue()`. Enqueues notifications (D-019) on contradiction detection.
    - **Research sessions (D-016/DJ-008):** `_maybe_queue_research()` → checks weight centers (skip if both < 0.3), skips user-sourced memories, rate-limits (1/hour, 24/day), classifies via `_classify_contradiction()` (LLM: factual/subjective + confidence + research_worthy). Factual + confidence>0.7 → INSERT into `research_queue`. `_process_research_queue()` → 1st search via `retry_llm_call_with_search()` (Google Search grounding), structural confidence from grounding_chunk_count (0=UNRESOLVED, 1=LOW, 2+=MEDIUM), stores `type=research_finding` memory. 24h later → 2nd search with rephrased prompt. Displacement only if both searches agree + MEDIUM confidence → beta += 5.0 on loser via safety. Enqueues notifications on MEDIUM findings and confirmed displacements.
    - `_pattern_detection()` every 24h (D-021, was 15min): HDBSCAN clustering on pre-filtered memories (weight>0.25, access>=2, insight_level<2). Per-cluster LLM analysis generates typed insights (insight_level=1). Cross-cluster meta-insight (insight_level=2) from 2+ cluster insights. Meta-insights excluded from future clustering. Dedup via check_novelty before each storage.
  - **Tier 2 (DeepConsolidation)** -- hourly or triggered:
    - `_merge_and_insight()`: LLM generates questions from recent memories, extracts insights via search_similar + LLM. D-023: novelty-checked, if sim>=0.85 reinforces existing via apply_retrieval_mutation instead of creating new. Clusters reflections into first-person narratives (also novelty-checked).
    - `_promote_patterns()`: D-005 simplified. Goal promotion (5+ access, 14d+, center<0.65, alpha+=2.0). Identity promotion (10+ access, 30d+, center 0.65-0.82, alpha+=5.0). **CQ-005 fix:** all alpha boosts routed through `safety.check_weight_change()` — HardCeiling (0.95) and DiminishingReturns enforced. Blocked promotions counted and logged.
    - `_decay_and_reconsolidate()`: stale memories (90d, <3 access) get beta+=1.0. Revalidates existing insights via LLM. D-023: novelty-checked before storing updated insight. **CQ-006 fix:** deep decay and insight weakening (beta+=1.0) routed through `safety.check_weight_change()`. Stale decay: per-row safety check, then batch UPDATE on allowed IDs. Insight weakening: per-row safety check before UPDATE.
    - `_tune_parameters()`: Shannon entropy over 20 bins of weight centers, log only.
    - `_contextual_retrieval()`: generates WHO/WHEN/WHY preambles via LLM, updates content_contextualized column, re-embeds with contextualized content.
  - **ConsolidationEngine** -- runs both tiers via asyncio.gather(), supports trigger + status
  - **Dedup threshold**: MERGE_SIMILARITY_THRESHOLD = 0.85, used across all 4 creation paths

- Error isolation: every operation wrapped in individual try/except, one failing agent/operation does not crash the engine
- Multi-agent: iterates SELECT DISTINCT agent_id FROM memories each cycle
- All operations log to consolidation_log table. CQ-014 fix: `_log_consolidation()` passes dict directly to asyncpg (not `json.dumps()`). asyncpg's JSONB codec serializes internally; pre-serializing caused double-serialization (details stored as JSON string literals, not objects).
- Insight linking via memory_supersedes table. D-025: store_insight inherits weighted-avg alpha/beta from sources, no source importance demotion.

BUG-001 (Session 18): gemini-3-flash-preview is a thinking model. Internal chain-of-thought consumes from max_output_tokens budget. With default max_tokens=200, model spent approx 180 on thinking, left approx 20 for output. All consolidation insights were sentence fragments. Fix: remove max_output_tokens cap.

**API endpoints** (in `brain/src/api.py`, version 0.4.0):
```
GET  /consolidation/status
  Response: { running, constant: { last_decay_tick, last_contradiction_scan, last_pattern_detect }, deep: { last_deep_cycle, pending_triggers, deep_running } }

POST /consolidation/trigger
  Request:  { agent_id }
  Response: { agent_id, triggered, message }
```

**Plugin update** (`openclaw/extensions/memory-brain/index.ts`):
- Tools: `consolidation_status` (view engine state), `consolidation_trigger` (trigger deep cycle)
- HTTP clients: `brainConsolidationStatus()`, `brainTriggerConsolidation()`

**Environment**: `GOOGLE_API_KEY` must be set in docker-compose env for brain service (D-006).

**Cross-module dependency graph** (Phase 1+2+3+4+5):
```
config.py            <- standalone
stochastic.py        <- standalone
activation.py        <- numpy
relevance.py         <- activation
memory.py            <- config, relevance, db (pool)
gate.py              <- activation, memory
gut.py               <- activation (cosine_similarity), numpy, json, pathlib
context_assembly.py  <- activation (cosine_similarity)
llm.py               <- config (RetryConfig), google-genai
consolidation.py     <- llm, memory, activation (cosine_similarity), numpy, asyncpg (direct pool for logging)
api.py               <- memory, gate, context_assembly, gut, consolidation, db, numpy
```

## Phase 6: DMN / Idle Loop (implemented)

**Brain modules:**
- `brain/src/rumination.py` — persistent thought threads:
  - `RuminationThread` dataclass: topic, seed_memory_id, seed_content, history (max 10 entries), cycle_count, last_gut_magnitude, resolved, resolution_reason
  - `should_random_pop()`: probability = `0.10 + (cycle_count * 0.02)`, capped at 0.5
  - `render_for_prompt()`: formats thread for LLM continuation (last 5 history + "THREAD_RESOLVED" instruction)
  - `RuminationManager`: manages active thread + completed archive (last 20). Methods: `start_thread()`, `end_thread(reason)`, `continue_thread(summary, gut_magnitude)` (checks terminal: max 50 cycles, gut < 0.1 after 3+), `save()`/`load()`.
  - Persistence: `/app/state/{agent_id}/rumination_state.json` (same pattern as gut.py)

- `brain/src/dmn_store.py` — ephemeral thought queue:
  - `AttentionCandidate` dataclass: thought, channel (`DMN/goal`, `DMN/creative`, `DMN/identity`, `DMN/reflect`), urgency (always 0.2), memory_id, timestamp
  - `ThoughtQueue`: `defaultdict(asyncio.Queue)` per agent_id. Methods: `put_thought()`, `get_thoughts()` (non-blocking drain), `queue_size()`, `all_queue_sizes()`
  - In-memory queue is ephemeral, but thoughts are also persisted to `dmn_log` table for observability (Session 19).

- `brain/src/idle.py` — DMN idle loop:
  - `IdleLoop`: main background loop (same pattern as consolidation.py — `asyncio.wait_for(shutdown_event.wait(), timeout=...)`)
  - Constructor: `IdleLoop(pool, memory_store, thought_queue, gut_getter, notification_store=None)`. `gut_getter` is `_get_gut` callable injected from api.py to avoid circular imports. D-019: `notification_store` passed for DMN/goal and DMN/identity channel notifications (urgency=0.1, importance=0.6).
  - Per-agent heartbeat intervals based on idle duration: <10min → 60s, 10-60min → 300s, 1-4h → 900s, 4h+ → 1800s. Loop sleeps at 30s, skips agents whose interval hasn't elapsed.
  - **4 Sampling Channels** (roll [0-1)):
    - Neglected (35%): high weight center > 0.5, last_accessed > 7 days ago
    - Tension (20%): high-weight seed + moderately similar partner (sim 0.3-0.7, different type)
    - Temporal (20%): old memories (created > 30 days ago)
    - Introspective (25%): type IN (reflection, narrative, preference, tension), center > 0.6
  - **Output channel classification** (`_classify_channel`):
    1. Goal: keyword overlap ≥ 3 with top-5 high-weight narrative/reflection memories → `DMN/goal`
    2. Creative: `spread_activation(hops=2)` via co-access network, requires `max(scores) >= 0.15` (MIN_CREATIVE_ACTIVATION) → `DMN/creative`. At hop 0, 0.15 needs ≥3 co-accesses. CQ-025 fix: was `len(activated) > 0` which always triggered with any co-access.
    3. Identity: reflective memory types → `DMN/identity`
    4. Default: `DMN/reflect`
  - **Thread lifecycle**: `_start_new_thread()` calls LLM (temperature=0.6), `_continue_thread()` calls LLM (temperature=0.5, checks for "THREAD_RESOLVED")
  - **Repetition filter**: `_is_repetitive()` checks thought[:50] against last 5 topics per agent
  - **Thought persistence** (Session 19): after queueing, `_queue_thought()` INSERTs into `dmn_log` table (non-blocking, try/except wrapped)
  - D-005 adaptation: goals from DB query (high-weight narrative/reflection), not LayerStore

**DB table** — `dmn_log` (Session 19):
```sql
dmn_log (id SERIAL PK, agent_id TEXT, thought TEXT, channel TEXT, source_memory_id TEXT, created_at TIMESTAMPTZ)
idx_dmn_log_agent ON dmn_log (agent_id, created_at DESC)
```

**DB table** — `injection_logs` (D-018d, T-P13):
```sql
injection_logs (id SERIAL PK, agent_id TEXT, memory_id TEXT, weight_center FLOAT, cosine_sim FLOAT, injection_score FLOAT, was_injected BOOLEAN, query_hash TEXT, created_at TIMESTAMPTZ)
idx_injection_logs_agent ON injection_logs (agent_id, created_at DESC)
idx_injection_logs_memory ON injection_logs (memory_id)
idx_injection_logs_query ON injection_logs (query_hash)
```

**API endpoints** (in `brain/src/api.py`, version 0.6.0):
```
GET  /dmn/thoughts?agent_id=X
  Response (DMNThoughtResponse): { agent_id, thoughts: [{ thought, channel, urgency, memory_id, timestamp }], count }

GET  /dmn/status
  Response (DMNStatusResponse): { running, heartbeat_counts, queue_sizes, active_threads }

POST /dmn/activity
  Request  (DMNActivityRequest):  { agent_id }
  Response (DMNActivityResponse): { agent_id, acknowledged, idle_seconds }

GET  /monologue/{agent_id}?limit=50
  Response (MonologueResponse): { agent_id, entries: [{ ts, type, content, channel?, operation?, source_memory_id?, memory_id?, details? }], rumination: { active?, recent_completed[] } }
  Unified view combining: dmn_log (thoughts), consolidation_log (operations), memories (tension/narrative/reflection), rumination state (active thread + recent completed). CQ-023 fix: completed thread reason read from `"reason"` key (matching rumination.py's archive format).
```

**Plugin update** (`openclaw/extensions/memory-brain/index.ts`):
- Tool: `dmn_status` — calls `GET /dmn/status`, shows running state + heartbeats + queues + threads
- Tool: `monologue` — calls `GET /monologue/{agent_id}`, unified inner monologue view with optional limit param
- HTTP clients: `brainGetDMNThoughts()`, `brainNotifyActivity()`, `brainGetDMNStatus()`, `brainGetMonologue()`
- Hook update: `before_agent_start` calls `brainNotifyActivity()` (fire-and-forget) to reset DMN idle timer on user input

## Phase 7: Safety Monitor (implemented)

- `brain/src/safety.py` — Standalone module (no brain module imports), math + logging only:
  - **Phase A (always on):** HardCeiling (MAX_CENTER=0.95, MAX_GOAL_BUDGET_FRACTION=0.40) + DiminishingReturns (gain / log2(evidence))
  - **Phase B (consolidation-time, disabled by default):** RateLimiter (MAX_CHANGE_PER_CYCLE=0.10 per memory) + TwoGateGuardrail (evidence quality gate + 50 changes/cycle cap)
  - **Phase C (mature agent, disabled by default):** EntropyMonitor (ENTROPY_FLOOR=2.0 bits, 20-bin histogram) + CircuitBreaker (MAX_CONSECUTIVE=5 same-evidence reinforcements)
  - **SafetyMonitor** coordinator: `check_weight_change()` -> (allowed, adj_delta_alpha, adj_delta_beta, reasons). Synchronous (no async). `enable_phase_b()`, `enable_phase_c()`, `end_consolidation_cycle(cycle_id)`
  - Module-level `_audit_log` (deque, maxlen=1000), `log_safety_event()`, `get_audit_log()`
- Wired in `api.py` lifespan: `SafetyMonitor()` -> `_memory_store.safety`
- Wired in `consolidation.py`: `_deep_cycle()` calls `safety.enable_phase_b()` at start, `safety.end_consolidation_cycle(cycle_id)` at end (in finally block). `_current_cycle_id` instance attribute on DeepConsolidation plumbs cycle_id to step functions (`_promote_patterns`, `_decay_and_reconsolidate`) for Phase B rate limiting.
- `memory.py:422-431`: existing call site uses `self.safety.check_weight_change()` for retrieval mutation -- now active

**API** (2 new endpoints, version 0.6.0):
- `GET /safety/status` -> SafetyStatusResponse: { phase_a, phase_b, phase_c, audit_log_size }
- `GET /safety/audit?limit=50` -> SafetyAuditResponse: { events: [...], count }

**Cross-module dependency graph** (Phase 1+2+3+4+5+6+7):
```
config.py            <- standalone
stochastic.py        <- standalone
activation.py        <- numpy
relevance.py         <- activation
safety.py            <- standalone (collections, math, logging only)
db.py                <- asyncpg (pool + get_agent_ids)
memory.py            <- config, relevance, db (pool); safety wired at runtime
gate.py              <- activation, config (NOVELTY_THRESHOLD)
bootstrap.py         <- config (WEIGHT_CENTER_SQL), asyncpg
gut.py               <- numpy, json, pathlib
context_assembly.py  <- activation (cosine_similarity), config (WEIGHT_CENTER_SQL)
llm.py               <- config (RetryConfig), google-genai
consolidation.py     <- llm, memory, activation, config (NOVELTY_THRESHOLD, WEIGHT_CENTER_SQL), db (get_agent_ids), numpy, asyncpg
rumination.py        <- json, pathlib (standalone)
dmn_store.py         <- asyncio (standalone)
idle.py              <- rumination, dmn_store, llm, memory, config (WEIGHT_CENTER_SQL), db (get_agent_ids), relevance, asyncpg
api.py               <- memory, gate, context_assembly, gut, consolidation, idle, dmn_store, safety, config (WEIGHT_CENTER_SQL), db, numpy
```

## Phase 8: Bootstrap Readiness (implemented)

- `brain/src/bootstrap.py` — Stateless milestone checker (no background task, no persistence):
  - `BOOTSTRAP_PROMPT` — injected via context assembly for agents that haven't achieved all milestones
  - `Milestone` dataclass: name, description, achieved (bool), achieved_at
  - `BootstrapReadiness(pool)` — takes asyncpg pool, no other dependencies
  - `check_all(agent_id)` — runs 10 DB-direct checks, returns dict with milestones, achieved count, ready flag, bootstrap_prompt, status_text
  - `render_status()` — "Bootstrap Readiness: X/10" with checkmarks
  - 10 milestones (all `pool.fetchval()` COUNT queries):
    1. First Memory — `memories WHERE agent_id` count > 0
    2. First Retrieval — `access_count > 0`
    3. First Consolidation — `consolidation_log WHERE agent_id` count > 0
    4. Goal-Weight Promotion — non-immutable memory with center > 0.6
    5. First DMN Self-Prompt — `dmn_log WHERE agent_id` count > 0 (CQ-019 fix: was checking memories for source_tag='internal_dmn' but DMN writes to dmn_log)
    6. Identity-Weight Promotion — non-immutable memory with center > 0.8
    7. Conflict Detection — `memories WHERE type='tension'` count > 0 (CQ-019 fix: was checking metadata->>'resolved'='true' which nothing sets)
    8. Creative Association — `memories WHERE type='narrative'` count > 0 (CQ-019 fix: was checking metadata ? 'creative_insight' which nothing sets)
    9. Goal Reflected — reflection type with content matching goal/achieved
    10. Autonomous Decision — 3+ identity-weight (center>0.8) AND 2+ reflections

**API** (1 new endpoint, version 0.7.0):
- `GET /bootstrap/status?agent_id=X` -> BootstrapStatusResponse: { agent_id, milestones, achieved, total, ready, bootstrap_prompt, status_text }

**Cross-module dependency graph** (Phase 1-8 + D-019):
```
config.py            <- standalone
stochastic.py        <- standalone
activation.py        <- numpy
relevance.py         <- activation
safety.py            <- standalone (collections, math, logging only)
memory.py            <- config, relevance, db (pool); safety wired at runtime
gate.py              <- activation, memory
gut.py               <- activation (cosine_similarity), numpy, json, pathlib
context_assembly.py  <- activation (cosine_similarity)
llm.py               <- config (RetryConfig), google-genai
notification.py      <- config, asyncpg (standalone)
consolidation.py     <- llm, memory, activation, numpy, asyncpg, hdbscan; safety via memory.store.safety; notification optional
rumination.py        <- json, pathlib (standalone)
dmn_store.py         <- asyncio (standalone)
idle.py              <- rumination, dmn_store, llm, memory, relevance (spread_activation), asyncpg; notification optional
bootstrap.py         <- asyncpg (standalone, DB-direct queries only)
api.py               <- memory, gate, context_assembly, gut, consolidation, idle, dmn_store, safety, bootstrap, notification, db, numpy
```

## Proactive Notification System (D-019, T-P11, implemented)

**Brain module** (`brain/src/notification.py`):
- `NotificationStore(pool)` — outbox CRUD:
  - `enqueue(agent_id, content, urgency, importance, source, ...)` — auto-routes to channel based on preferences. High urgency + telegram_enabled → `channel='telegram'`, otherwise `channel='passive'`.
  - `get_pending_push(limit)` — pending telegram/webhook notifications (delivery worker polls this)
  - `get_pending_passive(agent_id, limit)` — pending passive notifications for context injection
  - `mark_delivered(id)`, `mark_failed(id, error)`, `expire_old()`
  - `get_preferences(agent_id)` / `set_preferences(agent_id, **kwargs)` — per-agent prefs upsert
- `DeliveryWorker(pool, store)` — background loop (same pattern as IdleLoop):
  - Polls pending push notifications every 30s
  - Delivers via Telegram Bot API (urllib.request, no extra dependency)
  - Respects quiet hours (handles midnight wrapping)
  - Expires old notifications past 24h TTL
  - Marks delivered/failed on each attempt

**DB tables** (`schema.sql`):
```sql
notification_outbox (id SERIAL PK, agent_id, content, urgency, importance, source, source_memory_id, channel, status, metadata JSONB, created_at, delivered_at, expires_at)
notification_preferences (agent_id TEXT PK, telegram_chat_id, telegram_enabled, quiet_hours_start/end, urgency_threshold, importance_threshold, enabled, updated_at)
```

**Wiring:**
- `api.py` lifespan: `NotificationStore(pool)` initialized, passed to `ConsolidationEngine` and `IdleLoop`. `DeliveryWorker` started as background task.
- `consolidation.py`: `ConstantConsolidation` and `DeepConsolidation` accept `notification_store`. Enqueues notifications on: contradiction detection (urgency=0.3), MEDIUM research findings (urgency=0.5), confirmed research displacements (urgency=0.5).
- `idle.py`: `IdleLoop` accepts `notification_store`. Enqueues notifications for DMN/goal and DMN/identity channels (urgency=0.1, importance=0.6).
- `/context/assemble`: fetches up to 3 pending passive notifications, appends to cognitive_state_report under `[Pending Notifications]`, marks delivered.

**API endpoints** (version 0.8.0):
```
GET  /notifications/pending?agent_id=X
  Response: { agent_id, notifications: [...], count }

POST /notifications/preferences
  Request:  { agent_id, telegram_chat_id?, telegram_enabled?, quiet_hours_start?, quiet_hours_end?, urgency_threshold?, importance_threshold?, enabled? }
  Response: { agent_id, preferences: {...} }

GET  /notifications/preferences/{agent_id}
  Response: { agent_id, preferences: {...} }

GET  /notifications/status
  Response: { delivery_worker_running, store_initialized }
```

**Key design:** Urgency (time-sensitive) != importance (content significance) != weight. Channel routing by urgency threshold + telegram availability. Passive notifications injected into next /context/assemble call.

## OpenClaw Agent Runtime (implemented)

**Docker service** (`docker-compose.yml`):
- `openclaw` container: builds from `./openclaw`, depends on brain (service_healthy), port 18789
- `GEMINI_API_KEY=${GOOGLE_API_KEY}` — same API key for agent conversations and brain
- `OPENCLAW_STATE_DIR=/data`, volume-mounted from `./openclaw-config`
- Gateway command: `node openclaw.mjs gateway --allow-unconfigured --bind lan --port 18789`
- Auth: `OPENCLAW_GATEWAY_TOKEN` (default: `botbot-dev`)

**Configuration** (`openclaw-config/openclaw.json`):
- Plugin: `plugins.slots.memory: "memory-brain"` + `plugins.entries.memory-brain.config` (brainUrl, agentId=default, autoRecall, autoCapture, recallLimit=7, captureMaxChars=1000)
- Model: `agents.defaults.model.primary: "google/gemini-3-flash-preview"` with full Google provider definition
- Plugin discovery: memory-brain auto-discovered as "bundled" via `resolveBundledPluginsDir()` (walks up from `dist/` to find `extensions/`). Explicit `plugins.slots.memory` enables it.

**Agent identity** (D-008):
- No preset name or persona. agentId=`default`
- Agent starts as newborn, gets BOOTSTRAP_PROMPT until all 10 milestones achieved
- Identity emerges from memory reinforcement (high-weight memories surface as identity)

**AGENTS.md** (`openclaw-workspace/AGENTS.md`, synced to `openclaw-config/AGENTS.md`):
- Memory: brain service only, no file-based memory (SOUL.md/MEMORY.md ignored)
- Anti-drift, intellectual honesty, verification table
- D-020 web search rules: brain memory trusted, LLM training data NOT, max 3 searches/turn
- Reply tag suppression: never echo `[[reply_to_current]]` or `[[ ... ]]` directives
- Group chat etiquette, heartbeat guidance, platform formatting rules

**Key decisions:**
- D-007: Agent LLM = Gemini 3 Flash Preview, reusing brain's GOOGLE_API_KEY as GEMINI_API_KEY
- D-008: No preset identity — newborn agent discovers itself through bootstrap milestones
- D-020: Web search rules in AGENTS.md — brain memory > web search > training knowledge

**Full stack**:
```
postgres (:5433)  →  brain (:8400)  →  openclaw (:18789)
  pgvector             FastAPI            Node.js gateway
  memories table       20 endpoints       memory-brain plugin
                       Gemini embed       Gemini 3 Flash (conversations)
                       Gemini Flash       WebChat UI
                       (consolidation)    9 tools, 2 hooks
```

## Session 17 Brainstorm: Identity Architecture Redesign

> Brainstorm session. No code changes. All decisions accepted in principle, need implementation plans.

**Core principle: injection = reinforcement = survival.** Anything injected into the system prompt gets retrieval mutation (access_count + alpha boost). Therefore the injection criteria IS the survival criteria. This is the foundation of all Session 17 decisions.

### Identity Injection (D-015 revised, D-017 revised)

**Formula:** `injection_score = weight_center * cosine_sim` — no floor, no core tier.

**System prompt structure (revised):**
```
[SAFETY BOUNDARIES]      ← immutables only (always injected, deliberately immortal)
[ACTIVE IDENTITY]        ← w×s scored memories (topic-dependent, cached between subject changes)
[RELEVANT MEMORIES]      ← situational, w×s scored
[COGNITIVE STATE]         ← gut, DMN, pending notifications
```

**Identity hash:** dormant (`IDENTITY_HASH_ENABLED = False`). Exists in code, not called. Rationale: heavy identity injection = strong ego = rigid agent. Let w×s prove what matters.

**Active identity caching (D-018a):** Recompute only when context_shift exceeds adaptive threshold. Threshold = P75 of last 200 context_shift values (self-evolving). Bootstrap default 0.5. Ring buffer of shift values, percentile recomputed each turn.

**Value emergence:** Abstract values ("I value honesty") are never stored directly. Behavioral instances ("I told user their code had a flaw") are stored, compete on w×s, get reinforced when similar situations arise. Consolidation detects behavioral patterns → creates insight memories. Insights inherit weighted-average alpha/beta from source memories (heavy sources skew more). Values are dispositional, not declarative.

### Memory Chunking (D-018b, D-018c)

**Gate-time chunking:** Content > 300 tokens → semantic chunking into separate linked memories. Each chunk gets own embedding (computed from full chunk text). Full injection (no truncation) — token budget limits count, not content.

**Memory groups:** Chunks linked by `memory_group_id`. Group beta refresh: any chunk accessed → all group members get `touch_memory()`. Insights synthesized from single group share the group_id. Injected chunks show: `[part N of M — recall for full context]`. Consolidation creates synthesis insights across groups but never merges chunks.

### Consolidation Research (D-016 revised)

**Flow:** Contradiction detected → cheap LLM classification (factual/subjective, research yes/no, confidence) → if factual + confidence>0.7: spawn agent research session with web search → verdict with self-assessed confidence → HIGH confidence: displace loser + create correction memory, MEDIUM: log as "likely resolved" keep both, LOW: log "unresolved tension."

**Budget:** 1 session/hour, max 24/day. Priority: highest single weight of either contradicting memory. Skip contradictions where both memories < 0.3 center. 24h cooldown on research-spawned memories (tag: `source_tag='consolidation_research'`).

**Session isolation:** Research session uses separate context, doesn't pollute user conversation. Gate creates only `type=correction` memories from research output.

### Pattern Detection (D-021)

**Pipeline:** Pre-filter (weight>0.25, access>2) → HDBSCAN cluster on embeddings → per-cluster LLM pattern analysis → cross-cluster summary. Run 1/day.

**Recursion:** 1 level allowed. Raw memories → insights (level 1) → meta-insights (level 2). Level 2 excluded from future pattern detection. Tag: `insight_level=1|2`.

**Dedup:** Before creating insight, check cosine_sim against existing insights. sim>0.85 → reinforce existing instead of creating new.

**No cap** on insight count (observe what happens).

### Observability (D-018d)

**injection_logs table:** Every identity injection logged with query_text, all candidates (memory_id, weight, cosine_sim, score, injected, rank), budget usage, cache_hit. Behavioral responses correlated by turn_id.

**3-tier analysis:**
- Tier 1 (real-time): rolling metrics via in-memory ring buffer. GET /debug/injection-metrics.
- Tier 2 (daily): SQL aggregation + batch LLM classification of behavioral decisions. Score distributions, high-weight rejection analysis, memories-influenced-behavior rate.
- Tier 3 (weekly): anomaly detection. Drift from baseline → alerts routed to DMN thought queue.

### Proactive Notifications (D-019)

**Urgency ≠ importance ≠ weight.** A low-weight memory about a doctor appointment can be urgent + important.
- Urgency (time-sensitive): temporal markers, deadlines → push notify immediately
- Importance (content significance): health, safety, commitments → "btw" next conversation
- Weight (reinforcement history): how established the memory is → not a notification factor

**Delivery:** notification_outbox DB table → async delivery worker → configured channels (Telegram bot API first, WhatsApp/webhook later). User preferences: channels, quiet hours, importance threshold. T-P11 in roadmap.

### AGENTS.md Web Search (D-020)

Brain memory = trusted. LLM training data = NOT trusted. Agent must verify facts from training data via web search. Rate limit: 3 searches/turn. Specified in AGENTS.md.

## Decision Journal

> The Decision Journal tracks **why** decisions changed and what was learned.
> Not a duplicate of roadmap.json — roadmap records what was decided, the Decision Journal records the evolution and lessons.
>
> **Scan headers first.** Expand an entry only if current task touches that tag domain.
> Tags must exist in `charter.json` `project.tag_taxonomy`.

### DJ-001 [memory] Phase 3 L0/L1 layers discarded for unified memory

- **Was:** Identity stored in separate L0/L1 JSON files (`layers.py`), loaded by LayerStore, embedded separately, passed to gate and context assembly
- **Now:** Identity emerges from unified memory table Beta weights. Top-N high-weight memories serve as gate relevance signal and identity render source. No separate files.
- **Why:** L0/L1 is a derived cache of the memory table, not a source of truth. The original's own context assembly Track 2 already pulls identity from DB by depth_weight center. Consolidation writes back to L0/L1 FROM memory — remove the middleman. Simpler, one table, identity IS the weights.
- **Lesson:** When the "cache" has its own CRUD API and the source already provides the same data, the cache is adding complexity not value. See `KB/blueprints/v0.2_unified_memory_rework.md` for full rework plan.

### DJ-005 [identity] D-015 floor dropped — pure weight × cosine_sim

- **Was:** `injection_score = weight_center * (0.3 + 0.7 * cosine_sim)` — 0.3 floor guaranteed minimum score for irrelevant high-weight memories, decaying with maturity
- **Now:** `injection_score = weight_center * cosine_sim` — zero relevance = zero score regardless of weight
- **Why:** Any floor creates immortal memories by guaranteeing injection → reinforcement → alpha boost → can never accumulate enough beta to decay. The floor protects memories from the natural selection pressure that makes the system work. Even a 0.1 floor gives high-weight memories an unfair survival advantage regardless of relevance.
- **Lesson:** In a system where injection = reinforcement, the injection criteria IS the survival criteria. Making relevance mandatory for injection ensures irrelevant memories die naturally. Weight amplifies relevance but cannot substitute for it.

### DJ-006 [identity] D-017 core/active split dropped — identity hash dormant

- **Was:** Two tiers: core (always injected: immutable + earned via weight>0.7 + access>20) and active (vector-scored, competes on w×s)
- **Now:** No core tier. Identity hash feature-flagged dormant (`IDENTITY_HASH_ENABLED = False`). System prompt = immutables + w×s scored memories. Core values emerge from behavioral patterns via consolidation insights, not from protected tiers.
- **Why:** Core tier would always be injected → always reinforced → weight only goes up → can never decay. This makes "core" immortal by construction, not by merit. Also: strong ego (heavy identity injection) = rigid agent. Less identity = better performance and adaptability. Values aren't declarative ("I value honesty") — they're dispositional (patterns of honest behavior). Consolidation detects these patterns and creates insights that compete on w×s like everything else.
- **Lesson:** If you protect something from selection pressure, you remove the mechanism that makes it earn its place. Let the system prove what matters through use, not through designation.

### DJ-007 [consolidation] D-016 revised — agent research sessions instead of internal fact-check

- **Was:** Consolidation's contradiction handler does internal LLM fact-check from training knowledge
- **Now:** Contradiction spawns a full agent session with web search tools. Agent researches the contradiction using the same tools available in regular conversation.
- **Why:** LLM training data is not a reliable source for factual verification (may be outdated, hallucinated, or biased). Real fact-checking requires external sources. Reusing agent session infrastructure means no new pipeline — just a new "user message" that's the contradiction description. Budget: 1/hour, max 24/day. LLM judges worthiness (factual vs subjective, confidence>0.7) before spawning expensive session.
- **Lesson:** Don't build parallel infrastructure when you can reuse existing infrastructure with a different input.

### DJ-004 [gate] Retrieval mutation loop was disconnected — death spiral

- **Was:** `context_assembly.py` called `search_hybrid(mutate=False)`, `apply_retrieval_mutation()` bundled access_count + alpha boost behind safety. Result: access_count=0 for all memories, alpha stuck at 1.0, consolidation decay pushed beta up unchecked, weight centers dropped below 0.3 identity threshold, gate ran blind (s_i=0.35), couldn't persist new memories.
- **Now:** (D-012) Two mutation types: context injection does full mutation (access_count + alpha), gate touch refreshes last_accessed only. (D-013) access_count separated from alpha boost — safety can't block counting. `assemble_context()` returns `injected_memory_ids`, `api.py` calls `apply_retrieval_mutation()` on them. `check_novelty()` returns 3-tuple with memory ID for gate touch.
- **Why:** The system was designed for feedback (gate uses identity embeddings from high-weight memories, memories gain weight from retrieval) but the feedback path was never connected. Context assembly was intentionally read-only (`mutate=False`) but this broke the loop.
- **Lesson:** Feedback loops require explicit wiring. "Read-only by default" is safe but can silently break downstream systems that depend on mutation side-effects. The decay/reinforcement balance must be tested end-to-end, not just each component in isolation.

### DJ-008 [consolidation] D-016 amended — 2-search confirmation replaces single-search auto-displace

- **Was:** Single grounded search → HIGH confidence → auto-displace loser (beta += 5.0)
- **Now:** 1st search creates research_finding → 24h later 2nd search with rephrased prompt → displace only if both agree with MEDIUM+ structural confidence
- **Why:** Gemini 3 has no API confidence scores (must self-assess, unreliable). beta += 5.0 is irreversible (memory can never recover). Context-dependent facts flattened by web search. Wrong verdict permanently kills correct memory. Structural confidence from grounding_chunk_count (0=UNRESOLVED, 1=LOW, 2+=MEDIUM) is safer than LLM self-assessment. Also: skip research for `source_tag='external_user'` (trust user over Google).
- **Lesson:** In irreversible systems, require independent confirmation before destructive actions. Single-source verdicts are insufficient when the source has no calibrated confidence.

<!--
RULES:
- One entry per superseded/amended decision. Not for new decisions (those go in roadmap.json).
- Tag in brackets = greppable domain (must be in charter.json tag_taxonomy).
- Keep entries to 4 lines max. Link to evidence, don't paste it.
- DJ numbering is sequential, never reused.
- When adding a DJ entry, also update the decision's status to "superseded" in roadmap.json.
- Add a devlog entry with event: "dj_entry".
-->
