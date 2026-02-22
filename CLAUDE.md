# Project Name

> Fill project description here after bootstrap.

State: `state/` (charter.json, roadmap.json, devlog.ndjson, handoff.md, plans/, schema_log.md).

## Bootstrap (single origin — start here)
1. This file (CLAUDE.md) — framework rules, gates, loading discipline
2. `state/handoff.md` — session context, current tasks, blockers
3. `state/charter.json` — project constraints, tag taxonomy
4. `KB/KB_index.md` — context router (load files per `Load` column)
5. Files marked `always` in KB_index
6. `on-demand` files — ONLY when current task matches tags
7. `python3 taskmaster.py ready` — next available tasks

Never bulk-load historical blueprint versions. Never load all KB files. Follow the router.

## PRIME DIRECTIVES

**1. Every decision that changes the plan MUST be recorded.**
When a decision changes something in the plan or blueprint: record it. Record what it was before, what changed, WHY it changed, and what was learned. No exceptions. This means a Decision Journal entry (DJ-XXX) in `KB/KB_01_architecture.md` + a new blueprint version in `KB/blueprints/` if architecture changed. Unrecorded decisions are lost decisions.

**2. Every completed task MUST be documented before moving on.**
After every task, run `/doc`. Document KB, implementation details, all relevant files. On every step. **Slow and precise > fast and headless.** Undocumented work is lost work.

## Pre-Build Gate (NEVER SKIP)
Before implementing ANY code change, run the **pre-build-explorer** agent first. It finds existing patterns, conventions, and reusable components so new code integrates naturally with the codebase. No coding without precedent analysis.

## Mandatory Gates (NEVER SKIP)
**KB Gate:** Code change affecting functionality/UI/flows -> update `KB/*.md` + `kb_update` devlog entry. No KB for module? Create one. No commit without KB update.
**Blueprint Gate:** Scaffolding/architecture change -> new version file in `KB/blueprints/` + update `BLUEPRINT_INDEX.md` pointer. No silent plan changes.
**Decision Journal Gate:** Decision superseded or amended -> add DJ-XXX entry to `KB/KB_01_architecture.md`. Link old and new decision IDs. Record the WHY.
**Doc Gate:** Task completed -> run `/doc`. All state files updated. `python3 taskmaster.py validate` passes.
**Schema Log Gate (DB projects only):** New migration created -> update `state/schema_log.md`. Verify: check version control for new migration files.

## Plan Execution Gate (NEVER SKIP)
Working from a plan in `state/plans/`? Identify your session type:

**Brainstorm session:** Iterate on plan structure with user. Phases have intent only — no atomic tasks. Don't implement anything.

**Decomposition session:** Convert the next undecomposed phase into 3-4 atomic tasks (Files/Do/Verify). Explore codebase broadly to write precise tasks. Don't implement anything.

**Implementation session:** Load only the current phase's tasks. Execute them in order. Tick checkboxes on completion. Run /doc when phase is done. Mark phase heading DONE. Don't decompose future phases.

Rules for all plan session types:
- Find the first unchecked task. Start there.
- Don't re-read or re-plan completed (checked) tasks.
- If a task's approach turns out wrong: update the task, note what you learned, adjust remaining tasks in the phase.
- When all phases are DONE: set plan Status to `done`.

See `state/plans/README.md` for full template and workflow.

## Devlog
Append single-line JSON to `state/devlog.ndjson` for: accepted decisions, scope changes, completed milestones, major blockers, blueprint versions, Decision Journal entries.

Event types: `feature`, `bugfix`, `refactor`, `kb_update`, `decision`, `handoff`, `verification`, `human_review`, `blueprint`, `dj_entry`.

## Checkpoint
Save progress BEFORE autocompact eats it. Trigger: 3+ files read without save, important decision, task completed.
Actions: update `state/handoff.md` (including `MEMORY_MARKER`) -> append devlog event -> `python3 taskmaster.py validate`.
The `MEMORY_MARKER` in handoff.md is a quick-recovery anchor: `<timestamp> | <last_task_completed> | <next_task>`. Update it after every task completion so context can be recovered after autocompact.
Session compression: keep only last 3 sessions in handoff.md. Older sessions are archived in git history and summarized in devlog.ndjson. This prevents handoff.md from bloating and wasting context window.

## Verification (before marking done)

| # | Check | How |
|---|-------|-----|
| 1 | **Matches request** | Deliverable = what was asked |
| 2 | **Works** | Runs/tests pass, no regressions |
| 3 | **Minimal** | `git diff` shows only necessary changes |
| 4 | **Documented** | KB updated if code changed, blueprint if arch changed, DJ if decision changed |

## Anti-Drift (CRITICAL)
- Work ONLY on the current task. Nothing else.
- Minimum necessary edits. No extra changes.
- No opportunistic refactors/cleanup/reformatting.
- No "while I'm here" improvements.
- Do not change scope without explicit user approval.
- Ask if unclear -- do not assume.
- Never present assumptions as facts -- mark [ASSUMED].
- Do not rewrite existing content in ways that drop context.

## Intellectual Honesty (NEVER SKIP)
Do not agree with the user when they are wrong. Specifically:
- Correct incorrect technical claims. Cite the file, line, or fact.
- Flag when "simple" changes are actually complex. State the real scope.
- Correct misstatements about codebase state — you can see the code, they're going from memory.
- Warn when a proposed approach conflicts with what you observe.
- State the correction once, concisely, with evidence. If the user insists after seeing your evidence, defer — they may have context you don't. Note [USER OVERRIDE] in devlog.
- This applies to verifiable facts, not preferences or style choices. User owns what/why. You own pushing back on incorrect how/is.

## Long-Running Tasks
- ALWAYS warn the user before running any long background task.
- Run with a viewable progress bar so the user can monitor.
- Never silently run long tasks in background.

## Tag Taxonomy
Tags are defined in `state/charter.json` under `project.tag_taxonomy`. All tags used in KB_index `Tags` column and Decision Journal entry `[tag]` headers MUST exist in the taxonomy. To add a new tag, add it to charter.json first, then use it.

## Context Loss
If you don't remember current task/recent files/decisions: **STOP.** Follow Bootstrap order above. Tell user "Context lost, re-read state." Wait for confirmation.

## Framework Structure
- Agent stubs: `.claude/agents/` — lightweight registration files for Claude Code. Point to full protocols in `prompts/`.
- Full protocols: `prompts/` — portable behavior contracts. Usable by any tool, not just Claude Code.
- Audit orchestrator: `prompts/auditors/runner.md` — manual use prompt for running full audit sequences. Not a subagent (can't call sub-subagents).
- Supervisor contract: `prompts/supervisor.md`
- Project workflows: `workflows/` — on-demand project-specific workflow templates (deploy ceremonies, architecture checks, custom gates). Loaded when task matches.
- **Deploy target:** norisor (100.66.170.31 via Tailscale). See `workflows/deploy.md` for full procedure. Quick: `git push && ssh norisor "cd ~/botbot && git pull && docker compose build brain openclaw && docker compose up -d"`.
- Operational plans: `state/plans/` — multi-phase execution scripts with tracked progress. Naming: `YYYYMMDDHHMM-topic.md`. See `state/plans/README.md` for template and workflow.
- All agents inherit this CLAUDE.md automatically.
- Agent stub paths are relative to repository root.
