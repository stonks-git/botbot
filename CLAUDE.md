# Project Name

> Fill project description here after bootstrap.

State: `state/` (charter.json, roadmap.json, devlog.ndjson, handoff.md).

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

## Mandatory Gates (NEVER SKIP)
**KB Gate:** Code change affecting functionality/UI/flows -> update `KB/*.md` + `kb_update` devlog entry. No KB for module? Create one. No commit without KB update.
**Blueprint Gate:** Scaffolding/architecture change -> new version file in `KB/blueprints/` + update `BLUEPRINT_INDEX.md` pointer. No silent plan changes.
**Decision Journal Gate:** Decision superseded or amended -> add DJ-XXX entry to `KB/KB_01_architecture.md`. Link old and new decision IDs. Record the WHY.
**Doc Gate:** Task completed -> run `/doc`. All state files updated. `python3 taskmaster.py validate` passes.

## Devlog
Append single-line JSON to `state/devlog.ndjson` for: accepted decisions, scope changes, completed milestones, major blockers, blueprint versions, Decision Journal entries.

Event types: `feature`, `bugfix`, `refactor`, `kb_update`, `decision`, `handoff`, `verification`, `human_review`, `blueprint`, `dj_entry`.

## Checkpoint
Save progress BEFORE autocompact eats it. Trigger: 3+ files read without save, important decision, task completed.
Actions: update `state/handoff.md` -> append devlog event -> `python3 taskmaster.py validate`.

## Verification (before marking done)
Task matches request. Tests/checks pass. No regressions. Minimal changes only. KB updated. Blueprint updated if plan changed. Decision Journal entry if decision changed.

## Anti-Drift (CRITICAL)
- Work ONLY on the current task. Nothing else.
- Minimum necessary edits. No extra changes.
- No opportunistic refactors/cleanup/reformatting.
- No "while I'm here" improvements.
- Do not change scope without explicit user approval.
- Ask if unclear -- do not assume.
- Never present assumptions as facts -- mark [ASSUMED].
- Do not rewrite existing content in ways that drop context.

## Long-Running Tasks
- ALWAYS warn the user before running any long background task.
- Run with a viewable progress bar so the user can monitor.
- Never silently run long tasks in background.

## Tag Taxonomy
Tags are defined in `state/charter.json` under `project.tag_taxonomy`. All tags used in KB_index `Tags` column and Decision Journal entry `[tag]` headers MUST exist in the taxonomy. To add a new tag, add it to charter.json first, then use it.

## Context Loss
If you don't remember current task/recent files/decisions: **STOP.** Follow Bootstrap order above. Tell user "Context lost, re-read state." Wait for confirmation.

## Framework Structure
- Agent prompts: `.claude/agents/`
- Audit orchestrator: `prompts/auditors/runner.md`
- Supervisor contract: `prompts/supervisor.md`
- All agents inherit this CLAUDE.md automatically.
