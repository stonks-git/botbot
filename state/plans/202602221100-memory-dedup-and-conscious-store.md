# PLAN: Memory Dedup, Identity Diversity, and Conscious Store
Status: done
Parent: none
Supersedes: none
Roadmap task: standalone

## Context

After the brain rebuild, testing revealed four interrelated problems:

1. **Identity is a wall of paraphrases.** ACTIVE IDENTITY fills all 20 slots with variations of the same idea ("I value continuous experiment...") because (a) novelty threshold 0.85 doesn't catch semantic paraphrases, (b) consolidation generates new "I value..." narratives that pass the gate, and (c) `score_identity_wxs()` has no diversity enforcement.

2. **Bootstrap greeting leaks into identity.** BOOTSTRAP.md (OpenClaw template) tells the agent to say "Hey. I just came online..." — the agent echoes it, auto-capture stores it as `self_reflection`, and `[[reply_to_current]]` directive tags go with it. Solution: disable BOOTSTRAP.md injection + strip directive tags from auto-capture.

3. **RELEVANT MEMORIES duplicates ACTIVE IDENTITY.** `_get_situational_memories()` doesn't exclude already-injected identity memory IDs. Same memories appear in both sections.

4. **Conscious memory store needs proper mechanics.** The `memory_store` tool works but bypasses the gate, uses wrong source_tag (`external_user`), and has no dedup. Agent should also be able to set scheduled reminders that wake it up.

### Key Design Decisions

- **Two-stage dedup**: Vector similarity (>= 0.75) identifies candidates, LLM arbitrates merge-vs-keep. LLM also selects the best-worded survivor (Option A) or synthesizes a replacement.
- **Triggered dedup**: Runs when similar memories are first seen together, not on a periodic batch. `dedup_verdicts` table tracks already-verified pairs to avoid redundant LLM calls.
- **Soft delete = `archived` flag**: Deterministic exclusion from all queries. Transfers both alpha AND beta from discarded to survivor (preserving weight distribution shape, not inflating alpha artificially). Evidence chains preserved.
- **Conscious store**: `source_tag: "agent_deliberate"`, higher initial weights (alpha=2.0, beta=3.0), two-stage novelty check before storing.
- **Scheduled reminders**: `remind_at` parameter on `memory_store` tool. Due reminders set importance=1.0 (forced context injection), delivered via notification system + DMN idle loop wake-up.

### Files Involved (known from exploration)

| Area | Files |
|------|-------|
| Dedup engine | `brain/src/consolidation.py` (new dedup step), `brain/src/memory.py` (check_novelty, archived filter) |
| Schema | `brain/src/schema.sql` (dedup_verdicts table, archived column, migration) |
| Identity diversity | `brain/src/context_assembly.py` (exclude identity from situational) |
| Gate integration | `brain/src/gate.py` (trigger dedup on high similarity) |
| Config | `brain/src/config.py` (dedup thresholds) |
| Bootstrap fix | `openclaw/extensions/memory-brain/index.ts` (strip tags, conscious store, remind_at) |
| Bootstrap disable | `openclaw/src/agents/workspace.ts` or `attempt.ts` (skip BOOTSTRAP.md) |
| Reminders | `brain/src/idle.py` (check due reminders), `brain/src/notification.py` (delivery), `brain/src/api.py` (new endpoint) |

## Phase 1: Schema + Archived Flag + Dedup Verdicts Table — DONE

Add the database foundation — `archived` column on memories, `dedup_verdicts` table, and update ALL existing queries to respect `AND NOT archived`.

- [x] **1.1: Schema changes — archived column + dedup_verdicts table**
  Files: `brain/src/schema.sql`
  Do: Add `archived BOOLEAN DEFAULT FALSE` and `archived_reason JSONB` columns to `memories` table. Create `dedup_verdicts` table with columns: `id SERIAL PRIMARY KEY`, `agent_id TEXT NOT NULL`, `mem_a_id TEXT NOT NULL`, `mem_b_id TEXT NOT NULL`, `verdict TEXT NOT NULL` ('redundant'|'distinct'), `survivor_id TEXT`, `reason TEXT`, `created_at TIMESTAMPTZ DEFAULT NOW()`, `UNIQUE (agent_id, mem_a_id, mem_b_id)`. Add index on `memories(archived)` for filtered scans.
  Verify: `schema.sql` parses cleanly. New columns and table defined.

- [x] **1.2: Add `AND NOT archived` to memory.py queries**
  Files: `brain/src/memory.py`
  Do: Add `AND NOT archived` to every SELECT and UPDATE on memories table: `search_similar`, `search_hybrid` (both dense and sparse CTEs), `score_identity_wxs`, `check_novelty`, `get_memory`, `get_random_memory`, `memory_count`, `search_corrections`, `get_stale_memories`, `avg_depth_weight_center`, `apply_retrieval_mutation` (all 3 UPDATE variants + the batch-fetch SELECT), `touch_memory` (subquery + both UPDATE variants), `decay_memories`. Skip `store_memory` (INSERT), `delete_memory` (explicit delete), evidence chain queries (archived memories should still appear in evidence trails).
  Verify: `grep -c 'FROM memories\|UPDATE memories\|JOIN memories' brain/src/memory.py` — count matches. Every SELECT/UPDATE that isn't INSERT or evidence-chain has the filter.

- [x] **1.3: Add `AND NOT archived` to consolidation.py + idle.py + bootstrap.py**
  Files: `brain/src/consolidation.py`, `brain/src/idle.py`, `brain/src/bootstrap.py`
  Do: Add `AND NOT archived` to all SELECT queries in: consolidation.py (`_decay_tick` UPDATE, `_contradiction_scan` SELECT, `_pattern_detection` SELECT, `_merge_and_insight` SELECT, `_cluster_narratives` SELECT, `_promote_patterns` both SELECTs, `_decay_and_reconsolidate` SELECT on insights, `_tune_parameters` SELECT, `_contextual_retrieval` SELECT, research queue SELECTs for content). idle.py (all 6 sampling channel queries + random fallback). bootstrap.py (all 8 milestone COUNT queries).
  Verify: `grep 'FROM memories' brain/src/consolidation.py brain/src/idle.py brain/src/bootstrap.py | grep -v 'NOT archived'` returns zero lines (all filtered).

- [x] **1.4: Add `AND NOT archived` to context_assembly.py + api.py + db.py + write migration**
  Files: `brain/src/context_assembly.py`, `brain/src/api.py`, `brain/src/db.py`, `brain/migrations/` (new file)
  Do: Add filter to: context_assembly.py (`render_identity_full`, `render_identity_hash`, `_get_immutable_memories`). api.py (status/debug queries, identity introspection query). db.py (`get_agent_ids` — use `SELECT DISTINCT agent_id FROM memories WHERE NOT archived`). Write a migration SQL file that applies the schema changes to an existing database: `ALTER TABLE memories ADD COLUMN archived BOOLEAN DEFAULT FALSE; ALTER TABLE memories ADD COLUMN archived_reason JSONB; CREATE TABLE dedup_verdicts (...); CREATE INDEX ...`.
  Verify: Final sweep: `grep -rn 'FROM memories\|UPDATE memories' brain/src/ | grep -v 'NOT archived' | grep -v INSERT | grep -v DELETE | grep -v 'supersedes\|evidence_chain'` returns zero unexpected lines. Migration file exists and is valid SQL.

## Phase 2: Two-Stage Dedup Engine — DONE

Intent: Build the core dedup logic — vector candidate finding (>= 0.75 similarity), LLM arbitration (merge vs keep-both, best-worded survivor selection), alpha+beta transfer to survivor, archive losers. Wire triggered execution from gate.py when high similarity is first detected. Track verified pairs in `dedup_verdicts` to avoid repeat LLM calls.

Depends on: Phase 1

- [x] **2.1: Core dedup engine — LLM arbitration function**
  Files: `brain/src/config.py`, `brain/src/consolidation.py`
  Do:
  1. Add `DEDUP_SIMILARITY_THRESHOLD = 0.75` to config.py.
  2. Add `async def dedup_pair(pool, store, agent_id, mem_a_id, mem_b_id) -> dict | None` to consolidation.py (near the top, after helpers).
     - Normalize pair order: `a, b = sorted([mem_a_id, mem_b_id])` — so (X,Y) and (Y,X) hit the same dedup_verdicts row.
     - Check dedup_verdicts: `SELECT verdict, survivor_id FROM dedup_verdicts WHERE agent_id=$1 AND mem_a_id=$2 AND mem_b_id=$3`. If found → return existing verdict dict immediately (no LLM call).
     - Fetch both memories from DB: `SELECT id, content, depth_weight_alpha, depth_weight_beta FROM memories WHERE id = ANY($1) AND agent_id = $2 AND NOT archived`. If either is missing/archived → record 'distinct' verdict, return.
     - LLM prompt (via `retry_llm_call`):
       ```
       Compare these two memories. Are they redundant paraphrases of the same idea?
       Memory A (id={a}): "{content_a[:500]}"
       Memory B (id={b}): "{content_b[:500]}"
       If redundant: pick the better-worded one as survivor, or write a superior synthesis.
       If distinct: they capture different ideas and both should be kept.
       Respond ONLY as JSON: {"verdict": "redundant" or "distinct", "survivor": "A" or "B" or "synthesize", "synthesis": "...", "reason": "one sentence"}
       ```
     - Parse JSON from LLM response (handle ```json fences). On parse failure → default to "distinct".
     - Map survivor: if "A" → survivor_id=a, loser_id=b; if "B" → survivor_id=b, loser_id=a; if "synthesize" → survivor_id=None (handled by caller).
     - INSERT into dedup_verdicts: `(agent_id, mem_a_id, mem_b_id, verdict, survivor_id, reason)`.
     - Log to consolidation_log: operation="dedup_verdict", details={verdict, survivor_id, reason}.
     - Return dict: `{"verdict": "redundant"|"distinct", "survivor_id": ..., "loser_id": ..., "reason": ..., "synthesis": ...}`.
  Verify: Function handles edge cases (missing memory, already-archived, LLM parse failure defaults to "distinct"). Dedup_verdicts row created on every call. Pair normalization prevents duplicate entries.

- [x] **2.2: Archive + weight transfer helpers on MemoryStore**
  Files: `brain/src/memory.py`
  Do:
  1. Add `async def archive_memory(self, memory_id, agent_id, reason: dict) -> bool`:
     - `UPDATE memories SET archived = TRUE, archived_reason = $3::jsonb, updated_at = NOW() WHERE id = $1 AND agent_id = $2 AND NOT archived`.
     - Return True if row was updated (result == "UPDATE 1").
  2. Add `async def transfer_weights(self, from_id, to_id, agent_id) -> None`:
     - Fetch from_mem: `SELECT depth_weight_alpha, depth_weight_beta FROM memories WHERE id = $1 AND agent_id = $2`.
     - Add from_mem's alpha to survivor's alpha, from_mem's beta to survivor's beta:
       `UPDATE memories SET depth_weight_alpha = depth_weight_alpha + $2, depth_weight_beta = depth_weight_beta + $3, updated_at = NOW() WHERE id = $1 AND agent_id = $4 AND NOT archived`.
     - This transfers BOTH alpha AND beta (plan decision: preserve distribution shape, don't inflate alpha artificially).
  3. Add `async def execute_dedup_verdict(self, pool, agent_id, verdict: dict) -> str | None`:
     - Convenience method that orchestrates the full dedup action based on a verdict dict from `dedup_pair()`.
     - If verdict["verdict"] == "distinct" → return None (nothing to do).
     - If verdict["survivor"] is "A" or "B" (i.e. survivor_id is set):
       - Call `self.transfer_weights(verdict["loser_id"], verdict["survivor_id"], agent_id)`.
       - Call `self.archive_memory(verdict["loser_id"], agent_id, {"dedup": True, "survivor_id": verdict["survivor_id"], "reason": verdict["reason"]})`.
       - Return survivor_id.
     - If verdict["survivor"] == "synthesize" (synthesis text provided):
       - Store new memory via `self.store_memory(content=verdict["synthesis"], agent_id=agent_id, source="dedup_synthesis", ...)`.
       - Transfer weights from BOTH original memories to the new one.
       - Archive BOTH originals with reason `{"dedup": True, "survivor_id": new_id, "reason": "synthesized replacement"}`.
       - Return new_memory_id.
  Verify: Weight transfer math correct (total alpha+beta preserved). Archived flag set. Synthesis creates new memory with combined weights. Method returns correct survivor_id.

- [x] **2.3: Wire dedup into gate flow + expose most_similar_id**
  Files: `brain/src/gate.py`, `brain/src/api.py`
  Do:
  1. In `gate.py` `ExitGate.evaluate()`: Add `"most_similar_id": most_similar_id` to the metadata dict (line ~340, alongside existing fields). Already a local variable, just not exposed.
  2. In `api.py` `gate_memory()`: After the PERSIST block stores a new memory (line ~558 area), before returning GateResponse:
     - Import: `from .consolidation import dedup_pair` and `from .config import DEDUP_SIMILARITY_THRESHOLD`.
     - Check: `exit_meta.get("max_similarity", 0) >= DEDUP_SIMILARITY_THRESHOLD` AND `exit_meta.get("most_similar_id")` is not None AND `memory_id` is not None (a new memory was just stored).
     - Call: `verdict = await dedup_pair(store.pool, store, req.agent_id, memory_id, exit_meta["most_similar_id"])`.
     - If verdict is not None and verdict["verdict"] == "redundant":
       - `survivor_id = await store.execute_dedup_verdict(store.pool, req.agent_id, verdict)`.
       - If the newly-stored memory was archived (it was the loser): update `memory_id = survivor_id` so GateResponse returns the survivor.
       - Log: `logger.info("Dedup at gate: archived %s, survivor %s", verdict.get("loser_id"), survivor_id)`.
     - Wrap in try/except — dedup failure should NOT block gate response. On failure, log warning and proceed normally.
  3. Add dedup metadata to GateResponse exit_gate dict: `exit_meta["dedup_verdict"] = verdict["verdict"]` if dedup was triggered.
  Verify: Gate flow triggers dedup only for PERSIST decisions with max_similarity >= 0.75. Dedup does NOT fire for REINFORCE/BUFFER/DROP/SKIP. GateResponse.memory_id reflects the survivor. Dedup failure is non-fatal. No dedup for chunked memories (multiple group members — skip dedup when group_id is set, to avoid partial-group dedup).

## Phase 3: Identity Diversity + Situational Exclusion (Bug 3) — DONE

Intent: Exclude `injected_memory_ids` from `_get_situational_memories()`. This is the simplest fix with immediate impact — after Phase 2 reduces duplicates at storage time, this prevents any remaining overlap from showing in both sections.

Depends on: Phase 1 (needs archived filter in queries)

- [x] **3.1: Pass injected_memory_ids into _get_situational_memories and filter results**
  Files: `brain/src/context_assembly.py`
  Do:
  1. Add `exclude_ids: set[str] | None = None` parameter to `_get_situational_memories()` (line 439).
  2. After `candidates = await memory_store.search_hybrid(...)` (line 446), filter out excluded IDs before the budget loop:
     ```python
     if exclude_ids:
         candidates = [m for m in candidates if m.get("id") not in exclude_ids]
     ```
  3. In `assemble_context()`, pass the current `injected_memory_ids` at the call site (line 228):
     ```python
     situational = await _get_situational_memories(
         memory_store, agent_id, query_text, situational_budget,
         exclude_ids=set(injected_memory_ids),
     )
     ```
  Verify:
  - `_get_situational_memories` signature includes `exclude_ids` param.
  - Filter applied before budget loop (not after — budget should fill with non-duplicate memories).
  - `assemble_context` passes `set(injected_memory_ids)` which contains all identity + immutable IDs collected up to that point.
  - No other callers of `_get_situational_memories` exist (it's a private helper). Confirm with grep.

## Phase 4: Bootstrap Fix + Directive Tag Stripping (Bug 2) — DONE

Intent: Disable BOOTSTRAP.md injection in OpenClaw. Strip directive tags (`[[reply_to_current]]`, etc.) from auto-capture content before sending to brain gate. Two independent changes.

Depends on: nothing (can parallelize with Phase 2/3, but sequenced for focus)

- [x] **4.1: Delete BOOTSTRAP.md and mark onboarding complete**
  Files: `openclaw-workspace/BOOTSTRAP.md`, `openclaw-workspace/.openclaw/workspace-state.json`
  Do:
  1. Delete `openclaw-workspace/BOOTSTRAP.md` — removes the greeting script ("Hey. I just came online...") from agent context injection.
  2. Update `openclaw-workspace/.openclaw/workspace-state.json` — set `"onboardingCompletedAt"` to current ISO timestamp. This tells OpenClaw that onboarding is done, preventing BOOTSTRAP.md from being re-created on next workspace setup.
  Verify: BOOTSTRAP.md file no longer exists. workspace-state.json has `onboardingCompletedAt` set. `loadWorkspaceBootstrapFiles()` will return `{missing: true}` for BOOTSTRAP.md — `buildBootstrapContextFiles()` emits a short `[MISSING]` marker (harmless, not the greeting script).

- [x] **4.2: Strip directive tags from auto-capture in memory-brain plugin**
  Files: `openclaw/extensions/memory-brain/index.ts`
  Do:
  1. Add a `stripDirectiveTags(text: string): string` helper near the other helpers (~line 473):
     ```typescript
     const DIRECTIVE_TAG_RE = /\[\[\s*[^\]\n]+\s*\]\]/g;
     function stripDirectiveTags(text: string): string {
       return text.replace(DIRECTIVE_TAG_RE, "").replace(/\s{2,}/g, " ").trim();
     }
     ```
  2. In the `agent_end` hook, apply stripping to captured text before pushing to `captured[]`. Two locations:
     - Line ~1165 (string content): `captured.push({ text: stripDirectiveTags(content), sourceTag });`
     - Line ~1178-1180 (array content text block): `captured.push({ text: stripDirectiveTags(block.text), sourceTag });`
  Verify: `[[reply_to_current]]`, `[[audio_as_voice]]`, and any other `[[...]]` tags are removed from text before it reaches `shouldCapture()` and `brainGate()`. Regex does not match across newlines (no `\n` inside `[^\]\n]+`). Existing `shouldCapture()` and `chunkText()` logic still works on stripped text.

## Phase 5: Conscious Memory Store + Scheduled Reminders — DONE

Intent: Upgrade `memory_store` tool — proper `source_tag: "agent_deliberate"`, initial weights alpha=2.0/beta=3.0, two-stage novelty check (reuses Phase 2 dedup engine). Add `remind_at` parameter. Build reminder delivery: background checker in idle loop, importance=1.0 for due reminders (forced context injection), DMN wake-up for idle agents.

Depends on: Phase 2 (reuses dedup engine for novelty check)

- [x] **5.1: Schema + config + API changes for agent_deliberate and remind_at**
  Files: `brain/src/schema.sql`, `brain/migrations/002_remind_at.sql`, `brain/src/config.py`, `brain/src/api.py`, `brain/src/memory.py`
  Do:
  1. **schema.sql**: Add `remind_at TIMESTAMPTZ` column to `memories` table definition (after `memory_group_id`). Add idempotent migration block at bottom: `DO $$ BEGIN ALTER TABLE memories ADD COLUMN remind_at TIMESTAMPTZ; EXCEPTION WHEN duplicate_column THEN NULL; END $$;`. Add partial index: `CREATE INDEX IF NOT EXISTS idx_memories_remind_at ON memories (remind_at) WHERE remind_at IS NOT NULL AND NOT archived;`.
  2. **migrations/002_remind_at.sql**: New migration file. Same `DO $$ BEGIN ... EXCEPTION` for remind_at column. Same partial index. Idempotent, safe to re-run.
  3. **config.py**: Add constants `DELIBERATE_INITIAL_ALPHA = 2.0`, `DELIBERATE_INITIAL_BETA = 3.0`, `DELIBERATE_SOURCE_TAG = "agent_deliberate"`.
  4. **memory.py** `store_memory()`: Add `remind_at: datetime | None = None` parameter. Add `remind_at` to INSERT column list and VALUES ($18). Pass `remind_at` as 18th param (can be None → SQL NULL).
  5. **api.py** `GateRequest`: Add `remind_at: str | None = None` field (ISO 8601 string, optional).
  6. **api.py** `gate_memory()`: In the PERSIST block, when calling `store.store_memory()`:
     - If `req.source_tag == DELIBERATE_SOURCE_TAG`: pass `initial_alpha=DELIBERATE_INITIAL_ALPHA, initial_beta=DELIBERATE_INITIAL_BETA`.
     - If `req.remind_at` is not None: parse to datetime, pass `remind_at=parsed_dt`.
     - Apply to both the single-memory path and the chunked-memory path (for chunked: only set remind_at on first chunk).
     - Import: `from .config import DELIBERATE_INITIAL_ALPHA, DELIBERATE_INITIAL_BETA, DELIBERATE_SOURCE_TAG` and `from datetime import datetime, timezone`.
  Verify:
  - `schema.sql` parses cleanly. remind_at column defined. Partial index on remind_at present.
  - Migration file is idempotent SQL.
  - `store_memory()` signature includes `remind_at` param. INSERT has 18 columns and 18 values.
  - `GateRequest` has `remind_at` field. `gate_memory` passes deliberate alpha/beta when source_tag matches. remind_at parsed and passed through.

- [x] **5.2: Upgrade memory_store tool in plugin to use gate + agent_deliberate + remind_at**
  Files: `openclaw/extensions/memory-brain/index.ts`
  Do:
  1. **Extend `brainGate()` helper** (line ~104): Add optional `remindAt?: string` parameter. When set, include `remind_at: remindAt` in the JSON body.
  2. **Change `memory_store` tool** (line ~634):
     - Add `remind_at` parameter to tool schema: `Type.Optional(Type.String({ description: "ISO 8601 datetime for scheduled reminder (e.g. 2026-02-23T10:00:00Z)" }))`.
     - In `execute()`: Call `brainGate()` instead of `brainStore()`. Use `source_tag = "agent_deliberate"`. Pass `remindAt: remind_at` param.
     - Parse the GateResult response: if `result.decision` contains "persist" → report stored with memory_id. If "reinforce" → report reinforced existing memory. If "drop"/"skip"/"buffer" → report that gate decided content wasn't novel enough (with the decision).
     - Return appropriate content/details for each case.
  3. **Remove direct `brainStore` import dependency** from memory_store tool (brainStore stays for other uses if any, but memory_store tool no longer calls it).
  Verify:
  - `memory_store` tool calls `brainGate()` with `sourceTag = "agent_deliberate"`.
  - remind_at parameter available in tool schema and passed through to gate.
  - Tool response distinguishes persist/reinforce/drop decisions meaningfully.
  - brainGate body includes remind_at when provided.
  - Auto-capture path still uses brainGate with `sourceTag = "auto_capture"` (unchanged).

- [x] **5.3: Reminder delivery — idle loop checker + importance boost + notification**
  Files: `brain/src/idle.py`
  Do:
  1. Add `async def _check_due_reminders(self) -> int` method to `IdleLoop`:
     - Query: `SELECT id, agent_id, content FROM memories WHERE remind_at IS NOT NULL AND remind_at <= NOW() AND NOT archived LIMIT 10`.
     - For each row:
       a. Set importance=1.0: `UPDATE memories SET importance = 1.0, updated_at = NOW() WHERE id = $1 AND agent_id = $2`.
       b. Clear remind_at to prevent re-processing: `UPDATE memories SET remind_at = NULL WHERE id = $1`.
       c. Enqueue notification via `self.notification_store.enqueue(agent_id=row["agent_id"], content=f"Reminder: {row['content'][:300]}", urgency=0.8, importance=1.0, source="reminder", source_memory_id=row["id"])`.
       d. Log: `logger.info("Reminder delivered: memory %s for agent %s", row["id"], row["agent_id"])`.
       e. Log to consolidation_log: `INSERT INTO consolidation_log (agent_id, operation, details) VALUES ($1, 'reminder_delivered', $2::jsonb)` with details `{"memory_id": row["id"]}`.
     - Return count of delivered reminders.
  2. In `run()` main loop (line ~83, inside the `while not shutdown_event.is_set()` block, before the per-agent heartbeat loop): Call `await self._safe_run_global(self._check_due_reminders)` on every tick.
  3. Add `_safe_run_global` static helper (unlike `_safe_run` which takes agent_id, this one takes no agent_id): wraps the call in try/except, returns {"status": "error"} on failure.
  Verify:
  - `_check_due_reminders` runs every LOOP_SLEEP (30s) iteration.
  - Due reminders get importance=1.0 (forces context_assembly to pick them up via high importance score).
  - remind_at cleared after delivery (no re-fire).
  - Notification enqueued (passive channel for low urgency, telegram for high urgency — routing handled by notification_store).
  - Error isolation: reminder check failure doesn't crash the idle loop.
  - Works across all agents in a single query (not per-agent).
