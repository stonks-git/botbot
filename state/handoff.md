# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-22 (#26) - T-P13: Injection Logging + w×s Analytics (D-018d)

**STATUS:** DONE

**What was done:**

1. **injection_logs table** — SERIAL PK, 8 columns (agent_id, memory_id, weight_center, cosine_sim, injection_score, was_injected, query_hash, created_at), 3 indexes (agent+time, memory_id, query_hash). No FK constraints (log table convention). `brain/src/schema.sql`.

2. **Injection decision logging** — After w×s identity scoring loop in `assemble_context()`, every candidate is logged with derived weight_center (alpha/(alpha+beta)), cosine_sim (injection_score/weight_center), and was_injected flag. Batch INSERT via `pool.executemany()`. Non-blocking (try/except). query_hash = SHA-256[:16] of query_text for turn grouping. `brain/src/context_assembly.py`.

3. **GET /injection/metrics endpoint** — SQL percentiles via `percentile_cont(ARRAY[0.5, 0.75, 0.95])`, injection_rate, top-10 memories by injection count. Time-filtered by `days` param (default 7). Graceful on empty table. `brain/src/api.py`.

| File | What was done |
|------|---------------|
| `brain/src/schema.sql` | injection_logs table + 3 indexes |
| `brain/src/context_assembly.py` | import hashlib, identity_candidates init, injection logging block after w×s loop |
| `brain/src/api.py` | InjectionMetricsResponse model + GET /injection/metrics endpoint |
| `state/plans/202602211500-identity-architecture-rework.md` | Phase 2 decomposed + all 3 tasks checked + DONE |
| `KB/KB_01_architecture.md` | injection_logs table, injection logging in context assembly, /injection/metrics endpoint |
| `state/devlog.ndjson` | 2 entries (feature + kb_update) |
| `state/roadmap.json` | T-P13 → done |

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

### SESSION 2026-02-22 (#24) - T-B04: Batch Embedding + Gate Redundant Search (CQ-008, CQ-010)

**STATUS:** DONE

**What was done:**

1. **CQ-008: True batch embedding.** Rewrote `embed_batch()` in memory.py to use Gemini's native batch API — passes `contents=[list]` to `embed_content()` in a single call per 100 texts, instead of N individual `embed()` calls via asyncio.gather. Retry logic (exponential backoff) preserved per chunk.

2. **CQ-010: Gate triple-embed eliminated.** Added optional `embedding` parameter to `check_novelty()` — when provided, skips internal `embed()` call (backward compatible). `ExitGate.evaluate()` now: (a) embeds content once, (b) passes that embedding to `check_novelty()`, (c) fetches contradiction content via `get_memory(most_similar_id)` instead of re-embedding+re-searching via `search_similar()`. 3 embed API calls → 1 per gate evaluation.

| File | What was done |
|------|---------------|
| `brain/src/memory.py` | CQ-008: Rewrote `embed_batch()` for true Gemini batch API. CQ-010: Added `embedding` param to `check_novelty()` |
| `brain/src/gate.py` | CQ-010: Pass pre-computed embedding to check_novelty. Replace search_similar with get_memory for contradiction |
| `KB/KB_01_architecture.md` | Updated memory method table, gate novelty description, dependency graph |
| `state/devlog.ndjson` | 3 entries (2 bugfixes + kb_update) |
| `state/roadmap.json` | T-B04 → done |

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
| Consolidation pattern detection at scale | OPEN — D-021 HDBSCAN designed, not implemented |

---

## Git Status

- **Branch:** main
- **Last commit:** ff18664 Sessions 18-19: Idle-state fixes, DB cleanup, AGENTS.md, DMN observability
- **Modified (tracked):** KB/KB_01_architecture.md, brain/src/api.py, brain/src/bootstrap.py, brain/src/consolidation.py, brain/src/context_assembly.py, brain/src/gate.py, brain/src/idle.py, brain/src/memory.py, brain/src/relevance.py, brain/src/schema.sql, state/devlog.ndjson, state/handoff.md, state/roadmap.json
- **New (untracked):** openclaw-workspace/.openclaw/, openclaw-workspace/BOOTSTRAP.md, etc.

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-22T17:00:00+02:00 | Session 26 complete (T-P13: D-018d) | injection_logs table + context_assembly logging + /injection/metrics endpoint. Plan Phase 2 DONE. | Next: T-B06 (code hygiene), T-P11 (proactive notifications), T-P14 (semantic chunking).
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
