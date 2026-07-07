"""Salesforce OAuth and API endpoints."""
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from app.auth import require_user
from app.services.salesforce import SalesforceService
from app.services.supabase_client import get_supabase
from app.services.tenancy import assert_tenant_access, accessible_tenant_ids

router = APIRouter()

# Map our object types to Salesforce SObject API names.
_SOBJECT = {"contacts": "Contact", "accounts": "Account", "leads": "Lead"}


class TokenExchangeRequest(BaseModel):
    code: str
    redirect_uri: str
    code_verifier: Optional[str] = None  # PKCE (required by External Client Apps)
    login_url: Optional[str] = None  # OAuth host used (multi-org: prod vs sandbox)


class ConnectionStatusResponse(BaseModel):
    connected: bool
    org_id: Optional[str] = None
    instance_url: Optional[str] = None
    error: Optional[str] = None


@router.post("/exchange-token")
async def exchange_token(
    request: TokenExchangeRequest, user_id: str = Depends(require_user)
):
    """Exchange OAuth code for access token and save connection for the caller."""
    service = SalesforceService()

    try:
        # Exchange code for tokens
        tokens = await service.exchange_code_for_tokens(
            code=request.code,
            redirect_uri=request.redirect_uri,
            code_verifier=request.code_verifier,
            login_url=request.login_url,
        )

        # Get org ID
        org_id = await service.get_org_id(tokens.access_token, tokens.instance_url)

        # Save connection
        connection = await service.save_connection(
            user_id=user_id,
            tokens=tokens,
            org_id=org_id,
        )

        return {
            "success": True,
            "org_id": org_id,
            "instance_url": tokens.instance_url,
            "connection_id": connection["id"] if connection else None,
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/fields")
async def list_fields(
    connection_id: str,
    object_type: str = "contacts",
    user_id: str = Depends(require_user),
):
    """List the fields of the connected org's object (for the custom-field
    discriminator picker). Returns [{name, label, type}] sorted by label."""
    supabase = get_supabase()
    conn = (
        supabase.table("crm_connections").select("*").eq("id", connection_id)
        .limit(1).execute().data or [None]
    )[0]
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    assert_tenant_access(supabase, conn.get("tenant_id"), user_id)

    connection = await SalesforceService().get_connection(conn["user_id"])
    if not connection:
        raise HTTPException(status_code=400, detail="Salesforce connection unavailable")

    sobject = _SOBJECT.get(object_type, "Contact")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{connection.instance_url}/services/data/v59.0/sobjects/{sobject}/describe",
            headers={"Authorization": f"Bearer {connection.access_token}"},
        )
    if resp.status_code != 200:
        print(f"[salesforce] describe {sobject} failed ({resp.status_code}): {resp.text}")
        raise HTTPException(status_code=400, detail="Failed to load fields.")

    fields = [
        {"name": f["name"], "label": f.get("label") or f["name"], "type": f.get("type")}
        for f in resp.json().get("fields", [])
        if f.get("type") not in ("address", "location", "base64")
    ]
    fields.sort(key=lambda f: (f["label"] or "").lower())
    return {"fields": fields}


@router.get("/connection-status")
async def connection_status(
    user_id: str = Depends(require_user),
) -> ConnectionStatusResponse:
    """Check if the authenticated user has a valid Salesforce connection."""
    service = SalesforceService()

    try:
        connection = await service.get_connection(user_id)

        if connection:
            return ConnectionStatusResponse(
                connected=True,
                org_id=connection.org_id,
                instance_url=connection.instance_url,
            )
        else:
            return ConnectionStatusResponse(
                connected=False,
            )

    except Exception as e:
        return ConnectionStatusResponse(
            connected=False,
            error=str(e),
        )


@router.delete("/disconnect")
async def disconnect(user_id: str = Depends(require_user)):
    """Disconnect Salesforce for the authenticated user."""
    service = SalesforceService()

    try:
        deleted = await service.delete_connection(user_id)
        return {"success": deleted}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/connections")
async def list_connections(user_id: str = Depends(require_user)):
    """List every Salesforce org the caller can access (multi-org dashboard)."""
    supabase = get_supabase()
    tenant_ids = accessible_tenant_ids(supabase, user_id)  # None => platform staff (all)
    rows = (
        supabase.table("crm_connections")
        .select("id,crm_type,org_id,portal_id,label,created_at,tenant_id")
        .eq("crm_type", "salesforce").order("created_at").execute().data or []
    )
    if tenant_ids is not None:
        allowed = set(tenant_ids)
        rows = [r for r in rows if r.get("tenant_id") in allowed]

    connections = []
    for r in rows:
        portal = r.get("portal_id") or ""
        instance = portal.split("|", 1)[1] if "|" in portal else None
        org_id = r.get("org_id") or (portal.split("|", 1)[0] if portal else None)
        connections.append({
            "id": r["id"],
            "crm_type": r["crm_type"],
            "org_id": org_id,
            "instance_url": instance,
            "label": r.get("label"),
            "created_at": r.get("created_at"),
        })
    return {"connections": connections}


@router.delete("/connections/{connection_id}")
async def delete_connection_by_id(connection_id: str, user_id: str = Depends(require_user)):
    """Disconnect ONE org by connection id (multi-org)."""
    supabase = get_supabase()
    conn = (
        supabase.table("crm_connections").select("id,tenant_id")
        .eq("id", connection_id).limit(1).execute().data or [None]
    )[0]
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    assert_tenant_access(supabase, conn.get("tenant_id"), user_id)
    supabase.table("crm_connections").delete().eq("id", connection_id).execute()
    return {"success": True}
