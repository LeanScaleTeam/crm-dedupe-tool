"""Factory for creating CRM-specific services."""
from typing import Tuple, Any

from app.services.supabase_client import get_supabase


async def get_crm_services(user_id: str, connection_id: str) -> Tuple[Any, Any, Any]:
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
        from app.services.hubspot_contacts import HubSpotContactsService
        from app.services.hubspot_merge import HubSpotMergeService

        service = HubSpotService()
        connection = await service.get_connection(owner_id)
        if not connection:
            raise Exception("HubSpot connection not found or expired")

        contacts_service = HubSpotContactsService(connection)
        merge_service = HubSpotMergeService(connection)

        return connection, contacts_service, merge_service

    elif crm_type == "salesforce":
        from app.services.salesforce import SalesforceService
        from app.services.salesforce_contacts import SalesforceContactsService
        from app.services.salesforce_merge import SalesforceMergeService

        service = SalesforceService()
        connection = await service.get_connection(owner_id)
        if not connection:
            raise Exception("Salesforce connection not found or expired")

        contacts_service = SalesforceContactsService(connection)
        merge_service = SalesforceMergeService(connection)

        return connection, contacts_service, merge_service

    else:
        raise Exception(f"Unsupported CRM type: {crm_type}")
