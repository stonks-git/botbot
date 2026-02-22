-- Migration 002: Add remind_at column for scheduled reminders (D-029)
--
-- Safe to re-run (all statements are idempotent).
-- Also applied automatically via schema.sql on next restart.

-- 1. Add remind_at column to memories
DO $$ BEGIN
    ALTER TABLE memories ADD COLUMN remind_at TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- 2. Partial index for efficient due-reminder queries
CREATE INDEX IF NOT EXISTS idx_memories_remind_at
    ON memories (remind_at) WHERE remind_at IS NOT NULL AND NOT archived;
