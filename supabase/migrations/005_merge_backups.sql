-- 005_merge_backups.sql
-- Pre-merge safety net. BEFORE the irreversible CRM merge, run_merge snapshots each
-- duplicate set's winner + losers into this table. The snapshot is a PRECONDITION,
-- enforced in code (api/app/routers/merge.py via app/services/merge_backup.py): if
-- the backup write fails, that set is NOT merged — we never destroy records we
-- couldn't back up first.
--
-- This is a BACKUP, NOT AN UNDO. A Salesforce merge deletes the loser records and
-- their Ids cannot be resurrected (a re-created record gets a NEW Id and loses its
-- relationships); a HubSpot merge collapses records similarly. The snapshot lets you
-- AUDIT exactly what existed pre-merge and MANUALLY re-create or reconcile the lost
-- data (see scripts/restore_from_backup.py) — it does not reverse the merge.
--
-- Tenant-stamped and consistent with 004: SELECT is visible within the tenant; all
-- writes are service-role only (no JWT write policy). Run AFTER 001-004.

BEGIN;

CREATE TABLE merge_backups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- The merge run this snapshot belongs to. CASCADE so a deleted merge cleans up.
    merge_id UUID NOT NULL REFERENCES merges(id) ON DELETE CASCADE,
    -- Lineage for auditing/restore. SET NULL (not CASCADE) on scan delete so the
    -- backup survives even if the originating scan is later removed.
    scan_id UUID REFERENCES scans(id) ON DELETE SET NULL,
    -- duplicate_sets.id. No FK: the set row may be edited/removed post-merge, but the
    -- backup must persist regardless.
    set_id UUID,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    crm_type TEXT,
    connection_id UUID,
    -- The surviving (winner) record + its pre-blend field state, so the winner's
    -- original values can be reconstructed if the blended write was wrong.
    winner_record_id TEXT NOT NULL,
    winner_snapshot JSONB,
    -- Every loser the merge will absorb/delete, plus their full pre-merge state.
    loser_record_ids TEXT[] NOT NULL,
    loser_snapshot JSONB,
    -- What the merge wrote onto the winner (the blended properties), for audit.
    blended_properties JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    -- One snapshot per (merge run, set): a resume keeps the FIRST (truest pre-merge)
    -- snapshot rather than overwriting it with a post-partial-merge state.
    UNIQUE (merge_id, set_id)
);

CREATE INDEX idx_merge_backups_merge  ON merge_backups(merge_id);
CREATE INDEX idx_merge_backups_tenant ON merge_backups(tenant_id);
CREATE INDEX idx_merge_backups_set    ON merge_backups(set_id);

-- RLS: readable within the tenant (mirrors every other data table after 004); the
-- service-role backend is the only writer, so there is no INSERT/UPDATE/DELETE policy.
ALTER TABLE merge_backups ENABLE ROW LEVEL SECURITY;

CREATE POLICY "merge backups visible within tenant"
    ON merge_backups FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));

COMMIT;
