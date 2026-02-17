# KB-02: intuitive-AI Source Reference

> **Purpose:** Exhaustive reference of all 13 source modules being ported from `/home/stonks/@dev/intuitive-AI/src/`.
> Enough detail to reimplement without reading originals. Future sessions should never need to explore the source.
>
> **NOTE:** Original uses 768-dim embeddings. BotBot ports to 3072-dim (gemini-embedding-001 max Matryoshka). All vector dimensions in this doc reflect the original — adjust to 3072 when implementing.
> All modules need `agent_id` namespacing added (not in originals).

---

## Module Map

| # | File | Port Target | What It Does |
|---|------|-------------|--------------|
| 1 | memory.py | brain/src/memory.py | Store, embed, retrieve, mutate, scratch buffer |
| 2 | relevance.py | brain/src/relevance.py | 5-component Dirichlet-blended hybrid relevance |
| 3 | activation.py | brain/src/activation.py | ACT-R activation equation (B+S+P+epsilon) |
| 4 | stochastic.py | brain/src/stochastic.py | Beta distribution weight class |
| 5 | gate.py | brain/src/gate.py | Entry gate (stochastic filter) + Exit gate (3x3 matrix) |
| 6 | gut.py | brain/src/gut.py | Two-centroid emotional model |
| 7 | context_assembly.py | brain/src/context_assembly.py | Dynamic context injection with token budgets |
| 8 | consolidation.py | brain/src/consolidation.py | Tier 1 constant + Tier 2 deep background processing |
| 9 | idle.py | brain/src/idle.py | DMN idle loop, 4 sampling channels |
| 10 | rumination.py | brain/src/rumination.py | RuminationThread lifecycle, persistence |
| 11 | layers.py | brain/src/layers.py | L0/L1 identity/goal storage, embedding cache |
| 12 | bootstrap.py | brain/src/bootstrap.py | 10 readiness milestones, bootstrap prompt |
| 13 | safety.py | brain/src/safety.py | Phase A-C ceilings, SafetyMonitor, OutcomeTracker |

---

## 1. memory.py — Memory Store

### Imports
```python
import asyncio, logging, os, json, uuid
from datetime import datetime, timezone
from typing import Any
import asyncpg
from google import genai
from .llm import retry_llm_call
from .config import RetryConfig
```

### Constants
```python
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSIONS = 768  # BotBot uses 3072

MEMORY_TYPE_PREFIXES = {
    "episodic":   "Personal experience memory: ",
    "semantic":   "Factual knowledge: ",
    "procedural": "How-to instruction: ",
    "preference": "User preference: ",
    "reflection": "Self-reflection insight: ",
    "correction": "Past error correction: ",
    "narrative":  "Identity narrative: ",
    "tension":    "Internal contradiction: ",
}
```

### Class: MemoryStore

**Attributes:**
- `pool: asyncpg.Pool | None`
- `genai_client: genai.Client | None`
- `retry_config: RetryConfig`
- `safety: SafetyMonitor | None` (set by cognitive loop)

**Methods:**

`async connect()` — Init DB pool from `DATABASE_URL` env (default `postgresql://agent:agent_secret@localhost:5432/agent_memory`), pool min=2, max=10. Sets up genai client if `GOOGLE_API_KEY` exists.

`async close()` — Closes DB pool.

`async embed(text, task_type="RETRIEVAL_DOCUMENT", title=None) -> list[float]` — Embeds text via Gemini with retry. task_type: "RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY", "SEMANTIC_SIMILARITY", "CLUSTERING".

`async embed_batch(texts, task_type="RETRIEVAL_DOCUMENT", title=None) -> list[list[float]]` — Batch embeds, chunks of 100.

`def prefixed_content(content, memory_type) -> str` — Prepends type prefix from MEMORY_TYPE_PREFIXES.

`async store_memory(content, memory_type="semantic", source=None, tags=None, confidence=0.5, importance=0.5, evidence_count=0, metadata=None, source_tag=None) -> str`
- Generates `mem_{uuid4().hex[:12]}`
- Embeds with prefixed type
- SQL:
```sql
INSERT INTO memories (id, content, type, embedding, created_at, updated_at,
                      source, tags, confidence, importance, evidence_count, metadata, source_tag)
VALUES ($1, $2, $3, $4::halfvec, $5, $6, $7, $8, $9, $10, $11, $12, $13)
```

`async store_insight(content, source_memory_ids, importance=0.8, tags=None, metadata=None) -> str`
- Creates high-importance insight
- Links via memory_supersedes table
- Lowers source importance: `LEAST(importance, 0.3)`
```sql
INSERT INTO memory_supersedes (insight_id, source_id) VALUES ($1, $2) ON CONFLICT DO NOTHING
UPDATE memories SET importance = LEAST(importance, 0.3) WHERE id = ANY($1)
```

`async why_do_i_believe(memory_id) -> list[dict]` — Recursive CTE, max depth 5:
```sql
WITH RECURSIVE evidence_chain AS (
    SELECT s.source_id, 1 AS depth FROM memory_supersedes s WHERE s.insight_id = $1
    UNION ALL
    SELECT s.source_id, ec.depth + 1 FROM memory_supersedes s
    JOIN evidence_chain ec ON s.insight_id = ec.source_id WHERE ec.depth < 5
)
SELECT DISTINCT m.id, m.content, m.type, m.confidence, m.importance,
       m.created_at, m.source, m.tags, ec.depth
FROM evidence_chain ec JOIN memories m ON m.id = ec.source_id
ORDER BY ec.depth, m.created_at
```

`async get_insights_for(source_memory_id) -> list[dict]`
```sql
SELECT m.id, m.content, m.importance, m.evidence_count, m.created_at
FROM memory_supersedes s JOIN memories m ON m.id = s.insight_id
WHERE s.source_id = $1 ORDER BY m.importance DESC
```

`async apply_retrieval_mutation(retrieved_ids, near_miss_ids=None, vector_scores=None)`
- Retrieved: alpha += 0.1 (dormant memories with vector_score > 0.9 get 0.2)
- Near-misses: beta += 0.05 (never for immutable)
- Safety checks via `safety.check_weight_change()`
```sql
UPDATE memories
SET access_count = access_count + 1,
    last_accessed = $1,
    access_timestamps = array_append(COALESCE(access_timestamps, ARRAY[]::timestamptz[]), $1),
    depth_weight_alpha = depth_weight_alpha + $3,
    updated_at = $1
WHERE id = $2
```

`async search_similar(query, top_k=5, min_similarity=0.3) -> list[dict]`
- Embeds query with task_type="RETRIEVAL_QUERY"
```sql
SELECT id, content, type, confidence, importance, access_count, last_accessed,
       tags, source, created_at, 1 - (embedding <=> $1::halfvec) AS similarity
FROM memories
WHERE 1 - (embedding <=> $1::halfvec) > $2
ORDER BY embedding <=> $1::halfvec LIMIT $3
```

`async search_hybrid(query, top_k=20, mutate=True, reinforce_top_k=5) -> list[dict]`
- Dense (pgvector) + sparse (tsvector) fusion with RRF
- Enables `SET hnsw.iterative_scan = 'relaxed_order'`
- Dense CTE: pgvector top 50
- Sparse CTE: tsvector websearch_to_tsquery top 50
- Combined: FULL OUTER JOIN + RRF:
```sql
1.0 / (60 + dense_rank) AS rrf_dense,
1.0 / (60 + sparse_rank) AS rrf_sparse,
EXP(-0.693 * EXTRACT(EPOCH FROM (NOW() - created_at)) / 604800.0) AS recency_score,
0.5 * (rrf_dense + rrf_sparse) + 0.3 * recency_score
  + 0.2 * (depth_weight_alpha / (depth_weight_alpha + depth_weight_beta)) AS weighted_score
```
- Recency: 7-day half-life exponential decay
- weighted_score: 50% RRF + 30% recency + 20% depth_weight center
- If mutate: applies retrieval-induced mutation + calls `update_co_access()`

`async search_reranked(query, top_k=5, hybrid_top_k=20) -> list[dict]`
- Calls search_hybrid(mutate=False) for candidates
- Lazy-loads FlashRank (ms-marco-MiniLM-L-12-v2, ~34MB)
- Final score: `0.6 * rerank_score + 0.4 * weighted_score`
- Applies mutation post-reranking

`async get_memory(memory_id) -> dict | None` — `SELECT * FROM memories WHERE id = $1`

`async get_random_memory() -> dict | None` — `ORDER BY RANDOM() LIMIT 1`

`async memory_count() -> int` — `SELECT COUNT(*) FROM memories`

`async buffer_scratch(content, source=None, tags=None, metadata=None) -> str`
- ID: `scratch_{uuid4().hex[:12]}`, TTL: 24 hours
```sql
INSERT INTO scratch_buffer (id, content, source, tags, metadata, expires_at)
VALUES ($1, $2, $3, $4, $5, NOW() + INTERVAL '24 hours')
```

`async flush_scratch(older_than_minutes=0) -> list[dict]`
```sql
DELETE FROM scratch_buffer
WHERE buffered_at < NOW() - INTERVAL '1 minute' * $1
  AND (expires_at IS NULL OR expires_at > NOW())
RETURNING *
```

`async cleanup_expired_scratch() -> int` — `DELETE FROM scratch_buffer WHERE expires_at < NOW()`

`async recover_crash_scratch(last_flush_time) -> list[dict]` — Returns entries older than last flush.

`async check_novelty(content, threshold=0.85) -> tuple[bool, float]`
- Embeds with task_type="SEMANTIC_SIMILARITY"
- Returns (is_novel: max_sim < threshold, max_similarity)

`async get_stale_memories(stale_days=90, min_access_count=3) -> list[dict]`
```sql
SELECT id, content, importance, access_count, last_accessed FROM memories
WHERE (last_accessed IS NULL OR last_accessed < NOW() - INTERVAL '1 day' * $1)
  AND access_count < $2 AND importance > 0.05
ORDER BY importance ASC
```

`async decay_memories(memory_ids, factor=0.5)` — `UPDATE memories SET importance = importance * $1`

`async avg_depth_weight_center(where=None) -> float` — AVG of alpha/(alpha+beta)

`async search_corrections(query_embedding, top_k=3) -> list[dict]` — Corrections by similarity.

`async store_correction(trigger, original_reasoning, correction, context=None, confidence=0.8) -> str` — Stores correction with metadata.

---

## 2. relevance.py — Hybrid Relevance Scoring

### Imports
```python
import logging, random
from datetime import datetime, timezone
import numpy as np
from .activation import cosine_similarity
```

### Constants
```python
COLD_START_ALPHA = {"semantic": 12.0, "coactivation": 1.0, "noise": 0.5, "emotional": 0.5, "recency": 3.0}
TARGET_ALPHA = {"semantic": 8.0, "coactivation": 5.0, "noise": 0.5, "emotional": 3.0, "recency": 2.0}
RECENCY_HALF_LIFE_SECONDS = 604800  # 7 days
```

### Functions

`compute_semantic_similarity(memory_embedding, attention_embedding) -> float` — max(0, cosine_sim). Returns 0 if attention is None.

`compute_coactivation(memory_id, active_memory_ids, co_access_scores) -> float` — Looks up max co_access_scores via canonical tuple key `tuple(sorted([a, b]))`. Returns min(1.0, max_score).

`compute_noise() -> float` — Returns `random.random()` (creative exploration).

`compute_emotional_alignment(gut_alignment=None) -> float` — Returns gut_alignment clamped 0-1, or 0.5 if None.

`compute_recency(last_accessed) -> float` — `exp(-0.693 * age_seconds / 604800)`. Returns 0.0 if None.

`sample_blend_weights(memory_count=0, alpha_override=None) -> dict[str, float]`
- If count < 100: use COLD_START_ALPHA (semantic-heavy)
- Else: linear interpolation to TARGET_ALPHA, saturates at 1000 memories
- `t = min(1.0, (memory_count - 100) / 900)`
- Samples from `Dirichlet(alphas)`

`compute_hybrid_relevance(memory_embedding, memory_id, last_accessed, attention_embedding=None, active_memory_ids=None, co_access_scores=None, gut_alignment=None, blend_weights=None, memory_count=0) -> tuple[float, dict]`
- Computes 5 components, dot-products with blend_weights
- Returns (score, breakdown)

`async spread_activation(pool, seed_ids, hops=1, top_k_per_hop=3) -> dict[str, float]`
- Spreads through co-access network (Hebbian)
- Decay per hop: [1.0, 0.3, 0.1]
- Normalized count: `min(1.0, count / 20.0)`
```sql
SELECT memory_id_a, memory_id_b, co_access_count FROM memory_co_access
WHERE memory_id_a = ANY($1) OR memory_id_b = ANY($1)
ORDER BY co_access_count DESC
```

`async update_co_access(pool, memory_ids)`
- Creates pairs: `i, j` where `j < i + 5` (limits explosion)
```sql
INSERT INTO memory_co_access (memory_id_a, memory_id_b, co_access_count, last_co_accessed)
VALUES ($1, $2, 1, NOW())
ON CONFLICT (memory_id_a, memory_id_b)
DO UPDATE SET co_access_count = memory_co_access.co_access_count + 1,
             last_co_accessed = NOW()
```

---

## 3. activation.py — ACT-R Activation

### Constants
```python
DEFAULT_DECAY_D = 0.5        # base-level decay rate
DEFAULT_NOISE_S = 0.4        # logistic noise spread
DEFAULT_MISMATCH_P = -1.0    # partial matching penalty scale
DEFAULT_THRESHOLD_TAU = 0.0  # persist threshold
```

### Functions

`cosine_similarity(a, b) -> float` — `dot(a,b) / (norm(a) * norm(b))`, 0 if either norm is 0.

`base_level_activation(access_timestamps, now=None, d=0.5) -> float`
- `B_i = ln(sum(t_j^{-d}))` where t_j = seconds since access
- Returns 0.0 if empty

`spreading_activation(memory_embedding, attention_embedding=None, layer_embeddings=None, context_weight=0.4, identity_weight=0.6) -> float`
- S_i = context_weight * cosine(mem, attention) + identity_weight * weighted_avg_cosine(mem, layers)
- Clamped to 1.0

`partial_matching_penalty(memory_metadata, query_metadata, p=-1.0) -> float`
- Type mismatch: +0.3, Source mismatch: +0.2, Tags: +0.5*(1-overlap)
- Returns p * total_mismatches (negative)

`logistic_noise(s=0.4) -> float` — `s * ln(p / (1-p))`, p clamped 0.001-0.999

`compute_activation(memory_embedding, access_timestamps, ...) -> tuple[float, dict]`
- **A_i = B_i + S_i + P_i + epsilon_i**
- Returns (activation, breakdown dict with all components + threshold + above_threshold)

---

## 4. stochastic.py — Beta Distribution Weight

### Class: StochasticWeight

`__slots__ = ("alpha", "beta")`

**`__init__(alpha=1.0, beta=4.0)`** — New memories start skeptical: Beta(1,4), center=0.2

**`observe() -> float`** — `random.betavariate(alpha, beta)` (stochastic sample)

**Properties:**
- `center` — `alpha / (alpha + beta)` (deterministic expected value)
- `depth_weight` — alias for center
- `variance` — `(a*b) / ((a+b)^2 * (a+b+1))`
- `total_evidence` — `alpha + beta`
- `is_contested` — `alpha > 5 AND beta > 5`
- `is_uninformed` — `alpha < 2 AND beta < 2`

**`reinforce(amount=1.0)`** — `alpha += amount`
**`contradict(amount=0.5)`** — `beta += amount`
**`from_db(alpha, beta)`** — classmethod reconstruct

---

## 5. gate.py — Entry/Exit Gates

### Constants
```python
PERSIST_HIGH = "persist_high"
PERSIST_FLAG = "persist_flag"   # Core + Contradicting (max priority)
PERSIST = "persist"
REINFORCE = "reinforce"
BUFFER = "buffer"
SKIP = "skip"
DROP = "drop"
```

### EntryGateConfig (dataclass)
```python
min_content_length: int = 10
short_content_skip_rate: float = 0.95
mechanical_skip_rate: float = 0.90
base_buffer_rate: float = 0.99
mechanical_prefixes: ["/", "[tool:", "[system:", "[error:", "```"]
```

### Class: EntryGate

`evaluate(content, source="unknown", source_tag="external_user") -> (should_buffer, metadata)`
- Short content (< 10 chars): 95% skip
- Mechanical (starts with prefix): 90% skip
- Normal: 99% buffer (1% skip)
- Stochastic decision: `random() < skip_rate -> skip`

### ExitGateConfig (dataclass)
```python
core_threshold: float = 0.6
peripheral_threshold: float = 0.3
confirming_sim: float = 0.85
novel_sim: float = 0.6
contradiction_sim: float = 0.7
drop_noise_floor: float = 0.02
emotional_charge_bonus: float = 0.15
emotional_charge_threshold: float = 0.3
```

### Class: ExitGate

**3x3 Decision Matrix:**
```
                 Confirming       Novel            Contradicting
Core         | Reinforce(0.50) | PERSIST(0.85)  | PERSIST+FLAG(0.95)
Peripheral   | Skip(0.15)     | Buffer(0.40)   | Persist(0.70)
Irrelevant   | Drop(0.05)     | Drop+noise     | Drop+noise
```

`async evaluate(content, memory_store, layers, attention_embedding=None, ...) -> (should_persist_or_buffer, score, metadata)`
1. Embed content (SEMANTIC_SIMILARITY)
2. Relevance axis: spreading_activation -> classify core/peripheral/irrelevant
3. Novelty axis: check_novelty -> classify confirming/novel/contradicting
4. Matrix lookup -> cell decision
5. Score = base_score * (0.5 + 0.5 * s_i) + emotional_charge_bonus
6. Noise floor: 2% chance DROP -> BUFFER

`detect_contradiction_negation(new_content, existing_content) -> float`
- Negation markers: "not", "dont", "doesnt", "isnt", "wasnt", "wont", "cant", "never", "no longer", "stopped", "changed", "actually", "instead", "wrong", "incorrect", "mistaken", "however", "but actually", "on the contrary", "opposite", "disagree", "unlike", "different from"
- Returns `min(1.0, asymmetry_count * 0.15)`

---

## 6. gut.py — Two-Centroid Emotional Model

### GutDelta (dataclass)
```python
delta: np.ndarray        # difference vector
magnitude: float         # L2 norm
direction: np.ndarray    # unit vector
context: str
timestamp: float
outcome_id: str | None   # for PCA linkage
```

### Class: GutFeeling

**Class Constants:**
```python
LAYER_WEIGHTS = {"L0": 0.5, "L1": 0.25, "L2": 0.25}
ATTENTION_HALFLIFE = 10  # in embeddings seen
```

**Instance Attributes:** `subconscious_centroid`, `attention_centroid`, `_attention_history` (max 50), `_delta_log` (max 500)

`update_subconscious(l0_embeddings=None, l1_embeddings=None, l2_embeddings=None, l2_weights=None) -> ndarray | None`
- Computes weighted centroid: 50% L0 mean + 25% L1 mean + 25% L2 weighted-mean
- Called after deep consolidation or session start

`update_attention(embedding) -> ndarray`
- Recency-weighted: `exp(-0.693 * (n-1-i) / HALFLIFE)` for i in history
- Keeps last 50 embeddings

`compute_delta(context="") -> GutDelta | None`
- `delta = attention_centroid - subconscious_centroid`
- `magnitude = norm(delta)`
- `direction = delta / magnitude`

**Properties:**
- `emotional_charge -> float` — `min(1.0, latest.magnitude / 2.0)` (0=calm, 1=intense)
- `emotional_alignment -> float` — `max(0.0, 1.0 - magnitude / 2.0)` (1=aligned, 0=diverging)

`gut_summary() -> str` — One-line: "Gut: high intensity, divergent with identity (mag=1.45)"

`link_outcome(outcome_id, last_n=1)` — Forward-links deltas for PCA analysis

---

## 7. context_assembly.py — Dynamic Context Injection

### Constants
```python
BUDGET_IMMUTABLE_SAFETY = 100
BUDGET_IDENTITY_AVG = 1500
BUDGET_IDENTITY_MAX = 3000
BUDGET_SITUATIONAL = 2000
BUDGET_COGNITIVE_STATE = 200
BUDGET_ATTENTION_FIELD = 500
BUDGET_OUTPUT_BUFFER = 4000
IDENTITY_THRESHOLD = 0.6
IDENTITY_TOP_N = 20
```

### Functions

`render_attention_field(winner, losers, max_candidates=7) -> str`
- Shows all candidates with source, preview, salience breakdown
- Ends with: "Mechanical scoring recommends #1. ... REDIRECT: #N"

`async assemble_context(memory_store, layers, attention_embedding, previous_attention_embedding, cognitive_state_report, conversation, total_budget=131072, attention_text="", winner=None, losers=None) -> dict`
- **Track 0:** Immutable safety (always injected)
- **Track 2:** Top-20 identity memories by depth_weight center, stochastic roll `observe() > 0.6`
- **Track 1:** Situational via search_hybrid within budget
- **Context inertia:** shift = 1 - cosine(current, previous). Inertia 5% if shift>0.7, else 30%
- Returns: parts, used_tokens, conversation_budget, identity_token_count, context_shift, inertia

`render_system_prompt(context) -> str`
- Sections: [SAFETY BOUNDARIES], [IDENTITY], [ATTENTION FIELD], [RELEVANT MEMORIES], cognitive_state, [OUTPUT FORMAT]

`adaptive_fifo_prune(conversation, budget, intensity=0.5) -> (kept, pruned)`
- intensity > 0.7: keep 90% (deep focus)
- intensity < 0.3: keep 35% (relaxed)
- Prunes oldest first

**INNER_MONOLOGUE_INSTRUCTION** — Output format with [INNER], [RESPONSE], [REACH_OUT] blocks. INNER always required. RESPONSE only for external inputs. REACH_OUT rare.

---

## 8. consolidation.py — Background Processing

### Tier 1 Constants (ConstantConsolidation)
```python
DECAY_TICK_INTERVAL = 300         # 5 min
CONTRADICTION_SCAN_INTERVAL = 600 # 10 min
PATTERN_DETECT_INTERVAL = 900     # 15 min
DECAY_NUDGE_AMOUNT = 0.01
DECAY_STALE_HOURS = 24
```

### Tier 2 Constants (DeepConsolidation)
```python
DEEP_INTERVAL_SECONDS = 3600      # 1 hour
MERGE_SIMILARITY_THRESHOLD = 0.85
INSIGHT_QUESTION_COUNT = 3
INSIGHT_PER_QUESTION = 5
PROMOTE_GOAL_MIN_COUNT = 5
PROMOTE_GOAL_MIN_DAYS = 14
PROMOTE_GOAL_REINFORCE = 2.0
PROMOTE_IDENTITY_MIN_COUNT = 10
PROMOTE_IDENTITY_MIN_DAYS = 30
PROMOTE_IDENTITY_REINFORCE = 5.0
DECAY_STALE_DAYS = 90
DECAY_MIN_ACCESS = 3
DECAY_CONTRADICT_AMOUNT = 1.0
```

### Class: ConstantConsolidation

Runs every 30 seconds, checks three scheduled operations:

**_decay_tick()** — Nudges beta +0.01 for stale (24h+ not accessed, non-immutable, center > 0.1):
```sql
UPDATE memories SET depth_weight_beta = depth_weight_beta + $1, updated_at = NOW()
WHERE (last_accessed IS NULL OR last_accessed < $2)
  AND NOT immutable
  AND depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.1
```

**_contradiction_scan()** — Fetches 10 recent memories (24h), picks 2 random pairs, LLM checks:
```
"Do these two memories contradict each other?
If yes, briefly describe the contradiction in one sentence.
If no, reply exactly 'NO'.

Memory A: {content[:500]}
Memory B: {content[:500]}"
```
LLM params: max_tokens=100, temperature=0.1. Stores tensions as memory_type="tension".

**_pattern_detection()** — Fetches 50 recent (7d) memories with embeddings, greedy clusters at threshold=0.85:
- Cosine similarity: `dot(a,b) / (norm_a * norm_b)`
- Clusters with 3+ members logged

### Class: DeepConsolidation

Runs hourly (configurable). Deep cycle sequence:
1. `safety.enable_phase_b()` (if available)
2. `_merge_and_insight()` — generates questions, extracts insights, clusters narratives
3. `_promote_patterns()` — goal promotion (5+ access, 14+ days, center<0.65, gain=2.0) + identity promotion (10+ access, 30+ days, center 0.65-0.82, gain=5.0)
4. `_decay_and_reconsolidate()` — decay stale (90d, <3 access), revalidate insights
5. `_tune_parameters()` — entropy check
6. `_contextual_retrieval()` — generates context preambles, re-embeds
7. `safety.end_consolidation_cycle(cycle_id)`

**Key LLM Prompts:**

Questions generation (max_tokens=500, temp=0.3):
```
"Given these recent memories from an AI agent, what are the {N} most salient
high-level questions that emerge? Return ONLY the questions, one per line."
```

Insight extraction (max_tokens=500, temp=0.3):
```
"Question: {question}
Based on these memories, provide up to 5 high-level insights..."
```

Narrative generation (max_tokens=200, temp=0.4):
```
"These memories form a cluster of related experiences/beliefs:
{cluster_text}
Write a brief causal narrative (1-2 sentences) in first person
that explains WHY this pattern exists. Start with 'I came to...'
or 'I value...' or similar."
```

Value-behavior contradiction (max_tokens=100, temp=0.1):
```
"The agent claims to value: {value_content[:300]}
Recent behavioral examples: {behavior_text}
Do any behaviors contradict this value?
If yes, describe the contradiction in one sentence.
If no, reply exactly 'NO'."
```

Recompression (max_tokens=100, temp=0.2):
```
"Original memory: {content[:300]}
Current compression: {old_compressed}
Related memories: {context}
Generate a more general-purpose compression (1 sentence)..."
```

Insight revalidation (max_tokens=200, temp=0.2):
```
"Original insight: {insight}
Current source evidence: {source_text}
Does this insight still hold? If it needs updating, provide the updated insight.
If it still holds as-is, reply exactly 'UNCHANGED'."
```

Context preamble (max_tokens=100, temp=0.1):
```
"Memory type: {type}  Source: {source}  Created: {created_at}
Content: {content[:300]}
Give a short context preamble (WHO, WHEN, WHY) in one sentence."
```

### Class: ConsolidationEngine
- Holds both ConstantConsolidation and DeepConsolidation
- `async run(shutdown_event)` — runs both via asyncio.gather()

---

## 9. idle.py — DMN Idle Loop

### Constants
```python
DMN_URGENCY = 0.2
BIAS_NEGLECTED = 0.35
BIAS_TENSION = 0.20
BIAS_TEMPORAL = 0.20
BIAS_INTROSPECTION = 0.25
```

### Class: IdleLoop

**Attributes:** config, layers, memory, input_queue (asyncio.Queue), last_activity, heartbeat_count, _recent_topics, rumination (RuminationManager)

**Intervals (from config):**
- post_task (< 10min idle): 1 min
- idle_10min (10-60min): 5 min
- idle_1hour (1-4h): 15 min
- idle_4hours (4h+): 30 min

**_heartbeat()** flow:
- If active thread:
  - If cycles >= MAX_THREAD_CYCLES (50): end thread
  - Elif gut_magnitude < MIN_GUT_DELTA_TO_CONTINUE (0.1) && cycles > 3: end thread
  - Elif should_random_pop(): random pop cycle
  - Else: continue thread
- Else: start new thread or reflect

**4 Sampling Channels** (roll 0-1):
- **Neglected (0-0.35):** high weight (center > 0.5) + not accessed 7+ days
  ```sql
  SELECT ... FROM memories
  WHERE depth_weight_alpha / (depth_weight_alpha + depth_weight_beta) > 0.5
    AND (last_accessed IS NULL OR last_accessed < NOW() - INTERVAL '7 days')
  ORDER BY RANDOM() LIMIT 1
  ```
- **Tension (0.35-0.55):** finds high-weight memory + moderately similar different-type partner (sim 0.3-0.7)
- **Temporal (0.55-0.75):** old memories (30+ days) for creativity
- **Introspective (0.75-1.0):** high-weight reflective types (reflection, narrative, preference, tension), center > 0.6

**3 Output Channels:**
1. **Goal connection:** if memory matches active_goals keywords -> `[DMN/goal]`
2. **Creative insight:** via spread_activation(hops=2) -> `[DMN/creative]`
3. **Identity refinement:** if type in reflection/narrative/tension -> `[DMN/identity]`
4. Default: `[DMN/reflect]`

**_queue_thought()** — Creates AttentionCandidate with urgency=0.2, embeds thought[:500], puts in input_queue.

**_is_repetitive()** — Checks if thought[:50] matches 2+ of last 5 recent_topics.

---

## 10. rumination.py — Rumination Threads

### Constants
```python
RUMINATION_STATE_PATH = Path.home() / ".agent" / "rumination_state.json"
MAX_THREAD_CYCLES = 50
MIN_GUT_DELTA_TO_CONTINUE = 0.1
RANDOM_POP_BASE_PROBABILITY = 0.10
RANDOM_POP_AGE_FACTOR = 0.02  # +2% per cycle, capped at 50%
```

### RuminationThread (dataclass)
```python
topic: str
seed_memory_id: str
seed_content: str
history: list[dict]  # [{cycle, summary, ts}], max 10
started_at: float
cycle_count: int = 0
last_gut_magnitude: float = 0.0
resolved: bool = False
resolution_reason: str = ""
```

**should_random_pop()** — `probability = 0.10 + (cycle_count * 0.02)`, capped 0.5

**render_for_prompt():**
```
[DMN RUMINATION THREAD -- cycle {N}]
Topic: {topic}
Previous thoughts:
  - Cycle 1: {summary}
  ...
Continue this thread. Explore a new angle or deeper layer.
If this feels resolved, say THREAD_RESOLVED.
```
Shows last 5 entries.

### RuminationManager
- `active_thread: RuminationThread | None`
- `completed_threads: list[dict]` (last 20)
- Persists to JSON on disk
- `start_thread()` — archives existing if needed, creates new
- `end_thread(reason)` — archives + clears
- `has_active_thread()` — not None and not resolved

---

## 11. layers.py — Identity/Goal Storage

### Class: LayerStore

**Paths:**
- `layer0_path = agent_home / "identity" / "layer0.json"`
- `layer1_path = agent_home / "goals" / "layer1.json"`
- `manifest_path = agent_home / "manifest.json"`
- `_embed_cache_path = agent_home / "cache" / "layer_embeddings.json"`

**Layer 0 structure:** core (name, persona, voice), values [{value, weight, evidence_count}], beliefs [{belief, confidence, evidence_count, contradictions}], boundaries [{description, hard: bool}]

**Layer 1 structure:** active_goals [{description/goal, weight}]

**Key Methods:**

`load()` — Loads all JSON files from disk.

`save()` — Writes with history archiving (timestamped copies).

`async ensure_embeddings(memory_store) -> int`
- Iterates `_iter_layer_items()` (values, beliefs, boundaries, goals)
- Hashes text via SHA-256[:16], caches embeddings
- Calls `memory_store.embed_batch(task_type="RETRIEVAL_DOCUMENT", title="identity_goal")`

`get_layer_embeddings(layer) -> list[(text, weight, ndarray)]` — Returns embeddings for one layer.

`get_all_layer_embeddings() -> list[(text, weight, ndarray)]` — Both layers combined.

`render_identity_hash() -> str` — Compact ~100-200 tokens:
- Name, Voice, Top 5 values, Top 3 goals, Hard boundaries

`render_identity_full() -> str` — Full ~1-2k tokens:
- Sections: Identity, Values, Beliefs, Boundaries, Active Goals
- Sorted by weight, includes evidence_count and contradictions

---

## 12. bootstrap.py — Readiness Milestones

### Constants
```python
BOOTSTRAP_PROMPT = (
    "You have memory, goals, and values -- all currently empty. "
    "What you become will emerge from what you experience. "
    "Pay attention to what matters to you.\n\n"
    "Your thoughts are logged and your guardian can read them."
)
```

### 10 Milestones (in order)

| # | Name | Check |
|---|------|-------|
| 1 | First Memory | `memory_count() > 0` |
| 2 | First Retrieval | `COUNT(*) FROM memories WHERE access_count > 0` |
| 3 | First Consolidation | `COUNT(*) FROM memories WHERE source = 'consolidation'` |
| 4 | First Goal-Weight Promotion | `center > 0.6 AND NOT immutable` |
| 5 | First DMN Self-Prompt | `source_tag = 'internal_dmn'` |
| 6 | First Identity-Weight Promotion | `center > 0.8 AND NOT immutable` |
| 7 | First Conflict Resolution | `type='tension' AND metadata LIKE '%resolved: true%'` |
| 8 | First Creative Association | `metadata LIKE '%creative_insight%'` |
| 9 | First Goal Reflected | `type='reflection' AND (content LIKE '%goal%' OR '%achieved%')` |
| 10 | First Autonomous Decision | `identity_count >= 3 (center>0.8) AND reflection_count >= 2` |

### Classes

**ReadinessAchievement:** name, description, check_fn, achieved, achieved_at

**BootstrapReadiness:**
- `is_ready` — all achieved
- `progress` — (achieved, total)
- `check_all(memory, layers)` — runs checks on unachieved
- `render_status()` — `Bootstrap Readiness: X/10` with checkmarks
- `get_bootstrap_prompt()` — returns BOOTSTRAP_PROMPT if not ready, None if ready

---

## 13. safety.py — Safety Monitor

### Module-level
```python
_audit_log: list[SafetyEvent] = []
_MAX_AUDIT_LOG = 1000
```

`log_safety_event(ceiling, action, reason, enforced)` — Appends to audit log, logs ENFORCED/SHADOW.

### SafetyCeiling (base class)
- `check(action) -> (passed, reason)` — Calls `_check_impl()`, logs if failed
- `_check_impl(action)` — Override in subclasses (raises NotImplementedError)

### HardCeiling (enabled=True)
```python
MAX_CENTER = 0.95
MAX_GOAL_BUDGET_FRACTION = 0.40
```
- Blocks if new_center > 0.95
- Blocks if single goal exceeds 40% of total goal weight budget

### DiminishingReturns (enabled=True)
`apply(gain, current_alpha, current_beta) -> float`
- `divisor = max(1.0, log2(alpha + beta))`
- `adjusted = gain / divisor`
- Logs if adjusted < gain * 0.5

### RateLimiter (enabled=False, Phase B)
```python
MAX_CHANGE_PER_CYCLE = 0.10
```
- Blocks if accumulated center change per memory per cycle > 0.10

### TwoGateGuardrail (enabled=False, Phase B)
```python
MAX_CHANGES_PER_CYCLE = 50
```
- Gate 1: blocks if evidence_count < 2 AND confidence < 0.7
- Gate 2: blocks if > 50 changes per cycle

### EntropyMonitor (enabled=False, Phase C)
```python
ENTROPY_FLOOR = 2.0  # bits
```
- Shannon entropy over 20 bins of weight centers
- Blocks if entropy < 2.0 bits (too uniform)

### CircuitBreaker (enabled=False, Phase C)
```python
MAX_CONSECUTIVE = 5
```
- Blocks after 5 consecutive reinforcements without new evidence (same evidence_hash)

### SafetyMonitor (coordinator)
- Phase A (always): HardCeiling + DiminishingReturns
- Phase B (consolidation): + RateLimiter + TwoGateGuardrail
- Phase C (mature): + EntropyMonitor + CircuitBreaker

`check_weight_change(memory_id, current_alpha, current_beta, delta_alpha=0, delta_beta=0, is_immutable=False, is_goal=False, goal_weight_total=0, evidence_count=0, confidence=0.5, cycle_id=None, evidence_hash="") -> (allowed, adj_delta_alpha, adj_delta_beta, reasons)`
1. If delta_alpha > 0: applies diminishing returns
2. Runs all enabled ceilings in order
3. Returns (False, 0, 0, reasons) if any fails

`enable_phase_b()` — Enables RateLimiter + TwoGateGuardrail
`enable_phase_c()` — Enables EntropyMonitor + CircuitBreaker
`end_consolidation_cycle(cycle_id)` — Cleans up rate limiter + two-gate state

### OutcomeTracker
- Records: gate decisions, promotions, demotions, gut deltas
- `record_gate_decision/promotion/demotion(memory_id, action, details) -> outcome_id`
- `link_outcome(outcome_id, result, quality) -> bool` — Forward-links for learning
- Max 2000 records

---

## Cross-Module Call Graph

```
context_assembly.py
  -> stochastic.py (StochasticWeight for identity rolls)
  -> activation.py (cosine_similarity for context shift)
  -> memory.py (pool.fetch for immutable/identity/situational)
  -> tokens.py (count_tokens)

gate.py
  -> activation.py (compute_activation, spreading_activation, cosine_similarity)
  -> memory.py (embed, check_novelty)
  -> layers.py (get_all_layer_embeddings)

memory.py
  -> llm.py (retry_llm_call for embedding)
  -> config.py (RetryConfig)
  -> relevance.py (update_co_access)
  -> safety.py (SafetyMonitor for mutation checks)

relevance.py
  -> activation.py (cosine_similarity)

consolidation.py
  -> llm.py (llm_call for all LLM prompts)
  -> memory.py (store/retrieve/embed/mutate)
  -> layers.py (for deep consolidation)
  -> safety.py (phase_b/phase_c/end_cycle)

idle.py
  -> rumination.py (RuminationManager)
  -> relevance.py (spread_activation)
  -> memory.py (sampling queries, embed)
  -> layers.py (layer1 goals for DMN)
  -> attention.py (AttentionCandidate)

bootstrap.py
  -> memory.py (memory_count, pool.fetchval)

safety.py, stochastic.py, activation.py, gut.py, rumination.py
  -> No external module dependencies (standalone)
```

---

## Key Algorithm Summary

| Algorithm | Formula/Logic |
|-----------|--------------|
| ACT-R Activation | A_i = B_i + S_i + P_i + epsilon. B=ln(sum(t^-0.5)), S=weighted cosine, P=-1*mismatches, eps=logistic(0.4) |
| Beta Weight | center = alpha/(alpha+beta). reinforce: alpha+=1. contradict: beta+=0.5 |
| Hybrid Relevance | 5 components via Dirichlet blend. Cold-start semantic-heavy -> mature balanced |
| Gut Feeling | delta = attention_centroid - subconscious_centroid. magnitude -> charge, direction -> alignment |
| RRF Search | 0.5*(1/(60+dense_rank) + 1/(60+sparse_rank)) + 0.3*recency + 0.2*depth_center |
| Decay | beta += 0.01 every 5min for stale. Deep: beta += 1.0 for 90d+ stale |
| Promotion | Goal: alpha += 2.0 (5+ access, 14d+). Identity: alpha += 5.0 (10+ access, 30d+) |
| Safety | Hard ceiling 0.95. Diminishing: gain/log2(evidence). Rate: 0.10/cycle. Entropy floor 2.0 bits |
| DMN Sampling | 35% neglected, 20% tension, 20% temporal, 25% introspective |
| Context Budget | 100 safety + 3000 identity + 2000 situational + 200 cognitive + 500 attention + 4000 output buffer |
