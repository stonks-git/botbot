# KB-01: Architecture

## Overview

BotBot integrates the intuitive-AI cognitive architecture into OpenClaw via a Python sidecar brain service.

**Stack**: Python 3.12 (brain) + Node.js/TypeScript (OpenClaw) + PostgreSQL 17 + pgvector

**Architecture**: Three Docker containers — `postgres` (pgvector), `brain` (FastAPI on :8400), `openclaw` (Node.js). Brain and OpenClaw communicate over HTTP on a shared Docker network. OpenClaw's `memory-brain` plugin hooks into the message lifecycle to inject/capture memories.

**Brain subsystems** (ported from intuitive-AI with agent_id namespacing):
- Memory store — unified table, Beta(alpha,beta) weight distributions, halfvec(3072) embeddings
- Entry/Exit gates — ACT-R activation-based filtering
- Identity layers — L0 (identity) + L1 (goals) as JSON files per agent
- Gut feeling — two-centroid emotional model (subconscious vs attention)
- Consolidation — background Tier 1 (constant) + Tier 2 (hourly deep) processing
- DMN/Idle loop — spontaneous self-prompts with rumination threads
- Safety monitor — hard ceilings, diminishing returns, circuit breakers
- Bootstrap readiness — 10 milestones tracking agent maturation

**Embedding model**: `gemini-embedding-001` at 3072 dimensions (max Matryoshka resolution)

**Key files**: `brain/src/schema.sql` (DB schema), `brain/src/api.py` (HTTP API), `docker-compose.yml` (orchestration)

See `KB/blueprints/v0.1_brain_integration_plan.md` for the full 9-phase implementation plan.

## Decision Journal

> The Decision Journal tracks **why** decisions changed and what was learned.
> Not a duplicate of roadmap.json — roadmap records what was decided, the Decision Journal records the evolution and lessons.
>
> **Scan headers first.** Expand an entry only if current task touches that tag domain.
> Tags must exist in `charter.json` `project.tag_taxonomy`.

<!--
### DJ-001 [tag] D-XXX superseded by D-YYY

- **Was:** (original decision, one line)
- **Now:** (replacement decision, one line)
- **Why:** (what evidence/reasoning caused the change)
- **Lesson:** (reusable takeaway — the point of this whole system)

RULES:
- One entry per superseded/amended decision. Not for new decisions (those go in roadmap.json).
- Tag in brackets = greppable domain (must be in charter.json tag_taxonomy).
- Keep entries to 4 lines max. Link to evidence, don't paste it.
- DJ numbering is sequential, never reused.
- When adding a DJ entry, also update the decision's status to "superseded" in roadmap.json.
- Add a devlog entry with event: "dj_entry".
-->
