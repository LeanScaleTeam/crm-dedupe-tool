-- Phase 0 (safety): make the merge path gate-aware and partial-merge-safe.
--
-- Background: before this migration the merge executor selected duplicate sets
-- by (excluded = false AND merged = false) ONLY — it never consulted any
-- verification/approval state, and a merge request with no set_ids meant
-- "merge everything". This migration adds the columns the executor now requires
-- so that nothing merges without an explicit, recorded human/auto decision, and
-- so a partially-merged set can resume without re-merging already-deleted losers.
--
-- Run in the Supabase SQL editor AFTER 001 and 002.

-- 1. Per-set decision + verification state -----------------------------------
ALTER TABLE duplicate_sets
    ADD COLUMN IF NOT EXISTS decision TEXT NOT NULL DEFAULT 'pending'
        CHECK (decision IN ('pending', 'approved', 'excluded', 'escalated', 'merged')),
    -- queue mirrors the match engine's verification gate output for accounts
    -- ('auto_merge' | 'needs_review' | 'escalated' | 'known_active'); NULL for
    -- the legacy contact path until that engine emits it (Phase 2).
    ADD COLUMN IF NOT EXISTS queue TEXT,
    ADD COLUMN IF NOT EXISTS verification_status TEXT,
    ADD COLUMN IF NOT EXISTS verification_reason TEXT,
    ADD COLUMN IF NOT EXISTS certainty TEXT,
    ADD COLUMN IF NOT EXISTS decided_by UUID REFERENCES auth.users(id),
    ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;

-- 2. Partial-merge bookkeeping ------------------------------------------------
-- Loser IDs that Salesforce has already absorbed (deleted) for this set. On
-- resume the executor merges only loser_record_ids that are NOT in this list,
-- so a set that merged 2 of 4 losers then failed never re-attempts the 2 gone.
ALTER TABLE duplicate_sets
    ADD COLUMN IF NOT EXISTS merged_loser_ids TEXT[] NOT NULL DEFAULT '{}';

-- 3. Helpful indexes ----------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_duplicate_sets_decision ON duplicate_sets(decision);
CREATE INDEX IF NOT EXISTS idx_duplicate_sets_queue ON duplicate_sets(queue);

-- 4. Backfill: existing excluded sets keep their intent; everything else stays
--    'pending' (i.e. NOT mergeable until explicitly approved). This is the safe
--    default — no pre-existing set becomes auto-mergeable by this migration.
UPDATE duplicate_sets SET decision = 'excluded' WHERE excluded = TRUE AND decision = 'pending';
UPDATE duplicate_sets SET decision = 'merged'   WHERE merged   = TRUE AND decision = 'pending';
