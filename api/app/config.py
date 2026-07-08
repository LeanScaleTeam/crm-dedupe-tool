"""Application configuration from environment variables."""
from __future__ import annotations
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    # Supabase
    supabase_url: str
    supabase_service_key: str  # Service role key for backend operations
    # JWT secret for verifying Supabase access tokens (Project Settings → API →
    # JWT Settings). Required to authenticate requests; endpoints fail closed if
    # this is unset. See app/auth.py.
    supabase_jwt_secret: str = ""

    # Redis (for Celery)
    redis_url: str = "redis://localhost:6379/0"

    # HubSpot OAuth
    hubspot_client_id: str
    hubspot_client_secret: str
    hubspot_redirect_uri: str = "http://localhost:3000/api/hubspot/callback"

    # Salesforce OAuth (optional - leave empty if not using Salesforce)
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_redirect_uri: str = "http://localhost:3000/api/salesforce/callback"
    # OAuth login host. Defaults to production; set to https://test.salesforce.com
    # (or a sandbox My Domain URL) to connect a SANDBOX org.
    salesforce_login_url: str = "https://login.salesforce.com"

    # Encryption
    encryption_key: str  # 32-byte key for AES-256

    # App settings
    environment: str = "development"
    cors_origins: list[str] = [
        "http://localhost:3000",
        "https://crm-dedup-tool.netlify.app",
        "https://crmclean.netlify.app",
        "https://dedupe.leanscale.team",
    ]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
