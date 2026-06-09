"""Salesforce accounts fetching service (for the config-driven match engine).

Returns raw SF record dicts (the shape MatchEngine consumes). Uses queryAll so
soft-deleted/archived rows are visible to dedupe and the pre-merge snapshot.
"""
import httpx
from urllib.parse import quote
from typing import Optional

from app.services.salesforce import SalesforceConnection

# Only what the account profiles bind to — keeps ~48k rows light.
ACCOUNT_FIELDS = [
    "Id", "Name", "Website", "BillingCountry", "BillingCountryCode",
    "BillingStateCode", "Vertical__c", "ParentId", "LastActivityDate",
    "SCD_NetSuite_Sync_Active__c", "SCD_NetSuite_ID__c", "AccountNumber",
    "OwnerId", "CreatedDate",
]


class SalesforceAccountsService:
    """Fetches Account records from Salesforce as raw dicts."""

    def __init__(self, connection: SalesforceConnection):
        self.access_token = connection.access_token
        self.instance_url = connection.instance_url

    async def get_total_accounts(self) -> int:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.instance_url}/services/data/v59.0/query",
                params={"q": "SELECT COUNT() FROM Account"},
                headers=self._headers(),
            )
            return resp.json().get("totalSize", 0) if resp.status_code == 200 else 0

    async def get_all_accounts(self, progress_callback: Optional[callable] = None) -> list[dict]:
        """Fetch all accounts (queryAll, paginated). Returns a list of raw SF dicts."""
        query = f"SELECT {', '.join(ACCOUNT_FIELDS)} FROM Account"
        next_url = f"{self.instance_url}/services/data/v59.0/queryAll?q={quote(query)}"
        records: list[dict] = []

        async with httpx.AsyncClient(timeout=120.0) as client:
            while next_url:
                resp = await client.get(next_url, headers=self._headers())
                if resp.status_code != 200:
                    raise Exception(f"Failed to fetch accounts: {resp.text}")
                data = resp.json()
                for rec in data.get("records", []):
                    rec.pop("attributes", None)
                    records.append(rec)
                if progress_callback:
                    await progress_callback(len(records))
                rel = data.get("nextRecordsUrl")
                next_url = f"{self.instance_url}{rel}" if rel else None

        return records

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
