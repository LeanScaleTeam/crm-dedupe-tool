-- ============================================================================
-- FRESH PRODUCTION INSTALL — crm-dedupe-tool
-- Paste this whole file into the Supabase SQL Editor of a NEW project and Run.
-- Applies migrations 001-007 in dependency order (008 is redundant with 002 on
-- a fresh DB; *_down rollbacks and the LIVE_apply consolidation are excluded).
-- ============================================================================

-- ============================================================================
-- 001_initial_schema.sql
-- ============================================================================

-- CRM Deduplication Tool - Initial Schema
-- Run this in Supabase SQL Editor or via migrations

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- CRM Connections table
CREATE TABLE crm_connections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    crm_type TEXT NOT NULL CHECK (crm_type IN ('hubspot', 'salesforce')),
    access_token_encrypted TEXT NOT NULL,
    refresh_token_encrypted TEXT NOT NULL,
    portal_id TEXT,  -- HubSpot portal ID or Salesforce org ID
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, crm_type)
);

-- Scans table
CREATE TABLE scans (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    connection_id UUID NOT NULL REFERENCES crm_connections(id) ON DELETE CASCADE,
    object_type TEXT NOT NULL CHECK (object_type IN ('contacts', 'companies', 'deals')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    config JSONB NOT NULL,  -- winner rules, thresholds, etc.
    progress INTEGER DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    records_scanned INTEGER DEFAULT 0,
    duplicates_found INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Duplicate Sets table (temporary, for review)
CREATE TABLE duplicate_sets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scan_id UUID NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    confidence NUMERIC(5,2) NOT NULL CHECK (confidence >= 0 AND confidence <= 100),
    winner_record_id TEXT NOT NULL,
    loser_record_ids TEXT[] NOT NULL,
    winner_data JSONB NOT NULL,
    loser_data JSONB NOT NULL,
    merged_preview JSONB NOT NULL,
    excluded BOOLEAN DEFAULT FALSE,
    merged BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Merges table
CREATE TABLE merges (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scan_id UUID NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'paused')),
    total_sets INTEGER NOT NULL,
    completed_sets INTEGER DEFAULT 0,
    failed_sets INTEGER DEFAULT 0,
    error_log JSONB,  -- Array of {set_id, error}
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Reports table
CREATE TABLE reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    merge_id UUID NOT NULL REFERENCES merges(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    report_data JSONB NOT NULL,
    pdf_url TEXT,  -- Supabase storage URL
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX idx_scans_user_id ON scans(user_id);
CREATE INDEX idx_scans_status ON scans(status);
CREATE INDEX idx_duplicate_sets_scan_id ON duplicate_sets(scan_id);
CREATE INDEX idx_duplicate_sets_excluded ON duplicate_sets(excluded);
CREATE INDEX idx_merges_scan_id ON merges(scan_id);
CREATE INDEX idx_reports_user_id ON reports(user_id);

-- Row Level Security (RLS)
ALTER TABLE crm_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE scans ENABLE ROW LEVEL SECURITY;
ALTER TABLE duplicate_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE merges ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;

-- RLS Policies: Users can only access their own data
CREATE POLICY "Users can view own connections"
    ON crm_connections FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own connections"
    ON crm_connections FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own connections"
    ON crm_connections FOR UPDATE
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own connections"
    ON crm_connections FOR DELETE
    USING (auth.uid() = user_id);

CREATE POLICY "Users can view own scans"
    ON scans FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own scans"
    ON scans FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can view duplicate sets from own scans"
    ON duplicate_sets FOR SELECT
    USING (EXISTS (SELECT 1 FROM scans WHERE scans.id = duplicate_sets.scan_id AND scans.user_id = auth.uid()));

CREATE POLICY "Users can update duplicate sets from own scans"
    ON duplicate_sets FOR UPDATE
    USING (EXISTS (SELECT 1 FROM scans WHERE scans.id = duplicate_sets.scan_id AND scans.user_id = auth.uid()));

CREATE POLICY "Users can view own merges"
    ON merges FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own merges"
    ON merges FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can view own reports"
    ON reports FOR SELECT
    USING (auth.uid() = user_id);

-- Service role bypass for backend operations
-- Note: Backend uses service_role key which bypasses RLS

-- Updated_at trigger function
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to crm_connections
CREATE TRIGGER update_crm_connections_updated_at
    BEFORE UPDATE ON crm_connections
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- 002_accounts_object_type.sql
-- ============================================================================

-- Widen scans.object_type to allow accounts (and leads, for the coming L2A work).
-- See 06-Cross-Client-Build-Plan.md. Accounts run as a config-driven, view-only
-- dry-run today (no merge), so no destructive path is enabled by this migration.

ALTER TABLE scans DROP CONSTRAINT scans_object_type_check;

ALTER TABLE scans ADD CONSTRAINT scans_object_type_check
    CHECK (object_type IN ('contacts', 'companies', 'deals', 'accounts', 'leads'));

-- ============================================================================
-- 003_phase0_merge_safety.sql
-- ============================================================================

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

-- ============================================================================
-- 004_multi_tenant.sql
-- ============================================================================

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
-- deploy as one step. Run AFTER 001 / 002 / 003.

BEGIN;

-- 1. Tenancy tables ----------------------------------------------------------

CREATE TABLE tenants (
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

CREATE TABLE tenant_members (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    -- owner  = the operator who connected the org (always has access);
    -- client = an invited viewer (gated by tenants.client_access_enabled).
    role TEXT NOT NULL DEFAULT 'client' CHECK (role IN ('owner', 'client')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX idx_tenant_members_user ON tenant_members(user_id);

-- LeanScale internal operators: cross-tenant access (they run the dedupe service).
-- Membership here is a deliberate, backend-only grant. Empty this table (or drop it
-- via the down migration) to remove ALL staff cross-tenant access at once.
CREATE TABLE platform_staff (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. tenant_id columns (nullable now; backfilled; set NOT NULL at the end) ----

ALTER TABLE crm_connections ADD COLUMN tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE scans           ADD COLUMN tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE merges          ADD COLUMN tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE reports         ADD COLUMN tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;

CREATE INDEX idx_crm_connections_tenant ON crm_connections(tenant_id);
CREATE INDEX idx_scans_tenant   ON scans(tenant_id);
CREATE INDEX idx_merges_tenant  ON merges(tenant_id);
CREATE INDEX idx_reports_tenant ON reports(tenant_id);

-- 3. Backfill (tenant = connected org) ---------------------------------------
-- Pre-generate one (connection -> tenant) mapping so we can insert the tenants and
-- then point everything back at them deterministically.

CREATE TEMP TABLE _conn_tenant ON COMMIT DROP AS
SELECT
    c.id AS conn_id,
    uuid_generate_v4() AS tenant_id,
    -- Human label: Salesforce portal_id is "orgId|instanceUrl" -> take orgId;
    -- HubSpot portal_id is the hub id. Fall back to the crm_type if blank.
    COALESCE(NULLIF(split_part(c.portal_id, '|', 1), ''), c.crm_type)
        || ' (' || c.crm_type || ')' AS name,
    c.user_id AS owner_id
FROM crm_connections c;

-- One tenant per existing connection. client_access_enabled = FALSE (secure by
-- default, same as new tenants): the only members created at backfill time are
-- OWNERS (below), and owners are never gated by this flag, so FALSE grants nothing
-- away on day one. It only governs future invited CLIENT members — leave each
-- legacy tenant closed until an operator deliberately invites a client and flips it.
INSERT INTO tenants (id, name, client_access_enabled)
SELECT tenant_id, name, FALSE FROM _conn_tenant;

-- Point each connection at its tenant.
UPDATE crm_connections c
SET tenant_id = ct.tenant_id
FROM _conn_tenant ct
WHERE ct.conn_id = c.id;

-- The connecting user becomes the tenant OWNER (always-on access).
INSERT INTO tenant_members (tenant_id, user_id, role, is_active)
SELECT tenant_id, owner_id, 'owner', TRUE FROM _conn_tenant
ON CONFLICT (tenant_id, user_id) DO NOTHING;

-- scans / merges / reports inherit tenant_id from their lineage. Every row has an
-- intact lineage (scans.connection_id, merges.scan_id, reports.merge_id are all
-- NOT NULL FKs with ON DELETE CASCADE), so this covers 100% of rows.
UPDATE scans s
SET tenant_id = c.tenant_id
FROM crm_connections c
WHERE s.connection_id = c.id;

UPDATE merges m
SET tenant_id = s.tenant_id
FROM scans s
WHERE m.scan_id = s.id;

UPDATE reports r
SET tenant_id = m.tenant_id
FROM merges m
WHERE r.merge_id = m.id;

-- Lock the invariant: every row now belongs to a tenant. These fail loudly (and
-- roll back the whole migration) if any row was somehow missed, which is the safe
-- outcome — better a failed migration than a NULL-tenant row that escapes RLS.
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

CREATE POLICY "tenant visible to members and staff"
    ON tenants FOR SELECT
    USING (can_access_tenant(id, auth.uid()));

CREATE POLICY "membership visible to self and staff"
    ON tenant_members FOR SELECT
    USING (user_id = auth.uid() OR is_platform_staff(auth.uid()));

CREATE POLICY "staff list visible to staff only"
    ON platform_staff FOR SELECT
    USING (is_platform_staff(auth.uid()));

-- 6. Pivot RLS on the data tables from user_id -> tenant membership ----------
-- Writes move to backend/service-role only (the old per-user INSERT policies are
-- dropped); the down migration recreates every original policy verbatim.

-- crm_connections
DROP POLICY "Users can view own connections"   ON crm_connections;
DROP POLICY "Users can insert own connections" ON crm_connections;
DROP POLICY "Users can update own connections" ON crm_connections;
DROP POLICY "Users can delete own connections" ON crm_connections;
CREATE POLICY "connections visible within tenant"
    ON crm_connections FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));

-- scans
DROP POLICY "Users can view own scans"   ON scans;
DROP POLICY "Users can insert own scans" ON scans;
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
DROP POLICY "Users can view duplicate sets from own scans"   ON duplicate_sets;
DROP POLICY "Users can update duplicate sets from own scans" ON duplicate_sets;
CREATE POLICY "duplicate sets visible within tenant"
    ON duplicate_sets FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM scans s
        WHERE s.id = duplicate_sets.scan_id
          AND can_access_tenant(s.tenant_id, auth.uid())
    ));

-- merges
DROP POLICY "Users can view own merges"   ON merges;
DROP POLICY "Users can insert own merges" ON merges;
CREATE POLICY "merges visible within tenant"
    ON merges FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));

-- reports
DROP POLICY "Users can view own reports" ON reports;
CREATE POLICY "reports visible within tenant"
    ON reports FOR SELECT
    USING (can_access_tenant(tenant_id, auth.uid()));

COMMIT;

-- ============================================================================
-- 005_merge_backups.sql
-- ============================================================================

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

-- ============================================================================
-- 006_multi_org.sql
-- ============================================================================

-- 006_multi_org.sql
-- Allow a user to connect MULTIPLE orgs per CRM (sandbox, production, or entirely
-- different orgs). Previously crm_connections had UNIQUE(user_id, crm_type), so a
-- second Salesforce connect overwrote the first. We key uniqueness on the org too.
--
-- Idempotent / re-paste-safe. Apply in the Supabase SQL editor.

-- 1) Give connections a first-class org id + optional display label.
--    (org_id was previously packed into portal_id as "<org_id>|<instance_url>".)
ALTER TABLE crm_connections ADD COLUMN IF NOT EXISTS org_id TEXT;
ALTER TABLE crm_connections ADD COLUMN IF NOT EXISTS label TEXT;

-- 2) Backfill org_id from portal_id for existing rows.
UPDATE crm_connections
SET org_id = CASE
    WHEN position('|' in COALESCE(portal_id, '')) > 0 THEN split_part(portal_id, '|', 1)
    ELSE COALESCE(portal_id, '')
  END
WHERE org_id IS NULL;

-- 3) org_id is required going forward (never null → clean uniqueness).
ALTER TABLE crm_connections ALTER COLUMN org_id SET DEFAULT '';
UPDATE crm_connections SET org_id = '' WHERE org_id IS NULL;
ALTER TABLE crm_connections ALTER COLUMN org_id SET NOT NULL;

-- 4) Replace one-connection-per-CRM with one-connection-per-ORG.
ALTER TABLE crm_connections DROP CONSTRAINT IF EXISTS crm_connections_user_id_crm_type_key;
ALTER TABLE crm_connections DROP CONSTRAINT IF EXISTS crm_connections_user_crm_org_key;
ALTER TABLE crm_connections
  ADD CONSTRAINT crm_connections_user_crm_org_key UNIQUE (user_id, crm_type, org_id);

-- ============================================================================
-- 007_per_record_exclusion.sql
-- ============================================================================

-- 007_per_record_exclusion.sql
-- Let a reviewer exclude SPECIFIC records from a duplicate set (mark them
-- "not a duplicate") without discarding the whole set. Excluded records are
-- never merged into the winner — they're left untouched as standalone records.
-- Idempotent / safe. Apply in the Supabase SQL editor.

ALTER TABLE duplicate_sets
  ADD COLUMN IF NOT EXISTS excluded_record_ids TEXT[] DEFAULT '{}';
