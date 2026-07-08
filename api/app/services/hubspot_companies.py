"""HubSpot companies fetching service.

Mirrors HubSpotContactsService but targets the companies object: different
endpoint, property catalog, and record mapping (name/domain/website, not
email/first/last). Yields Company records for the company dedupe pipeline.
"""
import httpx
from typing import AsyncGenerator, Optional
from datetime import datetime

from app.models.company import Company
from app.services.hubspot import HubSpotConnection


class HubSpotCompaniesService:
    """Service for fetching companies from HubSpot."""

    BASE_URL = "https://api.hubapi.com"
    BATCH_SIZE = 100  # HubSpot's max per request

    def __init__(self, connection: HubSpotConnection):
        self.connection = connection
        self.access_token = connection.access_token

    async def get_all_companies(
        self,
        progress_callback: Optional[callable] = None
    ) -> AsyncGenerator[Company, None]:
        """
        Fetch all companies from HubSpot with pagination.
        Yields companies one at a time for memory efficiency.
        """
        after = None
        total_fetched = 0

        async with httpx.AsyncClient() as client:
            # Discover ALL company properties so raw_properties captures every
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
                    f"{self.BASE_URL}/crm/v3/objects/companies",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code != 200:
                    # Surface the provider status + detail in scans.error_message.
                    detail = (response.text or "")[:250]
                    print(f"[hubspot] fetch companies failed ({response.status_code}): {response.text}")
                    raise Exception(f"HubSpot companies fetch failed ({response.status_code}): {detail}")

                data = response.json()
                results = data.get("results", [])

                for record in results:
                    props = record.get("properties", {})
                    company = Company(
                        id=record["id"],
                        name=props.get("name"),
                        domain=props.get("domain"),
                        website=props.get("website"),
                        phone=props.get("phone"),
                        industry=props.get("industry"),
                        country=props.get("country"),
                        created_at=self._parse_datetime(props.get("createdate")),
                        updated_at=self._parse_datetime(
                            props.get("hs_lastmodifieddate") or props.get("lastmodifieddate")
                        ),
                        association_count=self._count_associations(props),
                        raw_properties=props,
                    )
                    yield company
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
        """Every company property name (from the property catalog) so we can request
        them all. Falls back to a core set if the catalog call fails."""
        try:
            resp = await client.get(
                f"{self.BASE_URL}/crm/v3/properties/companies",
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            if resp.status_code == 200:
                names = [p["name"] for p in resp.json().get("results", []) if p.get("name")]
                if names:
                    return names
            print(f"[hubspot] company property catalog fetch failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"[hubspot] company property catalog error: {e}")
        return [
            "name", "domain", "website", "phone", "industry", "country",
            "city", "state", "numberofemployees", "annualrevenue",
            "createdate", "hs_lastmodifieddate",
            "num_associated_contacts", "num_associated_deals",
        ]

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Parse HubSpot datetime string."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _count_associations(self, props: dict) -> int:
        """Count associated records from properties."""
        count = 0
        for key in ["num_associated_contacts", "num_associated_deals"]:
            try:
                count += int(props.get(key, 0) or 0)
            except (ValueError, TypeError):
                pass
        return count

    async def get_total_companies(self) -> int:
        """Total company count (for progress). The v3 LIST endpoint doesn't return a
        total, so use the SEARCH endpoint (empty filter = all records)."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/crm/v3/objects/companies/search",
                json={"limit": 1},
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code != 200:
                return 0

            return response.json().get("total", 0)
