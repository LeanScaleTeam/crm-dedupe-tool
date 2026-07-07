"""Tenant resolution + access control (multi-tenant model; tenant = connected org).

WHY THIS EXISTS IN CODE AS WELL AS SQL: the backend talks to Postgres with the
service-role key, which BYPASSES row-level security. So the RLS policies in
004_multi_tenant.sql only guard the anon/JWT path the browser could take — every
protected backend endpoint must ALSO enforce tenant access here, in code. This
module mirrors the SQL `can_access_tenant()` rule exactly:

    access(tenant) := is_platform_staff(user)
                      OR ( active member of tenant
                           AND ( role = 'owner'
                                 OR ( role = 'client' AND tenant.client_access_enabled ) ) )

`user_id` is kept on every row as the created_by / actor (and is what the rollback
restores to); it is no longer the access boundary — tenant membership is.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException


# --------------------------------------------------------------------------- #
# Access checks (mirror of the SQL can_access_tenant / is_platform_staff)
# --------------------------------------------------------------------------- #
def is_platform_staff(supabase, user_id: str) -> bool:
    """True if user_id is a LeanScale platform operator (cross-tenant access)."""
    res = supabase.table("platform_staff").select("user_id").eq(
        "user_id", user_id
    ).execute()
    return bool(res.data)


def can_access_tenant(supabase, tenant_id: Optional[str], user_id: str) -> bool:
    """Mirror of the SQL rule. Platform staff can reach any tenant; otherwise the
    caller must be an active member whose role grants access (owner always; client
    only while the tenant's client_access_enabled flag is on)."""
    if not tenant_id or not user_id:
        return False
    if is_platform_staff(supabase, user_id):
        return True

    member = supabase.table("tenant_members").select("role,is_active").eq(
        "tenant_id", tenant_id
    ).eq("user_id", user_id).execute()
    rows = member.data or []
    if not rows:
        return False

    active = [r for r in rows if r.get("is_active")]
    if not active:
        return False
    # Owner membership is always sufficient.
    if any(r.get("role") == "owner" for r in active):
        return True
    # Client membership only counts while the tenant has client access enabled.
    if any(r.get("role") == "client" for r in active):
        tn = supabase.table("tenants").select("client_access_enabled").eq(
            "id", tenant_id
        ).single().execute()
        return bool(tn.data and tn.data.get("client_access_enabled"))
    return False


def assert_tenant_access(supabase, tenant_id: Optional[str], user_id: str) -> None:
    """Raise 404 (not 403 — don't reveal existence to a non-member) if the caller
    cannot access the given tenant."""
    if not can_access_tenant(supabase, tenant_id, user_id):
        raise HTTPException(status_code=404, detail="Not found")


def accessible_tenant_ids(supabase, user_id: str) -> Optional[list[str]]:
    """Tenant ids this user may read, for list endpoints.

    Returns None to mean "all tenants" (the caller is platform staff and should not
    be filtered). Otherwise returns the concrete list of tenant ids the user can
    access right now (active owner memberships, plus client memberships whose tenant
    has client access enabled). May be empty."""
    if is_platform_staff(supabase, user_id):
        return None

    res = supabase.table("tenant_members").select("tenant_id,role,is_active").eq(
        "user_id", user_id
    ).execute()
    rows = [r for r in (res.data or []) if r.get("is_active")]
    if not rows:
        return []

    owner_ids = {r["tenant_id"] for r in rows if r.get("role") == "owner"}
    client_ids = {r["tenant_id"] for r in rows if r.get("role") == "client"}
    enabled_client_ids: set[str] = set()
    if client_ids:
        tns = supabase.table("tenants").select("id,client_access_enabled").in_(
            "id", list(client_ids)
        ).execute()
        enabled_client_ids = {
            t["id"] for t in (tns.data or []) if t.get("client_access_enabled")
        }
    return list(owner_ids | enabled_client_ids)


# --------------------------------------------------------------------------- #
# Tenant provisioning (tenant = connected org)
# --------------------------------------------------------------------------- #
def _ensure_owner_membership(supabase, tenant_id: str, user_id: str) -> None:
    """Make user_id an active OWNER of tenant_id ONLY if they have no membership yet.

    Provisioning-only and deliberately NON-destructive: if a membership row already
    exists, leave it exactly as-is (role, is_active untouched). This is what makes a
    staff revocation DURABLE. resolve_tenant_for_save runs on every token-refresh
    re-save (salesforce/hubspot get_connection -> save_connection), not just on first
    connect — so an upsert here would silently flip a deliberately deactivated or
    downgraded member back to active owner on the owner's next ~2h token refresh. We
    insert only when absent, so a staff change to tenant_members persists.
    """
    existing = supabase.table("tenant_members").select("tenant_id").eq(
        "tenant_id", tenant_id
    ).eq("user_id", user_id).execute()
    if existing.data:
        return
    supabase.table("tenant_members").insert(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": "owner",
            "is_active": True,
        }
    ).execute()


def resolve_tenant_for_save(
    supabase, user_id: str, crm_type: str, label: Optional[str], org_id: Optional[str] = None
) -> str:
    """Return the tenant_id to stamp on a connection being saved (tenant = org).

    Multi-org: reuse the tenant of an existing connection to the SAME org (so
    re-saving tokens for an org never spawns a duplicate tenant), but a DIFFERENT
    org gets its own fresh tenant — client access OFF by default — with the
    connecting user as owner. When org_id is not given, fall back to the legacy
    one-tenant-per-crm_type behavior.

    Must be called BEFORE inserting the connection, because crm_connections.tenant_id
    is NOT NULL.
    """
    q = supabase.table("crm_connections").select("tenant_id").eq(
        "user_id", user_id
    ).eq("crm_type", crm_type)
    if org_id is not None:
        q = q.eq("org_id", org_id)
    existing = q.execute()
    rows = existing.data or []
    if rows and rows[0].get("tenant_id"):
        tenant_id = rows[0]["tenant_id"]
        _ensure_owner_membership(supabase, tenant_id, user_id)
        return tenant_id

    tenant_id = str(uuid.uuid4())
    name = f"{label or crm_type} ({crm_type})"
    supabase.table("tenants").insert(
        {"id": tenant_id, "name": name, "client_access_enabled": False}
    ).execute()
    _ensure_owner_membership(supabase, tenant_id, user_id)
    return tenant_id
