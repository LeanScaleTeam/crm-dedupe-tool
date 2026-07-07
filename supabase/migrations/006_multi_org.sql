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
