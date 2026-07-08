"""Salesforce Account merge operations service.

Mirrors SalesforceMergeService (contacts) but targets the Account sObject via the
SOAP merge() call. The master account is kept; merge accounts are deleted and
their child records (contacts, opportunities, cases, …) are re-parented to the
master. Salesforce merges at most 2 records per call, so N losers take ceil(N/2)
sequential calls. Same public contract as the other merge services so the
CRM-agnostic run_merge drives it unchanged.
"""
from __future__ import annotations
import asyncio
import xml.etree.ElementTree as ET
from typing import Optional
from xml.sax.saxutils import escape

import httpx

from app.services.salesforce import SalesforceConnection


class SalesforceAccountMergeService:
    """Service for merging Account records in Salesforce (SOAP merge())."""

    RATE_LIMIT_DELAY = 0.05
    SOAP_API_VERSION = "59.0"

    def __init__(self, connection: SalesforceConnection):
        self.connection = connection
        self.access_token = connection.access_token
        self.instance_url = connection.instance_url

    async def merge_accounts(self, master_id: str, merge_ids: list[str]) -> dict:
        """Merge accounts into master via SOAP merge(), max 2 per call.

        Each successful call IRREVERSIBLY deletes its loser records; on a later
        failure the earlier deletions have already happened. We report exactly which
        loser ids were absorbed and stop on the first failure, so a partial success
        is never reported as a total failure and a deleted id is never re-attempted.
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
                        f"Salesforce account merge failed for {batch}: "
                        f"{response.status_code} - {detail}"
                    )
                    break  # Stop on first failure — do not keep deleting after an error.

                absorbed.extend(batch)
                await asyncio.sleep(self.RATE_LIMIT_DELAY)

        return {"success": len(errors) == 0, "absorbed_ids": absorbed, "errors": errors}

    def _merge_envelope(self, master_id: str, loser_ids: list[str]) -> str:
        """SOAP Partner-API merge() envelope for Accounts. Blended winner fields are
        already applied via update_account (REST PATCH), so masterRecord carries only
        type + Id here."""
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
            "<urn1:type>Account</urn1:type>"
            f"<urn1:Id>{escape(master_id)}</urn1:Id>"
            "</urn:masterRecord>"
            f"{losers}"
            "</urn:request></urn:merge>"
            "</soapenv:Body></soapenv:Envelope>"
        )

    @staticmethod
    def _parse_merge_response(response: httpx.Response) -> tuple[bool, str]:
        """Parse a SOAP merge() response -> (success, detail)."""
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

    async def update_account(self, account_id: str, properties: dict) -> dict:
        """Update an account's fields (applies blended winner values before merge).

        Maps the blended Company-model keys to Salesforce Account field names. Only
        writable standard fields are mapped; anything else (incl. domain, which has
        no Account field, and the blender's metadata keys) is dropped so we never
        send an unknown column (INVALID_FIELD).
        """
        field_mapping = {
            "name": "Name",
            "website": "Website",
            "phone": "Phone",
            "industry": "Industry",
            "country": "BillingCountry",
        }

        sf_properties = {}
        for key, value in properties.items():
            sf_key = field_mapping.get(key.lower())
            if sf_key and value:
                sf_properties[sf_key] = value

        if not sf_properties:
            return {"success": True}

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{self.instance_url}/services/data/v59.0/sobjects/Account/{account_id}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json=sf_properties,
            )

            if response.status_code in [200, 204]:
                return {"success": True}
            return {
                "success": False,
                "error": f"Salesforce account update failed: {response.status_code} - {response.text}",
            }

    async def merge_duplicate_set(
        self,
        winner_id: str,
        loser_ids: list[str],
        blended_properties: Optional[dict] = None,
    ) -> dict:
        """Merge a complete duplicate set of accounts.

        Returns {success, merged_loser_ids, merged_count, errors} — same contract as
        the other merge services so run_merge records partial progress correctly.
        """
        errors: list[str] = []

        # Step 1: apply blended winner fields. If this fails, do NOT merge (losers
        # would be deleted before their gap-fill lands on the master).
        if blended_properties:
            update_result = await self.update_account(winner_id, blended_properties)
            if not update_result["success"]:
                return {
                    "success": False,
                    "merged_loser_ids": [],
                    "merged_count": 0,
                    "errors": [f"Failed to update winner: {update_result['error']}"],
                }

        # Step 2: merge losers. absorbed_ids is the ground truth of what SF deleted.
        merge_result = await self.merge_accounts(winner_id, loser_ids)
        errors.extend(merge_result.get("errors", []))
        absorbed = merge_result.get("absorbed_ids", [])

        return {
            "success": len(errors) == 0,
            "merged_loser_ids": absorbed,
            "merged_count": len(absorbed),
            "errors": errors,
        }
