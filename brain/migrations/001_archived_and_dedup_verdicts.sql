-- Migration 001: Add archived soft-delete + dedup_verdicts table (D-026)
--
-- Safe to re-run (all statements are idempotent).
-- Also applied automatically via schema.sql on next restart.

-- 1. Add archived columns to memories
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN archived BOOLEAN DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN archived_reason JSONB;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- 2. Partial index for efficient archived filtering
CREATE INDEX IF NOT EXISTS idx_memories_archived
    ON memories (archived) WHERE archived = true;

-- 3. Dedup verdicts tracking table
CREATE TABLE IF NOT EXISTS dedup_verdicts (
    id          SERIAL      PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    mem_a_id    TEXT        NOT NULL,
    mem_b_id    TEXT        NOT NULL,
    verdict     TEXT        NOT NULL,  -- 'redundant' | 'distinct'
    survivor_id TEXT,
    reason      TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (agent_id, mem_a_id, mem_b_id)
);

CREATE INDEX IF NOT EXISTS idx_dedup_verdicts_agent
    ON dedup_verdicts (agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_dedup_verdicts_pair
    ON dedup_verdicts (agent_id, mem_a_id);
