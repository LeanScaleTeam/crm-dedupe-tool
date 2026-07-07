"""Request authentication — verify the Supabase access-token JWT.

Phase 0 (safety): the backend previously trusted a `user_id` sent in the request
body and used the service-role key (which bypasses RLS), so any caller could act
as any user — including triggering a production merge. This module replaces that
with real authentication: every protected endpoint derives the user id from a
verified Supabase JWT, never from the request body.

Supabase projects issue access tokens with either:
  * asymmetric JWT signing keys (ES256/RS256) — verified against the project's
    published JWKS at `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`; or
  * the legacy HS256 shared secret (SUPABASE_JWT_SECRET) — used by older projects
    and by the integration test harness, which mints its own HS256 tokens.
We pick the path from the token header's `alg`, then verify signature + expiry +
the `authenticated` audience and return the `sub` claim (the auth.users UUID).

NOTE: the DB client still uses the service-role key (RLS bypassed), so verifying
the token is necessary but NOT sufficient — protected endpoints must ALSO check
tenant access (scan/connection belongs to a tenant the caller can access)
explicitly. See merge.py / scan.py `_assert_scan_access`.
"""
from __future__ import annotations

from functools import lru_cache

import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_bearer = HTTPBearer(auto_error=True)

# Asymmetric algorithms Supabase may use for JWT signing keys.
_ASYMMETRIC_ALGS = {"ES256", "ES384", "ES512", "RS256", "RS384", "RS512"}


@lru_cache(maxsize=4)
def _jwk_client(jwks_url: str) -> PyJWKClient:
    """Cached JWKS client (it also caches fetched signing keys internally)."""
    return PyJWKClient(jwks_url)


def verify_supabase_jwt(token: str) -> str:
    """Verify a Supabase access token and return its user id (`sub`).

    Raises HTTPException(401) on any verification failure.
    """
    settings = get_settings()

    try:
        alg = jwt.get_unverified_header(token).get("alg", "HS256")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    decode_kwargs = dict(
        audience="authenticated",
        options={"require": ["exp", "sub"]},
    )

    try:
        if alg in _ASYMMETRIC_ALGS:
            # New Supabase JWT signing keys — verify against the project JWKS.
            jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
            signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token)
            payload = jwt.decode(token, signing_key.key, algorithms=[alg], **decode_kwargs)
        else:
            # Legacy HS256 shared-secret tokens (and the integration harness).
            secret = settings.supabase_jwt_secret
            if not secret:
                # Fail closed: no secret means we cannot verify HS256 tokens.
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Auth not configured (SUPABASE_JWT_SECRET missing).",
                )
            payload = jwt.decode(token, secret, algorithms=["HS256"], **decode_kwargs)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired.")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    except HTTPException:
        raise
    except Exception as e:  # JWKS fetch / key-resolution failures, etc.
        raise HTTPException(status_code=401, detail=f"Token verification failed: {e}")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject.")
    return sub


def require_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """FastAPI dependency → the authenticated user id. Use as:

        async def endpoint(..., user_id: str = Depends(require_user)):
    """
    return verify_supabase_jwt(creds.credentials)
