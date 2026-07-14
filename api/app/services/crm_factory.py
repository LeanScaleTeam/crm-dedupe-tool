"""Factory for creating CRM-specific services."""
from typing import Tuple, Any

from app.services.supabase_client import get_supabase


async def get_crm_services(
    user_id: str, connection_id: str, object_type: str = "contacts"
) -> Tuple[Any, Any, Any]:
    """
    Get the appropriate CRM services based on connection type.

    `user_id` is the acting caller (kept for signature stability / logging). Token
    resolution is keyed off the connection's OWNER, not the caller, so a
    platform-staff operator can act on a tenant's connection they don't personally
    own. Tenant access is enforced by the routers before this is reached.

    Returns:
        Tuple of (connection, contacts_service, merge_service)
    """
    supabase = get_supabase()

    # Get connection to determine CRM type and its owner.
    conn_result = supabase.table("crm_connections").select("*").eq(
        "id", connection_id
    ).single().execute()

    if not conn_result.data:
        raise Exception("Connection not found")

    crm_type = conn_result.data["crm_type"]
    owner_id = conn_result.data["user_id"]  # whose stored tokens back this connection

    if crm_type == "hubspot":
        from app.services.hubspot import HubSpotService

        service = HubSpotService()
        # Multi-org: resolve the SPECIFIC connection (by id), not the owner's single
        # HubSpot connection, so scans/merges use the right portal's token.
        connection = await service.get_connection_by_id(connection_id)
        if not connection:
            raise Exception("HubSpot connection not found or expired")

        # object_type selects the fetch + merge services. A companies scan must
        # NEVER fall through to the contacts merge endpoint (that would merge the
        # wrong records) — unsupported types raise instead of defaulting to contacts.
        if object_type == "companies":
            from app.services.hubspot_companies import HubSpotCompaniesService
            from app.services.hubspot_company_merge import HubSpotCompanyMergeService

            return (
                connection,
                HubSpotCompaniesService(connection),
                HubSpotCompanyMergeService(connection),
            )

        if object_type != "contacts":
            raise Exception(f"HubSpot object type '{object_type}' is not supported yet.")

        from app.services.hubspot_contacts import HubSpotContactsService
        from app.services.hubspot_merge import HubSpotMergeService

        return (
            connection,
            HubSpotContactsService(connection),
            HubSpotMergeService(connection),
        )

    elif crm_type == "salesforce":
        from app.services.salesforce import SalesforceService

        service = SalesforceService()
        # Multi-org: resolve the SPECIFIC connection (by id), not the owner's single
        # SF connection, so scans/merges use the right org's token.
        connection = await service.get_connection_by_id(connection_id)
        if not connection:
            raise Exception("Salesforce connection not found or expired")

        # Accounts get the Account fetch + Account SOAP merge service — the merge
        # targets the Account sObject, NEVER Contact. (The config-driven dry-run
        # path fetches accounts directly and never reaches a merge service.)
        if object_type == "accounts":
            from app.services.salesforce_accounts import SalesforceAccountsService
            from app.services.salesforce_account_merge import SalesforceAccountMergeService

            return (
                connection,
                SalesforceAccountsService(connection),
                SalesforceAccountMergeService(connection),
            )

        # Leads dedupe: person-shaped, so the fetch service maps leads onto the
        # Contact model and the merge service targets the Lead sObject (SOAP merge()
        # supports Lead) — NEVER the Contact merge.
        if object_type == "leads":
            from app.services.salesforce_leads import SalesforceLeadsService
            from app.services.salesforce_lead_merge import SalesforceLeadMergeService

            return (
                connection,
                SalesforceLeadsService(connection),
                SalesforceLeadMergeService(connection),
            )

        # Lead -> existing Contact conversion (cross-object). The "merge service" here
        # is the convert service, which shares the merge_duplicate_set contract so
        # run_merge drives it unchanged (winner=Contact, loser=Lead, convertLead()).
        if object_type == "lead_conversion":
            from app.services.salesforce_leads import SalesforceLeadsService
            from app.services.salesforce_lead_convert import SalesforceLeadConvertService

            return (
                connection,
                SalesforceLeadsService(connection),
                SalesforceLeadConvertService(connection),
            )

        # Contacts are the only other Salesforce path. Anything else must raise,
        # never fall through to the contact merge (wrong-record deletion).
        if object_type != "contacts":
            raise Exception(f"Salesforce object type '{object_type}' is not supported yet.")

        from app.services.salesforce_contacts import SalesforceContactsService
        from app.services.salesforce_merge import SalesforceMergeService

        return (
            connection,
            SalesforceContactsService(connection),
            SalesforceMergeService(connection),
        )

    else:
        raise Exception(f"Unsupported CRM type: {crm_type}")
