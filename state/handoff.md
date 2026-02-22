# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-23 (#29) - T-P15 + T-P17 + T-P16 + T-P11 (4 P3 features)

**STATUS:** DONE

**What was done:**

1. **T-P15: Adaptive context shift threshold (D-018a)** — Added `context_shift_buffer` table (ring buffer of 200 values). `_get_adaptive_threshold()` returns P75 percentile (default 0.5 when < 200 values). Identity cache per agent (`_identity_cache`), invalidated when shift >= threshold. Replaced hardcoded 0.7 with adaptive threshold for inertia.

2. **T-P17: HDBSCAN pattern detection (D-021)** — Added `hdbscan>=0.8.38` to requirements. Added `insight_level` column migration. `_hdbscan_cluster()` function. Replaced `_pattern_detection()` body with HDBSCAN clustering, per-cluster LLM analysis (insight_level=1), cross-cluster meta-insights (insight_level=2). Schedule changed from 15min to 1/day. Added `build-essential` to Dockerfile.

3. **T-P16: Consolidation research sessions (D-016/DJ-008)** — Added `research_queue` table. `llm_call_with_search()` + retry wrapper using Gemini GoogleSearch tool. Research config constants. Classification, rate limiting, 2-search confirmation lifecycle. Structural confidence from grounding chunks. Safe mode: displacement only when 2 independent searches agree.

4. **T-P11: Proactive notification system (D-019)** — Created `brain/src/notification.py` with NotificationStore + DeliveryWorker. Added `notification_outbox` + `notification_preferences` tables. Wired into api.py (lifespan + 4 endpoints + passive injection in /context/assemble), consolidation.py (contradiction + research triggers), idle.py (DMN goal/identity triggers).

5. **Schema fix** — Added `memory_group_id` column migration (DO/EXCEPTION block) before `idx_memories_group` index to fix startup on existing DBs.

| File | What was done |
|------|---------------|
| `brain/src/schema.sql` | context_shift_buffer, notification_outbox, notification_preferences, research_queue tables. insight_level + memory_group_id migrations |
| `brain/src/config.py` | Research constants, notification constants, research_finding type prefix |
| `brain/src/context_assembly.py` | Adaptive threshold, identity cache, _get_adaptive_threshold(), _record_context_shift() |
| `brain/src/consolidation.py` | HDBSCAN clustering, _hdbscan_cluster(), research methods (_classify, _queue, _process, _first, _confirmation), notification triggers |
| `brain/src/memory.py` | insight_level param on store_insight() |
| `brain/src/llm.py` | llm_call_with_search(), retry_llm_call_with_search() |
| `brain/src/notification.py` | NEW: NotificationStore, DeliveryWorker |
| `brain/src/api.py` | Notification imports/globals/lifespan/endpoints, passive injection in /context/assemble |
| `brain/src/idle.py` | notification_store param, DMN/goal + DMN/identity notification triggers |
| `brain/requirements.txt` | hdbscan>=0.8.38 |
| `brain/Dockerfile` | build-essential for hdbscan C extension compilation |
| `KB/KB_01_architecture.md` | DJ-008, all 4 features documented, dependency graph updated |
| `state/roadmap.json` | T-P15/T-P17/T-P16/T-P11 → done, D-016 → superseded |

### SESSION 2026-02-22 (#28) - T-B06: Code Hygiene Pass (11 audit findings)

**STATUS:** DONE

**What was done:**

1. **Dead code removal (CQ-009/015/018/021)** — Removed `flush_scratch()` from memory.py (never called), `adaptive_fifo_prune()` from context_assembly.py (never called), `OutcomeTracker` class from safety.py (~75 lines, never instantiated), unused `where` param from `avg_depth_weight_center()`.

2. **DRY constants (CQ-007/013/020)** — Extracted `WEIGHT_CENTER_SQL` and `NOVELTY_THRESHOLD` to config.py. Replaced 11+ inline SQL occurrences of `depth_weight_alpha / (depth_weight_alpha + depth_weight_beta)` across 8 files with f-string interpolation. Moved `_get_agent_ids()` to db.py as `get_agent_ids()`, removed duplicate definitions from idle.py and consolidation.py. `MERGE_SIMILARITY_THRESHOLD` and gate defaults now reference `NOVELTY_THRESHOLD`.

3. **Minor fixes (CQ-016/017/022/024)** — Fixed `_sample_tension()` in idle.py: removed `embedding::float4[] AS emb_arr` + manual string formatting, pass `embedding` column directly, no-partner return no longer leaks full embedding array. Converted `_audit_log` in safety.py from `list[dict]` with manual `pop(0)` to `collections.deque(maxlen=1000)`. Added `_dirty` flag to gut.py `save()` — skips disk I/O when state hasn't changed.

4. **Deferred** — CQ-011 (monologue UNION: already parallel via asyncio.gather), CQ-012 (O(n^2) clustering: T-P17 HDBSCAN replaces entirely).

| File | What was done |
|------|---------------|
| `brain/src/config.py` | Added `NOVELTY_THRESHOLD = 0.85`, `WEIGHT_CENTER_SQL` constant |
| `brain/src/db.py` | Added shared `get_agent_ids()` function |
| `brain/src/memory.py` | Removed flush_scratch, fixed avg_depth_weight_center param, used NOVELTY_THRESHOLD + WEIGHT_CENTER_SQL |
| `brain/src/safety.py` | Removed OutcomeTracker, deque for audit_log, replaced uuid with collections import |
| `brain/src/context_assembly.py` | Removed adaptive_fifo_prune, used WEIGHT_CENTER_SQL |
| `brain/src/gate.py` | Imported NOVELTY_THRESHOLD for confirming_sim + decision matrix |
| `brain/src/consolidation.py` | Imported get_agent_ids/NOVELTY_THRESHOLD/WEIGHT_CENTER_SQL, removed duplicate helper |
| `brain/src/idle.py` | Imported get_agent_ids/WEIGHT_CENTER_SQL, fixed _sample_tension emb_arr leak |
| `brain/src/api.py` | Imported WEIGHT_CENTER_SQL for _get_identity_embeddings |
| `brain/src/bootstrap.py` | Imported WEIGHT_CENTER_SQL for 3 milestone checks |
| `brain/src/gut.py` | Added _dirty flag for save() optimization |
| `KB/KB_01_architecture.md` | Config constants, removed dead code refs, updated dependency graph |
| `state/devlog.ndjson` | 2 entries (refactor + kb_update) |
| `state/roadmap.json` | T-B06 → done |

### SESSION 2026-02-22 (#27) - T-P14: Semantic Chunking + Memory Groups (D-018b, D-018c)

**STATUS:** DONE

**What was done:**

1. **memory_group_id column** — `TEXT` nullable column on memories table. Partial index `idx_memories_group ON memories (memory_group_id) WHERE memory_group_id IS NOT NULL`. `store_memory()` accepts optional `memory_group_id` parameter. `brain/src/schema.sql`, `brain/src/memory.py`.

2. **Semantic chunking at gate time** — `semantic_chunk(text, max_tokens=300)` in gate.py: splits by paragraph (`\n\n`), then sentence boundaries, greedily merges under 300-token limit. Gate PERSIST path in api.py: content > 300 tokens → chunks stored as separate memories with shared `memory_group_id` and metadata `{group_part, group_total}`. `brain/src/gate.py`, `brain/src/api.py`.

3. **Group-wide touch_memory** — `touch_memory()` queries `memory_group_id` of target memory. If non-NULL, UPDATEs `last_accessed` on all group siblings. Standalone memories (NULL group_id) use original single-row UPDATE. `brain/src/memory.py`.

4. **Insight group_id inheritance + context annotation** — `store_insight()` queries source memories' `memory_group_id` values. If ALL share the same non-NULL group_id, insight inherits it. `score_identity_wxs()` now includes `metadata` column. `_annotate_chunk()` helper prepends `[part N of M]` to content in context assembly for chunked memories. `brain/src/memory.py`, `brain/src/context_assembly.py`.

| File | What was done |
|------|---------------|
| `brain/src/schema.sql` | memory_group_id column + partial index |
| `brain/src/memory.py` | store_memory memory_group_id param, store_insight group inheritance, group-wide touch_memory, score_identity_wxs +metadata |
| `brain/src/gate.py` | semantic_chunk() function + _estimate_tokens helper |
| `brain/src/api.py` | Gate PERSIST chunking path with group_id linking |
| `brain/src/context_assembly.py` | _annotate_chunk() helper, identity + situational chunk annotation |
| `state/plans/202602211500-identity-architecture-rework.md` | Phase 3 decomposed + all 4 tasks checked + DONE |
| `KB/KB_01_architecture.md` | memory groups, semantic chunking, group-wide touch, chunk annotation |
| `state/devlog.ndjson` | 2 entries (feature + kb_update) |
| `state/roadmap.json` | T-P14 → done |

### SESSION 2026-02-22 (#25) - T-B05: DMN Channel Fix + Monologue Key + JSONB (CQ-025, CQ-023, CQ-014)

**STATUS:** DONE

**What was done:**

1. **CQ-025: DMN creative channel always wins.** `_classify_channel()` checked `len(activated) > 0` — any memory with even 1 co-access (score 0.05) triggered "DMN/creative", starving identity/reflect channels. Added `MIN_CREATIVE_ACTIVATION = 0.15` constant. Now requires `max(activated.values()) >= 0.15`, meaning ≥3 co-accesses at hop 0 for creative classification.

2. **CQ-023: Monologue key mismatch.** `api.py` monologue endpoint read `ct.get("resolution_reason")` but `rumination.py` stores completed threads with key `"reason"`. Result: resolution_reason always `""`. Fixed to `ct.get("reason")`. API response field name preserved.

3. **CQ-014: JSONB double-serialization.** `_log_consolidation()` passed `json.dumps(details)` string to asyncpg for a JSONB column. asyncpg's default JSONB codec calls `json.dumps()` internally → double-serialized (JSON string literal stored instead of object). `details->>'key'` returned NULL, `isinstance(details, dict)` returned False. Fixed: pass dict directly. Removed unused `import json`.

| File | What was done |
|------|---------------|
| `brain/src/idle.py` | CQ-025: Added MIN_CREATIVE_ACTIVATION=0.15, changed _classify_channel creative check |
| `brain/src/api.py` | CQ-023: Fixed monologue completed thread key from "resolution_reason" to "reason" |
| `brain/src/consolidation.py` | CQ-014: Pass dict directly to asyncpg JSONB column, removed json import |
| `KB/KB_01_architecture.md` | Updated _classify_channel, consolidation_log, monologue descriptions |
| `state/devlog.ndjson` | 4 entries (3 bugfixes + kb_update) |
| `state/roadmap.json` | T-B05 → done |

### SESSION 2026-02-22 (#23) - T-P12: Identity w×s Scoring + Hash Feature Flag (D-015, D-017)

**STATUS:** DONE

**What was done:**

1. **D-015: Replaced stochastic identity injection with w×s scoring.** Added `score_identity_wxs()` method to MemoryStore — computes `injection_score = weight_center × cosine_sim` entirely in SQL via pgvector `<=>` operator. Context assembly now embeds `query_text` (RETRIEVAL_QUERY task type), calls the scoring method, and injects identity memories ranked by injection_score within `BUDGET_IDENTITY_MAX`. No query = no identity injection (relevance mandatory per DJ-005). Removed stochastic Beta sampling, `_get_top_identity_memories` helper, `IDENTITY_THRESHOLD` constant, and `StochasticWeight` import.

2. **D-017: Identity hash feature-flagged dormant.** Added `IDENTITY_HASH_ENABLED = False` constant. `render_identity_hash()` skipped during context assembly (saves ~100-200 tokens per call). API endpoints `GET /identity/{id}` and `GET /identity/{id}/hash` still functional. System prompt header renamed from `[IDENTITY -- active beliefs/values this cycle]` to `[ACTIVE IDENTITY]`.

| File | What was done |
|------|---------------|
| `brain/src/memory.py` | D-015: Added `score_identity_wxs()` method — SQL-native w×s identity scoring |
| `brain/src/context_assembly.py` | D-015: w×s identity injection loop. D-017: `IDENTITY_HASH_ENABLED=False`. Header → `[ACTIVE IDENTITY]`. Removed stochastic imports + dead helper |
| `state/plans/202602211500-identity-architecture-rework.md` | Phase 1 decomposed + DONE. Plan status → active |
| `KB/KB_01_architecture.md` | Updated Phase 3 context assembly description + dependency graph |
| `state/devlog.ndjson` | 3 entries (2 features + kb_update) |
| `state/roadmap.json` | T-P12 → done |

---

## What is this project?

BotBot bolts the intuitive-AI cognitive architecture (memory with Beta-distributed weights, gut feeling, consolidation, DMN/idle loop, safety monitoring) onto OpenClaw as a Python sidecar brain service, giving OpenClaw agents a real mind that develops identity from experience.

---

## Tasks DOING now

| Task ID | Status |
|---------|--------|
| T-B01 | DONE — Critical bugs: dir(), N+1 retrieval mutation, N+1 co-access (CQ-001/002/003/004) |
| T-B02 | DONE — Safety bypass in promotion + decay (CQ-005/006) |
| T-B03 | DONE — Unreachable bootstrap milestones 5/7/8 (CQ-019) |
| T-P12 | DONE — Identity w×s scoring + hash feature flag (D-015/D-017) |
| T-B04 | DONE — True batch embedding + gate redundant search fix (CQ-008/CQ-010) |
| T-B05 | DONE — DMN channel fix + monologue key + JSONB (CQ-025/CQ-023/CQ-014) |
| T-P13 | DONE — Injection logging + w×s analytics (D-018d) |
| T-P14 | DONE — Semantic chunking + memory groups (D-018b, D-018c) |
| T-B06 | DONE — Code hygiene: dead code, DRY constants, deque, dirty flag, emb_arr fix (11/13 CQ items) |
| T-P15 | DONE — Adaptive context shift threshold (D-018a) |
| T-P17 | DONE — HDBSCAN pattern detection (D-021) |
| T-P16 | DONE — Consolidation research sessions (D-016/DJ-008, safe 2-search mode) |
| T-P11 | DONE — Proactive notification system (D-019) |

## Blockers or open questions

| Blocker/Question | Status |
|------------------|--------|
| Gate cold-start: newborn can't form memories | RESOLVED — hunger curve (D-009) |
| Retrieval mutation loop disconnected | RESOLVED — D-012/D-013 (Session 16) |
| Safety blocks access_count during consolidation | RESOLVED — separated from alpha boost (D-013) |
| BUG-001: Gemini thinking model eats output tokens | RESOLVED — removed max_output_tokens cap, verified 124-369 char output (Session 19) |
| Idle-state decay winning | RESOLVED — D-022 (1h interval), D-023 (dedup), D-024 (DMN fallback), D-025 (weight inheritance) |
| DMN produces nothing (268 heartbeats, 0 thoughts) | RESOLVED — D-024 fallback sampling bypasses unreachable thresholds |
| Consolidation duplicate flood | RESOLVED — D-023 dedup+reinforce on all 4 creation paths |
| Weight centers dropping | RESOLVED — DB cleanup (100 junk deleted) + D-022/D-023/D-025 + BUG-001 fix. Brain restarted, healthy |
| OpenClaw default AGENTS.md conflicts with brain | RESOLVED — T-P9: custom AGENTS.md with brain orientation, web search rules (D-020), no file-based memory refs |
| `[[reply_to_current]]` tag leaking into agent output | RESOLVED — T-P9: AGENTS.md instructs agent to never echo directive tags |
| Where to see DMN/rumination output? | RESOLVED — T-P10: GET /monologue/{agent_id} endpoint + monologue plugin tool |
| How to validate w×s formula empirically? | OPEN — D-018d logging pipeline needed first |
| Consolidation pattern detection at scale | RESOLVED — D-021 HDBSCAN implemented (T-P17) |

---

## Git Status

- **Branch:** main
- **Last commit:** 0e97690 Sessions 20-26: Critical bugfixes, identity w×s scoring, injection logging
- **Modified (tracked):** KB/KB_01_architecture.md, brain/Dockerfile, brain/requirements.txt, brain/src/api.py, brain/src/bootstrap.py, brain/src/config.py, brain/src/consolidation.py, brain/src/context_assembly.py, brain/src/db.py, brain/src/gate.py, brain/src/gut.py, brain/src/idle.py, brain/src/llm.py, brain/src/memory.py, brain/src/safety.py, brain/src/schema.sql, state/devlog.ndjson, state/handoff.md, state/plans/202602211500-identity-architecture-rework.md, state/roadmap.json
- **New (untracked):** brain/src/notification.py, openclaw-workspace/.openclaw/, openclaw-workspace/BOOTSTRAP.md, etc.

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-23T03:00:00+02:00 | Session 29 doc finalized | All 4 P3 features done. Plan marked done. D-016 status fixed (amended→superseded). All roadmap tasks complete. | Next: No remaining tasks. Ready for commit.
```

---

## Next Session Bootstrap

1. Read `KB/blueprints/v0.3_current_state.md` — **the single source of truth** for project state
2. All brain phases (0-8) complete + integration tested — 19/19 endpoints pass
3. **OpenClaw runtime is live** — 3 containers: postgres (:5433) + brain (:8400) + openclaw (:18789)
4. memory-brain plugin active: auto-recall + auto-capture (user+assistant), 9 tools (added monologue), agentId=default
5. Agent LLM: Gemini 3 Flash Preview (D-007), same `GOOGLE_API_KEY` as brain (D-006)
6. Agent is newborn, no preset identity (D-008) — bootstrap milestones track maturation
7. `docker compose up -d` starts all 3 services. WebChat at `http://localhost:18789/?token=botbot-dev`
8. **Retrieval mutation loop active (D-012/D-013)**: context assembly mutates injected memories (access_count + alpha), gate touch prevents decay on referenced memories.
9. **Weight centers climbing but below identity threshold**: 0.17→0.24 after 7 retrievals. Need ~27 more to reach 0.3+ for identity embeddings to become visible to gate.

### Session 17 Brainstorm Decisions — MUST READ BEFORE IMPLEMENTING

All decisions are accepted. They interlock. Read the full summary in `KB/KB_01_architecture.md` section "Session 17 Brainstorm: Identity Architecture Redesign" before starting any implementation.

**Implementation order (recommended):**

1. **D-014: DMN touch_memory (quick fix)** — When idle loop samples a memory for rumination, call `touch_memory()` on it. File: `brain/src/idle.py`. Simple change.

2. **Identity architecture rework (needs plan in state/plans/):**
   - D-015: Replace identity selection with w×s scoring
   - D-017: Feature-flag identity hash dormant
   - D-018a: Adaptive context shift threshold (P75 ring buffer)
   - D-018b: Semantic chunking at gate time (300-token max)
   - D-018c: Memory group_id + group beta refresh
   - D-018d: injection_logs table + logging + metrics endpoint

3. **T-P9: Replace AGENTS.md** — Custom AGENTS.md with brain memory orientation + web search rules (D-020). Fix `[[reply_to_current]]` tag leak.

4. **D-016: Consolidation research sessions** — Spawn agent sessions for fact-checkable contradictions. Needs: LLM judgment prompt, session isolation, research budget tracking.

5. **D-021: Pattern detection overhaul** — HDBSCAN clustering, per-cluster analysis, 1/day schedule, weighted-avg insight weights, 1-level recursion.

6. **T-P10: DMN Observability** — Unified view of agent inner monologue.

7. **T-P11: Proactive notifications** — notification_outbox, urgency/importance scoring, Telegram delivery.

### Live Brain State (as of Session 19 end)

- 30 memories total (3 agents: default=25, test=3, test-agent=2)
- Default agent: 9 clean memories after DB cleanup → 25 after consolidation ran post-restart
- Consolidation producing full-length output (124-369 chars vs 14-50 char junk before)
- 16 new consolidation memories created within 30s of restart — all coherent
- Weight centers: 0.071-0.233 on original memories (need 0.3+ for identity visibility)
- 1 strong narrative identity memory kept from cleanup (289 chars, accessed 7x)

---

## Before updating this file

- [x] devlog entry added for each change
- [x] Session section filled (what was done, verifications, files touched)
- [x] **KB updated** if code was modified + `kb_update` devlog entry
- [x] **Blueprint updated** if scaffolding/architecture changed + `blueprint` devlog entry
- [x] **Decision Journal entry** if any decision was superseded + `dj_entry` devlog entry
- [x] **Schema Log updated** if DB migrations were created
- [x] `python3 taskmaster.py validate` exits 0
- [x] Keep only last 3 sessions (older ones archived in git)
