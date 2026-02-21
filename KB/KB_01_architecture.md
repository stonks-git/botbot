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

**Key files**: `brain/src/schema.sql` (DB schema), `brain/src/api.py` (HTTP API), `docker-compose.yml` (orchestration)

See `KB/blueprints/v0.3_current_state.md` for the current blueprint (done phases + remaining plan).

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
- Novelty axis: `check_novelty()` + `detect_contradiction_negation()` → confirming (sim≥0.85) / novel (sim<0.6) / contradicting (sim≥0.7 + negation markers)
- Score: `base_score * (0.5 + 0.5 * s_i) + emotional_charge_bonus`
- Noise floor: 2% chance DROP → BUFFER
- Decision constants: PERSIST_HIGH, PERSIST_FLAG, PERSIST, REINFORCE, BUFFER, SKIP, DROP

**API endpoint** (`POST /memory/gate`):
```
POST /memory/gate
  Request:  { agent_id, content, source?, source_tag="external_user" }
  Response: { decision, score, memory_id?, scratch_id?, entry_gate: {...}, exit_gate: {...} }
```
Pipeline: entry gate → scratch buffer → exit gate → act on decision:
- PERSIST/PERSIST_HIGH/PERSIST_FLAG → `store_memory()` with importance derived from gate score
- REINFORCE → find most similar memory + `apply_retrieval_mutation()`
- BUFFER → leave in scratch (24h TTL, consolidation picks up later)
- DROP/SKIP → clean up scratch

**Plugin update** (`openclaw/extensions/memory-brain/index.ts`):
- `agent_end` hook now calls `POST /memory/gate` instead of direct `POST /memory/store`
- Added `brainGate()` HTTP client function
- Gate decisions logged at debug level, gate counts at info level
- `shouldCapture()` still does client-side pre-filtering (length, prefix) before sending to gate

**Gate wiring** (all parameters now active):

| Param | Source | Phase | Status |
|-------|--------|-------|--------|
| `layer_embeddings` | `_get_identity_embeddings()` — top-N DB memories by weight center | P3 | DONE |
| `attention_embedding` | `GutFeeling.attention_centroid` | P4 | DONE |
| `emotional_charge` | `GutFeeling.emotional_charge` | P4 | DONE |

**Fallback when no memories (new agent):** `subconscious_centroid=None` → `emotional_charge=0.0` → gate uses no emotional bonus. Identity embeddings return None → relevance defaults to "peripheral".

**Cross-module dependency graph** (Phase 1+2):
```
config.py       ← standalone
stochastic.py   ← standalone
activation.py   ← numpy
relevance.py    ← activation
memory.py       ← config, relevance, db (pool)
gate.py         ← activation, memory (embed, check_novelty, search_similar)
api.py          ← memory, gate, db
```

## Phase 3: Context Assembly + Unified Identity (implemented, D-005 rework)

> **D-005: Identity is the weights.** No L0/L1 layers — identity emerges from high-weight memories in the unified table. `layers.py` deleted. See DJ-001 below and `KB/blueprints/v0.2_unified_memory_rework.md`.

**Brain modules:**
- `brain/src/context_assembly.py` — dynamic context injection + identity rendering:
  - `assemble_context(memory_store, agent_id, ...)` — Track 0 (immutable safety) + identity hash + Track 2 (stochastic identity memories, Beta-sampled `observe() > 0.6`) + Track 1 (situational via `search_hybrid(mutate=False)`)
  - Token budgets: identity max 3000, situational 2000, output buffer 4000
  - `render_system_prompt()` → `[SAFETY BOUNDARIES]`, `[IDENTITY]`, `[IDENTITY -- active beliefs/values]`, `[RELEVANT MEMORIES]`, cognitive state
  - `render_identity_hash(memory_store, agent_id)` — compact ~100-200 tokens from top-10 memories by weight center (replaces LayerStore.render_identity_hash)
  - `render_identity_full(memory_store, agent_id)` — full ~1-2k tokens grouped by memory type from top-30 memories (replaces LayerStore.render_identity_full)
  - `adaptive_fifo_prune()` — intensity-adaptive context pruning (for future use)
  - Context inertia: shift = 1-cosine(current, previous attention), inertia 5% if shift>0.7, else 30% (Phase 4 wires attention)

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
context_assembly.py  <- stochastic, activation (cosine_similarity)
api.py               <- memory, gate, context_assembly, gut, db, numpy
```

## Phase 5: Consolidation Engine (implemented)

**Brain modules:**
- `brain/src/llm.py` -- Anthropic Claude client wrapper:
  - `retry_llm_call(prompt, max_tokens, temperature, model, retry_config)` -- async with exponential backoff retry
  - Model: `claude-haiku-4-5` for all consolidation prompts
  - Lazy singleton `AsyncAnthropic` client, reads `ANTHROPIC_API_KEY` from env

- `brain/src/consolidation.py` -- background memory processing:
  - **Tier 1 (ConstantConsolidation)** -- 30s loop, 3 scheduled operations:
    - `_decay_tick()` every 5min: `beta += 0.01` for stale memories (24h+ not accessed, non-immutable, center > 0.1)
    - `_contradiction_scan()` every 10min: fetch 10 recent memories, 2 random pairs, LLM contradiction check, store tensions (memory_type="tension", source="consolidation")
    - `_pattern_detection()` every 15min: fetch 50 recent memories (7d), greedy cosine cluster at 0.85, log clusters with 3+ members
  - **Tier 2 (DeepConsolidation)** -- hourly or triggered:
    - `_merge_and_insight()`: LLM generates questions from recent memories, extracts insights via search_similar + LLM, novelty-checked, stored via `store_insight()` (links `memory_supersedes`), clusters reflections into first-person narratives (memory_type="narrative")
    - `_promote_patterns()`: D-005 simplified -- direct SQL alpha updates. Goal promotion (5+ access, 14d+, center<0.65, alpha+=2.0). Identity promotion (10+ access, 30d+, center 0.65-0.82, alpha+=5.0). No L0/L1 writes.
    - `_decay_and_reconsolidate()`: stale memories (90d, <3 access) get beta+=1.0. Revalidates existing insights via `why_do_i_believe()` + LLM, stores updated insight if changed, weakens old.
    - `_tune_parameters()`: Shannon entropy over 20 bins of weight centers, log only (Phase 7 adds enforcement).
    - `_contextual_retrieval()`: generates WHO/WHEN/WHY preambles via LLM, updates `content_contextualized` column, re-embeds with contextualized content.
  - **ConsolidationEngine** -- runs both tiers via `asyncio.gather()`, supports trigger + status

- Error isolation: every operation wrapped in individual try/except, one failing agent/operation does not crash the engine
- Multi-agent: iterates `SELECT DISTINCT agent_id FROM memories` each cycle
- All operations log to `consolidation_log` table (9 log call sites)
- Insight linking via `memory_supersedes` table (2 `store_insight` calls)

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

**Environment**: `ANTHROPIC_API_KEY` must be set in docker-compose env for brain service.

**Cross-module dependency graph** (Phase 1+2+3+4+5):
```
config.py            <- standalone
stochastic.py        <- standalone
activation.py        <- numpy
relevance.py         <- activation
memory.py            <- config, relevance, db (pool)
gate.py              <- activation, memory
gut.py               <- activation (cosine_similarity), numpy, json, pathlib
context_assembly.py  <- stochastic, activation (cosine_similarity)
llm.py               <- config (RetryConfig), anthropic
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
  - In-memory only — no persistence. Thoughts are ephemeral and opportunistic.

- `brain/src/idle.py` — DMN idle loop:
  - `IdleLoop`: main background loop (same pattern as consolidation.py — `asyncio.wait_for(shutdown_event.wait(), timeout=...)`)
  - Constructor: `IdleLoop(pool, memory_store, thought_queue, gut_getter)`. `gut_getter` is `_get_gut` callable injected from api.py to avoid circular imports.
  - Per-agent heartbeat intervals based on idle duration: <10min → 60s, 10-60min → 300s, 1-4h → 900s, 4h+ → 1800s. Loop sleeps at 30s, skips agents whose interval hasn't elapsed.
  - **4 Sampling Channels** (roll [0-1)):
    - Neglected (35%): high weight center > 0.5, last_accessed > 7 days ago
    - Tension (20%): high-weight seed + moderately similar partner (sim 0.3-0.7, different type)
    - Temporal (20%): old memories (created > 30 days ago)
    - Introspective (25%): type IN (reflection, narrative, preference, tension), center > 0.6
  - **Output channel classification** (`_classify_channel`):
    1. Goal: keyword overlap ≥ 3 with top-5 high-weight narrative/reflection memories → `DMN/goal`
    2. Creative: `spread_activation(hops=2)` via co-access network → `DMN/creative`
    3. Identity: reflective memory types → `DMN/identity`
    4. Default: `DMN/reflect`
  - **Thread lifecycle**: `_start_new_thread()` calls LLM (temperature=0.6), `_continue_thread()` calls LLM (temperature=0.5, checks for "THREAD_RESOLVED")
  - **Repetition filter**: `_is_repetitive()` checks thought[:50] against last 5 topics per agent
  - D-005 adaptation: goals from DB query (high-weight narrative/reflection), not LayerStore

**API endpoints** (in `brain/src/api.py`, version 0.5.0):
```
GET  /dmn/thoughts?agent_id=X
  Response (DMNThoughtResponse): { agent_id, thoughts: [{ thought, channel, urgency, memory_id, timestamp }], count }

GET  /dmn/status
  Response (DMNStatusResponse): { running, heartbeat_counts, queue_sizes, active_threads }

POST /dmn/activity
  Request  (DMNActivityRequest):  { agent_id }
  Response (DMNActivityResponse): { agent_id, acknowledged, idle_seconds }
```

**Plugin update** (`openclaw/extensions/memory-brain/index.ts`):
- Tool: `dmn_status` — calls `GET /dmn/status`, shows running state + heartbeats + queues + threads
- HTTP clients: `brainGetDMNThoughts()`, `brainNotifyActivity()`, `brainGetDMNStatus()`
- Hook update: `before_agent_start` calls `brainNotifyActivity()` (fire-and-forget) to reset DMN idle timer on user input

## Phase 7: Safety Monitor (implemented)

- `brain/src/safety.py` — Standalone module (no brain module imports), math + logging only:
  - **Phase A (always on):** HardCeiling (MAX_CENTER=0.95, MAX_GOAL_BUDGET_FRACTION=0.40) + DiminishingReturns (gain / log2(evidence))
  - **Phase B (consolidation-time, disabled by default):** RateLimiter (MAX_CHANGE_PER_CYCLE=0.10 per memory) + TwoGateGuardrail (evidence quality gate + 50 changes/cycle cap)
  - **Phase C (mature agent, disabled by default):** EntropyMonitor (ENTROPY_FLOOR=2.0 bits, 20-bin histogram) + CircuitBreaker (MAX_CONSECUTIVE=5 same-evidence reinforcements)
  - **SafetyMonitor** coordinator: `check_weight_change()` -> (allowed, adj_delta_alpha, adj_delta_beta, reasons). Synchronous (no async). `enable_phase_b()`, `enable_phase_c()`, `end_consolidation_cycle(cycle_id)`
  - **OutcomeTracker**: records gate_decision/promotion/demotion events, forward-linkable via `link_outcome()`, max 2000 records
  - Module-level `_audit_log` (list[dict], max 1000), `log_safety_event()`, `get_audit_log()`
- Wired in `api.py` lifespan: `SafetyMonitor()` -> `_memory_store.safety`
- Wired in `consolidation.py`: `_deep_cycle()` calls `safety.enable_phase_b()` at start, `safety.end_consolidation_cycle(cycle_id)` at end (in finally block)
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
safety.py            <- standalone (math, logging, uuid only)
memory.py            <- config, relevance, db (pool); safety wired at runtime
gate.py              <- activation, memory
gut.py               <- activation (cosine_similarity), numpy, json, pathlib
context_assembly.py  <- stochastic, activation (cosine_similarity)
llm.py               <- config (RetryConfig), anthropic
consolidation.py     <- llm, memory, activation, numpy, asyncpg; safety via memory.store.safety
rumination.py        <- json, pathlib (standalone)
dmn_store.py         <- asyncio (standalone)
idle.py              <- rumination, dmn_store, llm, memory, relevance (spread_activation), asyncpg
api.py               <- memory, gate, context_assembly, gut, consolidation, idle, dmn_store, safety, db, numpy
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
    5. First DMN Self-Prompt — `source_tag = 'internal_dmn'`
    6. Identity-Weight Promotion — non-immutable memory with center > 0.8
    7. Conflict Resolution — tension type with `metadata->>'resolved' = 'true'`
    8. Creative Association — `metadata ? 'creative_insight'`
    9. Goal Reflected — reflection type with content matching goal/achieved
    10. Autonomous Decision — 3+ identity-weight (center>0.8) AND 2+ reflections

**API** (1 new endpoint, version 0.7.0):
- `GET /bootstrap/status?agent_id=X` -> BootstrapStatusResponse: { agent_id, milestones, achieved, total, ready, bootstrap_prompt, status_text }

**Cross-module dependency graph** (Phase 1-8):
```
config.py            <- standalone
stochastic.py        <- standalone
activation.py        <- numpy
relevance.py         <- activation
safety.py            <- standalone (math, logging, uuid only)
memory.py            <- config, relevance, db (pool); safety wired at runtime
gate.py              <- activation, memory
gut.py               <- activation (cosine_similarity), numpy, json, pathlib
context_assembly.py  <- stochastic, activation (cosine_similarity)
llm.py               <- config (RetryConfig), anthropic
consolidation.py     <- llm, memory, activation, numpy, asyncpg; safety via memory.store.safety
rumination.py        <- json, pathlib (standalone)
dmn_store.py         <- asyncio (standalone)
idle.py              <- rumination, dmn_store, llm, memory, relevance (spread_activation), asyncpg
bootstrap.py         <- asyncpg (standalone, DB-direct queries only)
api.py               <- memory, gate, context_assembly, gut, consolidation, idle, dmn_store, safety, bootstrap, db, numpy
```

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

<!--
RULES:
- One entry per superseded/amended decision. Not for new decisions (those go in roadmap.json).
- Tag in brackets = greppable domain (must be in charter.json tag_taxonomy).
- Keep entries to 4 lines max. Link to evidence, don't paste it.
- DJ numbering is sequential, never reused.
- When adding a DJ entry, also update the decision's status to "superseded" in roadmap.json.
- Add a devlog entry with event: "dj_entry".
-->
