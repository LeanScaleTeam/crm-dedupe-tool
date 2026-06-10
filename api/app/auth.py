"""Request authentication — verify the Supabase access-token JWT.

Phase 0 (safety): the backend previously trusted a `user_id` sent in the request
body and used the service-role key (which bypasses RLS), so any caller could act
as any user — including triggering a production merge. This module replaces that
with real authentication: every protected endpoint derives the user id from a
verified Supabase JWT, never from the request body.

Supabase issues HS256 access tokens signed with the project's JWT secret
(Project Settings → API → JWT Settings). We verify the signature + expiry + the
`authenticated` audience and return the `sub` claim (the auth.users UUID).

NOTE: the DB client still uses the service-role key (RLS bypassed), so verifying
the token is necessary but NOT sufficient — protected endpoints must ALSO check
row ownership (scan/connection belongs to this user) explicitly. See merge.py /
scan.py `_assert_scan_owner`.
"""
from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_bearer = HTTPBearer(auto_error=True)


def verify_supabase_jwt(token: str) -> str:
    """Verify a Supabase access token and return its user id (`sub`).

    Raises HTTPException(401) on any failure. Fails closed if the JWT secret is
    not configured (so a misconfigured deploy cannot silently accept anyone).
    """
    settings = get_settings()
    secret = settings.supabase_jwt_secret
    if not secret:
        # Fail closed: no secret means we cannot verify anyone.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth not configured (SUPABASE_JWT_SECRET missing).",
        )
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="authenticated",
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired.")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

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
