"""Fetch a record's associated (related) records from HubSpot for the review UI.

Given one contact or company, returns its associated records grouped by type
(e.g. a company's contacts + deals) with a few display properties each. Read-only
context for the comparison screen — helps a reviewer see what's attached to each
duplicate before merging.
"""
import httpx

BASE_URL = "https://api.hubapi.com"

# Which related objects (and display properties) to fetch per source object.
# Deals require the crm.objects.deals.read scope (added to the app 2026-07-07); a
# portal connected before that must reconnect to consent. If the scope is still
# ungranted the deals call 403s and is swallowed to an empty list (no crash).
RELATED = {
    "companies": {
        "contacts": ["firstname", "lastname", "email", "jobtitle"],
        "deals": ["dealname", "amount", "dealstage"],
    },
    "contacts": {
        "companies": ["name", "domain"],
        "deals": ["dealname", "amount", "dealstage"],
    },
}

# Cap per related type so a record with thousands of associations stays responsive.
MAX_PER_TYPE = 100


async def get_related_records(access_token: str, from_object: str, record_id: str) -> dict:
    """Return {to_object: [{id, properties}]} of associated records.

    Two calls per related type: v4 associations to list ids, then a v3 batch-read
    for the display properties. Failures degrade to an empty list for that type.
    """
    related = RELATED.get(from_object, {})
    out: dict = {}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        for to_object, props in related.items():
            out[to_object] = []
            # 1) List associated ids (v4).
            try:
                resp = await client.get(
                    f"{BASE_URL}/crm/v4/objects/{from_object}/{record_id}/associations/{to_object}",
                    params={"limit": MAX_PER_TYPE},
                    headers=headers,
                )
            except Exception as e:
                print(f"[hubspot] associations list error ({from_object}->{to_object}): {e}")
                continue
            if resp.status_code != 200:
                print(f"[hubspot] associations list failed ({resp.status_code}): {resp.text}")
                continue

            ids = [str(r.get("toObjectId")) for r in resp.json().get("results", []) if r.get("toObjectId") is not None]
            if not ids:
                continue

            # 2) Batch-read display properties for those ids (v3).
            try:
                read = await client.post(
                    f"{BASE_URL}/crm/v3/objects/{to_object}/batch/read",
                    json={"properties": props, "inputs": [{"id": i} for i in ids[:MAX_PER_TYPE]]},
                    headers=headers,
                )
            except Exception as e:
                print(f"[hubspot] associations batch-read error ({to_object}): {e}")
                continue
            if read.status_code != 200:
                print(f"[hubspot] associations batch-read failed ({read.status_code}): {read.text}")
                continue

            out[to_object] = [
                {"id": rec.get("id"), "properties": rec.get("properties", {})}
                for rec in read.json().get("results", [])
            ]

    return out
