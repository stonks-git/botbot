# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-18/19 (#5+6) - Phase 3: Context Assembly + Unified Memory Rework

**STATUS:** DONE

**What was done:**
1. Initially built layers.py (L0/L1 JSON), context_assembly.py, api.py endpoints, plugin tools
2. User feedback: identity should emerge from unified memory weights, not L0/L1 files (D-005)
3. Created v0.2 rework blueprint documenting exhaustive modification plan
4. Executed rework:
   - DELETED brain/src/layers.py
   - Rewrote context_assembly.py: removed `layers` param, added `render_identity_hash()` and `render_identity_full()` querying top-N memories by weight center from DB
   - Rewrote api.py: removed LayerStore imports/cache/helper, added `_get_identity_embeddings()` (DB query with `embedding::float4[]` cast), removed PUT /identity endpoint + IdentityUpdateRequest model, rewired gate to use DB identity embeddings
   - Updated plugin: removed identity_update tool + brainUpdateIdentity(), updated introspect description

**Verifications:**
- Python syntax check passes for api.py, context_assembly.py
- No LayerStore/layers.py/identity_update references in codebase (only historical comments)
- layers.py file deleted from disk
- Gate wiring: `_get_identity_embeddings()` queries top-N memories by weight center, passes to ExitGate
- Empty DB: returns None → gate falls back to "peripheral" (same pre-Phase 3 behavior)

| File | What was done |
|------|---------------|
| `brain/src/layers.py` | DELETED (D-005: unified memory) |
| `brain/src/context_assembly.py` | Rewritten: removed layers param, added render_identity_hash/full from DB, 3-track assembly unchanged |
| `brain/src/api.py` | Rewritten: _get_identity_embeddings from DB, removed LayerStore/PUT identity, rewired gate, version 0.2.0 |
| `brain/src/gate.py` | Minor: updated comment (LayerStore → top-N identity memory embeddings from DB) |
| `openclaw/extensions/memory-brain/index.ts` | Removed identity_update tool + brainUpdateIdentity(), updated header + introspect description |
| `KB/blueprints/v0.2_unified_memory_rework.md` | New: exhaustive rework plan (created prior session) |
| `state/roadmap.json` | T-P3 status doing → done, D-005 decision added |

---

### SESSION 2026-02-19 (#7) - Phase 4: Gut Feeling

**STATUS:** DONE

**What was done:**
1. Created `brain/src/gut.py` with two-centroid emotional model (D-005 adapted)
2. Wired gut into api.py (gate + context assembly + 2 new endpoints, version 0.3.0)
3. Updated context_assembly.py + plugin (gut_check tool)

| File | What was done |
|------|---------------|
| `brain/src/gut.py` | New: GutFeeling (EMA attention, DB-weighted subconscious, GutDelta, persistence) |
| `brain/src/api.py` | Updated: gut cache, new endpoints, gate wiring, context assembly wiring, version 0.3.0 |
| `brain/src/context_assembly.py` | Updated: [COGNITIVE STATE] section header |
| `openclaw/extensions/memory-brain/index.ts` | Updated: gut_check tool, brainGetGutState/brainUpdateAttention clients |

---

### SESSION 2026-02-19 (#8) - Phase 5: Consolidation Engine

**STATUS:** DONE

**What was done:**
1. Created `brain/src/llm.py` -- Anthropic Claude wrapper (claude-haiku-4-5) with retry_llm_call
2. Created `brain/src/consolidation.py` -- full consolidation engine:
   - ConstantConsolidation (Tier 1): _decay_tick (5min, beta+=0.01 for stale), _contradiction_scan (10min, LLM pair check, store tensions), _pattern_detection (15min, greedy cosine clustering)
   - DeepConsolidation (Tier 2): _merge_and_insight (LLM questions+insights+narratives), _promote_patterns (D-005 direct SQL alpha updates), _decay_and_reconsolidate (beta+=1.0 for 90d stale + insight revalidation), _tune_parameters (Shannon entropy), _contextual_retrieval (WHO/WHEN/WHY preambles + re-embed)
   - ConsolidationEngine wrapper (asyncio.gather both tiers, trigger + status)
   - Error isolation per operation, multi-agent via SELECT DISTINCT agent_id
3. Wired into `api.py`: background task in lifespan, GET /consolidation/status, POST /consolidation/trigger, version 0.4.0
4. Updated plugin: consolidation_status + consolidation_trigger tools + HTTP clients

**Verifications:**
- Python syntax check passes for llm.py, consolidation.py, api.py
- No LayerStore/layers.py code references (D-005)
- No TODO/placeholder in new code
- 9 consolidation_log writes, 2 store_insight calls (memory_supersedes linking)

| File | What was done |
|------|---------------|
| `brain/src/llm.py` | New: Anthropic Claude wrapper with retry |
| `brain/src/consolidation.py` | New: Tier 1 + Tier 2 consolidation engine |
| `brain/src/api.py` | Updated: consolidation background task, 2 new endpoints, version 0.4.0 |
| `openclaw/extensions/memory-brain/index.ts` | Updated: consolidation_status + consolidation_trigger tools |

**Post-implementation audit (5 fixes):**
- v0.3 API Surface: added 4 missing endpoints (context/attention, gut/{agent_id}, consolidation/status, consolidation/trigger)
- v0.3 + KB_01 dependency graph: added asyncpg to consolidation.py deps
- consolidation_log count: corrected 10 to 9 across handoff, devlog, KB_01, v0.3
- roadmap T-P4 deliverable: corrected `GET /gut/state` to `GET /gut/{agent_id}`
- roadmap Q-001 status: corrected `open` to `answered`

---

## What is this project?

BotBot bolts the intuitive-AI cognitive architecture (memory with Beta-distributed weights, gut feeling, consolidation, DMN/idle loop, safety monitoring) onto OpenClaw as a Python sidecar brain service, giving OpenClaw agents a real mind that develops identity from experience.

---

## Tasks DOING now

| Task ID | Status |
|---------|--------|
| T-P6 | NEXT — DMN / Idle Loop |
| T-P7 | AFTER — Safety Monitor |

## Blockers or open questions

| Blocker/Question | Status |
|------------------|--------|
| GOOGLE_API_KEY needed for embeddings | RESOLVED — key present in env, Gemini client initializes |
| halfvec deserialization from asyncpg | RESOLVED — using `embedding::float4[]` cast in SQL returns Python list, then np.array() |

---

## Git Status

- **Branch:** main
- **Last commit:** 446036c Phase 1: Memory Core — store, retrieve, embed, plugin
- **Modified (tracked):** KB/KB_01_architecture.md, KB/KB_index.md, KB/blueprints/BLUEPRINT_INDEX.md, KB/blueprints/v0.1_brain_integration_plan.md, brain/src/api.py, state/devlog.ndjson, state/handoff.md, state/roadmap.json
- **New (untracked):** brain/src/llm.py, brain/src/consolidation.py, brain/src/context_assembly.py, brain/src/gate.py, brain/src/gut.py, KB/blueprints/v0.2_unified_memory_rework.md, KB/blueprints/v0.3_current_state.md

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-19T21:30:00+02:00 | T-P5 DONE | llm.py + consolidation.py created, api.py wired, plugin updated | Next: T-P6 DMN/Idle Loop
```

---

## Next Session Bootstrap

1. Read `KB/blueprints/v0.3_current_state.md` — **the single source of truth** for project state + what's next
2. Read `KB/KB_02_intuitive_ai_reference.md` idle + rumination sections — source reference for DMN port
3. Implement Phase 6 (DMN / Idle Loop): `brain/src/idle.py` + `brain/src/rumination.py` + `brain/src/dmn_store.py`
4. 4 sampling channels: neglected (35%), tension (20%), temporal (20%), introspective (25%)
5. Plugin: background poll every 30s, queue as self-prompts when idle

---

## Before updating this file

- [x] devlog entry added for each change
- [x] Session section filled (what was done, verifications, files touched)
- [x] **KB updated** if code was modified + `kb_update` devlog entry
- [x] `python3 taskmaster.py validate` exits 0
- [x] Keep only last 3 sessions (older ones archived in git)
