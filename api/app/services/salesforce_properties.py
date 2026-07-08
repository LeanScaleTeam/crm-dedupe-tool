"""Fetch the WRITABLE (updateable) field set for a Salesforce SObject via describe.

Used by the review UI so a reviewer can pick which record's value wins for any
editable field on merge, while read-only/formula/system fields stay display-only
(writing them would fail the pre-merge update).
"""
import httpx

API_VERSION = "59.0"


async def get_writable_sf_fields(instance_url: str, access_token: str, sobject: str) -> list:
    """Return [{name, label}] of UPDATEABLE fields for the SObject (Contact/Account)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            f"{instance_url}/services/data/v{API_VERSION}/sobjects/{sobject}/describe",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            print(f"[salesforce] describe {sobject} failed ({resp.status_code}): {resp.text[:300]}")
            return []

        out = []
        for f in resp.json().get("fields", []):
            name = f.get("name")
            if not name or not f.get("updateable"):
                continue
            out.append({"name": name, "label": f.get("label") or name})
        return out
