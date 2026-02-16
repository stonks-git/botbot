---
name: doc
description: Document session changes in tracking files (devlog, handoff, KB, roadmap). Use when the user says "document", "/doc", or asks to note what was done.
---

# Document Session

When the user asks to document, follow these steps **IN ORDER**:

## 1. devlog.ndjson (MANDATORY)

Add 1+ JSON lines to `state/devlog.ndjson`. Format:

```json
{"ts":"2026-01-07T12:00:00+02:00","event":"<type>","id":"<ID>","summary":"<short description>"}
```

**Event types:**
- `feature` - new functionality
- `bugfix` - bug fix
- `refactor` - code restructuring
- `kb_update` - KB documentation update
- `decision` - new decision (with `id: D-XXX`)
- `blueprint` - new blueprint version created
- `dj_entry` - Decision Journal entry added (with `id: DJ-XXX`)
- `handoff` - session summary
- `verification` - smoke test passed
- `human_review` - approved by user

## 2. handoff.md (MANDATORY)

Update session sections in `state/handoff.md`:
- Previous Sessions: add current session summary
- Tasks DOING: update status
- Git Status: current branch, last commit, modified files

## 3. KB/*.md (CRITICAL — DO NOT SKIP!)

**NO EXCEPTIONS:** If code was modified, KB MUST be updated.

**Steps:**
1. List files: `ls KB/*.md`
2. Ask: "What module did I modify? Which KB describes it?"
3. Read the relevant KB and update it
4. If no KB exists for the modified module -> create one
5. Update `KB/KB_index.md` if a new KB page was created (assign tags from charter.json taxonomy)

**After KB update:** add entry in devlog with `event: kb_update`.

## 4. Blueprints (IF ARCHITECTURE CHANGED)

If scaffolding/architecture changed (new component, dropped component, pattern change):
1. Create new version file in `KB/blueprints/` (complete plan snapshot, not a diff)
2. Add changelog section: what changed from previous version and WHY
3. Update `KB/blueprints/BLUEPRINT_INDEX.md` pointer to new version
4. Update `KB/KB_index.md` — new version row as `always (latest only)`, old version as `on-demand`
5. Add devlog entry with `event: blueprint`

## 5. Decision Journal (IF DECISION SUPERSEDED)

If any decision was superseded or amended:
1. Add DJ-XXX entry to `KB/KB_01_architecture.md` with: Was / Now / Why / Lesson
2. Use a tag from charter.json `tag_taxonomy` in the header brackets
3. Update decision status to `superseded` in `state/roadmap.json`
4. Add devlog entry with `event: dj_entry`

## 6. Git Status (MANDATORY)

Run git commands and update handoff.md:

```bash
git status --short
git log --oneline -3
```

## 7. roadmap.json (IF APPLICABLE)

Update `state/roadmap.json` only if:
- A task changes status (doing -> done)
- New task added
- Dependencies changed
- Decision status changed (including superseded)

## 8. VALIDATE (MANDATORY)

```bash
python3 taskmaster.py validate
```

Must exit 0. If it doesn't, fix the issues before finishing.

## 9. FINAL CHECKLIST

**STOP! Don't commit until you verify ALL:**

- [ ] `devlog.ndjson` - entry added for each change
- [ ] `handoff.md` - session sections updated
- [ ] **KB updated** - MANDATORY if code was modified
- [ ] `kb_update` entry in devlog (if KB was updated)
- [ ] **Blueprint updated** - if architecture changed
- [ ] **Decision Journal entry** - if decision was superseded
- [ ] Tags used exist in charter.json taxonomy
- [ ] `taskmaster validate` - exit 0

## Workflow Summary

```
1. Ask user WHAT was done (if you don't know)
2. Write entry in devlog.ndjson
3. Update handoff.md session sections
4. GATE: Update KB for modified modules
   -> What modules did I touch? -> Which KB describes them? -> Update
   -> Add kb_update entry in devlog
5. GATE: Update blueprint if architecture changed
   -> New version file + update index + devlog entry
6. GATE: Add Decision Journal entry if decision was superseded
   -> DJ-XXX entry + update roadmap status + devlog entry
7. Check git status, add to handoff
8. Update roadmap if task status changed
9. Run `python3 taskmaster.py validate` — exit 0
10. CHECKLIST COMPLETE? -> tell user documentation is done
```

## Timestamp format

Use ISO 8601 with timezone: `2026-01-07T12:00:00+02:00`
