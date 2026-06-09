-- Widen scans.object_type to allow accounts (and leads, for the coming L2A work).
-- See 06-Cross-Client-Build-Plan.md. Accounts run as a config-driven, view-only
-- dry-run today (no merge), so no destructive path is enabled by this migration.

ALTER TABLE scans DROP CONSTRAINT scans_object_type_check;

ALTER TABLE scans ADD CONSTRAINT scans_object_type_check
    CHECK (object_type IN ('contacts', 'companies', 'deals', 'accounts', 'leads'));
