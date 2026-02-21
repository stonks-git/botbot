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
    utility_score   FLOAT       DEFAULT 0.0
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
