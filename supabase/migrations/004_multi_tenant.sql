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
