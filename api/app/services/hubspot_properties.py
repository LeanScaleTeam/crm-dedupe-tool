"""Fetch the WRITABLE property set for a HubSpot object.

Used by the review UI so a reviewer can pick which record's value wins for any
editable field on merge — while read-only/calculated properties (most hs_* fields)
stay display-only and are never PATCHed (which would 400 and fail the merge).
"""
import httpx

BASE_URL = "https://api.hubapi.com"


async def get_writable_properties(access_token: str, hs_object: str) -> list:
    """Return [{name, label}] of properties that can be written via the API.

    Filters out read-only, calculated, and hidden properties using the catalog's
    modificationMetadata. hs_object is a HubSpot object name ("companies"/"contacts").
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/crm/v3/properties/{hs_object}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            print(f"[hubspot] writable-properties fetch failed ({resp.status_code}): {resp.text}")
            return []

        out = []
        for p in resp.json().get("results", []):
            name = p.get("name")
            if not name:
                continue
            mod = p.get("modificationMetadata") or {}
            if mod.get("readOnlyValue"):
                continue
            if p.get("calculated"):
                continue
            if p.get("hidden"):
                continue
            out.append({"name": name, "label": p.get("label") or name})
        return out
