-- Migration 004: Add survivor_label column to dedup_verdicts (BUG-003)
--
-- Safe to re-run (all statements are idempotent).
-- Also applied automatically via schema.sql on next restart.

-- 1. Add survivor_label column to dedup_verdicts
DO $$ BEGIN
    ALTER TABLE dedup_verdicts ADD COLUMN survivor_label TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;
