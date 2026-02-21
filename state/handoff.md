# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-22 (#17) - Identity Architecture Brainstorm

**STATUS:** DONE (brainstorm only, no code changes)

**What was done:**

Extended brainstorm session revising the identity architecture (D-015/D-016/D-017 from Session 16) and creating new design decisions (D-018a-d, D-019, D-020, D-021).

**Key architectural shifts:**

1. **Dropped floor from identity scoring (DJ-005):** `injection_score = weight_center * cosine_sim` — no floor. Any floor creates immortal memories by guaranteeing injection → reinforcement regardless of relevance. Zero relevance = zero score.

2. **Dropped core identity tier (DJ-006):** No protected "core" tier. Identity hash feature-flagged dormant. All non-immutable memories compete on w×s. Core values emerge from behavioral patterns detected by consolidation, not from designation. "Strong ego = rigid agent."

3. **Consolidation spawns research sessions (DJ-007):** Instead of internal LLM fact-check, contradictions trigger a full agent session with web search tools. LLM judges worthiness first. 1/hour, max 24/day.

4. **Adaptive context shift (D-018a):** P75 percentile of last 200 shift values. Self-evolving threshold. Active identity cached between subject changes.

5. **Semantic chunking + memory groups (D-018b/c):** Gate enforces 300-token max. Long content chunked. Chunks linked by group_id. Group-wide beta refresh. Insights from single group share group_id.

6. **3-tier injection analytics (D-018d):** injection_logs table. Real-time rolling metrics, daily batch analysis + behavioral classification, weekly anomaly detection.

7. **Proactive notifications (D-019):** Urgency ≠ importance ≠ weight. Urgent → push NOW (Telegram/WhatsApp/webhook). Important → btw next chat. T-P11 added to roadmap.

8. **AGENTS.md web search (D-020):** Brain memory trusted, LLM training data not. Max 3 searches/turn.

9. **Pattern detection redesign (D-021):** HDBSCAN clustering, per-cluster LLM analysis, 1/day, 1-level recursion, weighted avg insights, dedup at sim>0.85, no cap.

**Decisions:** D-015 (revised), D-016 (revised), D-017 (revised), D-018a, D-018b, D-018c, D-018d, D-019, D-020, D-021
**Decision Journal:** DJ-005, DJ-006, DJ-007
**New task:** T-P11 (proactive notification system)

| File | What was done |
|------|---------------|
| `state/devlog.ndjson` | 17 new entries (decisions, DJ entries, kb_update, handoff) |
| `state/handoff.md` | Session 17 summary, updated priorities |
| `state/roadmap.json` | D-015/D-016/D-017 revised to accepted, D-018a-d/D-019/D-020/D-021 added, T-P11 added, meta updated |
| `KB/KB_01_architecture.md` | Session 17 brainstorm section (full architecture summary), DJ-005/DJ-006/DJ-007 entries, DJ table updated |

---

### SESSION 2026-02-22 (#18) - Idle-State Sustainability Fixes + Truncation Bug

**STATUS:** DONE

**What was done:**

1. **Diagnosed 5 problems from 4.5h runtime data:** Decay winning (beta +0.48/4hr), consolidation over-production (15 memories/hr, all decaying), DMN producing nothing (268 heartbeats, 0 thoughts), DNS transient failure, no conversation.

2. **Mapped brainstorm decisions to problems:** Only ~20% of observed problems covered by Session 17 brainstorm. Identified "idle-state sustainability" as new problem class.

3. **D-022: Decay interval 5min→1h (IMPLEMENTED):** `DECAY_TICK_INTERVAL = 3600`. Decay pressure drops from 0.12 to 0.01 beta/hr (12x reduction).

4. **D-023: Consolidation dedup+reinforce (IMPLEMENTED):** All 4 creation paths (tensions, insights, narratives, revalidation) now call `check_novelty(threshold=0.85)`. If not novel, reinforces existing memory via `apply_retrieval_mutation` instead of creating new. DB analysis showed 49% of pool were near-exact duplicates (sim >= 0.95).

5. **D-024: DMN cold-start fallback (IMPLEMENTED):** When all 4 channels return None (thresholds unreachable for young agent), `_sample_fallback` picks from ALL memories with probability proportional to weight center.

6. **D-025: Insight weight inheritance (IMPLEMENTED):** `store_insight` computes weighted-avg alpha/beta from sources. Removed source importance demotion (`importance = LEAST(0.3)` deleted). Added `initial_alpha`/`initial_beta` params to `store_memory`.

7. **BUG-001 FOUND: Gemini thinking model truncation.** `gemini-3-flash-preview` is a reasoning model — internal chain-of-thought consumes from `max_output_tokens` budget. With `max_tokens=200`, model spends ~180 on thinking, leaves ~20 for actual output. 101/109 memories are sentence fragments (14-50 chars). Root cause of all garbage consolidation output. Fix pending: disable thinking or raise budget.

**Decisions:** D-022, D-023, D-024, D-025
**Bugs found:** BUG-001 (Gemini thinking model token budget)

| File | What was done |
|------|---------------|
| `brain/src/consolidation.py` | D-022: decay interval 5min→1h. D-023: dedup+reinforce all 4 paths |
| `brain/src/memory.py` | D-025: initial_alpha/beta on store_memory, weight inheritance in store_insight, removed source demotion |
| `brain/src/idle.py` | D-024: _sample_fallback weight-proportional sampling |
| `state/devlog.ndjson` | 9 entries (D-022-025, BUG-001, 4 feature implementations) |
| `state/handoff.md` | Session 18 summary |
| `KB/KB_01_architecture.md` | Updated consolidation, DMN, memory sections |
| `state/roadmap.json` | Added D-022-D-025 |

---

### SESSION 2026-02-21 (#19) - T-S18 Deploy + T-P9 AGENTS.md + T-P10 DMN Observability

**STATUS:** DONE

**What was done:**

1. **T-S18 Deploy (DB cleanup + restart + verify):**
   - Deleted 100 junk consolidation memories (truncated fragments from BUG-001). Kept 1 legitimate 289-char narrative. Deleted 456 orphaned `memory_supersedes` rows.
   - BUG-001 verified fixed (removed `max_output_tokens` cap).
   - Rebuilt brain, verified 16 new full-length consolidation memories (124-369 chars).

2. **T-P9: AGENTS.md updates (D-020 + tag leak fix):**
   - Added "Web Search (CRITICAL)" section: brain memory trusted, LLM training data not, max 3 searches/turn, verify facts from training knowledge.
   - Added "Reply Tags (DO NOT OUTPUT)" section: never echo `[[reply_to_current]]` or `[[ ... ]]` directives.
   - Synced `openclaw-workspace/AGENTS.md` → `openclaw-config/AGENTS.md`.

3. **T-P10: DMN Observability:**
   - Added `dmn_log` table to `schema.sql` (id, agent_id, thought, channel, source_memory_id, created_at + index).
   - Modified `idle.py` `_queue_thought()` to INSERT into `dmn_log` after queueing (non-blocking, try/except wrapped).
   - Added `GET /monologue/{agent_id}?limit=50` endpoint to `api.py`: unified reverse-chronological view combining dmn_log (thoughts), consolidation_log (operations), memories (tension/narrative/reflection), and rumination state (active thread + recent completed).
   - Added `monologue` tool to OpenClaw plugin (`index.ts`): calls `/monologue/{agent_id}`, formats entries as timeline.
   - Verified: endpoint returns unified view, dmn_log table persisting thoughts (16 rows within first minute).

| File | What was done |
|------|---------------|
| `openclaw-workspace/AGENTS.md` | D-020 web search rules, reply tag leak fix |
| `openclaw-config/AGENTS.md` | Synced with workspace |
| `brain/src/schema.sql` | Added `dmn_log` table + index |
| `brain/src/idle.py` | Persist thoughts to `dmn_log` on generation |
| `brain/src/api.py` | Added `GET /monologue/{agent_id}` endpoint + response models |
| `openclaw/extensions/memory-brain/index.ts` | Added `monologue` tool + `brainGetMonologue()` HTTP client |
| `state/devlog.ndjson` | 6 entries (T-S18 deploy, T-P9, T-P10, KB update, verification, handoff) |
| `state/handoff.md` | Session 19 expanded, tasks/blockers updated |
| `state/roadmap.json` | T-S18→done, T-P9→done, T-P10→done |
| `KB/KB_01_architecture.md` | Updated DMN (Phase 6) + OpenClaw runtime sections |

---

## What is this project?

BotBot bolts the intuitive-AI cognitive architecture (memory with Beta-distributed weights, gut feeling, consolidation, DMN/idle loop, safety monitoring) onto OpenClaw as a Python sidecar brain service, giving OpenClaw agents a real mind that develops identity from experience.

---

## Tasks DOING now

| Task ID | Status |
|---------|--------|
| D-014 | TODO — DMN touch_memory (quick fix, deferred — D-024 fallback addresses cold start first) |
| T-P11 | TODO — Proactive notification system (urgency/importance scoring + Telegram delivery) |

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
- **Last commit:** ef4031b Sessions 14-15: Gate hunger curve, plugin capture improvements, custom AGENTS.md
- **Modified (tracked):** KB/KB_01_architecture.md, brain/src/api.py, brain/src/consolidation.py, brain/src/context_assembly.py, brain/src/gate.py, brain/src/idle.py, brain/src/llm.py, brain/src/memory.py, brain/src/schema.sql, openclaw-workspace/AGENTS.md, openclaw/extensions/memory-brain/index.ts, state/devlog.ndjson, state/handoff.md, state/roadmap.json
- **New (untracked):** openclaw-workspace/.openclaw/, openclaw-workspace/BOOTSTRAP.md, etc.

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-21T22:30:00+02:00 | Session 19 complete (T-S18 deploy + T-P9 + T-P10) | DB cleanup, AGENTS.md updated (D-020 + tag leak), DMN observability (dmn_log table + /monologue endpoint + plugin tool). | Next: D-014 (DMN touch_memory), identity architecture rework (D-015/D-017/D-018a-d), T-P11 (notifications).
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
