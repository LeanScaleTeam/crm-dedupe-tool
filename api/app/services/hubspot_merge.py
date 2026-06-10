"""HubSpot merge operations service."""
from __future__ import annotations
import httpx
import asyncio
from typing import Optional
from datetime import datetime

from app.services.hubspot import HubSpotConnection


class HubSpotMergeService:
    """
    Service for merging contacts in HubSpot.

    HubSpot provides two approaches:
    1. Native merge endpoint: POST /crm/v3/objects/contacts/merge
       - Automatically re-parents associations
       - Less control over field blending
    2. Manual update + archive:
       - Update winner with blended fields
       - Archive losers
       - More control but more API calls

    We use the native merge endpoint for simplicity and reliability.
    """

    BASE_URL = "https://api.hubapi.com"
    RATE_LIMIT_DELAY = 0.1  # 10 requests per second

    def __init__(self, connection: HubSpotConnection):
        self.connection = connection
        self.access_token = connection.access_token

    async def merge_contacts(
        self,
        winner_id: str,
        loser_id: str,
    ) -> dict:
        """
        Merge two contacts using HubSpot's native merge endpoint.

        The loser contact's associations will be transferred to the winner.
        The loser contact will be archived (soft deleted).

        Args:
            winner_id: HubSpot ID of the contact to keep
            loser_id: HubSpot ID of the contact to merge into winner

        Returns:
            Dict with merge result
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/crm/v3/objects/contacts/merge",
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
                    "error": f"HubSpot merge failed: {response.status_code} - {response.text}",
                }

    # HubSpot read-only properties that cannot be set via the API
    READ_ONLY_PROPERTIES = {
        "hs_object_id", "createdate", "lastmodifieddate", "hs_lastmodifieddate",
        "hs_createdate", "hs_is_contact", "hs_pipeline", "hs_lifecyclestage_lead_date",
        "hs_lifecyclestage_marketingqualifiedlead_date",
        "hs_lifecyclestage_salesqualifiedlead_date",
        "hs_lifecyclestage_opportunity_date", "hs_lifecyclestage_customer_date",
        "hs_lifecyclestage_evangelist_date", "hs_lifecyclestage_subscriber_date",
        "hs_lifecyclestage_other_date", "num_associated_deals", "num_contacted_times",
        "hs_analytics_source", "hs_analytics_source_data_1", "hs_analytics_source_data_2",
        "hs_analytics_num_page_views", "hs_analytics_num_visits",
        "hs_analytics_num_event_completions", "hs_analytics_first_url",
        "hs_analytics_last_url", "hs_analytics_average_page_views",
        "hs_analytics_first_timestamp", "hs_analytics_last_timestamp",
        "hs_analytics_first_visit_timestamp", "hs_analytics_last_visit_timestamp",
        "hs_email_optout", "hs_all_contact_vids", "hs_merged_object_ids",
        "hs_calculated_merged_vids", "hs_is_unworked",
    }

    # Map Contact model field names to HubSpot property names
    FIELD_TO_HUBSPOT = {
        "email": "email",
        "first_name": "firstname",
        "last_name": "lastname",
        "phone": "phone",
        "company": "company",
        "job_title": "jobtitle",
    }

    async def update_contact(
        self,
        contact_id: str,
        properties: dict,
    ) -> dict:
        """
        Update a contact's properties.

        Used to apply blended field values to the winner.
        Filters out read-only properties and maps Contact model
        field names to HubSpot property names.
        """
        # Map field names and filter out read-only / metadata fields
        hs_properties = {}
        for key, value in properties.items():
            # Map Contact model field name -> HubSpot property name
            hs_key = self.FIELD_TO_HUBSPOT.get(key, key)
            # Skip read-only, metadata, and empty values
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
                f"{self.BASE_URL}/crm/v3/objects/contacts/{contact_id}",
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
                    "error": f"HubSpot update failed: {response.status_code} - {response.text}",
                }

    async def merge_duplicate_set(
        self,
        winner_id: str,
        loser_ids: list[str],
        blended_properties: Optional[dict] = None,
    ) -> dict:
        """
        Merge a complete duplicate set.

        Steps:
        1. Update winner with blended properties (if provided)
        2. Merge each loser into winner

        Args:
            winner_id: HubSpot ID of the winner contact
            loser_ids: List of HubSpot IDs to merge into winner
            blended_properties: Optional dict of properties to update on winner

        Returns:
            {success, merged_loser_ids, merged_count, errors} — same contract as
            SalesforceMergeService so the CRM-agnostic run_merge can record real
            partial-merge progress and never re-attempt an already-archived loser
            on resume. (HubSpot merges are soft-archives and recoverable, but the
            accounting must still be correct.)
        """
        # Step 1: Update winner with blended fields (fill gaps). If this fails,
        # do NOT merge — losers would be archived before their gap-fill lands.
        if blended_properties:
            update_result = await self.update_contact(winner_id, blended_properties)
            if not update_result["success"]:
                return {
                    "success": False,
                    "merged_loser_ids": [],
                    "merged_count": 0,
                    "errors": [f"Failed to update winner: {update_result['error']}"],
                }

        # Step 2: Merge each loser; record which ones succeeded and STOP on the
        # first failure (mirrors the Salesforce-side invariant).
        absorbed: list[str] = []
        errors: list[str] = []
        for loser_id in loser_ids:
            await asyncio.sleep(self.RATE_LIMIT_DELAY)
            merge_result = await self.merge_contacts(winner_id, loser_id)
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

    async def batch_merge(
        self,
        merge_operations: list[dict],
        progress_callback: Optional[callable] = None,
    ) -> dict:
        """
        Execute multiple merge operations.

        Args:
            merge_operations: List of {winner_id, loser_ids, blended_properties}
            progress_callback: Optional callback(completed, total, errors)

        Returns:
            Dict with batch results
        """
        total = len(merge_operations)
        completed = 0
        failed = 0
        all_errors = []

        for op in merge_operations:
            result = await self.merge_duplicate_set(
                winner_id=op["winner_id"],
                loser_ids=op["loser_ids"],
                blended_properties=op.get("blended_properties"),
            )

            if result["success"]:
                completed += 1
            else:
                failed += 1
                all_errors.extend(result["errors"])

            # Progress callback
            if progress_callback:
                await progress_callback(completed + failed, total, all_errors)

        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "errors": all_errors,
        }
