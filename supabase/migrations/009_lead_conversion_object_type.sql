-- Widen scans.object_type to allow 'lead_conversion' — the Lead -> existing Contact
-- conversion scan (cross-object match staged as a convert, not a merge).
-- 'leads' (Lead <-> Lead dedupe) is already permitted by migration 002/008.
-- Idempotent: safe to run whatever the current constraint is.

ALTER TABLE scans DROP CONSTRAINT IF EXISTS scans_object_type_check;

ALTER TABLE scans ADD CONSTRAINT scans_object_type_check
    CHECK (object_type IN ('contacts', 'companies', 'deals', 'accounts', 'leads', 'lead_conversion'));
