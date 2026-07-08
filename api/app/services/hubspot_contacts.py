"""HubSpot contacts fetching service."""
import httpx
from typing import AsyncGenerator, Optional
from datetime import datetime

from app.models.contact import Contact
from app.services.hubspot import HubSpotService, HubSpotConnection


class HubSpotContactsService:
    """Service for fetching contacts from HubSpot."""

    BASE_URL = "https://api.hubapi.com"
    BATCH_SIZE = 100  # HubSpot's max per request

    def __init__(self, connection: HubSpotConnection):
        self.connection = connection
        self.access_token = connection.access_token

    async def get_all_contacts(
        self,
        progress_callback: Optional[callable] = None
    ) -> AsyncGenerator[Contact, None]:
        """
        Fetch all contacts from HubSpot with pagination.
        Yields contacts one at a time for memory efficiency.
        """
        after = None
        total_fetched = 0

        async with httpx.AsyncClient() as client:
            # Discover ALL contact properties so raw_properties captures every
            # populated field, not a hardcoded subset. (HubSpot returns nothing
            # you don't explicitly request.)
            properties = await self._get_property_names(client)
            while True:
                params = {
                    "limit": self.BATCH_SIZE,
                    "properties": ",".join(properties),
                }
                if after:
                    params["after"] = after

                response = await client.get(
                    f"{self.BASE_URL}/crm/v3/objects/contacts",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code != 200:
                    # Surface the provider status + detail (e.g. 403 MISSING_SCOPES,
                    # 429 rate limit) in scans.error_message so the user sees WHY.
                    detail = (response.text or "")[:250]
                    print(f"[hubspot] fetch contacts failed ({response.status_code}): {response.text}")
                    raise Exception(f"HubSpot contacts fetch failed ({response.status_code}): {detail}")

                data = response.json()
                results = data.get("results", [])

                for record in results:
                    props = record.get("properties", {})
                    contact = Contact(
                        id=record["id"],
                        email=props.get("email"),
                        first_name=props.get("firstname"),
                        last_name=props.get("lastname"),
                        phone=props.get("phone"),
                        company=props.get("company"),
                        job_title=props.get("jobtitle"),
                        created_at=self._parse_datetime(props.get("createdate")),
                        updated_at=self._parse_datetime(props.get("lastmodifieddate")),
                        association_count=self._count_associations(props),
                        raw_properties=props,
                    )
                    yield contact
                    total_fetched += 1

                # Progress callback
                if progress_callback:
                    await progress_callback(total_fetched)

                # Check for next page
                paging = data.get("paging", {})
                next_link = paging.get("next", {})
                after = next_link.get("after")

                if not after:
                    break  # No more pages

    async def _get_property_names(self, client: httpx.AsyncClient) -> list:
        """Every contact property name (from the property catalog) so we can request
        them all. Falls back to a core set if the catalog call fails."""
        try:
            resp = await client.get(
                f"{self.BASE_URL}/crm/v3/properties/contacts",
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            if resp.status_code == 200:
                names = [p["name"] for p in resp.json().get("results", []) if p.get("name")]
                if names:
                    return names
            print(f"[hubspot] property catalog fetch failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"[hubspot] property catalog error: {e}")
        return [
            "email", "firstname", "lastname", "phone", "company",
            "jobtitle", "createdate", "lastmodifieddate",
            "num_associated_deals", "num_contacted_times",
        ]

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Parse HubSpot datetime string."""
        if not value:
            return None
        try:
            # HubSpot uses ISO format
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _count_associations(self, props: dict) -> int:
        """Count associated records from properties."""
        count = 0
        for key in ["num_associated_deals", "num_contacted_times"]:
            try:
                count += int(props.get(key, 0) or 0)
            except (ValueError, TypeError):
                pass
        return count

    async def get_total_contacts(self) -> int:
        """Total contact count (for progress). The v3 LIST endpoint doesn't return a
        total, so use the SEARCH endpoint (empty filter = all records)."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/crm/v3/objects/contacts/search",
                json={"limit": 1},
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code != 200:
                return 0

            return response.json().get("total", 0)
