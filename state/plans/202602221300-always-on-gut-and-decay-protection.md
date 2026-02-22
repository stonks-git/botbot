# PLAN: Always-On Gut + Decay Protection for Deliberate Memories
Status: done
Parent: none
Supersedes: none
Roadmap task: standalone

## Context

Two related gaps in the cognitive architecture:

1. **Gut is conversation-only.** `gut.update_attention()` only fires during `/context/assemble` (user conversations). DMN heartbeats run 24/7 but never feed the gut. Result: `emotional_charge=0.0` when idle, gate sees peripheral relevance for everything, deliberate memories can't persist because spreading_activation stays low.

2. **Deliberate memories decay like noise.** Agent stores intentional memories via `memory_store` tool (source_tag=`agent_deliberate`). These immediately face `_decay_tick()` (+0.01 beta/hour). Agent needs ability to protect memories from decay for a specified period, with LLM re-evaluation when protection expires.

### Key Decisions
- **Identity embeddings move to MemoryStore** — `_get_identity_embeddings()` in api.py uses `store.pool` anyway. Moving it to memory.py avoids circular imports when idle.py needs it.
- **Gut fed once per heartbeat** — embed sampled memory content, update attention+subconscious+delta. One embed call per heartbeat is well within rate limits.
- **`protect_until` is agent-specified, no default** — if not set, no protection. Prevents accidental immortal memories.
- **Re-evaluation uses full context assembly** — lightweight budget (20k tokens). LLM decides extend (up to 90 day cap) or release.

### Files Involved

| Area | Files |
|------|-------|
| Gut feeding | `idle.py`, `memory.py`, `api.py` |
| Decay protection | `schema.sql`, `migrations/003`, `memory.py`, `api.py`, `consolidation.py` |
| Expiration re-eval | `idle.py`, `context_assembly.py` (called, not modified) |
| Plugin | `openclaw/extensions/memory-brain/index.ts` |

---

## Phase 1: Always-On Gut Feeding

Intent: DMN heartbeat feeds gut attention centroid so emotional_charge is non-zero during idle. Gate can then classify relevant content as "core" for all API paths, not just conversations.

- [x] **1.1: Move `_get_identity_embeddings` into MemoryStore**
  Files: `brain/src/memory.py`, `brain/src/api.py`
  Do:
  1. Add `async def get_identity_embeddings(self, agent_id: str, top_n: int = 20) -> list[tuple] | None` to MemoryStore (after `score_identity_wxs`). Copy SQL from api.py:154-166. Uses `self.pool` and `WEIGHT_CENTER_SQL` (already imported). Add `import numpy as np` to memory.py (not currently imported).
  2. In api.py, replace `_get_identity_embeddings` body to delegate: `return await _store().get_identity_embeddings(agent_id, top_n)`. Keep the function signature so 3 call sites (lines 486, 680, 777) work unchanged.
  Verify: `grep get_identity_embeddings brain/src/memory.py` shows new method. api.py function delegates to store. No circular imports.

- [x] **1.2: Add `_feed_gut()` helper to IdleLoop**
  Files: `brain/src/idle.py`
  Do:
  1. Add method after `_touch_sampled`:
     - Embed memory content (truncated 500 chars) via `self.memory_store.embed()`
     - Call `gut.update_attention(embedding)`
     - Call `self.memory_store.get_identity_embeddings(agent_id)` → `gut.update_subconscious()`
     - Call `gut.compute_delta(context=f"dmn:{content[:80]}")`
     - Call `gut.save()`
     - Wrap entire body in try/except → logger.warning (non-blocking)
  Verify: Method exists, exception-safe. Does not block heartbeat on failure.

- [x] **1.3: Wire `_feed_gut` into `_heartbeat`**
  Files: `brain/src/idle.py`
  Do:
  1. Track sampled content across all branches of `_heartbeat()`:
     - `sampled_content = None` at top
     - Random pop branch (line 232): `sampled_content = memory["content"] if memory else None`
     - Continue thread branch (line 237): `sampled_content = rm.active_thread.seed_content`
     - New thread branch (line 240): `sampled_content = memory["content"] if memory else None`
  2. After the if/else block, before heartbeat_count update: `if sampled_content: await self._feed_gut(agent_id, sampled_content)`
  Verify: Gut gets fed every heartbeat. After first heartbeat, `gut.emotional_charge` is non-zero for agents with memories.

## Phase 2: Decay Protection Schema + Plumbing

Intent: Add `protect_until TIMESTAMPTZ` column, wire through store_memory, GateRequest, and gate flow.

- [x] **2.1: Schema + migration for protect_until**
  Files: `brain/src/schema.sql`, `brain/migrations/003_protect_until.sql`
  Do:
  1. schema.sql: Add `protect_until TIMESTAMPTZ` after `remind_at` (line 66). Add partial index `idx_memories_protect_until ON memories (protect_until) WHERE protect_until IS NOT NULL AND NOT archived`. Add idempotent `DO $$ BEGIN ALTER TABLE` block at bottom.
  2. migrations/003: Same pattern as 002_remind_at.sql.
  Verify: schema.sql parses. Migration idempotent.

- [x] **2.2: Wire protect_until through store_memory + GateRequest + gate flow**
  Files: `brain/src/memory.py`, `brain/src/api.py`
  Do:
  1. memory.py `store_memory()`: Add `protect_until: "datetime | None" = None` param. INSERT gets 19 columns, $19. Pass as 19th arg.
  2. api.py `GateRequest`: Add `protect_until: str | None = None`.
  3. api.py `gate_memory()`: In extra_kw block (line 525 area): `if req.protect_until: extra_kw["protect_until"] = datetime.fromisoformat(req.protect_until)`. Same pattern as remind_at. Chunked: first chunk only.
  Verify: INSERT has 19 cols/values. GateRequest has field. Gate passes through.

## Phase 3: Decay Exclusion

Intent: Both decay tiers skip memories with active protection.

- [x] **3.1: Guard _decay_tick and get_stale_memories**
  Files: `brain/src/consolidation.py`, `brain/src/memory.py`
  Do:
  1. consolidation.py `_decay_tick()` (line 362): Add `AND (protect_until IS NULL OR protect_until < NOW())` to WHERE clause after the `WEIGHT_CENTER_SQL > 0.1` line.
  2. memory.py `get_stale_memories()` (line 744): Add same guard to WHERE clause. This covers Tier 2 deep decay since `_decay_and_reconsolidate()` calls `get_stale_memories()`.
  Verify: Protected memories excluded from both Tier 1 and Tier 2 decay. Null protect_until = no protection (normal decay).

## Phase 4: Protection Expiration + LLM Re-Evaluation

Intent: Idle loop finds expired protections, runs LLM re-eval with context assembly, extends or releases.

- [x] **4.1: Add `_check_expired_protections` to IdleLoop**
  Files: `brain/src/idle.py`
  Do:
  1. Query expired protections: `WHERE protect_until IS NOT NULL AND protect_until <= NOW() AND NOT archived LIMIT 5`.
  2. For each: call `assemble_context(memory_store, agent_id, query_text=content[:200], total_budget=20000)` for context.
  3. LLM prompt: memory content + identity + situational context → decide extend/release. Parse JSON response (handle fences). Default to release on parse failure.
  4. Extend: `UPDATE memories SET protect_until = NOW() + make_interval(days => N)`. Cap N at 90.
  5. Release: `UPDATE memories SET protect_until = NULL`.
  6. Audit: INSERT into consolidation_log (operation='protection_reeval').
  7. Import `assemble_context` and `retry_llm_call` locally inside the method.
  Verify: Handles parse failures (defaults release). LLM failures non-fatal. Audit trail written. LIMIT 5 caps cost.

- [x] **4.2: Wire into idle loop run()**
  Files: `brain/src/idle.py`
  Do: Add `await self._safe_run_global(self._check_expired_protections)` after `_check_due_reminders` call in run().
  Verify: Runs every 30s tick. Cross-agent. Error-isolated.

## Phase 5: Plugin Upgrade

Intent: `memory_store` tool gets `protect_until` param, passed through `brainGate`.

- [x] **5.1: Add protect_until to brainGate + memory_store tool**
  Files: `openclaw/extensions/memory-brain/index.ts`
  Do:
  1. `brainGate()`: Add `protectUntil?: string` param after `remindAt`. Add `if (protectUntil) body.protect_until = protectUntil;`.
  2. `memory_store` tool schema: Add `protect_until` Type.Optional(Type.String({description: "ISO 8601 datetime..."})).
  3. Execute: Destructure `protect_until`, pass to brainGate as 7th arg.
  4. Success response: append `(protected until: ...)` when set.
  Verify: Tool schema has param. brainGate sends it. Auto-capture path unaffected.

## Verification

1. **Gut feeding:** Rebuild brain, wait 60s. Check `curl /gut/status?agent_id=default` — `emotional_charge` should be non-zero, `attention_count > 0`.
2. **Decay protection:** Apply migration 003. Store memory with `protect_until` via gate. Run `_decay_tick` — memory should NOT get beta nudge. Advance protect_until to past — next decay tick SHOULD nudge.
3. **Re-evaluation:** Set protect_until in past on a memory. Wait for idle loop. Check consolidation_log for `protection_reeval` entry. Memory should have protect_until either extended or set to NULL.
4. **Plugin:** Open webchat, use memory_store tool with protect_until param. Verify gate receives it.
