-- ============================================================================
-- LIVE INTEGRATION TEST — apply migrations 003, 004, 005 in order.
-- Paste this whole file into the Supabase SQL Editor (project bpjgstwayjhsmaaxrwru)
-- and Run. Onto the current 001-only schema. NB: this project is NOT empty — as of
-- 2026-06-12 it carries existing rows (1 HubSpot connection, 2 contacts scans, 21
-- duplicate_sets, 2 merges; lineage verified intact). 003 adds columns with defaults;
-- 004 backfills every existing connection/scan/merge into one tenant via its lineage,
-- then SET NOT NULL (succeeds because lineage is complete). Reversible via the 004/005
-- down-migrations.
--
-- 002 is intentionally SKIPPED: it only widens the scans.object_type CHECK to allow
-- 'accounts'/'leads'. This bundle enables the CONTACTS end-to-end path (the only one
-- with a real merge — accounts are a view-only dry-run). If you later want to run the
-- accounts dry-run against this DB, ALSO apply 002_accounts_object_type.sql (it just
-- re-adds a widened CHECK; nothing destructive). Without 002, an accounts scan is
-- correctly rejected by the 001 CHECK.
--
-- IDEMPOTENT / RE-RUNNABLE: every statement is guarded (IF [NOT] EXISTS, DROP POLICY
-- IF EXISTS, CREATE OR REPLACE), and the 004 backfill only touches still-untenanted
-- connections (WHERE tenant_id IS NULL), so re-pasting the whole file after a partial
-- or full run is safe and will not error or create duplicate tenants. There is NO
-- inline BEGIN/COMMIT and NO temp table: the Supabase SQL editor executes statements
-- in autocommit (it ignores inline transaction control, and a CREATE TEMP TABLE ...
-- ON COMMIT DROP would be dropped before the next statement) — so the script relies on
-- per-statement idempotency, not transaction atomicity. If a statement fails, fix it
-- and re-paste the whole file; completed statements stay and the rest converge.
-- ============================================================================


-- >>>>>>>>>>>>>>>>>>>>>>>>  003_phase0_merge_safety.sql  <<<<<<<<<<<<<<<<<<<<<<<<

-- Phase 0 (safety): make the merge path gate-aware and partial-merge-safe.
--
-- Background: before this migration the merge executor selected duplicate sets
-- by (excluded = false AND merged = false) ONLY — it never consulted any
-- verification/approval state, and a merge request with no set_ids meant
-- "merge everything". This migration adds the columns the executor now requires
-- so that nothing merges without an explicit, recorded human/auto decision, and
-- so a partially-merged set can resume without re-merging already-deleted losers.
--
-- Part of the consolidated LIVE_apply bundle; depends only on 001.

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


-- >>>>>>>>>>>>>>>>>>>>>>>>  004_multi_tenant.sql  <<<<<<<<<<<<<<<<<<<<<<<<

-- 004_multi_tenant.sql
-- Multi-tenant model.  tenant = connected org (one tenant per crm_connection).
--
-- This migration is deliberately ADDITIVE and REVERSIBLE:
--   * it NEVER drops or repurposes `user_id` — that column stays populated as the
--     created_by / actor and is the anchor 004_multi_tenant_down.sql restores to,
--     so a full rollback loses no data;
--   * `tenant_id` columns are added nullable, backfilled from each row's lineage,
--     then set NOT NULL;
--   * RLS pivots from `auth.uid() = user_id` to tenant membership, but the down
--     migration recreates the original user_id policies verbatim.
--
-- Two rollback levers (fine-grained -> nuclear):
--   1. SURGICAL, no DDL:  UPDATE tenants SET client_access_enabled = false  cuts off
--      all *client*-role access to that tenant instantly. Owners, platform_staff and
--      the service-role backend are unaffected. Flip back to true to restore.
--   2. FULL:  run 004_multi_tenant_down.sql to restore the exact pre-tenant state.
--
-- Access model enforced here (and mirrored in the backend by app/services/tenancy.py,
-- which matters because the backend uses the service-role key and BYPASSES RLS):
--   access(tenant) :=  is_platform_staff(user)
--                      OR ( active member of tenant
--                           AND ( role = 'owner'
--                                 OR ( role = 'client' AND tenant.client_access_enabled ) ) )
--
-- Deploy note: apply this together with the tenant-aware backend. With tenant_id
-- NOT NULL, the OLD backend (which inserts connections/scans without tenant_id) would
-- fail INSERTs on new rows — a fail-safe error, not data corruption, but apply +
-- deploy as one step. Part of the consolidated LIVE_apply bundle (depends on 001+003).
--
-- NB: NO explicit BEGIN/COMMIT. The Supabase SQL editor runs statements in autocommit
-- (it ignores inline transaction control), so every statement below is individually
-- idempotent and NO temp table is used (a CREATE TEMP TABLE ... ON COMMIT DROP would
-- be dropped before the next statement could read it). A failed run can just be
-- re-pasted; it converges.

-- 1. Tenancy tables ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE,
    -- Gates CLIENT-role members ONLY. Owners + platform_staff are never gated.
    -- Default FALSE: a freshly-connected tenant exposes nothing to invited client
    -- users until LeanScale flips this on. Flip back to false to roll client access
    -- back (rollback lever #1). Backfilled tenants are ALSO set FALSE below (they
    -- have only owner members at migration time, and owners are never gated).
    client_access_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_members (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    -- owner  = the operator who connected the org (always has access);
    -- client = an invited viewer (gated by tenants.client_access_enabled).
    role TEXT NOT NULL DEFAULT 'client' CHECK (role IN ('owner', 'client')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_members_user ON tenant_members(user_id);

-- LeanScale internal operators: cross-tenant access (they run the dedupe service).
-- Membership here is a deliberate, backend-only grant. Empty this table (or drop it
-- via the down migration) to remove ALL staff cross-tenant access at once.
CREATE TABLE IF NOT EXISTS platform_staff (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. tenant_id columns (nullable now; backfilled; set NOT NULL at the end) ----

ALTER TABLE crm_connections ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE scans           ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE merges          ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE reports         ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_crm_connections_tenant ON crm_connections(tenant_id);
CREATE INDEX IF NOT EXISTS idx_scans_tenant   ON scans(tenant_id);
CREATE INDEX IF NOT EXISTS idx_merges_tenant  ON merges(tenant_id);
CREATE INDEX IF NOT EXISTS idx_reports_tenant ON reports(tenant_id);

-- 3. Backfill (tenant = connected org) ---------------------------------------
-- TEMP-TABLE-FREE: the editor's autocommit drops a CREATE TEMP TABLE ... ON COMMIT
-- DROP before the next statement can use it. Instead derive a STABLE tenant id from
-- each connection id via uuid_generate_v5 (a hash of the conn id, not random), so the
-- three statements below independently compute the SAME id with no shared state.
-- Order: create the tenant + owner membership for each still-untenanted connection
-- FIRST (while tenant_id IS NULL still identifies it), then point the connection at
-- its tenant LAST. Every statement is guarded, so a re-run is a no-op — an already-
-- tenanted connection (e.g. from an earlier partial run) is skipped and keeps the
-- tenant id it already has.

-- One tenant per still-untenanted connection. client_access_enabled = FALSE (secure
-- by default): the only members created here are OWNERS (below), who are never gated
-- by the flag, so FALSE grants nothing away. It only governs future invited CLIENTs.
INSERT INTO tenants (id, name, client_access_enabled)
SELECT
    uuid_generate_v5(uuid_ns_url(), 'dedupe-tenant:' || c.id::text),
    -- Human label: Salesforce portal_id is "orgId|instanceUrl" -> take orgId;
    -- HubSpot portal_id is the hub id. Fall back to the crm_type if blank.
    COALESCE(NULLIF(split_part(c.portal_id, '|', 1), ''), c.crm_type)
        || ' (' || c.crm_type || ')',
    FALSE
FROM crm_connections c
WHERE c.tenant_id IS NULL
ON CONFLICT (id) DO NOTHING;

-- The connecting user becomes that tenant's OWNER (always-on access). Same derived id.
INSERT INTO tenant_members (tenant_id, user_id, role, is_active)
SELECT
    uuid_generate_v5(uuid_ns_url(), 'dedupe-tenant:' || c.id::text),
    c.user_id, 'owner', TRUE
FROM crm_connections c
WHERE c.tenant_id IS NULL
ON CONFLICT (tenant_id, user_id) DO NOTHING;

-- Point each still-untenanted connection at its tenant (LAST, so the guards above
-- still saw it as untenanted).
UPDATE crm_connections c
SET tenant_id = uuid_generate_v5(uuid_ns_url(), 'dedupe-tenant:' || c.id::text)
WHERE c.tenant_id IS NULL;

-- scans / merges / reports inherit tenant_id from their lineage. Every row has an
-- intact lineage (scans.connection_id, merges.scan_id, reports.merge_id are all
-- NOT NULL FKs with ON DELETE CASCADE), so this covers 100% of rows. Self-scoped to
-- still-untenanted rows so a re-run is a harmless no-op.
UPDATE scans s
SET tenant_id = c.tenant_id
FROM crm_connections c
WHERE s.connection_id = c.id
  AND s.tenant_id IS NULL;

UPDATE merges m
SET tenant_id = s.tenant_id
FROM scans s
WHERE m.scan_id = s.id
  AND m.tenant_id IS NULL;

UPDATE reports r
SET tenant_id = m.tenant_id
FROM merges m
WHERE r.merge_id = m.id
  AND r.tenant_id IS NULL;

-- Lock the invariant: every row now belongs to a tenant. These fail loudly (and
-- roll back the whole migration) if any row was somehow missed, which is the safe
-- outcome — better a failed migration than a NULL-tenant row that escapes RLS.
-- (SET NOT NULL is a no-op if the column is already NOT NULL, so this is re-runnable.)
ALTER TABLE crm_connections ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE scans           ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE merges          ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE reports         ALTER COLUMN tenant_id SET NOT NULL;

-- 4. Access helper functions -------------------------------------------------
-- SECURITY DEFINER so a policy can consult tenant_members / platform_staff without
-- being blocked by (or recursing into) those tables' own RLS. search_path is pinned
-- to public to prevent search-path hijacking of an unqualified name.

CREATE OR REPLACE FUNCTION is_platform_staff(uid UUID)
RETURNS BOOLEAN
LANGUAGE SQL STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM platform_staff ps WHERE ps.user_id = uid);
$$;

CREATE OR REPLACE FUNCTION can_access_tenant(t UUID, uid UUID)
RETURNS BOOLEAN
LANGUAGE SQL STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT
        uid IS NOT NULL
        AND (
            is_platform_staff(uid)
            OR EXISTS (
                SELECT 1
                FROM tenant_members m
                JOIN tenants tn ON tn.id = m.tenant_id
                WHERE m.tenant_id = t
                  AND m.user_id = uid
                  AND m.is_active
                  AND (
                      m.role = 'owner'
                      OR (m.role = 'client' AND tn.client_access_enabled)
                  )
            )
        );
$$;

-- 5. RLS on the new tables ---------------------------------------------------
-- No INSERT/UPDATE/DELETE policies: with RLS enabled and no permissive write
-- policy, the anon/JWT path cannot mutate these tables, so a client cannot
-- self-promote to staff or grant itself a membership. The service-role backend
-- (which bypasses RLS) is the only writer; see app/services/tenancy.py.

ALTER TABLE tenants        ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_staff ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "tenant visible to members and staff" ON tenants;
CREATE POLICY "tenant visible to members and staff"
    ON tenants FOR SELECT
    USING (can_access_tenant(id, auth.uid()));

DROP POLICY IF EXISTS "membership visible to self and staff" ON tenant_members;
CREATE POLICY "membership visible to self and staff"
    ON tenant_members FOR SELECT
    USING (user_id = auth.uid() OR is_platform_staff(auth.uid()));

DROP POLICY IF EXISTS "staff list visible to staff only" ON platform_staff;
CREATE POLICY "staff list visible to staff only"
    ON platform_staff FOR SELECT
    USING (is_platform_staff(auth.uid()));

-- 6. Pivot RLS on the data tables from user_id -> tenant membership ----------
-- Writes move to backend/service-role only (the old per-user INSERT policies are
-- dropped); the down migration recreates every original policy verbatim.

-- crm_connections
DROP POLICY IF EXISTS "Users can view own connections"   ON crm_connections;
DROP POLICY IF EXISTS "Users can insert own connections" ON crm_connections;
DROP POLICY IF EXISTS "Users can update own connections" ON crm_connections;
DROP POLICY IF EXISTS "Users can delete own connections" ON crm_connections;
DROP POLICY IF EXISTS "connections visible within tenant" ON crm_connections;
CREATE POLICY "connections visible within tenant"
    ON crm_connections FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));

-- scans
DROP POLICY IF EXISTS "Users can view own scans"   ON scans;
DROP POLICY IF EXISTS "Users can insert own scans" ON scans;
DROP POLICY IF EXISTS "scans visible within tenant" ON scans;
CREATE POLICY "scans visible within tenant"
    ON scans FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));

-- duplicate_sets (no tenant_id column; reached via the parent scan's tenant)
-- SELECT-only on the JWT path, consistent with every other data table here: all
-- writes go through the service-role backend (which re-checks tenant access AND the
-- Phase-0 merge gate in code). We deliberately do NOT grant a JWT UPDATE policy —
-- duplicate_sets carries the merge-decision/verification state (decision, merged,
-- excluded, winner/loser ids), and a blanket UPDATE policy (USING with no WITH
-- CHECK) would let any tenant member mutate that state directly via PostgREST,
-- bypassing the backend safety gate and even re-parenting a set to another scan.
DROP POLICY IF EXISTS "Users can view duplicate sets from own scans"   ON duplicate_sets;
DROP POLICY IF EXISTS "Users can update duplicate sets from own scans" ON duplicate_sets;
DROP POLICY IF EXISTS "duplicate sets visible within tenant" ON duplicate_sets;
CREATE POLICY "duplicate sets visible within tenant"
    ON duplicate_sets FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM scans s
        WHERE s.id = duplicate_sets.scan_id
          AND can_access_tenant(s.tenant_id, auth.uid())
    ));

-- merges
DROP POLICY IF EXISTS "Users can view own merges"   ON merges;
DROP POLICY IF EXISTS "Users can insert own merges" ON merges;
DROP POLICY IF EXISTS "merges visible within tenant" ON merges;
CREATE POLICY "merges visible within tenant"
    ON merges FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));

-- reports
DROP POLICY IF EXISTS "Users can view own reports" ON reports;
DROP POLICY IF EXISTS "reports visible within tenant" ON reports;
CREATE POLICY "reports visible within tenant"
    ON reports FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));


-- >>>>>>>>>>>>>>>>>>>>>>>>  005_merge_backups.sql  <<<<<<<<<<<<<<<<<<<<<<<<

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
-- writes are service-role only (no JWT write policy). Part of the consolidated
-- LIVE_apply bundle (depends on 001+003+004).

CREATE TABLE IF NOT EXISTS merge_backups (
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

CREATE INDEX IF NOT EXISTS idx_merge_backups_merge  ON merge_backups(merge_id);
CREATE INDEX IF NOT EXISTS idx_merge_backups_tenant ON merge_backups(tenant_id);
CREATE INDEX IF NOT EXISTS idx_merge_backups_set    ON merge_backups(set_id);

-- RLS: readable within the tenant (mirrors every other data table after 004); the
-- service-role backend is the only writer, so there is no INSERT/UPDATE/DELETE policy.
ALTER TABLE merge_backups ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "merge backups visible within tenant" ON merge_backups;
CREATE POLICY "merge backups visible within tenant"
    ON merge_backups FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));
