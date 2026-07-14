"""Salesforce Lead -> existing Contact conversion service (SOAP convertLead()).

This is the "convert a lead into a MATCHED existing contact" flow — NOT a merge.
Converting a lead into an existing contact via the API is the operation people
report failing, because of one non-obvious Salesforce rule:

    When you pass an existing `contactId`, you MUST also pass `accountId`, and the
    contact must already belong to that account.

So the fix is: look up the target contact's AccountId and pass BOTH contactId and
accountId. That is exactly what run_lead_conversion_scan captures into
merged_preview.account_id, and what this service sends. Only EMPTY fields on the
target contact are overwritten by the lead, so existing contact data is preserved.

Conversion is IRREVERSIBLE (the lead becomes IsConverted=true and read-only).

To slot into the existing merge executor (run_merge) unchanged, this exposes the
same merge_duplicate_set(winner_id, loser_ids, blended_properties) contract:
  - winner_id     = the surviving existing Contact's Id (convert target)
  - loser_ids     = the Lead Id(s) to convert into that contact
  - blended_properties (= the set's merged_preview) carries:
      account_id       -> the target contact's AccountId (REQUIRED)
      converted_status -> a LeadStatus MasterLabel with IsConverted=true
"""
from __future__ import annotations
import asyncio
import xml.etree.ElementTree as ET
from typing import Optional
from xml.sax.saxutils import escape

import httpx

from app.services.salesforce import SalesforceConnection


class SalesforceLeadConvertService:
    """Converts Leads into existing Contacts via the SOAP convertLead() call."""

    RATE_LIMIT_DELAY = 0.05
    SOAP_API_VERSION = "59.0"

    def __init__(self, connection: SalesforceConnection):
        self.connection = connection
        self.access_token = connection.access_token
        self.instance_url = connection.instance_url
        self._converted_status_cache: Optional[str] = None

    async def get_converted_status(self) -> Optional[str]:
        """A LeadStatus MasterLabel whose IsConverted=true (required by convertLead).
        Cached after the first lookup. Returns None if the org has none configured."""
        if self._converted_status_cache:
            return self._converted_status_cache
        query = "SELECT MasterLabel FROM LeadStatus WHERE IsConverted = true ORDER BY SortOrder LIMIT 1"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.instance_url}/services/data/v59.0/query",
                params={"q": query},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                records = resp.json().get("records", [])
                if records:
                    self._converted_status_cache = records[0].get("MasterLabel")
        return self._converted_status_cache

    async def get_contact_account_id(self, contact_id: str) -> Optional[str]:
        """The target contact's AccountId (required to convert a lead INTO it).
        Used as a robust fallback when the scan-time value isn't on the set."""
        query = f"SELECT AccountId FROM Contact WHERE Id = '{escape(contact_id)}' LIMIT 1"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.instance_url}/services/data/v59.0/query",
                params={"q": query},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                records = resp.json().get("records", [])
                if records:
                    return records[0].get("AccountId")
        return None

    async def convert_lead(
        self,
        lead_id: str,
        contact_id: str,
        account_id: str,
        converted_status: str,
    ) -> tuple[bool, str]:
        """Convert ONE lead into the given existing contact/account.

        Passes contactId + accountId (the rule that makes convert-into-existing-contact
        work) and doNotCreateOpportunity=true (dedupe use case — no new pipeline).
        Returns (success, detail)."""
        endpoint = f"{self.instance_url}/services/Soap/u/{self.SOAP_API_VERSION}"
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "convertLead"}
        envelope = self._convert_envelope(lead_id, contact_id, account_id, converted_status)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                endpoint, headers=headers, content=envelope.encode("utf-8")
            )
        return self._parse_convert_response(response)

    def _convert_envelope(
        self, lead_id: str, contact_id: str, account_id: str, converted_status: str
    ) -> str:
        """SOAP Partner-API convertLead() envelope for ONE LeadConvert.

        contactId + accountId target the existing records; only empty contact fields
        are overwritten. overwriteLeadSource=false and sendNotificationEmail=false
        keep the conversion quiet and non-destructive to the contact's LeadSource.
        """
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:urn="urn:partner.soap.sforce.com">'
            "<soapenv:Header>"
            "<urn:SessionHeader>"
            f"<urn:sessionId>{escape(self.access_token)}</urn:sessionId>"
            "</urn:SessionHeader>"
            "</soapenv:Header>"
            "<soapenv:Body>"
            "<urn:convertLead>"
            "<urn:leadConverts>"
            f"<urn:leadId>{escape(lead_id)}</urn:leadId>"
            f"<urn:contactId>{escape(contact_id)}</urn:contactId>"
            f"<urn:accountId>{escape(account_id)}</urn:accountId>"
            f"<urn:convertedStatus>{escape(converted_status)}</urn:convertedStatus>"
            "<urn:doNotCreateOpportunity>true</urn:doNotCreateOpportunity>"
            "<urn:overwriteLeadSource>false</urn:overwriteLeadSource>"
            "<urn:sendNotificationEmail>false</urn:sendNotificationEmail>"
            "</urn:leadConverts>"
            "</urn:convertLead>"
            "</soapenv:Body></soapenv:Envelope>"
        )

    @staticmethod
    def _parse_convert_response(response: httpx.Response) -> tuple[bool, str]:
        """Parse a SOAP convertLead() response -> (success, detail). The result carries
        <success>true|false</success>; failures carry <errors><statusCode>/<message>
        or a top-level <faultstring>."""
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

    async def merge_duplicate_set(
        self,
        winner_id: str,
        loser_ids: list[str],
        blended_properties: Optional[dict] = None,
    ) -> dict:
        """Convert lead(s) into the surviving existing contact.

        Shares the merge-service contract so run_merge drives it unchanged:
        winner_id = target Contact, loser_ids = Lead(s), blended_properties carries
        account_id (the target contact's AccountId) + converted_status.

        Returns {success, merged_loser_ids, merged_count, errors}. merged_loser_ids
        are the leads actually converted (drawn from loser_ids), so the executor's
        "fully merged" accounting stays correct.
        """
        props = blended_properties or {}
        account_id = props.get("account_id") or await self.get_contact_account_id(winner_id)
        converted_status = props.get("converted_status") or await self.get_converted_status()

        if not converted_status:
            return {
                "success": False, "merged_loser_ids": [], "merged_count": 0,
                "errors": ["No converted Lead Status is configured in this org "
                           "(need a LeadStatus with IsConverted = true)."],
            }
        # Preflight the non-obvious rule: converting a lead INTO an existing contact
        # requires the contact's AccountId. Fail with a clear, actionable message
        # rather than the cryptic Salesforce error.
        if not account_id:
            return {
                "success": False, "merged_loser_ids": [], "merged_count": 0,
                "errors": ["Target contact is not linked to an Account. Salesforce "
                           "cannot convert a lead into a contact that has no Account — "
                           "attach the contact to an Account first, then re-scan."],
            }

        converted: list[str] = []
        errors: list[str] = []
        for lead_id in loser_ids:
            ok, detail = await self.convert_lead(
                lead_id, winner_id, account_id, converted_status
            )
            if ok:
                converted.append(lead_id)
            else:
                errors.append(f"Lead convert failed for {lead_id}: {detail}")
                # Independent per-lead; keep going so one bad lead doesn't block others.
            await asyncio.sleep(self.RATE_LIMIT_DELAY)

        return {
            "success": len(errors) == 0,
            "merged_loser_ids": converted,
            "merged_count": len(converted),
            "errors": errors,
        }

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
