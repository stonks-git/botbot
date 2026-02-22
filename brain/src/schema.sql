-- Brain Service Schema — PostgreSQL 17 + pgvector
-- All tables include agent_id for multi-agent namespacing.

CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- MEMORIES — Unified memory table with Beta(alpha, beta) weight distributions
-- =============================================================================
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT        PRIMARY KEY,
    agent_id        TEXT        NOT NULL,
    content         TEXT        NOT NULL,
    type            TEXT        NOT NULL DEFAULT 'semantic',
    embedding       halfvec(3072),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source          TEXT,
    tags            TEXT[]      DEFAULT '{}',
    confidence      FLOAT       DEFAULT 0.5,
    importance      FLOAT       DEFAULT 0.5,
    evidence_count  INT         DEFAULT 0,
    metadata        JSONB       DEFAULT '{}',
    source_tag      TEXT        DEFAULT 'external_user',

    -- Beta distribution weight (replaces fixed depth_weight)
    -- Default Beta(1, 4): center ~0.2, wide uncertainty = blank-slate new memory
    depth_weight_alpha  FLOAT   DEFAULT 1.0,
    depth_weight_beta   FLOAT   DEFAULT 4.0,

    -- Safety / immutability
    immutable       BOOLEAN     DEFAULT FALSE,

    -- Soft delete for dedup (D-026)
    archived        BOOLEAN     DEFAULT FALSE,
    archived_reason JSONB,

    -- Compressed summary (re-generated during consolidation)
    compressed      TEXT,

    -- Access tracking (ACT-R base-level learning)
    access_count    INT         DEFAULT 0,
    last_accessed   TIMESTAMPTZ,
    access_timestamps TIMESTAMPTZ[] DEFAULT '{}',

    -- Contextual retrieval (WHO/WHEN/WHY preamble for better embeddings)
    content_contextualized TEXT,

    -- Full-text search (indexes contextualized content when available)
    content_tsv     tsvector    GENERATED ALWAYS AS (
        to_tsvector('english', COALESCE(content_contextualized, content))
    ) STORED,

    -- Semantic timestamp (TSM dual time: when the event happened vs when stored)
    event_time      TIMESTAMPTZ,

    -- Embedding model version tracking
    embed_model     TEXT        DEFAULT 'gemini-embedding-001',

    -- Utility score for promotion decisions
    utility_score   FLOAT       DEFAULT 0.0,

    -- Memory group linking (D-018c): chunks from semantic chunking share group_id
    memory_group_id TEXT,

    -- Scheduled reminder (D-029): agent-set wake-up time
    remind_at       TIMESTAMPTZ,

    -- Decay protection (D-031): agent-set protection expiry
    protect_until   TIMESTAMPTZ
);

-- Vector similarity search (HNSW for approximate nearest neighbor)
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON memories USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 128);

-- Full-text search
CREATE INDEX IF NOT EXISTS idx_memories_fts
    ON memories USING GIN (content_tsv);

-- Agent scoping (most queries filter by agent_id)
CREATE INDEX IF NOT EXISTS idx_memories_agent_id
    ON memories (agent_id);

-- Type + agent compound (for type-filtered queries)
CREATE INDEX IF NOT EXISTS idx_memories_agent_type
    ON memories (agent_id, type);

-- Access patterns (for consolidation decay decisions)
CREATE INDEX IF NOT EXISTS idx_memories_access
    ON memories (agent_id, access_count, last_accessed);

-- Tags (for tag-filtered queries)
CREATE INDEX IF NOT EXISTS idx_memories_tags
    ON memories USING GIN (tags);

-- Weight center approximation (for consolidation promotion scans)
CREATE INDEX IF NOT EXISTS idx_memories_weight_center
    ON memories ((depth_weight_alpha / (depth_weight_alpha + depth_weight_beta)));

-- Memory group linking (D-018c): migration + partial index
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN memory_group_id TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_memories_group
    ON memories (memory_group_id) WHERE memory_group_id IS NOT NULL;

-- Scheduled reminder lookup (D-029): partial index for due reminder queries
CREATE INDEX IF NOT EXISTS idx_memories_remind_at
    ON memories (remind_at) WHERE remind_at IS NOT NULL AND NOT archived;

-- Decay protection expiry lookup (D-031): partial index for protected memory queries
CREATE INDEX IF NOT EXISTS idx_memories_protect_until
    ON memories (protect_until) WHERE protect_until IS NOT NULL AND NOT archived;

-- Archived soft-delete filter (D-026): partial index on archived=true only
CREATE INDEX IF NOT EXISTS idx_memories_archived
    ON memories (archived) WHERE archived = true;


-- =============================================================================
-- DEDUP VERDICTS — Tracks already-verified dedup pairs to avoid repeat LLM calls (D-026)
-- =============================================================================
CREATE TABLE IF NOT EXISTS dedup_verdicts (
    id              SERIAL      PRIMARY KEY,
    agent_id        TEXT        NOT NULL,
    mem_a_id        TEXT        NOT NULL,
    mem_b_id        TEXT        NOT NULL,
    verdict         TEXT        NOT NULL,  -- 'redundant' | 'distinct'
    survivor_id     TEXT,
    survivor_label  TEXT,       -- 'A' | 'B' | 'synthesize' (BUG-003 fix)
    reason          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (agent_id, mem_a_id, mem_b_id)
);

CREATE INDEX IF NOT EXISTS idx_dedup_verdicts_agent
    ON dedup_verdicts (agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_dedup_verdicts_pair
    ON dedup_verdicts (agent_id, mem_a_id);


-- =============================================================================
-- SCRATCH BUFFER — Temporary entry gate buffer with TTL
-- =============================================================================
CREATE TABLE IF NOT EXISTS scratch_buffer (
    id          TEXT        PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    source      TEXT,
    tags        TEXT[]      DEFAULT '{}',
    metadata    JSONB       DEFAULT '{}',
    buffered_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_scratch_agent
    ON scratch_buffer (agent_id);

CREATE INDEX IF NOT EXISTS idx_scratch_expires
    ON scratch_buffer (expires_at);


-- =============================================================================
-- MEMORY CO-ACCESS — Hebbian learning via spreading activation
-- =============================================================================
CREATE TABLE IF NOT EXISTS memory_co_access (
    memory_id_a     TEXT        NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    memory_id_b     TEXT        NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    agent_id        TEXT        NOT NULL,
    co_access_count INT         DEFAULT 1,
    last_co_accessed TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (memory_id_a, memory_id_b)
);

CREATE INDEX IF NOT EXISTS idx_co_access_agent
    ON memory_co_access (agent_id);

CREATE INDEX IF NOT EXISTS idx_co_access_a
    ON memory_co_access (memory_id_a);

CREATE INDEX IF NOT EXISTS idx_co_access_b
    ON memory_co_access (memory_id_b);


-- =============================================================================
-- MEMORY SUPERSEDES — Links consolidated insights to source memories
-- =============================================================================
CREATE TABLE IF NOT EXISTS memory_supersedes (
    insight_id  TEXT        NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    source_id   TEXT        NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    agent_id    TEXT        NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (insight_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_supersedes_agent
    ON memory_supersedes (agent_id);


-- =============================================================================
-- CONSOLIDATION LOG — Audit trail for background processing
-- =============================================================================
CREATE TABLE IF NOT EXISTS consolidation_log (
    id          SERIAL      PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    operation   TEXT        NOT NULL,
    details     JSONB       DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_consolidation_agent
    ON consolidation_log (agent_id, created_at DESC);


-- =============================================================================
-- DMN LOG — Persisted DMN thoughts for observability
-- =============================================================================
CREATE TABLE IF NOT EXISTS dmn_log (
    id                  SERIAL      PRIMARY KEY,
    agent_id            TEXT        NOT NULL,
    thought             TEXT        NOT NULL,
    channel             TEXT        NOT NULL,
    source_memory_id    TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dmn_log_agent
    ON dmn_log (agent_id, created_at DESC);


-- =============================================================================
-- INJECTION LOGS — w×s identity injection decisions for analytics (D-018d)
-- =============================================================================
CREATE TABLE IF NOT EXISTS injection_logs (
    id              SERIAL      PRIMARY KEY,
    agent_id        TEXT        NOT NULL,
    memory_id       TEXT        NOT NULL,
    weight_center   FLOAT       NOT NULL,
    cosine_sim      FLOAT       NOT NULL,
    injection_score FLOAT       NOT NULL,
    was_injected    BOOLEAN     NOT NULL,
    query_hash      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_injection_logs_agent
    ON injection_logs (agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_injection_logs_memory
    ON injection_logs (memory_id);

CREATE INDEX IF NOT EXISTS idx_injection_logs_query
    ON injection_logs (query_hash);


-- =============================================================================
-- CONTEXT SHIFT BUFFER — Ring buffer for adaptive threshold (D-018a)
-- =============================================================================
CREATE TABLE IF NOT EXISTS context_shift_buffer (
    id          SERIAL      PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    shift_value FLOAT       NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_context_shift_agent
    ON context_shift_buffer (agent_id, created_at DESC);


-- =============================================================================
-- MIGRATIONS — Additive column changes (idempotent)
-- =============================================================================

-- =============================================================================
-- NOTIFICATION OUTBOX — Queued notifications with urgency/importance (D-019)
-- =============================================================================
CREATE TABLE IF NOT EXISTS notification_outbox (
    id               SERIAL      PRIMARY KEY,
    agent_id         TEXT        NOT NULL,
    content          TEXT        NOT NULL,
    urgency          FLOAT       NOT NULL DEFAULT 0.0,
    importance       FLOAT       NOT NULL DEFAULT 0.0,
    source           TEXT        NOT NULL,
    source_memory_id TEXT,
    channel          TEXT        NOT NULL DEFAULT 'passive',
    status           TEXT        NOT NULL DEFAULT 'pending',
    metadata         JSONB       DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    delivered_at     TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_agent
    ON notification_outbox (agent_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_delivery
    ON notification_outbox (status, channel, created_at)
    WHERE status = 'pending';


-- =============================================================================
-- NOTIFICATION PREFERENCES — Per-agent notification settings (D-019)
-- =============================================================================
CREATE TABLE IF NOT EXISTS notification_preferences (
    agent_id             TEXT    PRIMARY KEY,
    telegram_chat_id     TEXT,
    telegram_enabled     BOOLEAN DEFAULT FALSE,
    quiet_hours_start    INT     DEFAULT 23,
    quiet_hours_end      INT     DEFAULT 7,
    urgency_threshold    FLOAT   DEFAULT 0.7,
    importance_threshold FLOAT   DEFAULT 0.5,
    enabled              BOOLEAN DEFAULT TRUE,
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);


-- =============================================================================
-- RESEARCH QUEUE — 2-search confirmation for contradiction research (D-016/DJ-008)
-- =============================================================================
CREATE TABLE IF NOT EXISTS research_queue (
    id                      SERIAL      PRIMARY KEY,
    agent_id                TEXT        NOT NULL,
    tension_id              TEXT        NOT NULL,
    mem_a_id                TEXT        NOT NULL,
    mem_b_id                TEXT        NOT NULL,
    classification          JSONB       NOT NULL,
    status                  TEXT        NOT NULL DEFAULT 'pending',
    first_result            JSONB,
    second_result           JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    first_researched_at     TIMESTAMPTZ,
    second_researched_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_research_queue_status
    ON research_queue (status, created_at);


-- D-021: insight_level for meta-insight exclusion in pattern detection
-- 0 = regular memory, 1 = insight, 2 = meta-insight (excluded from future clustering)
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN insight_level INT DEFAULT 0;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- D-026: archived soft-delete for dedup
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN archived BOOLEAN DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN archived_reason JSONB;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- D-029: remind_at column for scheduled reminders
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN remind_at TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- D-031: protect_until column for decay protection
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN protect_until TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- BUG-003: survivor_label column for dedup cache reconstruction
DO $$ BEGIN
    ALTER TABLE dedup_verdicts ADD COLUMN survivor_label TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;
