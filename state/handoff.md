# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-23 (#42) - Deploy BUG-003, JSONB Codec Fix, Dedup Prompt Rewrite

**STATUS:** DONE

**What was done:**
1. **Deployed BUG-003 fix:** Applied migration 004 (survivor_label column), rebuilt brain+openclaw containers. All 3 services healthy.
2. **BUG-004 (asyncpg JSONB codec):** Dedup sweep hit DataError — asyncpg doesn't auto-encode dicts for JSONB without registered codecs. Fixed: registered `json`/`jsonb` type codecs via `set_type_codec` on pool `init` callback in `db.py`. Removed all manual `json.dumps()` wrappers for JSONB params across memory.py (store_memory, scratch_buffer, archive_memory), notification.py (enqueue), idle.py (3 consolidation_log inserts). Supersedes BUG-002 workaround.
3. **Dedup prompt rewrite:** Old prompt caused LLM to default "synthesize" 80%+ without text. Rewrote with explicit structured options and "YOU MUST write merged text" instruction. LLM now correctly picks A/B for near-duplicates.
4. **Synthesis caching:** Added `synthesis TEXT` column to `dedup_verdicts`. Cache path returned `synthesis: None` always — LLM synthesis text was lost on re-lookup. Now stored on INSERT, returned from cache.
5. **POC verified:** 13 pairs deduped live — 10 correct A/B picks (new prompt), 3 old-cache fallbacks. All archived with weight transfer. No errors.
6. **DB wipe (D-035):** 490 active memories with 75,327 duplicate pairs at >= 0.85 similarity. DMN feedback loop created massive identity paraphrase wall. Incremental dedup impractical (~100h of LLM calls). Deleted all default agent data. Brain+OpenClaw stopped.

**Files modified:** brain/src/db.py, brain/src/memory.py, brain/src/notification.py, brain/src/idle.py, brain/src/consolidation.py
**Next:** T-D034 (context leak: prependContext → systemPrompt + strip prefix from auto-capture). Fresh agent restart.

### SESSION 2026-02-23 (#41) - BUG-003 Fix: Dedup Engine + Retroactive Sweep Endpoint

**STATUS:** DONE (code complete, deploy + sweep pending)

**What was done:**
1. **Bug A fix (synthesis fallback):** `execute_dedup_verdict()` now has a fallback path: when LLM says "synthesize" but provides no text (or unknown label), queries both memories for `weight_center`, picks higher as survivor, archives loser with weight transfer. File: `memory.py:949-979`.
2. **Bug B fix (cache dict):** `dedup_pair()` cache-hit SELECT now fetches `survivor_label, reason, mem_a_id, mem_b_id`. Reconstructs full verdict dict with all keys `execute_dedup_verdict` needs (`survivor`, `loser_id`, `mem_a_id`, `mem_b_id`, `reason`, `synthesis`). Backward-compatible: old NULL `survivor_label` rows derived from `survivor_id` ("A"/"B"), or left None for "synthesize" case (hits BUG-003 fallback). File: `consolidation.py:110-143`.
3. **Schema + migration:** Added `survivor_label TEXT` to `dedup_verdicts` CREATE TABLE + idempotent DO $$ ALTER TABLE block in `schema.sql`. Migration `004_survivor_label.sql`. INSERT in `dedup_pair` now stores `survivor_label` as 7th column.
4. **Retroactive sweep endpoint:** `POST /consolidation/dedup-sweep` — pgvector self-join finds high-similarity pairs (`1 - cosine_distance >= threshold`), runs `dedup_pair` + `execute_dedup_verdict` on each. `dry_run=true` default. Request/response models with pair count, redundant/archived/distinct/error counts.
5. **Docker build verified:** `docker compose build brain` succeeds. All 3 modified .py files pass `py_compile`.

**Files modified:** brain/src/consolidation.py, brain/src/memory.py, brain/src/api.py, brain/src/schema.sql
**Files created:** brain/migrations/004_survivor_label.sql
**Next:** Deploy (apply migration 004, rebuild containers), run dry sweep, then live sweep. Then T-D034.

### SESSION 2026-02-23 (#40) - Investigation + D-033 Priority Injection Bypass

**STATUS:** DONE

**What was done:**
1. **Memory DB investigation:** Queried live DB. 421 memories (0 archived), 311 (74%) are identity navel-gazing. 210 consolidation insights, nearly 1:1 with source memories. Top 30 by weight: all identity paraphrases. DMN self-referential feedback loop identified.
2. **BUG-003 found: Dedup broken.** 10 dedup verdicts all "redundant", 0 archives executed. Bug A: 8/10 verdicts chose "synthesize" but LLM didn't provide synthesis text → `execute_dedup_verdict` falls through. Bug B: `dedup_verdicts` cache returns only `verdict` + `survivor_id`, missing `survivor` label → `execute_dedup_verdict` can't determine A/B/synthesize code path on cache hits.
3. **Context leak found:** `prependContext` in OpenClaw is prepended to the USER message (`effectivePrompt`), not system prompt. [ACTIVE IDENTITY] etc. visible in chat. Auto-capture picks up context prefix → `mem_4bda25fd9c01` content starts with `[ACTIVE IDENTITY]`.
4. **D-033: Priority injection bypass.** Memories with importance >= 0.95 force-injected into [RELEVANT MEMORIES] regardless of w×s scoring. One-shot: importance reset to 0.5 after injection. Covers fired reminders (importance=1.0). Added `PRIORITY_IMPORTANCE_THRESHOLD` to config.py.

**Files modified:** brain/src/config.py, brain/src/context_assembly.py
**Next:** Fix BUG-003 (dedup) + retroactive sweep of 421 memories

### SESSION 2026-02-23 (#39) - Plan 202602221300: Always-On Gut + Decay Protection (All 5 Phases)

**STATUS:** DONE

**What was done:**
All 5 phases of plan `202602221300-always-on-gut-and-decay-protection.md` implemented. Plan status=done.
- **Phase 1 (Always-on gut feeding):** Moved `get_identity_embeddings()` to MemoryStore (D-030). Added `_feed_gut()` to IdleLoop (embed → update_attention → update_subconscious → compute_delta → save, exception-safe). Wired into `_heartbeat()` with `sampled_content` tracking across all 3 branches (random pop, continue thread, new thread).
- **Phase 2 (Decay protection schema):** Added `protect_until TIMESTAMPTZ` column to memories + partial index + migration 003. Wired through `store_memory` ($19), `GateRequest`, `gate_memory` extra_kw (first chunk only).
- **Phase 3 (Decay exclusion):** Both `_decay_tick` (consolidation.py) and `get_stale_memories` (memory.py) WHERE clauses now skip memories with active `protect_until`.
- **Phase 4 (Protection expiration + LLM re-eval):** Added `_check_expired_protections()` — queries expired protections (LIMIT 5), runs context assembly + LLM, extends (capped 90d) or releases, audit trail in consolidation_log. Wired into `run()` via `_safe_run_global`.
- **Phase 5 (Plugin upgrade):** `brainGate()` gets `protectUntil` param. `memory_store` tool schema, destructure, brainGate call, and success message all updated.

- **D-032 (Threshold removal + milestones):** Empirical analysis at 391 memories showed top-20 optimal (best discriminative gap 0.036, centroid 99.2% stable). Removed `> 0.3` threshold from `get_identity_embeddings`. Added `_check_memory_milestones()` to idle loop — notifies at 1k/5k/10k to re-run analysis. Verified: `emotional_charge=0.245`, `has_subconscious=true`.

**Files modified:** brain/src/memory.py, brain/src/api.py, brain/src/idle.py, brain/src/schema.sql, brain/src/consolidation.py, openclaw/extensions/memory-brain/index.ts, state/plans/202602221300-always-on-gut-and-decay-protection.md
**Files created:** brain/migrations/003_protect_until.sql

---

## What is this project?

BotBot bolts the intuitive-AI cognitive architecture (memory with Beta-distributed weights, gut feeling, consolidation, DMN/idle loop, safety monitoring) onto OpenClaw as a Python sidecar brain service, giving OpenClaw agents a real mind that develops identity from experience.

---

## Tasks DOING now

| Task ID | Status |
|---------|--------|
| D-033 | DONE — Priority injection bypass (config.py + context_assembly.py) |
| T-BUG3 | DONE — Dedup fix deployed, prompt rewrite, JSONB codec fix, 10 pairs deduped live |
| T-D034 | OPEN — Context leak: move prependContext → systemPrompt + strip context prefix from auto-capture |

### Next: Dedup Fix + Retroactive Sweep (BUG-003)

**Problem:** 10 dedup verdicts returned "redundant", 0 archives executed. Two bugs:
- **Bug A (synthesis fallback):** 8/10 LLM verdicts chose "synthesize" without providing text → code falls through
- **Bug B (cache dict):** dedup_verdicts table lacks `survivor` label column → cache hits return incomplete dict

**Fix plan:**
1. Add `survivor_label TEXT` column to dedup_verdicts table + migration 004
2. Store survivor label when recording verdict
3. On cache hit, reconstruct full dict (loser_id derivable from mem_a_id + mem_b_id + survivor_id)
4. When synthesis requested but text missing, fall back to picking higher weight_center as survivor
5. Run retroactive batch dedup sweep on existing 421 memories (cluster by cosine_sim >= 0.75, run dedup_pair on each cluster)

### Next: Context Leak Fix (D-034)

**Problem:** `prependContext` in OpenClaw prepends to user message, not system prompt. Context visible in chat + leaks into auto-capture.

**Fix plan:**
1. Change plugin to use `systemPrompt` hook field instead of `prependContext`
2. Add context prefix stripping to auto-capture (strip everything before first user message)

### Completed Plans (summary)

- Plan `202602221300` (always-on gut + decay protection) — all 5 phases done + D-032
- Plan `202602221100` (memory dedup + conscious store) — all 5 phases done
- T-B01 through T-P17: All DONE

### Previous Completed Plans

- Plan `202602221100` (memory dedup + conscious store) — all 5 phases done
- T-B01 through T-P11: All DONE. See git log for details.

### Previous completed tasks (summary)

T-B01 through T-P11: All DONE. See git log for details. Key: w×s identity scoring (D-015), semantic chunking (D-018b/c), HDBSCAN patterns (D-021), research sessions (D-016), notifications (D-019), adaptive threshold (D-018a), injection logging (D-018d), code hygiene (11 CQ items).

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
| Gut subconscious empty for newborn agents | RESOLVED — D-032: removed 0.3 threshold, top-20 weighted average. Verified: emotional_charge=0.245 |
| BUG-003: Dedup broken (0 archives) | FIXED + DEPLOYED — Bug A: synthesis fallback. Bug B: cache dict. Bug C: synthesis caching. Bug D: prompt rewrite. BUG-004: JSONB codec. 10 pairs deduped live, verified. |
| Identity paraphrase wall (74% navel-gazing) | RESOLVED — DB wiped (D-035). 75k dup pairs made sweep impractical. Agent starts fresh with working dedup-on-gate. |
| Context prefix leaks into auto-capture | OPEN — D-034: prependContext goes into user message. `[ACTIVE IDENTITY]` stored as memory content. |
| DMN self-referential feedback loop | OPEN — 74% identity memories → DMN samples identity → consolidation creates identity insights → loop. Need novelty gate on consolidation insight creation. |
| Reminder not surfacing in context | RESOLVED — D-033: priority injection bypass (importance >= 0.95 → force-inject, one-shot). |

---

## Git Status

- **Branch:** main
- **Last commit:** f994abb Sessions 27-29: Semantic chunking, code hygiene, 4 P3 features
- **Modified (tracked):** brain/src/{api,bootstrap,config,consolidation,context_assembly,db,gate,idle,memory,notification,schema}.py + schema.sql, openclaw/extensions/memory-brain/index.ts, state/{devlog.ndjson,handoff.md,roadmap.json}, KB/KB_01_architecture.md
- **New (untracked):** brain/migrations/ (001-004), openclaw-workspace/, state/plans/
- **Session 42 changes:** brain/src/db.py (JSONB codec), brain/src/memory.py (removed json.dumps), brain/src/notification.py (removed json.dumps), brain/src/idle.py (removed json.dumps), brain/src/consolidation.py (prompt rewrite + synthesis column)

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-23T07:00:00+02:00 | Session 42: BUG-003 deployed, BUG-004 JSONB codec, dedup prompt rewrite, synthesis caching, 13 pairs POC, DB wiped (75k dups), services stopped. | Next: T-D034 (context leak), fresh agent restart.
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
