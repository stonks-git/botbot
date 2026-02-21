# PLAN: Identity Architecture Rework + Sustainability Fixes
Status: active
Parent: none
Supersedes: none
Roadmap task: D-015, D-017, D-018a, D-018b, D-018c, D-018d

## Context

The brain is live but the identity system doesn't work as designed. After 4.5h of runtime:
- Avg weight center 0.134 (max 0.239) — nothing reaches the 0.3 identity threshold
- Identity selection uses pure weight-rank SQL (no cosine_sim, so irrelevant memories get reinforced)
- Identity hash always rendered in context assembly — no feature flag to disable it
- No injection logging — can't validate the w×s formula empirically
- No semantic chunking — long memories stored monolithically
- No memory groups — no group-wide beta refresh

**Already fixed (DO NOT re-implement):**
- D-014: DMN touch_memory (Session 20 — this session)
- D-022: Decay interval 5min→1h (Session 18)
- D-023: Consolidation dedup+reinforce (Session 18)
- D-024: DMN cold-start fallback (Session 18)
- D-025: Insight weight inheritance (Session 18)
- BUG-001: Gemini thinking model truncation (Session 19)

**Decisions governing this work (all accepted, Session 17):**
- DJ-005: No floor on identity scoring. injection_score = weight_center × cosine_sim
- DJ-006: No core identity tier. Identity hash feature-flagged dormant. Values are dispositional, not declarative.
- Read full context: KB/KB_01_architecture.md "Session 17 Brainstorm: Identity Architecture Redesign"

## Phase 1: Identity scoring + feature flag (D-015, D-017) — DONE

Highest architectural impact. Fixes the core identity selection mechanism and removes the always-on identity hash that wastes context budget on a newborn agent.

Intent: Replace pure weight-rank identity selection with w×s (weight_center × cosine_sim) scoring. Feature-flag identity hash to dormant. These two changes together mean identity memories are only injected when semantically relevant to the current conversation, and the static hash stops consuming tokens.

**Key files:** `brain/src/context_assembly.py`, `brain/src/memory.py`
**Key decisions:** D-015 (DJ-005), D-017 (DJ-006)

- [x] **1.1: Add `score_identity_wxs` method to MemoryStore**
  Files: `brain/src/memory.py`
  Do: Add method `score_identity_wxs(self, query_vec: list[float], agent_id: str, top_n: int = 20) -> list[dict]`. SQL computes injection_score entirely in DB: `(depth_weight_alpha / (depth_weight_alpha + depth_weight_beta)) * (1 - (embedding <=> $1::halfvec))`. Filter: `embedding IS NOT NULL`, exclude `immutable = true` (immutables handled by Track 0). ORDER BY injection_score DESC, LIMIT $top_n. Return columns: id, content, depth_weight_alpha, depth_weight_beta, injection_score.
  Verify: Method exists, SQL parses (no syntax errors). Check pgvector `<=>` operator returns cosine distance (1 - cosine_sim), so `1 - (embedding <=> vec)` = cosine_sim. Confirm.

- [x] **1.2: Wire w×s scoring into context assembly**
  Files: `brain/src/context_assembly.py`
  Do: In `assemble_context`, when `query_text` is provided: embed it with `memory_store.embed(query_text, task_type="RETRIEVAL_QUERY")` to get `query_vec`. Call `memory_store.score_identity_wxs(query_vec, agent_id, IDENTITY_TOP_N)`. Replace the stochastic identity injection loop (lines 80-98) with deterministic injection: iterate by injection_score rank, inject within `BUDGET_IDENTITY_MAX` token budget. Track injected IDs same as current. When `query_text` is empty: skip identity injection (empty list — no context means no relevance signal). Remove `_get_top_identity_memories` helper (dead after this). Remove `StochasticWeight` import if no longer used in this file. Remove `IDENTITY_THRESHOLD` constant (replaced by w×s ranking).
  Verify: `assemble_context` with query_text produces identity memories ranked by w×s. Without query_text, identity_memories is empty. Immutables still always injected via Track 0.

- [x] **1.3: Feature-flag identity hash dormant + update prompt headers**
  Files: `brain/src/context_assembly.py`
  Do: Add `IDENTITY_HASH_ENABLED = False` constant. In `assemble_context`: when disabled, skip `render_identity_hash()` call (set `identity_hash = ""`). In `render_system_prompt`: rename `[IDENTITY -- active beliefs/values this cycle]` to `[ACTIVE IDENTITY]` per brainstorm spec. Keep `render_identity_hash` and `render_identity_full` functions intact — they're used by API endpoints (`GET /identity/{id}/hash`, `GET /identity/{id}`). The `[IDENTITY]` section in the prompt naturally disappears when hash is empty string.
  Verify: Context assembly no longer includes `[IDENTITY]` hash section. System prompt shows `[ACTIVE IDENTITY]` for w×s memories. API endpoints `/identity/{id}` and `/identity/{id}/hash` still return data.

## Phase 2: Injection logging + analytics (D-018d) — DONE

Infrastructure for empirically validating the w×s formula. Must come before tuning thresholds.

Intent: Add `injection_logs` table. Log every identity injection (memory_id, agent_id, weight_center, cosine_sim, injection_score, was_injected). Add metrics endpoint for rolling stats + daily batch analysis.

**Key files:** `brain/src/schema.sql`, `brain/src/context_assembly.py`, `brain/src/api.py`
**Key decision:** D-018d

Depends on: Phase 1 (scoring must exist before logging it)

- [x] **2.1: Add injection_logs table to schema.sql**
  Files: `brain/src/schema.sql`
  Do: Add `CREATE TABLE IF NOT EXISTS injection_logs` with columns: `id SERIAL PRIMARY KEY`, `agent_id TEXT NOT NULL`, `memory_id TEXT NOT NULL`, `weight_center FLOAT NOT NULL`, `cosine_sim FLOAT NOT NULL`, `injection_score FLOAT NOT NULL`, `was_injected BOOLEAN NOT NULL`, `query_hash TEXT NOT NULL` (first 16 chars of SHA-256 of query_text — groups rows by turn without storing full text), `created_at TIMESTAMPTZ DEFAULT NOW()`. Add index `idx_injection_logs_agent` on `(agent_id, created_at DESC)` — main query axis. Add index `idx_injection_logs_memory` on `(memory_id)` — for top-memories-by-count. Add index `idx_injection_logs_query` on `(query_hash)` — for grouping by turn.
  Verify: SQL parses (no syntax errors). No conflicts with existing 6 tables. Table follows same conventions (IF NOT EXISTS, TIMESTAMPTZ, consistent naming).

- [x] **2.2: Log injection decisions in context_assembly.py**
  Files: `brain/src/context_assembly.py`
  Do: After the identity candidates loop (line 101), add async logging. For each candidate in `identity_candidates`: compute `weight_center = alpha / (alpha + beta)`, `cosine_sim = injection_score / weight_center` (guard: if weight_center <= 0 then cosine_sim = 0.0), `was_injected = mem["id"] in injected_set` (build a set from `injected_memory_ids` for O(1) lookup). `query_hash = hashlib.sha256(query_text.encode()).hexdigest()[:16]`. Batch INSERT via `pool.executemany()` with the injection_logs INSERT statement. Use `memory_store.pool` for DB access (already used indirectly by embed/score_identity_wxs). Wrap entire logging block in `try: ... except Exception: logger.warning(...)` — non-blocking, assembly must succeed even if logging fails. Import `hashlib` at top.
  Verify: Call `/context/assemble` with a query_text, then `SELECT * FROM injection_logs` — rows for all candidates present. `was_injected` is True for injected memories, False for budget-rejected ones. If DB insert fails, `/context/assemble` still returns normally (non-blocking).

- [x] **2.3: Add GET /injection/metrics endpoint to api.py**
  Files: `brain/src/api.py`
  Do: Add Pydantic models `InjectionMetricsResponse` with fields: `total_logs: int`, `injection_rate: float` (was_injected=true / total), `score_stats: dict` (avg, p50, p75, p95 of injection_score), `top_memories: list[dict]` (top 10 memory_id by was_injected=true count, with count + avg_score). Add endpoint `GET /injection/metrics?agent_id=X&days=7`. SQL: use `percentile_cont(array[0.5, 0.75, 0.95]) WITHIN GROUP (ORDER BY injection_score)` for percentiles. Top memories: `GROUP BY memory_id` where `was_injected = true`, `ORDER BY count DESC LIMIT 10`. Return empty/zero stats on empty table (no crash). Time-filter by `created_at >= NOW() - ($days || ' days')::interval`.
  Verify: Call `/context/assemble` 5+ times with varying queries, then `GET /injection/metrics` — stats populated with non-zero values. Call on fresh agent with no logs — returns zeros, no error.

## Phase 3: Semantic chunking + memory groups (D-018b, D-018c)

Structural changes to how memories are stored and maintained.

Intent: Enforce 300-token max at gate time via semantic chunking. Add `memory_group_id` column for linking chunks. Group-wide beta refresh (touching one chunk in a group refreshes all). Insights from a single group share group_id.

**Key files:** `brain/src/schema.sql`, `brain/src/gate.py`, `brain/src/memory.py`, `brain/src/consolidation.py`
**Key decisions:** D-018b, D-018c

Tasks: (empty until decomposition session)
Depends on: Phase 2 (logging should be in place to observe impact)

## Phase 4: Adaptive context shift threshold (D-018a)

Self-evolving threshold that replaces the hardcoded 0.7.

Intent: P75 percentile of last 200 context shift values stored in a ring buffer. Active identity cached between subject changes. Threshold adapts to the agent's actual injection score distribution.

**Key files:** `brain/src/context_assembly.py`
**Key decision:** D-018a

Tasks: (empty until decomposition session)
Depends on: Phase 2 (needs injection_logs data to compute meaningful percentiles)

---

## Audit Findings (Session 20 — full codebase audit)

26 findings total. Integrated by relevance to this plan:

### Critical (must fix)

| ID | File | Issue |
|----|------|-------|
| CQ-001 | memory.py:469 | Unsafe `'mem' in dir()` — fragile variable check, can TypeError on missing memory |
| CQ-002 | memory.py:443-512 | N+1 queries in retrieval mutation — 40 individual SQL statements per retrieval call |
| CQ-003 | memory.py:492-512 | N+1 in near-miss immutability check — individual SELECT per near-miss memory |

### High (should fix before scaling)

| ID | File | Issue |
|----|------|-------|
| CQ-005 | consolidation.py:638-720 | Promotion bypasses safety monitor — alpha += 5.0 skips HardCeiling/DiminishingReturns |
| CQ-006 | consolidation.py:176-202,724-744 | Decay bypasses safety monitor — beta changes skip entropy check |
| CQ-008 | memory.py:70-84 | `embed_batch` sends individual API calls, not true Gemini batch |
| CQ-010 | gate.py:215-237 | Gate contradiction check embeds+searches same content twice |
| CQ-019 | bootstrap.py | Milestones 5/7/8 unreachable — DMN doesn't create memories with expected tags, no code sets `resolved:true` or `creative_insight` metadata |

### Medium (quality/maintenance)

| ID | File | Issue |
|----|------|-------|
| CQ-004 | relevance.py:180-192 | N+1 in co-access update — individual INSERT per pair |
| CQ-011 | api.py:787-856 | Monologue endpoint fires 3 parallel queries, merges+sorts in Python instead of SQL UNION |
| CQ-012 | consolidation.py:79-108 | `_greedy_cluster` is O(n^2) cosine similarity on 3072-dim vectors (1,225 comparisons at limit=50) |
| CQ-007 | consolidation.py + idle.py | `_get_agent_ids` duplicated identically |
| CQ-009 | memory.py:651-661 | `avg_depth_weight_center` accepts unused `where` param |
| CQ-013 | 6+ files | Weight-center SQL expression copy-pasted 20+ times |
| CQ-014 | consolidation.py:75 | `json.dumps()` for JSONB column may cause double-serialization |
| CQ-015 | memory.py:561-577 | `flush_scratch` appears to be dead code (no callers) |
| CQ-023 | api.py:873 vs rumination.py:112 | Key mismatch: `resolution_reason` vs `reason` in monologue endpoint |
| CQ-025 | idle.py:379-394 | DMN channel classification always returns "DMN/creative" once any co-access exists |

### Low (style/minor)

| ID | File | Issue |
|----|------|-------|
| CQ-016 | idle.py:279-280 | Manual embedding string construction instead of `str(list(...))` |
| CQ-017 | safety.py:13-14 | `_audit_log` is a mutable list, should be `deque(maxlen=1000)` |
| CQ-018 | safety.py:451-525 | `OutcomeTracker` defined but never wired — dead code or Phase X |
| CQ-020 | multiple | Magic number 0.85 threshold in 3 places |
| CQ-021 | context_assembly.py:171-201 | `adaptive_fifo_prune` dead code (never called) |
| CQ-022 | gut.py:224-247 | Gut state saves 50KB JSON (two 3072-float arrays) per context assembly call |
| CQ-024 | idle.py:263-313 | `_sample_tension` returns raw embedding array (3072 floats) unused |

---

## Out of scope for this plan

These are accepted decisions but belong in separate plans:
- **D-016:** Consolidation research sessions (agent spawning + web search — complex, standalone)
- **D-021:** HDBSCAN pattern detection (needs enough memories to cluster — standalone)
- **D-019/T-P11:** Proactive notifications (standalone feature, no dependency on identity)

Audit bugs (CQ-001 through CQ-025) will be addressed in a separate bugfix plan unless they directly block a phase in this plan.
