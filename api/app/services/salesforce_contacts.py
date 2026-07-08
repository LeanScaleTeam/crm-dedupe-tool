"""Salesforce contacts fetching service."""
import httpx
from urllib.parse import quote
from typing import AsyncGenerator, Optional
from datetime import datetime

from app.models.contact import Contact
from app.services.salesforce import SalesforceConnection


class SalesforceContactsService:
    """Service for fetching contacts from Salesforce."""

    BATCH_SIZE = 2000  # Salesforce SOQL query limit

    def __init__(self, connection: SalesforceConnection):
        self.connection = connection
        self.access_token = connection.access_token
        self.instance_url = connection.instance_url

    async def get_all_contacts(
        self,
        progress_callback: Optional[callable] = None
    ) -> AsyncGenerator[Contact, None]:
        """
        Fetch all contacts from Salesforce with pagination.
        Uses SOQL queries with pagination via nextRecordsUrl.
        """
        query = """
            SELECT Id, Email, FirstName, LastName, Phone, Account.Name,
                   Title, CreatedDate, LastModifiedDate,
                   (SELECT Id FROM Opportunities)
            FROM Contact
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
                    print(f"[salesforce] fetch contacts failed ({response.status_code}): {response.text}")
                    raise Exception(f"Salesforce contacts fetch failed ({response.status_code}): {detail}")

                data = response.json()
                records = data.get("records", [])

                for record in records:
                    # Count opportunities
                    opps = record.get("Opportunities", {})
                    opp_count = len(opps.get("records", [])) if opps else 0

                    contact = Contact(
                        id=record["Id"],
                        email=record.get("Email"),
                        first_name=record.get("FirstName"),
                        last_name=record.get("LastName"),
                        phone=record.get("Phone"),
                        company=record.get("Account", {}).get("Name") if record.get("Account") else None,
                        job_title=record.get("Title"),
                        created_at=self._parse_datetime(record.get("CreatedDate")),
                        updated_at=self._parse_datetime(record.get("LastModifiedDate")),
                        association_count=opp_count,
                        raw_properties=record,
                    )
                    yield contact
                    total_fetched += 1

                # Progress callback
                if progress_callback:
                    await progress_callback(total_fetched)

                # Check for next page
                next_url = data.get("nextRecordsUrl")
                if next_url:
                    next_url = f"{self.instance_url}{next_url}"

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Parse Salesforce datetime string."""
        if not value:
            return None
        try:
            # Salesforce uses ISO format
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    async def get_total_contacts(self) -> int:
        """Get total count of contacts."""
        query = "SELECT COUNT() FROM Contact"

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

            data = response.json()
            return data.get("totalSize", 0)
