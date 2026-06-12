-- 004_multi_tenant_down.sql
-- Full rollback of 004_multi_tenant.sql (rollback lever #2).
--
-- Because the up migration never touched `user_id`, restoring the original
-- user_id-based RLS and dropping the tenant additions returns the database to the
-- exact pre-tenant state with ZERO data loss. Run in the Supabase SQL editor.
--
-- (If you only want to switch CLIENT access off without unwinding the model, you
-- do NOT need this file — use lever #1 instead: UPDATE tenants SET
-- client_access_enabled = false [WHERE id = ...].)

BEGIN;

-- 1. Restore the original user_id RLS on the data tables (verbatim from 001) --

-- crm_connections
DROP POLICY IF EXISTS "connections visible within tenant" ON crm_connections;
CREATE POLICY "Users can view own connections"
    ON crm_connections FOR SELECT   USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own connections"
    ON crm_connections FOR INSERT   WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can update own connections"
    ON crm_connections FOR UPDATE   USING (auth.uid() = user_id);
CREATE POLICY "Users can delete own connections"
    ON crm_connections FOR DELETE   USING (auth.uid() = user_id);

-- scans
DROP POLICY IF EXISTS "scans visible within tenant" ON scans;
CREATE POLICY "Users can view own scans"
    ON scans FOR SELECT   USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own scans"
    ON scans FOR INSERT   WITH CHECK (auth.uid() = user_id);

-- duplicate_sets
DROP POLICY IF EXISTS "duplicate sets visible within tenant"   ON duplicate_sets;
DROP POLICY IF EXISTS "duplicate sets updatable within tenant" ON duplicate_sets;
CREATE POLICY "Users can view duplicate sets from own scans"
    ON duplicate_sets FOR SELECT
    USING (EXISTS (SELECT 1 FROM scans WHERE scans.id = duplicate_sets.scan_id AND scans.user_id = auth.uid()));
CREATE POLICY "Users can update duplicate sets from own scans"
    ON duplicate_sets FOR UPDATE
    USING (EXISTS (SELECT 1 FROM scans WHERE scans.id = duplicate_sets.scan_id AND scans.user_id = auth.uid()));

-- merges
DROP POLICY IF EXISTS "merges visible within tenant" ON merges;
CREATE POLICY "Users can view own merges"
    ON merges FOR SELECT   USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own merges"
    ON merges FOR INSERT   WITH CHECK (auth.uid() = user_id);

-- reports
DROP POLICY IF EXISTS "reports visible within tenant" ON reports;
CREATE POLICY "Users can view own reports"
    ON reports FOR SELECT   USING (auth.uid() = user_id);

-- 2. Drop the tenant_id columns (user_id is intact, so access is fully restored)
ALTER TABLE reports         DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE merges          DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE scans           DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE crm_connections DROP COLUMN IF EXISTS tenant_id;

-- 3. Drop the tenancy tables (this also drops their RLS policies) and helpers.
--    Drop the tables before the functions: the tables' policies reference the
--    functions, so the functions cannot be dropped while those policies exist.
DROP TABLE IF EXISTS tenant_members;
DROP TABLE IF EXISTS platform_staff;
DROP TABLE IF EXISTS tenants;

DROP FUNCTION IF EXISTS can_access_tenant(UUID, UUID);
DROP FUNCTION IF EXISTS is_platform_staff(UUID);

COMMIT;
