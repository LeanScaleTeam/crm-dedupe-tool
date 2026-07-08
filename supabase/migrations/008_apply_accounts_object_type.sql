-- Re-apply the accounts/leads object_type widening (migration 002 was never applied
-- to the live Supabase project — the check still only permitted contacts+companies,
-- so any accounts scan failed on INSERT with scans_object_type_check).
-- Idempotent: safe to run whatever the current constraint is.

ALTER TABLE scans DROP CONSTRAINT IF EXISTS scans_object_type_check;

ALTER TABLE scans ADD CONSTRAINT scans_object_type_check
    CHECK (object_type IN ('contacts', 'companies', 'deals', 'accounts', 'leads'));
