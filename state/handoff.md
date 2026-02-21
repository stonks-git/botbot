# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-22 (#13) - OpenClaw Agent Runtime Setup

**STATUS:** DONE (see git history for details)

---

### SESSION 2026-02-21 (#14) - E2E Test + Hunger Curve

**STATUS:** DONE

**What was done:**
1. **E2E test (partial)**: WebChat connected (gateway token via `?token=botbot-dev` URL param + device pairing approved via `docker compose exec openclaw node dist/index.js devices approve <id>`). Agent responded via Gemini Flash. Context assembly confirmed: `[IDENTITY] bootstrapping`, `[COGNITIVE STATE] gut no signal`. memory-brain plugin fires `/context/assemble` + `/memory/gate` on each turn.
2. **Cold-start bug found**: Gate scored ALL messages as `peripheral×novel → buffer (score=0.27)` for newborn agent. With 0 memories, no identity embeddings exist → s_i defaults to 0.35, base_score=0.40, final = 0.40*(0.5+0.5*0.35) = 0.27 — always below persist threshold. Agent could never form its first memory.
3. **Hunger curve (D-009)**: Dynamic gate threshold based on memory count. `ExitGate._hunger_boost()` returns exponential decay from 2.5x (0 memories) to 1.0x (~30+ memories). Score multiplied by hunger boost. BUFFER promoted to PERSIST when boosted score ≥ 0.5 and hunger > 1.05.
4. **Tested decay**: 5 sequential stores confirmed hunger tapering: 2.50 → 2.36 → 2.23 → 2.11 → 2.01. All persisted. Cleaned test data, restarted openclaw.

---

### SESSION 2026-02-21 (#15) - Gate Input Preparation + System Prompt Discovery

**STATUS:** DONE

**What was done:**
1. **Pre-gate chunking**: Added `chunkText(text, maxChars)` to memory-brain plugin — splits long messages by paragraph (`\n\n`) then sentence boundaries (`.`/`!`/`?`), greedy-merges into chunks up to `captureMaxChars` (default 500). Replaces the hard `text.length > maxChars` drop in `shouldCapture()`.
2. **Assistant message capture**: Expanded `agent_end` hook from `role === "user"` only to also capture `role === "assistant"` messages. User messages tagged `source_tag="auto_capture"`, assistant messages tagged `source_tag="self_reflection"`. Enables identity formation from agent's own output.
3. **Gate call budget**: Replaced `toCapture.slice(0, 3)` with `MAX_GATE_CALLS = 10` total cap across all messages (user + assistant chunks). Logging shows breakdown: `gated 7/8 chunks (3 user, 5 self)`.
4. **Verified in Docker**: Rebuilt openclaw container, confirmed plugin loaded. Brain logs show 3 gate calls from a single WebChat exchange, all persisted. Hunger tapering confirmed (2.11 → 2.01 → 1.91, memories 3→4→5).
5. **Discovered OpenClaw default AGENTS.md conflict**: Agent's system prompt includes OpenClaw's built-in `AGENTS.default.md` which instructs it to use `SOUL.md`, `MEMORY.md`, `memory/` dir — contradicting the brain-based memory system. `writeFileIfMissing()` in `workspace.ts:309` recreates default if missing. Must provide custom `AGENTS.md` in workspace. Added T-P9 to roadmap.
6. **DMN observability gap identified**: Agent's inner monologue (DMN thoughts, rumination threads, consolidation insights) scattered across ephemeral queues, disk JSON files, and DB rows with no unified view. Added T-P10 to roadmap.
7. **`[[reply_to_current]]` tag still leaking** in agent responses — included in T-P9 scope.

**Decisions:**
- D-010: OpenClaw default AGENTS.md must be replaced with brain-aware version
- D-011: DMN observability needed — unified view of agent inner monologue

**Verifications:**
- `docker compose build openclaw` — clean build, no TypeScript errors
- Plugin registered: `memory-brain: started (brain: http://brain:8400, agent: default, recall: true, capture: true)`
- Gate calls confirmed in brain logs with hunger tapering
- Brain health: 12 memories, 3 agents, DMN running (11 heartbeats for default), consolidation active (found first tension memory)
- Gut: attention_count=4, no subconscious yet (needs more high-weight memories)

| File | What was done |
|------|---------------|
| `openclaw/extensions/memory-brain/index.ts` | `shouldCapture()` removed hard cap, added `chunkText()`, expanded `agent_end` to capture user+assistant with source_tags, 10-call budget |
| `openclaw/extensions/memory-brain/openclaw.plugin.json` | Updated descriptions: autoCapture (user+assistant), captureMaxChars (chunk target size) |
| `KB/KB_01_architecture.md` | Updated Phase 1 plugin config + Phase 2 gate plugin section with chunking/capture/budget docs |
| `state/roadmap.json` | Added T-P9 (replace AGENTS.md) and T-P10 (DMN observability) |

---

## What is this project?

BotBot bolts the intuitive-AI cognitive architecture (memory with Beta-distributed weights, gut feeling, consolidation, DMN/idle loop, safety monitoring) onto OpenClaw as a Python sidecar brain service, giving OpenClaw agents a real mind that develops identity from experience.

---

## Tasks DOING now

| Task ID | Status |
|---------|--------|
| T-P9 | TODO — replace OpenClaw default AGENTS.md + fix `[[reply_to_current]]` tag leak |
| T-P10 | TODO — DMN observability: unified view of agent inner monologue |
| E2E test | IN PROGRESS — gate input prep done, need full pipeline verification with new capture |

## Blockers or open questions

| Blocker/Question | Status |
|------------------|--------|
| Gate cold-start: newborn can't form memories | RESOLVED — hunger curve (D-009) |
| Does gate capture agent responses too? | RESOLVED — yes, `source_tag="self_reflection"` (Session 15) |
| OpenClaw default AGENTS.md conflicts with brain | OPEN — T-P9: must provide custom AGENTS.md in workspace |
| `[[reply_to_current]]` tag leaking into agent output | OPEN — T-P9: OpenClaw template tag not being processed |
| Where to see DMN/rumination output? | OPEN — T-P10: scattered across queues, disk, DB. Need unified view |

---

## Git Status

- **Branch:** main
- **Last commit:** 4cd649f Handoff: next phase is OpenClaw agent runtime setup
- **Modified (tracked):** KB/KB_01_architecture.md, KB/blueprints/v0.3_current_state.md, brain/src/api.py, brain/src/gate.py, docker-compose.yml, openclaw/extensions/memory-brain/index.ts, openclaw/extensions/memory-brain/openclaw.plugin.json, state/devlog.ndjson, state/handoff.md, state/roadmap.json
- **New (untracked):** .env.example, .gitignore

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-21T16:30:00+02:00 | Session 15 done | Gate input prep (chunking+assistant capture+budget) verified in docker. OpenClaw AGENTS.md conflict discovered (T-P9). DMN observability gap (T-P10). Brain running: 12 memories, DMN+consolidation active, first tension found. | Next: T-P9 (custom AGENTS.md + [[reply_to_current]] fix), T-P10 (DMN observability), continue e2e testing
```

---

## Next Session Bootstrap

1. Read `KB/blueprints/v0.3_current_state.md` — **the single source of truth** for project state
2. All brain phases (0-8) complete + integration tested — 19/19 endpoints pass
3. **OpenClaw runtime is live** — 3 containers: postgres (:5433) + brain (:8400) + openclaw (:18789)
4. memory-brain plugin active: auto-recall + auto-capture (user+assistant), 8 tools, agentId=default
5. Agent LLM: Gemini 3 Flash Preview (D-007), same `GOOGLE_API_KEY` as brain (D-006)
6. Agent is newborn, no preset identity (D-008) — bootstrap milestones track maturation
7. `docker compose up -d` starts all 3 services. WebChat at `http://localhost:18789/?token=botbot-dev`
8. **Hunger curve active (D-009)**: newborn agent gets 2.5x gate score boost, can now form memories
9. **Gate input prep (Session 15)**: pre-gate chunking, assistant capture with `source_tag="self_reflection"`, 10-call budget per turn

### Priority Next Tasks

1. **T-P9: Replace AGENTS.md** — Create custom `AGENTS.md` in `openclaw-config/agents/main/agent/` (or workspace dir) that removes file-based memory instructions. Also fix `[[reply_to_current]]` tag leak. The default is at `openclaw/docs/reference/AGENTS.default.md`, injected by `workspace.ts:309 writeFileIfMissing()`.

2. **T-P10: DMN Observability** — Build unified view of agent inner monologue. Check: `rumination_state.json` on disk, `source_tag='internal_dmn'` in memories table, `consolidation_log` table, ephemeral thought queue via `/dmn/thoughts`.

3. **Continue E2E** — Full pipeline: chat → gate captures (with chunking) → memories form → bootstrap milestones unlock → identity emerges.

### Live Brain State (as of Session 15 end)

- 12 memories total (3 agents: default, test-agent, test)
- Default agent: ~6 memories, hunger ~1.91 (tapering from 2.5)
- DMN: 11 heartbeats for default, no thoughts generated yet (needs more memories + idle time)
- Consolidation: Tier 1 running (decay/contradiction/pattern), found first tension memory for default
- Gut: attention_count=4, no subconscious centroid yet, emotional_charge=0.0

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
