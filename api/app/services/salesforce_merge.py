"""Salesforce merge operations service."""
from __future__ import annotations
import asyncio
import xml.etree.ElementTree as ET
from typing import Optional
from xml.sax.saxutils import escape

import httpx

from app.services.salesforce import SalesforceConnection


class SalesforceMergeService:
    """
    Service for merging contacts in Salesforce.

    Salesforce uses the Merge API endpoint:
    POST /services/data/vXX.0/sobjects/Contact/merge/

    Note: Salesforce requires specific permissions for merge operations.
    """

    RATE_LIMIT_DELAY = 0.05  # Salesforce allows more requests
    SOAP_API_VERSION = "59.0"  # matches the REST v59.0 used elsewhere

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
        Merge contacts using Salesforce's SOAP merge() call.

        Salesforce has NO REST endpoint for record merge — POSTing to
        /sobjects/Contact/merge is parsed as an upsert-by-external-id and 404s
        ("Provided external ID field does not exist or is not accessible: merge").
        Record merge is the SOAP API merge() operation (Partner WSDL). The OAuth
        access token doubles as the SOAP sessionId.

        The master record is kept, and merge records are deleted; related records
        are re-parented to the master. Salesforce merges at most 2 records per
        call, so N losers take ceil(N/2) sequential calls.

        IMPORTANT (partial-merge correctness): each successful call IRREVERSIBLY
        deletes its loser records. If a later batch fails, the earlier batches'
        deletions have already happened. We report exactly which loser ids were
        absorbed and stop on the first failure, so a partial success is never
        reported as a total failure and an already-deleted id is never re-attempted.

        Args:
            master_id: Salesforce ID of the contact to keep (master)
            merge_ids: List of Salesforce IDs to merge into master

        Returns:
            {success: bool, absorbed_ids: list[str], errors: list[str]}
        """
        absorbed: list[str] = []
        errors: list[str] = []

        endpoint = f"{self.instance_url}/services/Soap/u/{self.SOAP_API_VERSION}"
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "merge"}

        async with httpx.AsyncClient() as client:
            for i in range(0, len(merge_ids), 2):
                batch = merge_ids[i:i + 2]  # Salesforce allows max 2 per call
                envelope = self._merge_envelope(master_id, batch)
                response = await client.post(
                    endpoint, headers=headers, content=envelope.encode("utf-8")
                )
                ok, detail = self._parse_merge_response(response)
                if not ok:
                    errors.append(
                        f"Salesforce merge failed for {batch}: "
                        f"{response.status_code} - {detail}"
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

    def _merge_envelope(self, master_id: str, loser_ids: list[str]) -> str:
        """Build a SOAP Partner-API merge() envelope. The blended winner fields are
        already applied via update_contact (REST PATCH), so masterRecord carries
        only type + Id here."""
        losers = "".join(
            f"<urn:recordToMergeIds>{escape(lid)}</urn:recordToMergeIds>"
            for lid in loser_ids
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:urn="urn:partner.soap.sforce.com"'
            ' xmlns:urn1="urn:sobject.partner.soap.sforce.com">'
            "<soapenv:Header>"
            "<urn:SessionHeader>"
            f"<urn:sessionId>{escape(self.access_token)}</urn:sessionId>"
            "</urn:SessionHeader>"
            "</soapenv:Header>"
            "<soapenv:Body>"
            "<urn:merge><urn:request>"
            "<urn:masterRecord>"
            "<urn1:type>Contact</urn1:type>"
            f"<urn1:Id>{escape(master_id)}</urn1:Id>"
            "</urn:masterRecord>"
            f"{losers}"
            "</urn:request></urn:merge>"
            "</soapenv:Body></soapenv:Envelope>"
        )

    @staticmethod
    def _parse_merge_response(response: httpx.Response) -> tuple[bool, str]:
        """Parse a SOAP merge() response -> (success, detail). On a 200 the body
        carries <result><success>true|false</success>...; a SOAP fault (HTTP 500)
        carries <faultstring>. Returns the error text on failure."""
        try:
            root = ET.fromstring(response.content)
        except Exception:
            return False, (response.text or "")[:300]

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        fault: Optional[str] = None
        success: Optional[bool] = None
        messages: list[str] = []
        for el in root.iter():
            ln = local(el.tag)
            if ln == "faultstring" and el.text:
                fault = el.text.strip()
            elif ln == "success" and el.text is not None:
                success = el.text.strip().lower() == "true"
            elif ln in ("message", "statusCode") and el.text:
                messages.append(el.text.strip())

        if success is True:
            return True, ""
        if fault:
            return False, fault
        if messages:
            return False, "; ".join(messages)
        return False, (response.text or "")[:300]

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
        # Map the blended FieldBlender keys (snake_case: first_name/last_name/
        # job_title/email/phone) to Salesforce API field names. Only WRITABLE
        # identity fields are mapped; anything NOT in this map is skipped, so we
        # never send Salesforce an unknown column (INVALID_FIELD). That deliberately
        # drops: company (-> AccountId needs a real Id, not a name) and the metadata
        # fields the blender includes (created_at / updated_at / association_count).
        field_mapping = {
            "email": "Email",
            "first_name": "FirstName",
            "last_name": "LastName",
            "phone": "Phone",
            "job_title": "Title",
        }

        # Transform properties to Salesforce field names, dropping unmapped keys.
        sf_properties = {}
        for key, value in properties.items():
            sf_key = field_mapping.get(key.lower())
            if sf_key and value:
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
