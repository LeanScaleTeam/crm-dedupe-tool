"""Report endpoints."""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response

from app.auth import require_user
from app.services.reports import ReportService
from app.services.supabase_client import get_supabase

router = APIRouter()


def _assert_report_owner(supabase, report_id: str, user_id: str) -> dict:
    res = supabase.table("reports").select("*").eq("id", report_id).eq(
        "user_id", user_id
    ).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Report not found")
    return res.data


@router.post("/generate/{merge_id}")
async def generate_report(merge_id: str, user_id: str = Depends(require_user)):
    """Generate a report for a completed merge owned by the caller."""
    supabase = get_supabase()
    # Verify the merge belongs to the authenticated user before generating.
    merge = supabase.table("merges").select("id").eq("id", merge_id).eq(
        "user_id", user_id
    ).single().execute()
    if not merge.data:
        raise HTTPException(status_code=404, detail="Merge not found")

    service = ReportService()
    try:
        return await service.generate_report(merge_id, user_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/mine")
async def list_my_reports(
    page: int = 1, per_page: int = 20, user_id: str = Depends(require_user)
):
    """List the authenticated user's reports."""
    supabase = get_supabase()
    offset = (page - 1) * per_page

    result = supabase.table("reports").select("*").eq(
        "user_id", user_id
    ).order("created_at", desc=True).range(offset, offset + per_page - 1).execute()

    count_result = supabase.table("reports").select(
        "id", count="exact"
    ).eq("user_id", user_id).execute()

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
    """Get report data (owner only)."""
    supabase = get_supabase()
    return _assert_report_owner(supabase, report_id, user_id)


@router.get("/{report_id}/pdf")
async def download_report_pdf(report_id: str, user_id: str = Depends(require_user)):
    """Download report as PDF (owner only)."""
    supabase = get_supabase()
    _assert_report_owner(supabase, report_id, user_id)

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
