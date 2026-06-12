"""Salesforce API service for OAuth and CRM operations."""
import httpx
from datetime import datetime, timedelta, timezone
from typing import Optional
from pydantic import BaseModel

from app.config import get_settings
from app.services.encryption import encrypt_token, decrypt_token
from app.services.supabase_client import get_supabase
from app.services.tenancy import resolve_tenant_for_save


class SalesforceTokens(BaseModel):
    """Salesforce OAuth tokens."""
    access_token: str
    refresh_token: str
    instance_url: str  # Salesforce instance URL (e.g., https://na1.salesforce.com)
    issued_at: int  # Timestamp


class SalesforceConnection(BaseModel):
    """Stored Salesforce connection."""
    id: str
    user_id: str
    org_id: str
    instance_url: str
    access_token: str  # Decrypted
    refresh_token: str  # Decrypted


class SalesforceService:
    """Service for Salesforce OAuth and API operations."""

    AUTH_URL = "https://login.salesforce.com/services/oauth2"

    def __init__(self):
        self.settings = get_settings()
        self.supabase = get_supabase()

    async def exchange_code_for_tokens(
        self,
        code: str,
        redirect_uri: str
    ) -> SalesforceTokens:
        """Exchange OAuth authorization code for access tokens."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.AUTH_URL}/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.settings.salesforce_client_id,
                    "client_secret": self.settings.salesforce_client_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                # Never surface the raw provider body to the caller (it can carry
                # OAuth diagnostics and is attacker-influenced). Log it server-side;
                # raise a generic message that is safe to return / persist.
                print(f"[salesforce] token exchange failed ({response.status_code}): {response.text}")
                raise Exception("Salesforce token exchange failed.")

            data = response.json()
            return SalesforceTokens(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                instance_url=data["instance_url"],
                issued_at=int(data["issued_at"]),
            )

    async def refresh_tokens(self, refresh_token: str) -> SalesforceTokens:
        """Refresh expired access token."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.AUTH_URL}/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.settings.salesforce_client_id,
                    "client_secret": self.settings.salesforce_client_secret,
                    "refresh_token": refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                # See note in exchange_code_for_tokens: log server-side, raise generic.
                print(f"[salesforce] token refresh failed ({response.status_code}): {response.text}")
                raise Exception("Salesforce token refresh failed.")

            data = response.json()
            return SalesforceTokens(
                access_token=data["access_token"],
                refresh_token=refresh_token,  # Salesforce doesn't always return new refresh token
                instance_url=data["instance_url"],
                issued_at=int(data.get("issued_at", datetime.now(timezone.utc).timestamp() * 1000)),
            )

    async def get_org_id(self, access_token: str, instance_url: str) -> str:
        """Get the Salesforce org ID for the authenticated account."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{instance_url}/services/data/v59.0/query",
                params={"q": "SELECT Id FROM Organization LIMIT 1"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code != 200:
                # Log the raw provider body server-side; don't return it to the caller.
                print(f"[salesforce] get_org_id failed ({response.status_code}): {response.text}")
                raise Exception("Failed to get Salesforce org info.")

            data = response.json()
            records = data.get("records", [])
            if records:
                return records[0]["Id"]
            raise Exception("Could not determine Org ID")

    async def save_connection(
        self,
        user_id: str,
        tokens: SalesforceTokens,
        org_id: str
    ) -> dict:
        """Save or update Salesforce connection for a user."""
        # Salesforce tokens don't have explicit expiry, but we refresh proactively
        expires_at = datetime.now(timezone.utc) + timedelta(hours=2)

        # Encrypt tokens before storing
        encrypted_access = encrypt_token(tokens.access_token)
        encrypted_refresh = encrypt_token(tokens.refresh_token)

        # Resolve the tenant BEFORE the upsert (crm_connections.tenant_id is NOT
        # NULL). tenant = connected org: reuse this user's existing salesforce tenant
        # or create a fresh one (client access off) with the user as owner.
        tenant_id = resolve_tenant_for_save(self.supabase, user_id, "salesforce", org_id)

        # Store instance URL in portal_id field for retrieval
        result = self.supabase.table("crm_connections").upsert(
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "crm_type": "salesforce",
                "access_token_encrypted": encrypted_access,
                "refresh_token_encrypted": encrypted_refresh,
                "portal_id": f"{org_id}|{tokens.instance_url}",  # Store both
                "expires_at": expires_at.isoformat(),
            },
            on_conflict="user_id,crm_type",
        ).execute()

        return result.data[0] if result.data else None

    async def get_connection(self, user_id: str) -> Optional[SalesforceConnection]:
        """Get Salesforce connection for a user, refreshing if needed."""
        result = self.supabase.table("crm_connections").select("*").eq(
            "user_id", user_id
        ).eq("crm_type", "salesforce").single().execute()

        if not result.data:
            return None

        conn = result.data

        # Parse org_id and instance_url
        portal_data = conn["portal_id"] or ""
        if "|" in portal_data:
            org_id, instance_url = portal_data.split("|", 1)
        else:
            org_id = portal_data
            instance_url = "https://login.salesforce.com"

        # Check if token might need refresh (1 hour buffer)
        expires_at = datetime.fromisoformat(conn["expires_at"].replace("Z", "+00:00"))
        if expires_at < datetime.now(timezone.utc) + timedelta(hours=1):
            # Decrypt and refresh
            refresh_token = decrypt_token(conn["refresh_token_encrypted"])
            try:
                new_tokens = await self.refresh_tokens(refresh_token)
                await self.save_connection(user_id, new_tokens, org_id)

                return SalesforceConnection(
                    id=conn["id"],
                    user_id=conn["user_id"],
                    org_id=org_id,
                    instance_url=new_tokens.instance_url,
                    access_token=new_tokens.access_token,
                    refresh_token=new_tokens.refresh_token,
                )
            except Exception:
                # If refresh fails, return existing (might still work)
                pass

        # Decrypt and return existing
        return SalesforceConnection(
            id=conn["id"],
            user_id=conn["user_id"],
            org_id=org_id,
            instance_url=instance_url,
            access_token=decrypt_token(conn["access_token_encrypted"]),
            refresh_token=decrypt_token(conn["refresh_token_encrypted"]),
        )

    async def delete_connection(self, user_id: str) -> bool:
        """Delete Salesforce connection for a user."""
        result = self.supabase.table("crm_connections").delete().eq(
            "user_id", user_id
        ).eq("crm_type", "salesforce").execute()

        return len(result.data) > 0 if result.data else False
