"""Report endpoints."""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response

from app.auth import require_user
from app.services.reports import ReportService
from app.services.supabase_client import get_supabase
from app.services.tenancy import assert_tenant_access, accessible_tenant_ids

router = APIRouter()


def _assert_report_access(supabase, report_id: str, user_id: str) -> dict:
    res = supabase.table("reports").select("*").eq("id", report_id).limit(1).execute()
    report = (res.data or [None])[0]
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    assert_tenant_access(supabase, report.get("tenant_id"), user_id)
    return report


@router.post("/generate/{merge_id}")
async def generate_report(merge_id: str, user_id: str = Depends(require_user)):
    """Generate a report for a completed merge in a tenant the caller can access."""
    supabase = get_supabase()
    # Verify the merge exists and the caller can access its tenant before generating.
    merge_res = supabase.table("merges").select("id,tenant_id").eq(
        "id", merge_id
    ).limit(1).execute()
    merge = (merge_res.data or [None])[0]
    if not merge:
        raise HTTPException(status_code=404, detail="Merge not found")
    assert_tenant_access(supabase, merge.get("tenant_id"), user_id)

    service = ReportService()
    try:
        return await service.generate_report(merge_id, user_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/mine")
async def list_my_reports(
    page: int = 1, per_page: int = 20, user_id: str = Depends(require_user)
):
    """List reports in the tenants the caller can access (platform staff see all)."""
    supabase = get_supabase()
    offset = (page - 1) * per_page

    # None => platform staff (no tenant filter); [] => member of nothing accessible.
    tenant_ids = accessible_tenant_ids(supabase, user_id)
    if tenant_ids is not None and not tenant_ids:
        return {
            "reports": [], "total": 0, "page": page,
            "per_page": per_page, "total_pages": 0,
        }

    def _scoped(query):
        return query if tenant_ids is None else query.in_("tenant_id", tenant_ids)

    result = _scoped(
        supabase.table("reports").select("*")
    ).order("created_at", desc=True).range(offset, offset + per_page - 1).execute()

    count_result = _scoped(
        supabase.table("reports").select("id", count="exact")
    ).execute()

    total = count_result.count if count_result.count else 0

    return {
        "reports": result.data or [],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total > 0 else 0,
    }


@router.get("/{report_id}")
async def get_report(report_id: str, user_id: str = Depends(require_user)):
    """Get report data (accessible to any member of the report's tenant)."""
    supabase = get_supabase()
    return _assert_report_access(supabase, report_id, user_id)


@router.get("/{report_id}/pdf")
async def download_report_pdf(report_id: str, user_id: str = Depends(require_user)):
    """Download report as PDF (accessible to any member of the report's tenant)."""
    supabase = get_supabase()
    _assert_report_access(supabase, report_id, user_id)

    service = ReportService()
    try:
        pdf_bytes = await service.generate_pdf(report_id, user_id)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=dedup-report-{report_id[:8]}.pdf"
            },
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
