"""Salesforce accounts fetching service.

For the config-driven match engine it returns raw SF record dicts (the shape
MatchEngine consumes). For the SIMPLE generic dedupe path it also maps accounts
to the shared Company model using ONLY standard fields, so it works on any org
(no client-specific custom fields). Uses queryAll so soft-deleted/archived rows
are visible to dedupe and the pre-merge snapshot.
"""
import httpx
from urllib.parse import quote
from typing import Optional
from datetime import datetime

from app.models.company import Company
from app.services.salesforce import SalesforceConnection

# Only what the account profiles bind to — keeps ~48k rows light. NOTE: these
# include client-specific custom fields (Vertical__c, SCD_NetSuite_*) so this set
# is ONLY safe for orgs that have them (the config-driven Scandit path).
ACCOUNT_FIELDS = [
    "Id", "Name", "Website", "BillingCountry", "BillingCountryCode",
    "BillingStateCode", "Vertical__c", "ParentId", "LastActivityDate",
    "SCD_NetSuite_Sync_Active__c", "SCD_NetSuite_ID__c", "AccountNumber",
    "OwnerId", "CreatedDate",
]

# Standard Account fields present on EVERY Salesforce org — used by the simple
# domain+name dedupe path so it works for any client (e.g. Coactive) without
# depending on custom fields.
STANDARD_ACCOUNT_FIELDS = [
    "Id", "Name", "Website", "Phone", "Industry",
    "BillingCountry", "BillingCity", "CreatedDate", "LastModifiedDate",
]


def _parse_sf_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a Salesforce datetime (e.g. 2023-05-05T18:44:29.000+0000)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except (ValueError, TypeError):
        return None


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
                    detail = (resp.text or "")[:250]
                    raise Exception(f"Salesforce accounts fetch failed ({resp.status_code}): {detail}")
                data = resp.json()
                for rec in data.get("records", []):
                    rec.pop("attributes", None)
                    records.append(rec)
                if progress_callback:
                    await progress_callback(len(records))
                rel = data.get("nextRecordsUrl")
                next_url = f"{self.instance_url}{rel}" if rel else None

        return records

    async def get_all_accounts_as_companies(
        self, progress_callback: Optional[callable] = None
    ) -> list:
        """Fetch accounts with STANDARD fields only (works on any org) and map them
        to Company records for the simple domain+name matcher."""
        query = f"SELECT {', '.join(STANDARD_ACCOUNT_FIELDS)} FROM Account"
        next_url = f"{self.instance_url}/services/data/v59.0/queryAll?q={quote(query)}"
        companies: list = []

        async with httpx.AsyncClient(timeout=120.0) as client:
            while next_url:
                resp = await client.get(next_url, headers=self._headers())
                if resp.status_code != 200:
                    detail = (resp.text or "")[:250]
                    raise Exception(f"Salesforce accounts fetch failed ({resp.status_code}): {detail}")
                data = resp.json()
                for rec in data.get("records", []):
                    rec.pop("attributes", None)
                    companies.append(self._to_company(rec))
                if progress_callback:
                    await progress_callback(len(companies))
                rel = data.get("nextRecordsUrl")
                next_url = f"{self.instance_url}{rel}" if rel else None

        return companies

    @staticmethod
    def _to_company(rec: dict) -> Company:
        """Map a standard-fields Account dict to the shared Company model."""
        return Company(
            id=rec.get("Id"),
            name=rec.get("Name"),
            website=rec.get("Website"),
            phone=rec.get("Phone"),
            industry=rec.get("Industry"),
            country=rec.get("BillingCountry"),
            created_at=_parse_sf_dt(rec.get("CreatedDate")),
            updated_at=_parse_sf_dt(rec.get("LastModifiedDate")),
            raw_properties=rec,
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
