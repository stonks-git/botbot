# Handoff

> Session context for the supervisor agent.
> **Bootstrap order -> see CLAUDE.md** (single origin for the whole framework).

---

## Previous Sessions

### SESSION 2026-02-16 (#1) - Brain Integration Plan + Phase 0

**STATUS:** DONE

**What was done:**
1. Explored intuitive-AI cognitive architecture (~6000 lines across 14 modules)
2. Explored OpenClaw architecture (monorepo, plugin system, hooks, existing memory-lancedb)
3. Made 3 architectural decisions: Python sidecar, full brain scope, PostgreSQL+pgvector
4. Created brain integration plan v2 (9 phases, glossary, dependency graph)
5. Completed Phase 0: Foundation (scaffold, schema, db, api, docker-compose)
6. Updated embedding model to gemini-embedding-001 at 3072 dimensions (was 768)

**Verifications PASSED:**
- All Phase 0 files created and consistent
- Schema uses halfvec(3072) with proper indexes
- Docker Compose wires postgres → brain correctly

| File | What was done |
|------|---------------|
| `KB/blueprints/v0.1_brain_integration_plan.md` | Full integration plan v2 with glossary |
| `brain/Dockerfile` | Python 3.12 slim, uvicorn on port 8400 |
| `brain/requirements.txt` | fastapi, asyncpg, google-genai, flashrank, numpy |
| `brain/pyproject.toml` | Package metadata |
| `brain/src/__init__.py` | Package init |
| `brain/src/schema.sql` | 5 tables: memories, scratch_buffer, memory_co_access, memory_supersedes, consolidation_log |
| `brain/src/db.py` | asyncpg pool + idempotent schema migration |
| `brain/src/api.py` | FastAPI with /health endpoint |
| `docker-compose.yml` | 3 services: postgres, brain, volumes + networking |

---

### SESSION 2026-02-17 (#2) - intuitive-AI Source Reference KB

**STATUS:** DONE

**What was done:**
1. Created KB_02_intuitive_ai_reference.md — exhaustive reference of all 13 intuitive-AI source modules
2. Updated KB_index.md with KB-02 entry

**Purpose:** Future sessions can bootstrap brain implementation from KB without reading ~6000 lines of intuitive-AI source, saving ~100k tokens of context per session.

**Verifications PASSED:**
- KB_02 covers all 13 modules with constants, signatures, SQL, LLM prompts, algorithms
- KB_index.md updated with proper tags and load policy

| File | What was done |
|------|---------------|
| `KB/KB_02_intuitive_ai_reference.md` | New: all 13 source modules documented |
| `KB/KB_index.md` | Added KB-02 entry |

---

### SESSION 2026-02-17 (#3) - Phase 1: Memory Core

**STATUS:** DONE

**What was done:**
1. Ported 4 core Python modules from intuitive-AI: config, stochastic, activation, relevance
2. Created MemoryStore (memory.py) — full port with agent_id namespacing, Gemini embedding, hybrid search, FlashRank reranking, retrieval-induced mutation
3. Updated api.py with 4 endpoints: POST /memory/store, POST /memory/retrieve, GET /memory/{id}, DELETE /memory/{id}
4. Created OpenClaw memory-brain plugin (3 files) — tools (memory_recall, memory_store, memory_forget), hooks (before_agent_start auto-recall, agent_end auto-capture), service lifecycle
5. Fixed asyncpg JSONB serialization (json.dumps for metadata) and co-access ON CONFLICT clause

**Verifications PASSED:**
- `docker compose up postgres brain` — both services healthy
- `/health` returns 200 with memory_count and agent_count
- `POST /memory/store` — embeds via Gemini and stores with Beta(1,4) initial weight
- `POST /memory/retrieve` — hybrid (dense+sparse+RRF) and reranked (+ FlashRank) both work, correct ranking
- `GET /memory/{id}` — returns full memory with depth_weight_alpha/beta
- `DELETE /memory/{id}` — deletes, returns 404 after
- Retrieval-induced mutation confirmed: alpha increased from 1.0 → 1.3 after 3 retrievals

| File | What was done |
|------|---------------|
| `brain/src/config.py` | New: RetryConfig, EMBED_MODEL, EMBED_DIMENSIONS, MEMORY_TYPE_PREFIXES |
| `brain/src/stochastic.py` | New: StochasticWeight Beta(alpha,beta) class |
| `brain/src/activation.py` | New: ACT-R activation (B+S+P+epsilon), cosine_similarity |
| `brain/src/relevance.py` | New: 5-component Dirichlet relevance, co-access, spread_activation |
| `brain/src/memory.py` | New: MemoryStore — embed, store, search_similar/hybrid/reranked, mutation |
| `brain/src/api.py` | Updated: added 4 endpoints + Pydantic models, MemoryStore in lifespan |
| `openclaw/extensions/memory-brain/package.json` | New: plugin package metadata |
| `openclaw/extensions/memory-brain/openclaw.plugin.json` | New: plugin manifest (kind: memory) |
| `openclaw/extensions/memory-brain/index.ts` | New: tools + hooks + service + brain HTTP client |
| `KB/KB_01_architecture.md` | Updated: Phase 1 section with module details, API, retrieval pipeline |

---

## What is this project?

BotBot bolts the intuitive-AI cognitive architecture (memory with Beta-distributed weights, gut feeling, consolidation, DMN/idle loop, safety monitoring) onto OpenClaw as a Python sidecar brain service, giving OpenClaw agents a real mind that develops identity from experience.

---

## Tasks DOING now

| Task ID | Status |
|---------|--------|
| Phase 2 | NEXT — Entry/Exit Gate (ACT-R gate system, smart filter for what's worth remembering) |

## Blockers or open questions

| Blocker/Question | Status |
|------------------|--------|
| GOOGLE_API_KEY needed for embeddings | RESOLVED — key present in env, Gemini client initializes |

---

## Git Status

- **Branch:** main
- **Last commit:** 7e6f21c KB-02: Exhaustive intuitive-AI source reference for all 13 modules
- **Modified:** brain/src/ (5 new + 1 updated), openclaw/extensions/memory-brain/ (3 new), KB/KB_01, state/*

---

## Memory Marker

```
MEMORY_MARKER: 2026-02-17T14:15:00+02:00 | Phase 1 Memory Core complete | Phase 2 Entry/Exit Gate next
```

---

## Before updating this file

- [x] devlog entry added for each change
- [x] Session section filled (what was done, verifications, files touched)
- [x] **KB updated** if code was modified + `kb_update` devlog entry
- [ ] **Blueprint updated** if scaffolding/architecture changed + `blueprint` devlog entry
- [ ] **Decision Journal entry** if any decision was superseded + `dj_entry` devlog entry
- [ ] `python3 taskmaster.py validate` exits 0
- [ ] Keep only last 3 sessions (older ones archived in git)
