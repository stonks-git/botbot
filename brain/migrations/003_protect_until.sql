-- Migration 003: Add protect_until column for decay protection (D-031)
--
-- Safe to re-run (all statements are idempotent).
-- Also applied automatically via schema.sql on next restart.

-- 1. Add protect_until column to memories
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN protect_until TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- 2. Partial index for efficient expired-protection queries
CREATE INDEX IF NOT EXISTS idx_memories_protect_until
    ON memories (protect_until) WHERE protect_until IS NOT NULL AND NOT archived;
