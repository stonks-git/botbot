# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-21 (#10) - Phase 7: Safety Monitor

**STATUS:** DONE

**What was done:**
1. Created `brain/src/safety.py` — standalone safety module (no brain module deps, math+logging+uuid only):
   - SafetyEvent dataclass + module-level `_audit_log` (max 1000), `log_safety_event()`, `get_audit_log()`
   - Phase A (always): HardCeiling (MAX_CENTER=0.95, MAX_GOAL_BUDGET_FRACTION=0.40) + DiminishingReturns (gain / log2(evidence))
   - Phase B (consolidation): RateLimiter (MAX_CHANGE_PER_CYCLE=0.10) + TwoGateGuardrail (evidence quality + 50 changes/cycle)
   - Phase C (mature): EntropyMonitor (ENTROPY_FLOOR=2.0 bits, 20-bin histogram) + CircuitBreaker (MAX_CONSECUTIVE=5)
   - SafetyMonitor coordinator: synchronous `check_weight_change()` -> (allowed, adj_alpha, adj_beta, reasons)
   - OutcomeTracker: gate_decision/promotion/demotion recording, forward-linkable, max 2000
2. Wired into `api.py`: SafetyMonitor created in lifespan, assigned to `_memory_store.safety`, 2 new endpoints (GET /safety/status, GET /safety/audit), version 0.6.0
3. Wired into `consolidation.py`: `_deep_cycle()` calls `safety.enable_phase_b()` at start, `safety.end_consolidation_cycle(cycle_id)` in finally block

---

### SESSION 2026-02-21 (#11) - Phase 8: Bootstrap Readiness

**STATUS:** DONE

**What was done:**
1. Created `brain/src/bootstrap.py` — stateless milestone checker (no background task):
   - `BOOTSTRAP_PROMPT` constant for newborn agents
   - `Milestone` dataclass (name, description, achieved, achieved_at)
   - `BootstrapReadiness(pool)` with 10 DB-direct milestone checks via `pool.fetchval()`
   - `check_all(agent_id)` returns full status dict; `_render_status()` for text display
2. Wired into `api.py`: import + global + lifespan init, `BootstrapStatusResponse` Pydantic model, `GET /bootstrap/status?agent_id=X` endpoint, version 0.7.0

---

### SESSION 2026-02-21 (#12) - Integration Testing + LLM Switch

**STATUS:** DONE

**What was done:**
1. Inlined openclaw from git submodule to regular files (repo now self-contained, no upstream dependency)
2. Docker compose up — postgres + brain services verified healthy
3. Fixed Dockerfile bug: `/app/state` dir owned by root, brain user (UID 1000) got PermissionError → added `mkdir + chown` before USER switch
4. Switched LLM backend from Anthropic Haiku to Gemini 3 Flash Preview (D-006: Max subscription OAuth tokens restricted to Claude Code/Claude.ai per Anthropic policy)
5. Removed `anthropic` from requirements.txt, removed `ANTHROPIC_API_KEY` from docker-compose.yml
6. Full integration test: all 19/19 endpoints return 200

**Verifications:**
- All 19 endpoints tested via curl, all return 200
- Store→retrieve→gate→assemble full cycle works
- Consolidation deep cycle runs with Gemini Flash (HTTP 200 to generativelanguage.googleapis.com)
- Safety, DMN, bootstrap all responding correctly
- Gate correctly buffers trivial content ("The weather is nice today" → buffer)
- Bootstrap shows 0/10 milestones for new agent with bootstrap prompt

| File | What was done |
|------|---------------|
| `brain/src/llm.py` | Rewritten: Anthropic → Google Gemini 3 Flash Preview via google-genai SDK |
| `brain/requirements.txt` | Removed anthropic dependency |
| `brain/Dockerfile` | Fixed: mkdir + chown /app/state before USER switch |
| `docker-compose.yml` | Removed ANTHROPIC_API_KEY env var |
| `KB/KB_01_architecture.md` | Updated: LLM backend, DJ-002, integration status |

---

## What is this project?

BotBot bolts the intuitive-AI cognitive architecture (memory with Beta-distributed weights, gut feeling, consolidation, DMN/idle loop, safety monitoring) onto OpenClaw as a Python sidecar brain service, giving OpenClaw agents a real mind that develops identity from experience.

---

## Tasks DOING now

| Task ID | Status |
|---------|--------|
| OpenClaw agent setup | NEXT — wire OpenClaw runtime + memory-brain plugin |

## Blockers or open questions

| Blocker/Question | Status |
|------------------|--------|
| GOOGLE_API_KEY needed for embeddings | RESOLVED — key present in env, Gemini client initializes |
| halfvec deserialization from asyncpg | RESOLVED — using `embedding::float4[]` cast in SQL returns Python list, then np.array() |
| Anthropic OAuth for Max subscription | RESOLVED — policy prohibits non-Claude-Code use, switched to Gemini Flash (D-006) |

---

## Git Status

- **Branch:** main
- **Last commit:** cab2a7c Integration testing + LLM switch to Gemini 3 Flash Preview
- **Modified (tracked):** none (clean)
- **New (untracked):** .env, brain-state/

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-21T23:00:00+02:00 | Session 12 DONE | Brain 19/19 endpoints pass, LLM=gemini-3-flash-preview, repo self-contained | Next: OpenClaw agent runtime setup + memory-brain plugin wiring
```

---

## Next Session Bootstrap

1. Read `KB/blueprints/v0.3_current_state.md` — **the single source of truth** for project state
2. All brain phases (0-8) complete + integration tested — 19/19 endpoints pass
3. LLM is `gemini-3-flash-preview` (D-006), single `GOOGLE_API_KEY` for embed + LLM
4. Repo is self-contained (openclaw inlined, no submodule)
5. `docker compose up -d` starts postgres + brain on `:8400`

### Next phase: OpenClaw Agent Runtime

**Goal:** Get an actual agent running that uses the brain.

**What exists:**
- `openclaw/` — full OpenClaw source inlined in repo
- `openclaw/extensions/memory-brain/index.ts` — plugin with all brain HTTP clients, tools, and hooks already coded
- Plugin has: `before_agent_start` (context assembly + attention + DMN activity), `agent_end` (auto-capture via gate), 8 tools (memory_recall, memory_store, memory_forget, introspect, gut_check, consolidation_status, consolidation_trigger, dmn_status)

**What needs to happen:**
1. **Understand OpenClaw's setup** — how to configure and run an agent (likely `docker-compose` or direct Node.js)
2. **Add OpenClaw service to docker-compose.yml** — third container alongside postgres + brain
3. **Register the memory-brain plugin** — OpenClaw needs to know about the extension
4. **Configure the agent** — which LLM it uses for conversations (separate from brain's Gemini Flash), system prompt, personality
5. **Wire bootstrap prompt injection** — newborn agents get BOOTSTRAP_PROMPT from `/bootstrap/status`
6. **Test end-to-end** — send a message to the agent, verify memories are stored, identity forms, gut responds
7. **Create `.env.example`** — document required env vars for new users
8. **Add `.env` and `brain-state/` to `.gitignore`**

**Key files to explore:**
- `openclaw/docker-compose.yml` — OpenClaw's own compose (may need merging)
- `openclaw/Dockerfile` — how OpenClaw builds
- `openclaw/extensions/` — how extensions are registered
- `openclaw/CLAUDE.md` or `openclaw/docs/` — setup documentation

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
