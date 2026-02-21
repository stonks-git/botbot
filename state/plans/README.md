# Plans

Multi-phase execution scripts for work that spans multiple sessions.
Plans persist context across context windows so resuming agents can
pick up exactly where the last session left off.

## Naming Convention

```
YYYYMMDDHHMM-topic.md
```

Examples:
- `202602141023-reception-audit.md`
- `202602180900-api-migration-v2.md`

The timestamp prefix ensures chronological sorting and allows multiple plans about the same topic on the same day.

## Workflow

### 1. Brainstorm (1+ sessions)
Goal: agree on the plan — phases, scope, approach.
Phases start as **intent only** (no atomic tasks yet).
Iterate until user approves. Set Status: `active`.

### 2. Decompose Phase N (clean session)
Goal: convert the next undecomposed phase into 3-4 atomic tasks.
Load the plan + explore relevant codebase areas.
Fill in Files/Do/Verify for each task.
Do NOT start implementation in this session.

### 3. Implement Phase N (clean session)
Goal: execute the atomic tasks and document.
Load only the current phase's tasks + listed files.
Tick checkboxes as tasks complete. Run /doc when phase is done.
Mark phase heading DONE.

### 4. Repeat steps 2-3 for remaining phases.
When all phases are DONE, set Status: `done`.

**Why separate sessions:** Decomposition needs broad codebase context
(finding files, understanding patterns). Implementation needs deep
focus on specific files. Mixing them wastes the context window.

## Template

```markdown
# PLAN: <topic>
Status: draft | active | done | superseded
Parent: YYYYMMDDHHMM-topic.md  (or "none")
Supersedes: YYYYMMDDHHMM-topic.md  (or "none")
Roadmap task: T-XXX  (or "standalone")

## Context
<Why this plan exists. What problem it solves.
Key constraints. Enough for a cold-start agent to understand.>

## Phase 1: <name>

<1-2 sentences of phase context>

Intent: <what this phase accomplishes>

Tasks: (empty until decomposition session)

## Phase 2: <name>

Intent: <what this phase accomplishes>
Depends on: Phase 1

## Phase N: ...
```

### After decomposition, a phase looks like:

```markdown
## Phase 1: <name> — DOING

<1-2 sentences of phase context>

- [ ] **1.1: <title>**
  Files: `path/to/file.py`, `path/to/other.py`
  Do: <specific what and how — enough for a cold-start agent>
  Verify: <concrete check — test command, expected output, or observable result>

- [ ] **1.2: <title>**
  Files: ...
  Do: ...
  Verify: ...

- [ ] **1.3: <title>**
  Files: ...
  Do: ...
  Verify: ...
```

### When all tasks in a phase are done:

```markdown
## Phase 1: <name> — DONE

- [x] **1.1: <title>** ...
- [x] **1.2: <title>** ...
- [x] **1.3: <title>** ...
```

## Rules

- **Max 3-4 tasks per phase.** If a phase needs more, split it.
- **Decompose only the next phase.** Never atomize Phase 3 while
  working Phase 1 — learnings from earlier phases change later ones.
- **Decomposition and implementation are separate sessions.**
  Don't mix them.
- **Each task must be self-contained.** A fresh agent reads the task +
  listed files and can execute without loading the full plan.
- **Tick the checkbox when done.** Non-negotiable.
- **When all tasks in a phase are checked:** mark phase heading DONE.
- **When all phases are done:** set plan Status to `done`.

## When to Create a Plan

- Work spans 2+ sessions (context will be lost)
- Fix/feature requires 5+ coordinated changes
- Multi-phase rollout with dependencies between phases
- Audit findings that need a structured fix sequence
