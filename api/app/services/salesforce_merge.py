"""Salesforce merge operations service."""
from __future__ import annotations
import httpx
import asyncio
from typing import Optional

from app.services.salesforce import SalesforceConnection


class SalesforceMergeService:
    """
    Service for merging contacts in Salesforce.

    Salesforce uses the Merge API endpoint:
    POST /services/data/vXX.0/sobjects/Contact/merge/

    Note: Salesforce requires specific permissions for merge operations.
    """

    RATE_LIMIT_DELAY = 0.05  # Salesforce allows more requests

    def __init__(self, connection: SalesforceConnection):
        self.connection = connection
        self.access_token = connection.access_token
        self.instance_url = connection.instance_url

    async def merge_contacts(
        self,
        master_id: str,
        merge_ids: list[str],
    ) -> dict:
        """
        Merge contacts using Salesforce's Merge API.

        The master record is kept, and merge records are deleted. Related records
        are re-parented to the master. Salesforce merges at most 2 records per
        call, so N losers take ceil(N/2) sequential calls.

        IMPORTANT (partial-merge correctness): each successful call IRREVERSIBLY
        deletes its loser records. If a later batch fails, the earlier batches'
        deletions have already happened. We therefore report exactly which loser
        ids were absorbed so the caller can (a) record real progress and
        (b) never re-attempt an already-deleted id on resume. The previous
        implementation recursed and returned only the LAST batch's result, so a
        partial success was silently reported as a total failure.

        Args:
            master_id: Salesforce ID of the contact to keep (master)
            merge_ids: List of Salesforce IDs to merge into master

        Returns:
            {success: bool, absorbed_ids: list[str], errors: list[str]}
        """
        absorbed: list[str] = []
        errors: list[str] = []

        async with httpx.AsyncClient() as client:
            for i in range(0, len(merge_ids), 2):
                batch = merge_ids[i:i + 2]  # Salesforce allows max 2 per call
                response = await client.post(
                    f"{self.instance_url}/services/data/v59.0/sobjects/Contact/merge",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "masterRecord": {"Id": master_id},
                        "recordToMergeIds": batch,
                    },
                )

                if response.status_code not in [200, 201, 204]:
                    errors.append(
                        f"Salesforce merge failed for {batch}: "
                        f"{response.status_code} - {response.text}"
                    )
                    # Stop on first failure — do not keep deleting after an error.
                    break

                absorbed.extend(batch)
                await asyncio.sleep(self.RATE_LIMIT_DELAY)

        return {
            "success": len(errors) == 0,
            "absorbed_ids": absorbed,
            "errors": errors,
        }

    async def update_contact(
        self,
        contact_id: str,
        properties: dict,
    ) -> dict:
        """
        Update a contact's fields.

        Args:
            contact_id: Salesforce ID of the contact
            properties: Dict of field name -> value to update

        Returns:
            Dict with update result
        """
        # Map common field names to Salesforce API names
        field_mapping = {
            "phone": "Phone",
            "email": "Email",
            "firstname": "FirstName",
            "lastname": "LastName",
            "company": "AccountId",  # Note: This needs special handling
            "jobtitle": "Title",
        }

        # Transform properties to Salesforce field names
        sf_properties = {}
        for key, value in properties.items():
            sf_key = field_mapping.get(key.lower(), key)
            if sf_key != "AccountId" and value:  # Skip company for now
                sf_properties[sf_key] = value

        if not sf_properties:
            return {"success": True}  # Nothing to update

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{self.instance_url}/services/data/v59.0/sobjects/Contact/{contact_id}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json=sf_properties,
            )

            if response.status_code in [200, 204]:
                return {"success": True}
            else:
                return {
                    "success": False,
                    "error": f"Salesforce update failed: {response.status_code} - {response.text}",
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
        2. Merge losers into winner

        Args:
            winner_id: Salesforce ID of the winner contact
            loser_ids: List of Salesforce IDs to merge into winner
            blended_properties: Optional dict of properties to update on winner

        Returns:
            Dict with overall merge result
        """
        errors = []

        # Step 1: Update winner with blended fields
        if blended_properties:
            update_result = await self.update_contact(winner_id, blended_properties)
            if not update_result["success"]:
                # Winner update failed — do NOT merge (losers would be deleted
                # before their gap-fill values land on the winner).
                return {
                    "success": False,
                    "merged_loser_ids": [],
                    "merged_count": 0,
                    "errors": [f"Failed to update winner: {update_result['error']}"],
                }

        # Step 2: Merge losers. absorbed_ids is the ground truth of what Salesforce
        # actually deleted — report it whether or not the overall op succeeded.
        merge_result = await self.merge_contacts(winner_id, loser_ids)
        errors.extend(merge_result.get("errors", []))
        absorbed = merge_result.get("absorbed_ids", [])

        return {
            "success": len(errors) == 0,
            "merged_loser_ids": absorbed,
            "merged_count": len(absorbed),
            "errors": errors,
        }
