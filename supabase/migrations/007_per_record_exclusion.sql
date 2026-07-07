-- 007_per_record_exclusion.sql
-- Let a reviewer exclude SPECIFIC records from a duplicate set (mark them
-- "not a duplicate") without discarding the whole set. Excluded records are
-- never merged into the winner — they're left untouched as standalone records.
-- Idempotent / safe. Apply in the Supabase SQL editor.

ALTER TABLE duplicate_sets
  ADD COLUMN IF NOT EXISTS excluded_record_ids TEXT[] DEFAULT '{}';
