"""HubSpot company merge operations service.

Mirrors HubSpotMergeService (contacts) but targets the companies object via
HubSpot's native company merge endpoint. Same public contract as the contacts
merge service so the CRM-agnostic run_merge can drive it unchanged.
"""
from __future__ import annotations
import httpx
import asyncio
from typing import Optional

from app.services.hubspot import HubSpotConnection


class HubSpotCompanyMergeService:
    """
    Service for merging companies in HubSpot.

    Uses the native merge endpoint POST /crm/v3/objects/companies/merge:
    the loser company's associations transfer to the winner and the loser is
    merged away. Like all HubSpot merges, this is permanent and cannot be undone.
    """

    BASE_URL = "https://api.hubapi.com"
    RATE_LIMIT_DELAY = 0.1  # 10 requests per second

    def __init__(self, connection: HubSpotConnection):
        self.connection = connection
        self.access_token = connection.access_token

    async def merge_companies(
        self,
        winner_id: str,
        loser_id: str,
    ) -> dict:
        """
        Merge two companies using HubSpot's native merge endpoint.

        The loser company's associations transfer to the winner; the loser is
        merged into the winner (permanent).

        Args:
            winner_id: HubSpot ID of the company to keep
            loser_id: HubSpot ID of the company to merge into winner

        Returns:
            Dict with merge result
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/crm/v3/objects/companies/merge",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "primaryObjectId": winner_id,
                    "objectIdToMerge": loser_id,
                },
            )

            if response.status_code == 200:
                return {"success": True, "data": response.json()}
            else:
                return {
                    "success": False,
                    "error": f"HubSpot company merge failed: {response.status_code} - {response.text}",
                }

    # HubSpot read-only company properties that cannot be set via the API
    READ_ONLY_PROPERTIES = {
        "hs_object_id", "createdate", "lastmodifieddate", "hs_lastmodifieddate",
        "hs_createdate", "hs_is_company", "hs_merged_object_ids",
        "hs_calculated_merged_vids", "num_associated_contacts",
        "num_associated_deals", "hs_num_child_companies",
        "hs_num_open_deals", "hs_parent_company_id", "hs_all_owner_ids",
        "hs_analytics_num_page_views", "hs_analytics_num_visits",
        "hs_analytics_source", "hs_analytics_source_data_1",
        "hs_analytics_source_data_2", "hs_analytics_first_timestamp",
        "hs_analytics_last_timestamp", "hs_analytics_first_visit_timestamp",
        "hs_analytics_last_visit_timestamp",
    }

    # Map Company model field names to HubSpot company property names
    FIELD_TO_HUBSPOT = {
        "name": "name",
        "domain": "domain",
        "website": "website",
        "phone": "phone",
        "industry": "industry",
        "country": "country",
    }

    async def update_company(
        self,
        company_id: str,
        properties: dict,
    ) -> dict:
        """
        Update a company's properties.

        Used to apply blended field values to the winner. Filters out read-only
        properties and maps Company model field names to HubSpot property names.
        """
        hs_properties = {}
        for key, value in properties.items():
            hs_key = self.FIELD_TO_HUBSPOT.get(key, key)
            if hs_key in self.READ_ONLY_PROPERTIES:
                continue
            if key in ("created_at", "updated_at", "association_count", "id"):
                continue
            if value is not None and value != "":
                hs_properties[hs_key] = value

        if not hs_properties:
            return {"success": True}

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{self.BASE_URL}/crm/v3/objects/companies/{company_id}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={"properties": hs_properties},
            )

            if response.status_code == 200:
                return {"success": True, "data": response.json()}
            else:
                return {
                    "success": False,
                    "error": f"HubSpot company update failed: {response.status_code} - {response.text}",
                }

    async def merge_duplicate_set(
        self,
        winner_id: str,
        loser_ids: list[str],
        blended_properties: Optional[dict] = None,
    ) -> dict:
        """
        Merge a complete duplicate set of companies.

        Steps:
        1. Update winner with blended properties (if provided)
        2. Merge each loser into winner

        Returns:
            {success, merged_loser_ids, merged_count, errors} — same contract as
            HubSpotMergeService/SalesforceMergeService so the CRM-agnostic
            run_merge records real partial-merge progress and never re-attempts an
            already-merged loser on resume.
        """
        # Step 1: Update winner with blended fields (fill gaps). If this fails,
        # do NOT merge — losers would be merged before their gap-fill lands.
        if blended_properties:
            update_result = await self.update_company(winner_id, blended_properties)
            if not update_result["success"]:
                return {
                    "success": False,
                    "merged_loser_ids": [],
                    "merged_count": 0,
                    "errors": [f"Failed to update winner: {update_result['error']}"],
                }

        # Step 2: Merge each loser; record which succeeded and STOP on the first
        # failure (mirrors the contacts-side invariant).
        absorbed: list[str] = []
        errors: list[str] = []
        for loser_id in loser_ids:
            await asyncio.sleep(self.RATE_LIMIT_DELAY)
            merge_result = await self.merge_companies(winner_id, loser_id)
            if not merge_result["success"]:
                errors.append(f"Failed to merge {loser_id}: {merge_result['error']}")
                break
            absorbed.append(loser_id)

        return {
            "success": len(errors) == 0,
            "merged_loser_ids": absorbed,
            "merged_count": len(absorbed),
            "errors": errors,
        }
