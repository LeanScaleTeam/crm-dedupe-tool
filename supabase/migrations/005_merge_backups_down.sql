-- 005_merge_backups_down.sql
-- Rollback of 005_merge_backups.sql. Dropping the table also drops its RLS policy
-- and indexes. No other object references merge_backups, so this is self-contained
-- and loses only the backup snapshots themselves (no source data is affected).

BEGIN;

DROP TABLE IF EXISTS merge_backups;

COMMIT;
