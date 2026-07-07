"""Scan endpoints for duplicate detection."""
import uuid
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone

from pathlib import Path

from app.auth import require_user
from app.services.supabase_client import get_supabase
from app.services.crm_factory import get_crm_services
from app.services.tenancy import assert_tenant_access
from app.services.dedup_engine import DuplicateDetector, WinnerSelector, FieldBlender
from app.services.match_engine import MatchEngine, MatchProfile

router = APIRouter()

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
DEFAULT_ACCOUNT_PROFILE = "scandit/account_v3"


class WinnerRule(BaseModel):
    rule_type: str  # 'oldest_created', 'most_recent', 'most_associations', 'custom_field'
    field_name: Optional[str] = None  # For custom_field rule
    field_value: Optional[str] = None  # For custom_field rule


class ScanConfig(BaseModel):
    object_type: str  # 'contacts', 'accounts', 'companies', 'deals', 'leads'
    winner_rules: List[WinnerRule] = []
    confidence_threshold: float = 0.9
    match_profile: Optional[str] = None  # e.g. 'scandit/account_v3' (accounts dry-run)


class ScanRequest(BaseModel):
    connection_id: str
    config: ScanConfig


def _assert_scan_access(supabase, scan_id: str, user_id: str) -> dict:
    """Verify the scan exists AND the caller can access its tenant. RLS is bypassed
    by the service-role client, so tenant access is checked explicitly here (an
    owner/member of the scan's tenant, or a platform-staff operator)."""
    res = supabase.table("scans").select("*").eq("id", scan_id).limit(1).execute()
    scan = (res.data or [None])[0]
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    assert_tenant_access(supabase, scan.get("tenant_id"), user_id)
    return scan


def _account_display(m: dict) -> dict:
    """Map an account snapshot to a card-friendly shape (reuses the contact card)."""
    name = m.get("Name") or "(no name)"
    subtitle = " · ".join(
        str(v) for v in [m.get("Website"), m.get("BillingCountryCode"), m.get("Vertical__c")] if v
    )
    return {**m, "id": m.get("Id"), "full_name": name, "company": name, "email": subtitle}


async def run_account_scan(scan_id: str, user_id: str, connection_id: str, config: dict, supabase):
    """Config-driven account dedupe — VIEW ONLY (dry-run). No Salesforce writes."""
    from app.services.salesforce_accounts import SalesforceAccountsService

    connection, _, _ = await get_crm_services(user_id, connection_id)
    if not getattr(connection, "instance_url", None):
        raise Exception("Account dry-run currently supports Salesforce connections only.")

    accounts_service = SalesforceAccountsService(connection)
    total = await accounts_service.get_total_accounts()

    async def progress_callback(count: int):
        progress = min(int((count / max(total, 1)) * 50), 50)
        supabase.table("scans").update(
            {"progress": progress, "records_scanned": count}
        ).eq("id", scan_id).execute()

    records = await accounts_service.get_all_accounts(progress_callback)
    supabase.table("scans").update(
        {"progress": 60, "records_scanned": len(records)}
    ).eq("id", scan_id).execute()

    profile_name = config.get("match_profile") or DEFAULT_ACCOUNT_PROFILE
    profile_path = PROFILES_DIR / f"{profile_name}.json"
    if not profile_path.exists():
        raise Exception(f"Match profile not found: {profile_name}")
    profile = MatchProfile.from_json(str(profile_path))

    result = MatchEngine(profile).find_clusters(records)
    dupe_clusters = [c for c in result.clusters if c.is_dupe]

    for i, cluster in enumerate(dupe_clusters):
        members = cluster.members
        winner = _account_display(members[0])
        losers = [_account_display(m) for m in members[1:]]
        supabase.table("duplicate_sets").insert({
            "id": str(uuid.uuid4()),
            "scan_id": scan_id,
            "confidence": round(cluster.confidence * 100, 2),
            "winner_record_id": winner["id"],
            "loser_record_ids": [l["id"] for l in losers],
            "winner_data": winner,
            "loser_data": losers,
            "merged_preview": {
                "dry_run": True,
                "verification_status": cluster.verification_status,
                "certainty": cluster.certainty,
                "verification_reason": cluster.verification_reason,
                "bucket": cluster.bucket,
                "hierarchy_class": cluster.hierarchy_class,
                "match_path": cluster.match_path,
                "fingerprint": cluster.fingerprint,
            },
        }).execute()
        if i % 25 == 0:
            progress = 60 + int((i / max(len(dupe_clusters), 1)) * 40)
            supabase.table("scans").update(
                {"progress": progress, "duplicates_found": i + 1}
            ).eq("id", scan_id).execute()

    supabase.table("scans").update({
        "status": "completed",
        "progress": 100,
        "duplicates_found": len(dupe_clusters),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "config": {**config, "engine_stats": result.stats},
    }).eq("id", scan_id).execute()


async def run_scan(scan_id: str, user_id: str, connection_id: str, config: dict):
    """
    Background task to run the duplicate detection scan.
    """
    supabase = get_supabase()

    try:
        # Update status to running
        supabase.table("scans").update({
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", scan_id).execute()

        # Accounts run the config-driven match engine as a view-only dry-run.
        if config["object_type"] == "accounts":
            await run_account_scan(scan_id, user_id, connection_id, config, supabase)
            return

        # Get CRM services based on connection type
        connection, contacts_service, _ = await get_crm_services(user_id, connection_id)

        # Initialize dedup engine
        detector = DuplicateDetector(confidence_threshold=config["confidence_threshold"])
        winner_selector = WinnerSelector(config["winner_rules"])
        field_blender = FieldBlender()

        # Get total count for progress
        total_contacts = await contacts_service.get_total_contacts()

        # Fetch all contacts
        contacts = []
        records_scanned = 0

        async def progress_callback(count: int):
            nonlocal records_scanned
            records_scanned = count
            progress = min(int((count / max(total_contacts, 1)) * 50), 50)  # First 50% is fetching
            supabase.table("scans").update({
                "progress": progress,
                "records_scanned": count,
            }).eq("id", scan_id).execute()

        async for contact in contacts_service.get_all_contacts(progress_callback):
            contacts.append(contact)

        # Update progress - fetching complete
        supabase.table("scans").update({
            "progress": 50,
            "records_scanned": len(contacts),
        }).eq("id", scan_id).execute()

        # Find duplicates (this is the CPU-intensive part)
        duplicate_sets = detector.find_duplicates(contacts)

        # Process each duplicate set
        processed_sets = []
        for i, dup_set in enumerate(duplicate_sets):
            # Select winner
            all_contacts = [dup_set.winner] + dup_set.losers
            winner, losers = winner_selector.select_winner(all_contacts)

            # Blend fields
            merged_preview = field_blender.blend(winner, losers)

            # Store in database
            set_id = str(uuid.uuid4())
            supabase.table("duplicate_sets").insert({
                "id": set_id,
                "scan_id": scan_id,
                "confidence": dup_set.confidence,
                "winner_record_id": winner.id,
                "loser_record_ids": [l.id for l in losers],
                "winner_data": winner.model_dump(mode="json"),
                "loser_data": [l.model_dump(mode="json") for l in losers],
                "merged_preview": merged_preview,
            }).execute()

            processed_sets.append(set_id)

            # Update progress (50-100% is processing)
            progress = 50 + int((i / max(len(duplicate_sets), 1)) * 50)
            supabase.table("scans").update({
                "progress": progress,
                "duplicates_found": i + 1,
            }).eq("id", scan_id).execute()

        # Mark scan as complete
        supabase.table("scans").update({
            "status": "completed",
            "progress": 100,
            "duplicates_found": len(duplicate_sets),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", scan_id).execute()

    except Exception as e:
        # Mark scan as failed
        supabase.table("scans").update({
            "status": "failed",
            "error_message": str(e),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", scan_id).execute()
        raise


@router.post("/start")
async def start_scan(
    request: ScanRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
):
    """Start a new duplicate detection scan."""
    supabase = get_supabase()

    # Validate the connection exists AND the caller can access its tenant.
    conn_result = supabase.table("crm_connections").select("*").eq(
        "id", request.connection_id
    ).limit(1).execute()
    connection = (conn_result.data or [None])[0]
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")
    assert_tenant_access(supabase, connection.get("tenant_id"), user_id)

    # Create scan record (stamped with the connection's tenant; user_id is the actor).
    scan_id = str(uuid.uuid4())
    scan_data = {
        "id": scan_id,
        "user_id": user_id,
        "tenant_id": connection["tenant_id"],
        "connection_id": request.connection_id,
        "object_type": request.config.object_type,
        "status": "pending",
        "config": request.config.model_dump(),
        "progress": 0,
        "records_scanned": 0,
        "duplicates_found": 0,
    }

    supabase.table("scans").insert(scan_data).execute()

    # Start background task
    config_dict = request.config.model_dump()
    config_dict["winner_rules"] = [r.model_dump() for r in request.config.winner_rules]

    background_tasks.add_task(
        run_scan,
        scan_id,
        user_id,
        request.connection_id,
        config_dict,
    )

    return {"scan_id": scan_id, "status": "pending"}


@router.get("/{scan_id}/status")
async def get_scan_status(scan_id: str, user_id: str = Depends(require_user)):
    """Get current scan progress and status."""
    supabase = get_supabase()

    scan = _assert_scan_access(supabase, scan_id, user_id)
    return {
        "id": scan["id"],
        "status": scan["status"],
        "progress": scan["progress"],
        "records_scanned": scan["records_scanned"],
        "duplicates_found": scan["duplicates_found"],
        "error_message": scan.get("error_message"),
        "started_at": scan.get("started_at"),
        "completed_at": scan.get("completed_at"),
    }


@router.get("/{scan_id}/results")
async def get_scan_results(
    scan_id: str,
    page: int = 1,
    per_page: int = 50,
    user_id: str = Depends(require_user),
):
    """Get paginated duplicate sets from completed scan."""
    supabase = get_supabase()

    # Verify the scan exists and belongs to the authenticated user.
    scan = _assert_scan_access(supabase, scan_id, user_id)

    # Get duplicate sets with pagination
    offset = (page - 1) * per_page
    results = supabase.table("duplicate_sets").select("*").eq(
        "scan_id", scan_id
    ).order("confidence", desc=True).range(offset, offset + per_page - 1).execute()

    # Get total count
    count_result = supabase.table("duplicate_sets").select(
        "id", count="exact"
    ).eq("scan_id", scan_id).execute()

    total = count_result.count if count_result.count else 0

    return {
        "scan_id": scan_id,
        "scan_status": scan["status"],
        "total_duplicates": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total > 0 else 0,
        "duplicate_sets": results.data or [],
    }


class UpdateDuplicateSetRequest(BaseModel):
    excluded: Optional[bool] = None
    merged_preview: Optional[dict] = None
    # The reviewer's decision. 'approved' is the ONLY way a set becomes mergeable
    # (the merge executor refuses anything that isn't approved).
    decision: Optional[str] = None  # 'approved' | 'excluded' | 'escalated' | 'pending'
    # Specific record ids the reviewer marked "not a duplicate" — excluded from the
    # merge (left untouched), without discarding the whole set.
    excluded_record_ids: Optional[List[str]] = None


_ALLOWED_DECISIONS = {"approved", "excluded", "escalated", "pending"}


@router.patch("/{scan_id}/duplicate-sets/{set_id}")
async def update_duplicate_set(
    scan_id: str,
    set_id: str,
    request: UpdateDuplicateSetRequest,
    user_id: str = Depends(require_user),
):
    """Update a duplicate set: exclude it, edit the blended preview, or record a
    review decision (approve / exclude / escalate). Approval is what gates merge."""
    supabase = get_supabase()
    _assert_scan_access(supabase, scan_id, user_id)

    update_data: dict = {}
    if request.excluded is not None:
        update_data["excluded"] = request.excluded
        # Keep decision consistent with an explicit exclude/include toggle.
        update_data["decision"] = "excluded" if request.excluded else "pending"
    if request.merged_preview is not None:
        update_data["merged_preview"] = request.merged_preview
    if request.excluded_record_ids is not None:
        update_data["excluded_record_ids"] = request.excluded_record_ids
    if request.decision is not None:
        if request.decision not in _ALLOWED_DECISIONS:
            raise HTTPException(status_code=400, detail="Invalid decision.")
        update_data["decision"] = request.decision
        update_data["decided_by"] = user_id
        update_data["decided_at"] = datetime.now(timezone.utc).isoformat()
        if request.decision == "excluded":
            update_data["excluded"] = True

    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    # A set that's already been merged is immutable — never reopen it.
    result = supabase.table("duplicate_sets").update(
        update_data
    ).eq("id", set_id).eq("scan_id", scan_id).eq("merged", False).execute()

    if not result.data:
        raise HTTPException(
            status_code=404, detail="Duplicate set not found (or already merged)."
        )

    return result.data[0]
