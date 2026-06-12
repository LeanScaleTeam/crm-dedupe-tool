"""Merge endpoints for executing duplicate merges."""
import uuid
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone

from app.auth import require_user
from app.services.supabase_client import get_supabase
from app.services.crm_factory import get_crm_services
from app.services.reports import ReportService
from app.services.tenancy import assert_tenant_access
from app.services.merge_backup import build_backup_row, write_backup

router = APIRouter()


class MergeRequest(BaseModel):
    scan_id: str
    # set_ids: which approved sets to merge. None = "all currently-approved sets"
    # for this scan. It NO LONGER means "all non-excluded" — only sets a human/gate
    # has explicitly approved (decision='approved') are ever merged.
    set_ids: Optional[List[str]] = None


def _assert_scan_access(supabase, scan_id: str, user_id: str) -> dict:
    """Verify the scan exists AND the caller can access its tenant. Returns the row.

    The backend uses the service-role key (RLS bypassed), so tenant access must be
    checked explicitly here — a verified token alone does not scope data access.
    """
    res = supabase.table("scans").select("*").eq("id", scan_id).limit(1).execute()
    scan = (res.data or [None])[0]
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    assert_tenant_access(supabase, scan.get("tenant_id"), user_id)
    return scan


# A duplicate set may be merged only if it is approved and not already
# excluded/merged. This is the server-side gate — callers cannot bypass it by
# supplying set_ids.
def _approved_set_query(supabase, scan_id: str):
    return supabase.table("duplicate_sets").select("*").eq(
        "scan_id", scan_id
    ).eq("decision", "approved").eq("excluded", False).eq("merged", False)


async def run_merge(merge_id: str, user_id: str, scan_id: str, set_ids: List[str]):
    """
    Background task to execute merges.
    """
    supabase = get_supabase()

    try:
        # Update status to running
        supabase.table("merges").update({
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", merge_id).execute()

        # Get scan to find connection + tenant (tenant_id stamps the pre-merge backup).
        scan_result = supabase.table("scans").select("connection_id,tenant_id").eq(
            "id", scan_id
        ).single().execute()

        if not scan_result.data:
            raise Exception("Scan not found")

        connection_id = scan_result.data["connection_id"]
        tenant_id = scan_result.data.get("tenant_id")

        # crm_type is recorded on each backup row for the restore path.
        conn_row = supabase.table("crm_connections").select("crm_type").eq(
            "id", connection_id
        ).single().execute()
        crm_type = (conn_row.data or {}).get("crm_type")

        # Get CRM services based on connection type
        _, _, merge_service = await get_crm_services(user_id, connection_id)

        # Get duplicate sets to merge — re-assert the approval gate here (defence
        # in depth: never merge a set that isn't approved, even if its id was
        # queued earlier and approval was since revoked). Self-scope to this scan so
        # the safety re-check enforces scan/tenant scope independently of how set_ids
        # was derived (a future caller could pass ids it didn't pre-filter).
        sets_result = supabase.table("duplicate_sets").select("*").eq(
            "scan_id", scan_id
        ).in_(
            "id", set_ids
        ).eq("decision", "approved").eq("excluded", False).eq("merged", False).execute()

        duplicate_sets = sets_result.data or []
        total_sets = len(duplicate_sets)

        if total_sets == 0:
            supabase.table("merges").update({
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", merge_id).execute()
            return

        # Prepare merge operations. Skip loser ids Salesforce already absorbed on a
        # prior (partial) run so we never re-merge a deleted record.
        merge_operations = []
        for dup_set in duplicate_sets:
            already = set(dup_set.get("merged_loser_ids") or [])
            remaining = [l for l in dup_set["loser_record_ids"] if l not in already]
            merge_operations.append({
                "set_id": dup_set["id"],
                "winner_id": dup_set["winner_record_id"],
                "loser_ids": remaining,
                "already_merged": list(already),
                "all_loser_ids": list(dup_set["loser_record_ids"]),  # full original set
                "blended_properties": dup_set.get("merged_preview", {}),
                # Pre-merge snapshots (captured at scan time; not mutated by merge) —
                # backed up before the irreversible merge below.
                "winner_data": dup_set.get("winner_data"),
                "loser_data": dup_set.get("loser_data"),
            })

        # Execute merges
        completed = 0
        failed = 0
        error_log = []

        for op in merge_operations:
            # Check if merge was paused
            merge_check = supabase.table("merges").select("status").eq(
                "id", merge_id
            ).single().execute()

            if merge_check.data and merge_check.data["status"] == "paused":
                break

            # Nothing left to merge for this set (all losers absorbed on a prior
            # run) — mark it done without another CRM round-trip.
            if not op["loser_ids"]:
                completed += 1
                supabase.table("duplicate_sets").update({
                    "merged": True, "decision": "merged",
                    "merged_loser_ids": sorted(op["already_merged"]),
                }).eq("id", op["set_id"]).execute()
                continue

            # PRE-MERGE BACKUP (precondition): snapshot this set before the
            # irreversible CRM merge. If the backup can't be written, do NOT merge —
            # we never destroy records we couldn't back up first.
            try:
                write_backup(
                    supabase,
                    build_backup_row(
                        merge_id, scan_id, tenant_id, crm_type, connection_id, op
                    ),
                )
            except Exception as backup_err:
                failed += 1
                error_log.append({
                    "set_id": op["set_id"],
                    "error": f"Pre-merge backup failed; set not merged: {backup_err}",
                })
                supabase.table("merges").update({
                    "completed_sets": completed,
                    "failed_sets": failed,
                    "error_log": error_log,
                }).eq("id", merge_id).execute()
                continue

            # Execute merge
            result = await merge_service.merge_duplicate_set(
                winner_id=op["winner_id"],
                loser_ids=op["loser_ids"],
                blended_properties=op.get("blended_properties"),
            )

            # Record exactly which losers the CRM absorbed, even on failure —
            # those deletions/archives are irreversible (SF) or recoverable-but-
            # not-to-be-repeated (HubSpot) and must not be re-attempted.
            newly_absorbed = result.get("merged_loser_ids", [])
            all_absorbed = sorted(set(op["already_merged"]) | set(newly_absorbed))
            set_update = {"merged_loser_ids": all_absorbed}

            # merged=True ONLY when every original loser is gone — not merely when
            # the current batch succeeded. Decouples the completion signal from a
            # single batch's success.
            fully_merged = result["success"] and set(all_absorbed) == set(op["all_loser_ids"])

            if result["success"]:
                completed += 1
            else:
                failed += 1
                # Partial progress is preserved via merged_loser_ids; the set stays
                # unmerged so a resume retries only the remaining losers.
                for err in result["errors"]:
                    error_log.append({"set_id": op["set_id"], "error": err})

            if fully_merged:
                set_update["merged"] = True
                set_update["decision"] = "merged"

            supabase.table("duplicate_sets").update(set_update).eq(
                "id", op["set_id"]
            ).execute()

            # Update progress
            supabase.table("merges").update({
                "completed_sets": completed,
                "failed_sets": failed,
                "error_log": error_log if error_log else None,
            }).eq("id", merge_id).execute()

        # Mark merge as complete
        final_status = "completed"
        merge_check = supabase.table("merges").select("status").eq(
            "id", merge_id
        ).single().execute()

        if merge_check.data and merge_check.data["status"] == "paused":
            final_status = "paused"

        supabase.table("merges").update({
            "status": final_status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", merge_id).execute()

        # Auto-generate report on successful completion
        if final_status == "completed":
            try:
                report_service = ReportService()
                await report_service.generate_report(merge_id, user_id)
            except Exception as report_err:
                # Don't fail the merge if report generation fails
                print(f"Report generation failed: {report_err}")

    except Exception as e:
        # Mark merge as failed
        supabase.table("merges").update({
            "status": "failed",
            "error_log": [{"set_id": "system", "error": str(e)}],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", merge_id).execute()
        raise


@router.post("/execute")
async def execute_merge(
    request: MergeRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
):
    """Start merge execution for APPROVED duplicate sets in this scan.

    Safety invariants (Phase 0):
      - the caller is authenticated and owns the scan;
      - only sets with decision='approved' are ever merged — set_ids that are not
        approved are ignored, and set_ids=None means "all approved sets", NOT
        "all non-excluded".
    """
    supabase = get_supabase()
    scan = _assert_scan_access(supabase, request.scan_id, user_id)

    # The gate: approved, not excluded, not merged. Optionally narrowed to the
    # caller's set_ids — but never widened past it.
    query = _approved_set_query(supabase, request.scan_id).select("id")
    if request.set_ids is not None:
        if not request.set_ids:
            raise HTTPException(status_code=400, detail="set_ids was empty.")
        query = query.in_("id", request.set_ids)
    sets_result = query.execute()

    set_ids = [s["id"] for s in (sets_result.data or [])]

    if len(set_ids) == 0:
        raise HTTPException(
            status_code=400,
            detail="No approved duplicate sets to merge. Approve sets first.",
        )

    # Create merge record
    merge_id = str(uuid.uuid4())
    merge_data = {
        "id": merge_id,
        "scan_id": request.scan_id,
        "tenant_id": scan["tenant_id"],
        "user_id": user_id,
        "status": "pending",
        "total_sets": len(set_ids),
        "completed_sets": 0,
        "failed_sets": 0,
    }

    supabase.table("merges").insert(merge_data).execute()

    # Start background task
    background_tasks.add_task(
        run_merge,
        merge_id,
        user_id,
        request.scan_id,
        set_ids,
    )

    return {"merge_id": merge_id, "status": "pending", "total_sets": len(set_ids)}


def _assert_merge_access(supabase, merge_id: str, user_id: str) -> dict:
    res = supabase.table("merges").select("*").eq("id", merge_id).limit(1).execute()
    merge = (res.data or [None])[0]
    if not merge:
        raise HTTPException(status_code=404, detail="Merge not found")
    assert_tenant_access(supabase, merge.get("tenant_id"), user_id)
    return merge


@router.get("/{merge_id}/status")
async def get_merge_status(merge_id: str, user_id: str = Depends(require_user)):
    """Get current merge progress and status."""
    supabase = get_supabase()

    merge = _assert_merge_access(supabase, merge_id, user_id)
    return {
        "id": merge["id"],
        "status": merge["status"],
        "total_sets": merge["total_sets"],
        "completed_sets": merge["completed_sets"],
        "failed_sets": merge["failed_sets"],
        "error_log": merge.get("error_log"),
        "started_at": merge.get("started_at"),
        "completed_at": merge.get("completed_at"),
    }


@router.post("/{merge_id}/pause")
async def pause_merge(merge_id: str, user_id: str = Depends(require_user)):
    """Pause an in-progress merge."""
    supabase = get_supabase()

    merge = _assert_merge_access(supabase, merge_id, user_id)
    if merge["status"] != "running":
        raise HTTPException(status_code=400, detail="Merge is not running")

    # Set to paused - the background task will check this
    supabase.table("merges").update({
        "status": "paused"
    }).eq("id", merge_id).execute()

    return {"success": True, "status": "paused"}


@router.post("/{merge_id}/resume")
async def resume_merge(
    merge_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
):
    """Resume a paused merge."""
    supabase = get_supabase()

    merge = _assert_merge_access(supabase, merge_id, user_id)

    if merge["status"] != "paused":
        raise HTTPException(status_code=400, detail="Merge is not paused")

    # Get remaining APPROVED sets to merge (same gate as execute).
    sets_result = _approved_set_query(supabase, merge["scan_id"]).select("id").execute()

    set_ids = [s["id"] for s in (sets_result.data or [])]

    if len(set_ids) == 0:
        # Already complete
        supabase.table("merges").update({
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", merge_id).execute()
        return {"success": True, "status": "completed"}

    # Update total and restart
    supabase.table("merges").update({
        "status": "pending",
        "total_sets": merge["completed_sets"] + merge["failed_sets"] + len(set_ids),
    }).eq("id", merge_id).execute()

    # Start background task
    background_tasks.add_task(
        run_merge,
        merge_id,
        merge["user_id"],
        merge["scan_id"],
        set_ids,
    )

    return {"success": True, "status": "running", "remaining_sets": len(set_ids)}
