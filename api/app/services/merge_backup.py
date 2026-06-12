"""Pre-merge backup: snapshot a duplicate set's winner + losers BEFORE the merge.

run_merge calls write_backup() for each set right before the irreversible CRM merge,
and treats a failed write as a hard stop for that set (precondition: never destroy
records we couldn't back up first).

This is a BACKUP, NOT AN UNDO. Salesforce merges delete the loser records and their
Ids cannot be resurrected; the snapshot lets you audit / manually re-create the lost
data (scripts/restore_from_backup.py), not reverse the merge. See 005_merge_backups.sql.
"""
from __future__ import annotations

import uuid
from typing import Optional


def build_backup_row(
    merge_id: str,
    scan_id: Optional[str],
    tenant_id: str,
    crm_type: Optional[str],
    connection_id: Optional[str],
    op: dict,
) -> dict:
    """Build the merge_backups row for one duplicate set's merge operation.

    `op` is a run_merge merge_operation dict; it carries the set's scan-time snapshot
    columns (winner_data / loser_data), which the merge does not mutate, so they
    faithfully record the pre-merge state. We back up the FULL original loser set
    (all_loser_ids), not just the losers remaining on a partial resume.
    """
    return {
        "id": str(uuid.uuid4()),
        "merge_id": merge_id,
        "scan_id": scan_id,
        "set_id": op["set_id"],
        "tenant_id": tenant_id,
        "crm_type": crm_type,
        "connection_id": connection_id,
        "winner_record_id": op["winner_id"],
        "winner_snapshot": op.get("winner_data"),
        "loser_record_ids": list(op.get("all_loser_ids") or []),
        "loser_snapshot": op.get("loser_data"),
        "blended_properties": op.get("blended_properties") or {},
    }


def write_backup(supabase, row: dict) -> None:
    """Persist a backup row as a merge precondition. Idempotent per (merge_id, set_id):
    if a backup for this run+set already exists (e.g. on a resumed merge) it is left
    untouched — the first, truest pre-merge snapshot wins. Raises on a real write
    failure so the caller can refuse to merge a set it could not back up.
    """
    existing = supabase.table("merge_backups").select("id").eq(
        "merge_id", row["merge_id"]
    ).eq("set_id", row["set_id"]).execute()
    if existing.data:
        return
    supabase.table("merge_backups").insert(row).execute()
