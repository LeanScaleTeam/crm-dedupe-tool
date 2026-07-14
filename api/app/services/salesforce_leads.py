"""Salesforce leads fetching service.

Leads are person-shaped, so they map onto the shared Contact model and reuse the
same email/name matcher (DuplicateDetector) and person FieldBlender used for
contacts. Only UNCONVERTED leads are fetched (IsConverted = false): a converted
lead is historical/read-only and merging it is nonsense.

Note vs contacts: Lead.Company is a plain TEXT field (not an Account lookup), so
`company` is a first-class, writable Lead field here — unlike Contact where
company implies AccountId.
"""
import httpx
from urllib.parse import quote
from typing import AsyncGenerator, Optional
from datetime import datetime

from app.models.contact import Contact
from app.services.salesforce import SalesforceConnection


class SalesforceLeadsService:
    """Service for fetching Lead records from Salesforce as Contact models."""

    BATCH_SIZE = 2000  # Salesforce SOQL query page size

    def __init__(self, connection: SalesforceConnection):
        self.connection = connection
        self.access_token = connection.access_token
        self.instance_url = connection.instance_url

    async def get_all_leads(
        self,
        progress_callback: Optional[callable] = None,
    ) -> AsyncGenerator[Contact, None]:
        """Fetch all UNCONVERTED leads (paginated via nextRecordsUrl), yielding one
        Contact-shaped record at a time for memory efficiency."""
        query = """
            SELECT Id, Email, FirstName, LastName, Phone, Company, Title,
                   Status, LeadSource, CreatedDate, LastModifiedDate
            FROM Lead
            WHERE IsConverted = false
        """

        total_fetched = 0
        next_url = f"{self.instance_url}/services/data/v59.0/query?q={quote(query.strip())}"

        async with httpx.AsyncClient() as client:
            while next_url:
                response = await client.get(
                    next_url,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code != 200:
                    # Surface the provider status + detail (e.g. 401 INVALID_SESSION_ID
                    # -> reconnect) in scans.error_message so the user sees WHY.
                    detail = (response.text or "")[:250]
                    print(f"[salesforce] fetch leads failed ({response.status_code}): {response.text}")
                    raise Exception(f"Salesforce leads fetch failed ({response.status_code}): {detail}")

                data = response.json()
                records = data.get("records", [])

                for record in records:
                    lead = Contact(
                        id=record["Id"],
                        email=record.get("Email"),
                        first_name=record.get("FirstName"),
                        last_name=record.get("LastName"),
                        phone=record.get("Phone"),
                        company=record.get("Company"),  # plain text on Lead
                        job_title=record.get("Title"),
                        created_at=self._parse_datetime(record.get("CreatedDate")),
                        updated_at=self._parse_datetime(record.get("LastModifiedDate")),
                        association_count=0,  # leads have no child opportunities
                        raw_properties=record,
                    )
                    yield lead
                    total_fetched += 1

                if progress_callback:
                    await progress_callback(total_fetched)

                next_url = data.get("nextRecordsUrl")
                if next_url:
                    next_url = f"{self.instance_url}{next_url}"

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Parse Salesforce datetime string."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    async def get_total_leads(self) -> int:
        """Count of unconverted leads (for scan progress)."""
        query = "SELECT COUNT() FROM Lead WHERE IsConverted = false"

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.instance_url}/services/data/v59.0/query",
                params={"q": query},
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code != 200:
                return 0

            return response.json().get("totalSize", 0)
