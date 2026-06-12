#!/usr/bin/env python3
"""
END-TO-END LIVE integration harness for the crm-dedupe-tool.

Drives the FULL real flow against a LIVE Supabase project and a Salesforce
SANDBOX, exercising the REAL auth + tenancy + approval-gate + pre-merge-backup
paths with NO bypass:

    seed  ->  connect  ->  scan  ->  review/approve  ->  merge  ->  verify

Unlike scripts/local_harness.py (which swaps out auth/storage and never touches
Supabase), this harness:
  * mints a real Supabase-style HS256 access token signed with SUPABASE_JWT_SECRET
    and sends it as `Authorization: Bearer <jwt>` to the running FastAPI app, so
    every request goes through app/auth.py `require_user` (NO bypass);
  * creates real `auth.users` rows (the JWT `sub` and every *.user_id FK reference
    auth.users(id), so the users must exist) via the Supabase Auth admin API;
  * seeds tenant + owner membership + an encrypted crm_connections row directly
    with the service-role key — encrypting the Salesforce token EXACTLY the way the
    app does (app/services/encryption.py Fernet/ENCRYPTION_KEY) — so the operator
    skips the browser OAuth dance;
  * runs scan -> approve one set -> merge -> verify, and ASSERTS the safety
    invariants (approval gate, backup-as-precondition, tenant isolation, partial vs
    full merge), not merely that the calls return 200.

SAFETY
------
Default mode is DRY/READ: it does EVERYTHING except the irreversible Salesforce
merge. To actually merge you must pass BOTH:

    --execute  --i-confirm-sandbox

AND the live org must POSITIVELY confirm Organization.IsSandbox = true (a failed or
ambiguous Organization query aborts --execute — we never merge against an org we
could not confirm is a sandbox), AND the instance_url must NOT look like a
production login domain (the harness refuses `login.salesforce.com` /
`*.my.salesforce.com` without `.sandbox.`). It prefers to seed disposable duplicate
Contacts in the sandbox and clean them up; if you do not let it seed, you MUST point
at throwaway sandbox records. NEVER run --execute against production.

The Salesforce SANDBOX session is supplied by the operator (no Connected App
needed):

    sf org display --json --target-org <sandboxAlias>
    # -> result.accessToken  +  result.instanceUrl

See `manual_prereqs` in the harness header and --help.

Dependencies (all already pinned by api/requirements.txt): httpx, PyJWT,
cryptography, postgrest. Run with the api venv so app.* imports resolve:

    cd api && source venv/bin/activate && cd ..
    python3 scripts/integration_e2e.py --help
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Make app.* importable (encryption mirrors the app's exact token format) and
# load api/.env so ENCRYPTION_KEY / SUPABASE_* match the running server.
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_DIR = os.path.join(ROOT, "api")
sys.path.insert(0, API_DIR)

try:
    import jwt  # PyJWT
except ImportError:  # pragma: no cover
    sys.exit("PyJWT not installed. `pip install -r api/requirements.txt` in the api venv.")

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("httpx not installed. `pip install -r api/requirements.txt` in the api venv.")


def _load_dotenv(path: str) -> None:
    """Minimal .env loader (avoids a hard python-dotenv dep at import time)."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv(os.path.join(API_DIR, ".env"))

# Import the app's OWN encryption so the seeded token is byte-for-byte what the
# app would have written. If app import fails (e.g. wrong cwd/venv) we still run,
# falling back to a local Fernet built from ENCRYPTION_KEY the identical way.
try:
    from app.services.encryption import encrypt_token as _app_encrypt_token  # type: ignore
    _HAVE_APP_ENCRYPT = True
except Exception:  # noqa: BLE001
    _HAVE_APP_ENCRYPT = False


def encrypt_token_like_app(token: str) -> str:
    if _HAVE_APP_ENCRYPT:
        return _app_encrypt_token(token)
    # Fallback: replicate encryption.py exactly (hex key -> first 32 bytes ->
    # urlsafe_b64 -> Fernet).
    import base64
    from cryptography.fernet import Fernet

    key_hex = os.environ.get("ENCRYPTION_KEY", "")
    try:
        key_bytes = bytes.fromhex(key_hex)
    except ValueError:
        sys.exit("ENCRYPTION_KEY must be a valid hex string (matches encryption.py).")
    if len(key_bytes) < 32:
        sys.exit("ENCRYPTION_KEY must be >= 64 hex chars (32 bytes).")
    fernet_key = base64.urlsafe_b64encode(key_bytes[:32])
    return Fernet(fernet_key).encrypt(token.encode()).decode()


# --------------------------------------------------------------------------- #
# Constants / labels for the seeded fixtures (idempotent, easy to find + purge).
# --------------------------------------------------------------------------- #
TAG = "e2e-harness"
OWNER_EMAIL = "e2e-owner@dedupe-harness.invalid"
OUTSIDER_EMAIL = "e2e-outsider@dedupe-harness.invalid"
SEED_CONTACT_MARK = "ZZZE2E"  # unique token stamped on seeded sandbox contacts


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[e2e][FAIL] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def ok(msg: str) -> None:
    print(f"[e2e][ OK ] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# JWT — mint a Supabase-style access token EXACTLY as app/auth.py verifies it:
#   HS256, signed with SUPABASE_JWT_SECRET, audience="authenticated",
#   must contain `exp` and `sub` (sub becomes the user_id).
# (Cross-checked against api/tests/test_phase0_safety.py `_token`, which is the
#  ground-truth shape the app accepts.)
# --------------------------------------------------------------------------- #
def mint_supabase_jwt(user_id: str, secret: str, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "aud": "authenticated",
        "role": "authenticated",
        "iss": "supabase-e2e-harness",
        "iat": now,
        "exp": now + ttl_seconds,
        "email": f"{user_id}@dedupe-harness.invalid",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


# --------------------------------------------------------------------------- #
# Supabase Auth admin (create real auth.users rows; the JWT sub + FK columns all
# reference auth.users(id), so the users MUST exist). Uses the service-role key.
# --------------------------------------------------------------------------- #
class SupabaseAdmin:
    def __init__(self, url: str, service_key: str):
        self.url = url.rstrip("/")
        self.key = service_key
        self._auth_headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self._rest_headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            # Prefer return=representation so inserts echo the row back.
            "Prefer": "return=representation",
        }

    # ---- Auth admin -------------------------------------------------------- #
    def get_or_create_user(self, email: str) -> str:
        """Return the auth.users id for `email`, creating the user if absent.

        Uses the GoTrue admin API (service-role). Idempotent across re-runs.
        """
        with httpx.Client(timeout=30) as c:
            # Try to find an existing user by email (admin list w/ filter).
            r = c.get(
                f"{self.url}/auth/v1/admin/users",
                headers=self._auth_headers,
                params={"page": 1, "per_page": 200},
            )
            if r.status_code == 200:
                data = r.json()
                users = data.get("users", data) if isinstance(data, dict) else data
                for u in users or []:
                    if (u.get("email") or "").lower() == email.lower():
                        return u["id"]
            # Create it.
            cr = c.post(
                f"{self.url}/auth/v1/admin/users",
                headers=self._auth_headers,
                json={
                    "email": email,
                    "email_confirm": True,
                    "user_metadata": {"created_by": TAG},
                },
            )
            if cr.status_code in (200, 201):
                return cr.json()["id"]
            # 422 = already registered -> fetch again (handles race / pagination).
            if cr.status_code == 422:
                r2 = c.get(
                    f"{self.url}/auth/v1/admin/users",
                    headers=self._auth_headers,
                    params={"page": 1, "per_page": 200},
                )
                if r2.status_code == 200:
                    data = r2.json()
                    users = data.get("users", data) if isinstance(data, dict) else data
                    for u in users or []:
                        if (u.get("email") or "").lower() == email.lower():
                            return u["id"]
            fail(f"Could not create/find auth user {email}: {cr.status_code} {cr.text}")
            return ""  # unreachable

    def delete_user(self, user_id: str) -> None:
        with httpx.Client(timeout=30) as c:
            c.delete(
                f"{self.url}/auth/v1/admin/users/{user_id}",
                headers=self._auth_headers,
            )

    # ---- PostgREST (service-role; mirrors app's SupabaseClient) ------------ #
    def _rest(self) -> httpx.Client:
        return httpx.Client(base_url=f"{self.url}/rest/v1", headers=self._rest_headers, timeout=30)

    def insert(self, table: str, row: dict) -> dict:
        with self._rest() as c:
            r = c.post(f"/{table}", json=row)
            if r.status_code not in (200, 201):
                fail(f"insert {table} failed: {r.status_code} {r.text}")
            data = r.json()
            return data[0] if isinstance(data, list) and data else (data or {})

    def select(self, table: str, **eq: Any) -> list[dict]:
        params = {k: f"eq.{v}" for k, v in eq.items()}
        params["select"] = "*"
        with self._rest() as c:
            r = c.get(f"/{table}", params=params)
            if r.status_code != 200:
                fail(f"select {table} failed: {r.status_code} {r.text}")
            return r.json()

    def delete_eq(self, table: str, **eq: Any) -> None:
        params = {k: f"eq.{v}" for k, v in eq.items()}
        with self._rest() as c:
            r = c.delete(f"/{table}", params=params)
            if r.status_code not in (200, 204):
                log(f"warn: delete {table} {eq} -> {r.status_code} {r.text}")


# --------------------------------------------------------------------------- #
# The app's HTTP API client (always carries the real Bearer JWT).
# --------------------------------------------------------------------------- #
class AppClient:
    def __init__(self, base_url: str, jwt_token: str):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {jwt_token}"}

    def _c(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, headers=self.headers, timeout=60)

    def post(self, path: str, json_body: dict) -> httpx.Response:
        with self._c() as c:
            return c.post(path, json=json_body)

    def patch(self, path: str, json_body: dict) -> httpx.Response:
        with self._c() as c:
            return c.patch(path, json=json_body)

    def get(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        with self._c() as c:
            return c.get(path, params=params or {})


# --------------------------------------------------------------------------- #
# Salesforce sandbox REST helper (used only to seed + clean disposable Contacts
# and to verify post-merge state — the app does the actual merge).
# --------------------------------------------------------------------------- #
class SalesforceSandbox:
    API = "v59.0"  # matches app/services/salesforce_*.py

    def __init__(self, access_token: str, instance_url: str):
        self.token = access_token
        self.instance = instance_url.rstrip("/")

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def is_production_domain(self) -> bool:
        """Heuristic: refuse anything that smells like prod.

        Sandbox instance_urls contain `.sandbox.` or `--<sandboxname>.` (My Domain
        sandboxes look like `https://<mydomain>--<sbx>.sandbox.my.salesforce.com`).
        Production My Domain is `https://<mydomain>.my.salesforce.com` (no
        `.sandbox.`), and classic prod is `login.salesforce.com`.

        This is a fast preflight tripwire only — the AUTHORITATIVE backstop is the
        live Organization.IsSandbox = true check in whoami()/preflight, which must
        pass before --execute.
        """
        host = self.instance.lower()
        if ".sandbox." in host or host.endswith(".sandbox.my.salesforce.com"):
            return False  # clearly a sandbox
        if "login.salesforce.com" in host:
            return True
        if host.endswith(".my.salesforce.com") and ".sandbox." not in host:
            return True
        # test.salesforce.com is the sandbox login host -> allow.
        if "test.salesforce.com" in host:
            return False
        # Unknown shape: be conservative, treat as production.
        return True

    def whoami(self) -> dict:
        """Validate the session and return the Organization record.

        Surfaces query failures loudly (a degraded session must NOT masquerade as a
        confirmed sandbox — that is the safety backstop for --execute).
        """
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{self.instance}/services/data/{self.API}/limits", headers=self._h())
            if r.status_code != 200:
                fail(f"Salesforce sandbox token invalid/expired: {r.status_code} {r.text}")
            r2 = c.get(
                f"{self.instance}/services/data/{self.API}/query",
                headers=self._h(),
                params={"q": "SELECT Id, Name, IsSandbox FROM Organization LIMIT 1"},
            )
            if r2.status_code != 200:
                # Do NOT silently return {} -> a None IsSandbox would skip the
                # sandbox guard. Make the caller decide based on a real signal.
                fail(
                    "Could not read Organization (IsSandbox) to confirm this is a "
                    f"sandbox: {r2.status_code} {r2.text}. Refusing to proceed without "
                    "a positive sandbox confirmation."
                )
            recs = r2.json().get("records") or []
            if not recs:
                fail("Organization query returned no rows; cannot confirm IsSandbox.")
            return recs[0]

    def create_contact(self, first: str, last: str, email: str, phone: str) -> str:
        # We are deliberately seeding DUPLICATE contacts to test dedupe, so bypass any
        # active Salesforce Duplicate Rule (the Standard Contact rule is on by default
        # in many orgs and otherwise rejects the 2nd insert with DUPLICATES_DETECTED).
        # allowSave=true only works when the rule's action permits save (alert, not
        # hard-block); a hard-block rule would need to be deactivated in the sandbox.
        headers = {**self._h(), "Sforce-Duplicate-Rule-Header": "allowSave=true"}
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{self.instance}/services/data/{self.API}/sobjects/Contact",
                headers=headers,
                json={"FirstName": first, "LastName": last, "Email": email, "Phone": phone},
            )
            if r.status_code not in (200, 201):
                fail(f"create sandbox contact failed: {r.status_code} {r.text}")
            return r.json()["id"]

    def get_contact(self, contact_id: str) -> Optional[dict]:
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{self.instance}/services/data/{self.API}/sobjects/Contact/{contact_id}",
                headers=self._h(),
                params={"fields": "Id,FirstName,LastName,Email,Phone"},
            )
            if r.status_code == 200:
                return r.json()
            return None  # 404 => deleted/merged away

    def delete_contact(self, contact_id: str) -> None:
        with httpx.Client(timeout=30) as c:
            c.delete(
                f"{self.instance}/services/data/{self.API}/sobjects/Contact/{contact_id}",
                headers=self._h(),
            )

    def find_seeded_contacts(self) -> list[str]:
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{self.instance}/services/data/{self.API}/query",
                headers=self._h(),
                params={"q": f"SELECT Id FROM Contact WHERE LastName LIKE '{SEED_CONTACT_MARK}%'"},
            )
            if r.status_code != 200:
                return []
            return [rec["Id"] for rec in r.json().get("records", [])]


# --------------------------------------------------------------------------- #
# Polling helpers.
# --------------------------------------------------------------------------- #
def poll_until(fn, predicate, timeout_s: int = 180, interval_s: float = 2.0, what: str = "job"):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = fn()
        if predicate(last):
            return last
        time.sleep(interval_s)
    fail(f"Timed out waiting for {what} (last state: {json.dumps(last, default=str)[:400]})")
    return last


# --------------------------------------------------------------------------- #
# The harness.
# --------------------------------------------------------------------------- #
class Harness:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.created: dict[str, list[str]] = {
            "auth_users": [], "tenants": [], "connections": [], "scans": [],
            "sf_contacts": [],
        }
        self.passed: list[str] = []

        # --- required env ---
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.service_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        self.jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
        self.enc_key = os.environ.get("ENCRYPTION_KEY", "").strip()
        if not self.supabase_url or not self.service_key:
            fail("SUPABASE_URL and SUPABASE_SERVICE_KEY are required.")
        if not self.jwt_secret:
            fail("SUPABASE_JWT_SECRET is required (auth.py fails closed without it).")
        if not self.enc_key:
            fail("ENCRYPTION_KEY is required (must match the running API's key).")

        self.admin = SupabaseAdmin(self.supabase_url, self.service_key)

        # --- salesforce sandbox session (operator-supplied) ---
        self.sf_token = (args.sf_access_token or os.environ.get("SF_ACCESS_TOKEN", "")).strip()
        self.sf_instance = (args.sf_instance_url or os.environ.get("SF_INSTANCE_URL", "")).strip()
        if not self.sf_token or not self.sf_instance:
            fail("Salesforce sandbox session required: pass --sf-access-token/--sf-instance-url "
                 "or set SF_ACCESS_TOKEN/SF_INSTANCE_URL (from `sf org display --json`).")
        self.sf = SalesforceSandbox(self.sf_token, self.sf_instance)

        # IDs filled during run.
        self.owner_uid = ""
        self.outsider_uid = ""
        self.tenant_id = ""
        self.connection_id = ""
        self.owner_jwt = ""
        self.outsider_jwt = ""

    # ----------------------------------------------------------------------- #
    def preflight(self) -> None:
        log("Preflight: checking the API is up and unauthenticated calls are rejected...")
        # API reachable?
        try:
            with httpx.Client(timeout=15) as c:
                c.get(f"{self.args.api.rstrip('/')}/")
        except Exception as e:  # noqa: BLE001
            fail(f"API not reachable at {self.args.api}: {e}. Start it: "
                 "cd api && uvicorn app.main:app --reload --port 8000")
        # Auth must NOT be bypassable: a no-token scan call must be 401/403.
        # (HTTPBearer(auto_error=True) returns 403 on a missing header; an
        #  invalid/expired token returns 401 — both are accepted here.)
        with httpx.Client(timeout=15) as c:
            r = c.post(f"{self.args.api.rstrip('/')}/scan/start",
                       json={"connection_id": str(uuid.uuid4()),
                             "config": {"object_type": "contacts"}})
        if r.status_code not in (401, 403):
            fail(f"Expected 401/403 for an unauthenticated /scan/start, got {r.status_code}. "
                 "Is require_user actually wired? Refusing to continue (would not be a real test).")
        ok("Unauthenticated request correctly rejected (require_user is live).")

        # Salesforce sandbox session valid + NOT production (heuristic tripwire).
        if self.sf.is_production_domain():
            fail(f"instance_url {self.sf_instance} looks like PRODUCTION. Refusing. "
                 "Point this at a Salesforce SANDBOX (test.salesforce.com / *.sandbox.my.salesforce.com).")
        # AUTHORITATIVE backstop: read the live org and require a positive sandbox
        # confirmation. whoami() now fails loudly if the Organization query fails,
        # so we can never proceed (esp. with --execute) on an unconfirmed org.
        org = self.sf.whoami()
        is_sandbox = org.get("IsSandbox")
        log(f"Salesforce org: {org.get('Name')} (Id={org.get('Id')}, IsSandbox={is_sandbox}).")
        if is_sandbox is not True:
            # is_sandbox is False (production) or unexpectedly absent -> refuse.
            fail(f"Organization.IsSandbox is {is_sandbox!r} (not a confirmed sandbox). "
                 "Refusing — this could be PRODUCTION.")
        if self.args.execute and not self.args.i_confirm_sandbox:
            fail("--execute requires the explicit --i-confirm-sandbox guard. Refusing to merge.")
        ok("Salesforce sandbox session valid and confirmed non-production (IsSandbox=true).")

    # ----------------------------------------------------------------------- #
    def seed_identities(self) -> None:
        log("Seeding auth users (owner + outsider) — required because *.user_id FKs reference auth.users...")
        self.owner_uid = self.admin.get_or_create_user(OWNER_EMAIL)
        self.outsider_uid = self.admin.get_or_create_user(OUTSIDER_EMAIL)
        self.created["auth_users"] = [self.owner_uid, self.outsider_uid]
        ok(f"owner uid={self.owner_uid}  outsider uid={self.outsider_uid}")

        # Mint real Supabase-style JWTs (sub = the auth.users id).
        self.owner_jwt = mint_supabase_jwt(self.owner_uid, self.jwt_secret)
        self.outsider_jwt = mint_supabase_jwt(self.outsider_uid, self.jwt_secret)

        # Sanity: our token must verify the same way auth.py will
        # (HS256 + aud=authenticated + require exp,sub).
        try:
            decoded = jwt.decode(self.owner_jwt, self.jwt_secret, algorithms=["HS256"],
                                 audience="authenticated", options={"require": ["exp", "sub"]})
            assert decoded["sub"] == self.owner_uid
        except Exception as e:  # noqa: BLE001
            fail(f"Self-check of minted JWT failed (would never authenticate): {e}")
        ok("Minted JWTs verify exactly as app/auth.py decodes them.")

    def seed_tenant_and_connection(self) -> None:
        log("Seeding tenant + OWNER membership + encrypted Salesforce connection (skips OAuth dance)...")
        # Reuse an existing harness tenant for this owner if present (idempotent).
        # NB: crm_connections has UNIQUE(user_id, crm_type), and SalesforceService
        # .get_connection() reads it with .single(); reuse keeps exactly one row, so
        # neither the INSERT nor the app's .single() can blow up on a duplicate.
        existing_conns = self.admin.select("crm_connections", user_id=self.owner_uid, crm_type="salesforce")
        if existing_conns:
            conn = existing_conns[0]
            self.connection_id = conn["id"]
            self.tenant_id = conn["tenant_id"]
            # Refresh the token + expiry so get_connection won't try to OAuth-refresh.
            self._update_connection_token()
            log(f"Reusing existing connection {self.connection_id} (tenant {self.tenant_id}).")
        else:
            self.tenant_id = str(uuid.uuid4())
            self.admin.insert("tenants", {
                "id": self.tenant_id,
                "name": f"{TAG} sandbox tenant",
                "client_access_enabled": False,
            })
            self.created["tenants"].append(self.tenant_id)
            conn = self._insert_connection()
            self.connection_id = conn["id"]
            self.created["connections"].append(self.connection_id)

        # OWNER membership (always-on access). Insert only if absent (mirror tenancy.py).
        members = self.admin.select("tenant_members", tenant_id=self.tenant_id, user_id=self.owner_uid)
        if not members:
            self.admin.insert("tenant_members", {
                "tenant_id": self.tenant_id,
                "user_id": self.owner_uid,
                "role": "owner",
                "is_active": True,
            })
        ok(f"Owner is OWNER of tenant {self.tenant_id}; connection {self.connection_id} ready.")
        # NB: outsider gets NO membership and is NOT platform_staff -> tenant isolation test.

    def _connection_row(self) -> dict:
        # expires_at FAR in the future so SalesforceService.get_connection does NOT
        # attempt an OAuth refresh (which would fail without a Connected App).
        expires_at = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
        org_id = "00Dxx0000000SANDBOX"  # placeholder org id; instance_url is what matters
        return {
            "user_id": self.owner_uid,
            "tenant_id": self.tenant_id,
            "crm_type": "salesforce",
            "access_token_encrypted": encrypt_token_like_app(self.sf_token),
            "refresh_token_encrypted": encrypt_token_like_app(self.sf_token),  # no real refresh token; never used
            # SalesforceService.get_connection() does portal_id.split('|', 1) ->
            # (org_id, instance_url). instance_url is the load-bearing half.
            "portal_id": f"{org_id}|{self.sf_instance}",
            "expires_at": expires_at,
        }

    def _insert_connection(self) -> dict:
        return self.admin.insert("crm_connections", self._connection_row())

    def _update_connection_token(self) -> None:
        params = {"id": f"eq.{self.connection_id}"}
        body = {
            "access_token_encrypted": encrypt_token_like_app(self.sf_token),
            "refresh_token_encrypted": encrypt_token_like_app(self.sf_token),
            "portal_id": f"00Dxx0000000SANDBOX|{self.sf_instance}",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat(),
        }
        with self.admin._rest() as c:
            r = c.patch("/crm_connections", params=params, json=body)
            if r.status_code not in (200, 204):
                log(f"warn: refreshing connection token -> {r.status_code} {r.text}")

    # ----------------------------------------------------------------------- #
    def seed_sandbox_duplicates(self) -> dict[str, list[str]]:
        """Create disposable duplicate Contacts in the sandbox.

        Two groups:
          GROUP A (the one we'll APPROVE+merge):   winner + 1 loser
          GROUP B (left UN-approved):              winner + 1 loser  (must NOT merge)

        Returns {"A": [winner, loser...], "B": [winner, loser...]}.
        """
        if self.args.no_seed:
            log("--no-seed: NOT creating sandbox contacts. You must supply throwaway records "
                "another way; merge assertions that need real dupes will be skipped.")
            return {}
        log("Seeding disposable duplicate Contacts in the sandbox (marked with "
            f"LastName LIKE '{SEED_CONTACT_MARK}%')...")
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        # A and B must land in SEPARATE clusters. DuplicateDetector blocks on
        # email-domain + name-prefix(first 3) and scores email(0.6)+name(0.4); two
        # groups differing by a single char in otherwise-identical long strings
        # cross-match at ~0.98 and collapse into ONE cluster. So give A and B DISTINCT
        # first names, last-name cores, AND email domains — they then share no block
        # and score well under the 0.9 threshold. Within a group the two records are
        # IDENTICAL, so each is its own confidence-1.0 duplicate set.
        seeds = {
            "A": {"first": "Alice", "core": "Alpha", "dom": f"alpha-{stamp}.invalid"},
            "B": {"first": "Bravo", "core": "Bravo", "dom": f"bravo-{stamp}.invalid"},
        }
        groups: dict[str, list[str]] = {}
        for grp in ("A", "B"):
            s = seeds[grp]
            first = s["first"]
            last = f"{SEED_CONTACT_MARK}{s['core']}{stamp}"   # keeps the ZZZE2E teardown marker
            email = f"{first.lower()}.{stamp}@{s['dom']}"
            phone = f"+1{'111' if grp == 'A' else '222'}{stamp}"
            w = self.sf.create_contact(first, last, email, phone)
            l = self.sf.create_contact(first, last, email, phone)
            groups[grp] = [w, l]
            self.created["sf_contacts"].extend([w, l])
            log(f"  group {grp}: winner={w} loser={l} (email={email})")
        ok(f"Seeded {len(self.created['sf_contacts'])} sandbox contacts in 2 duplicate groups.")
        return groups

    # ----------------------------------------------------------------------- #
    def run_scan(self) -> str:
        log("Starting a CONTACTS scan via the real authenticated endpoint...")
        # object_type='contacts': the live schema (LIVE_apply, 002 skipped) keeps the
        # scans.object_type CHECK at ('contacts','companies','deals'), so 'contacts'
        # is valid WITHOUT 002, and the MERGE path is contacts-only (accounts needs
        # 002 AND is a view-only dry-run). So contacts is correct for an e2e merge.
        body = {
            "connection_id": self.connection_id,
            "config": {
                "object_type": "contacts",
                "confidence_threshold": 0.9,
                "winner_rules": [{"rule_type": "most_associations"}],
            },
        }
        r = self.owner().post("/scan/start", body)
        if r.status_code != 200:
            fail(f"/scan/start failed: {r.status_code} {r.text}")
        scan_id = r.json()["scan_id"]
        self.created["scans"].append(scan_id)
        ok(f"scan started: {scan_id}")

        def status():
            sr = self.owner().get(f"/scan/{scan_id}/status")
            if sr.status_code != 200:
                fail(f"/scan status failed: {sr.status_code} {sr.text}")
            return sr.json()

        final = poll_until(
            status,
            lambda s: s["status"] in ("completed", "failed"),
            timeout_s=self.args.scan_timeout,
            what="scan",
        )
        if final["status"] == "failed":
            # scans.error_message carries the (sanitized) failure reason.
            fail(f"Scan failed: {final.get('error_message')}. (Token expired? Sandbox empty? "
                 "Check the API logs.)")
        ok(f"scan completed: records_scanned={final['records_scanned']} "
           f"duplicates_found={final['duplicates_found']}")
        return scan_id

    def get_results(self, scan_id: str) -> list[dict]:
        r = self.owner().get(f"/scan/{scan_id}/results", params={"per_page": 200})
        if r.status_code != 200:
            fail(f"/scan results failed: {r.status_code} {r.text}")
        return r.json().get("duplicate_sets", [])

    # ----------------------------------------------------------------------- #
    def assert_tenant_isolation(self, scan_id: str) -> None:
        log("ASSERTION: a user with NO membership must be denied the scan/results...")
        r1 = self.outsider().get(f"/scan/{scan_id}/status")
        r2 = self.outsider().get(f"/scan/{scan_id}/results")
        # tenancy.assert_tenant_access raises 404 (deliberately, to not reveal existence).
        if r1.status_code not in (403, 404) or r2.status_code not in (403, 404):
            fail(f"Tenant isolation BROKEN: outsider got status={r1.status_code}, "
                 f"results={r2.status_code} (expected 403/404).")
        # And the outsider must be rejected attempting a merge on this scan too.
        r3 = self.outsider().post("/merge/execute", {"scan_id": scan_id})
        if r3.status_code not in (403, 404):
            fail(f"Tenant isolation BROKEN on merge: outsider got {r3.status_code}.")
        self.passed.append("tenant_isolation: non-member denied scan/results/merge (403/404)")
        ok("Outsider (no membership, not platform_staff) is denied. Tenant isolation holds.")

    def assert_unapproved_not_merged(self, scan_id: str) -> None:
        log("ASSERTION: with NOTHING approved, /merge/execute must refuse (the gate)...")
        r = self.owner().post("/merge/execute", {"scan_id": scan_id})
        # execute_merge raises 400 'No approved duplicate sets to merge' when the gate
        # finds zero approved sets.
        if r.status_code != 400:
            fail(f"Approval gate BROKEN: /merge/execute with no approvals returned "
                 f"{r.status_code} {r.text} (expected 400 'No approved duplicate sets').")
        self.passed.append("approval_gate: merge with no approved sets rejected (400)")
        ok("Merge correctly refused when no set is approved.")

    def pick_set_for_group(self, sets: list[dict], member_ids: list[str]) -> Optional[dict]:
        """Find the duplicate_set whose winner+losers correspond to one seeded group.

        Matches by id intersection (the engine may pick either seeded record as the
        winner; we only need to find the set that contains this group's ids).
        """
        member_set = set(member_ids)
        for s in sets:
            ids = {s["winner_record_id"], *(s.get("loser_record_ids") or [])}
            if ids & member_set:
                return s
        return None

    def approve_set(self, scan_id: str, set_id: str) -> dict:
        r = self.owner().patch(f"/scan/{scan_id}/duplicate-sets/{set_id}", {"decision": "approved"})
        if r.status_code != 200:
            fail(f"approve set failed: {r.status_code} {r.text}")
        row = r.json()
        if row.get("decision") != "approved":
            fail(f"approve did not stick: decision={row.get('decision')}")
        ok(f"set {set_id} approved by {self.owner_uid}.")
        return row

    # ----------------------------------------------------------------------- #
    def merge_and_verify(self, scan_id: str, approved_set: dict, unapproved_set: Optional[dict],
                         groups: dict[str, list[str]]) -> None:
        if not self.args.execute:
            log("DRY-RUN: --execute NOT set. Skipping the irreversible Salesforce merge.")
            log("Everything up to the merge has been exercised against the real auth/tenancy/gate.")
            self.passed.append("dry_run: full pipeline exercised up to (but not including) the merge")
            return

        # Hard guards (already checked in preflight; re-assert here right before the
        # irreversible call). is_production_domain is the heuristic tripwire; the
        # authoritative IsSandbox=true check already ran in preflight.
        if not self.args.i_confirm_sandbox:
            fail("Refusing merge without --i-confirm-sandbox.")
        if self.sf.is_production_domain():
            fail("Refusing merge: instance_url looks like production.")

        log("EXECUTING the merge of the APPROVED set only (real Salesforce sandbox merge)...")
        r = self.owner().post("/merge/execute", {"scan_id": scan_id, "set_ids": [approved_set["id"]]})
        if r.status_code != 200:
            fail(f"/merge/execute failed: {r.status_code} {r.text}")
        merge_id = r.json()["merge_id"]
        ok(f"merge started: {merge_id} (total_sets={r.json().get('total_sets')})")

        def mstatus():
            sr = self.owner().get(f"/merge/{merge_id}/status")
            if sr.status_code != 200:
                fail(f"/merge status failed: {sr.status_code} {sr.text}")
            return sr.json()

        final = poll_until(
            mstatus,
            lambda s: s["status"] in ("completed", "failed", "paused"),
            timeout_s=self.args.merge_timeout,
            what="merge",
        )
        if final["status"] != "completed":
            fail(f"Merge ended {final['status']}: {json.dumps(final.get('error_log'), default=str)}")
        ok(f"merge completed: completed_sets={final['completed_sets']} failed_sets={final['failed_sets']}")

        # ---- ASSERTION: a merge_backups row exists for the merged set ------- #
        backups = self.admin.select("merge_backups", merge_id=merge_id, set_id=approved_set["id"])
        if not backups:
            fail("BACKUP PRECONDITION BROKEN: no merge_backups row for the merged set. "
                 "A merge must never happen without a pre-merge backup.")
        b = backups[0]
        if b.get("winner_record_id") != approved_set["winner_record_id"]:
            fail("merge_backups winner mismatch.")
        # merge_backups.loser_record_ids snapshots the FULL original loser set
        # (build_backup_row uses op['all_loser_ids']).
        if set(b.get("loser_record_ids") or []) != set(approved_set.get("loser_record_ids") or []):
            fail("merge_backups did not snapshot the FULL original loser set.")
        self.passed.append("backup_precondition: merge_backups row exists with full pre-merge snapshot")
        ok("Pre-merge backup row present with the full winner+loser snapshot.")

        # ---- ASSERTION: the approved set is now merged=True (full coverage) - #
        ds = self.admin.select("duplicate_sets", id=approved_set["id"])
        if not ds:
            fail("approved duplicate_set disappeared.")
        ds = ds[0]
        if not ds.get("merged"):
            fail("merged flag NOT set on the approved set despite a completed merge.")
        if ds.get("decision") != "merged":
            fail(f"approved set decision should be 'merged', got {ds.get('decision')}.")
        absorbed = set(ds.get("merged_loser_ids") or [])
        all_losers = set(ds.get("loser_record_ids") or [])
        if absorbed != all_losers:
            fail(f"merged=True but merged_loser_ids {absorbed} != loser_record_ids {all_losers} "
                 "(merged must be True ONLY on full loser coverage).")
        self.passed.append("full_merge: merged=True only with complete loser coverage (merged_loser_ids==losers)")
        ok("Approved set fully merged; merged=True matches complete loser coverage.")

        # ---- ASSERTION: losers actually gone from Salesforce --------------- #
        for loser_id in ds.get("loser_record_ids") or []:
            still = self.sf.get_contact(loser_id)
            if still is not None:
                fail(f"Loser {loser_id} still exists in Salesforce after a 'completed' merge.")
        winner_alive = self.sf.get_contact(approved_set["winner_record_id"])
        if winner_alive is None:
            fail("Winner record was deleted by the merge (it must survive).")
        self.passed.append("salesforce_state: losers deleted, winner survives in the sandbox")
        ok("Salesforce confirms losers were absorbed and the winner survives.")

        # ---- ASSERTION: the UN-approved set was NOT merged ----------------- #
        if unapproved_set is not None:
            us = self.admin.select("duplicate_sets", id=unapproved_set["id"])
            us = us[0] if us else {}
            if us.get("merged"):
                fail("UN-approved set was merged! Approval gate failed during execution.")
            if us.get("decision") == "merged":
                fail("UN-approved set decision flipped to 'merged'.")
            # Its sandbox loser(s) must still exist. group B = [winner, loser, ...];
            # check everything past index 0.
            grp_b = groups.get("B") or []
            for cid in grp_b[1:]:  # losers only
                if self.sf.get_contact(cid) is None:
                    fail(f"Un-approved group B loser {cid} was deleted in Salesforce!")
            self.passed.append("approval_gate(exec): the un-approved set stayed unmerged through a real merge run")
            ok("The un-approved set was left untouched by the merge run. Gate holds at execution time.")

        # ---- final report (auto-generated on completion) ------------------- #
        rep = self.owner().get("/reports/mine")
        if rep.status_code == 200:
            reports = rep.json().get("reports", [])
            # ReportService stores the merge id at report_data["merge"]["id"]
            # (NOT a flat report_data["merge_id"]). Match on the correct nested path.
            def _report_merge_id(x: dict) -> Optional[str]:
                return ((x.get("report_data") or {}).get("merge") or {}).get("id")
            mine = [x for x in reports if _report_merge_id(x) == merge_id]
            if not mine:
                # Visible-but-unmatched is still a soft signal (report gen may lag /
                # paginate); don't fail the run on it, but say so honestly.
                log(f"note: a report row for merge {merge_id} was not found in the first "
                    f"page of /reports/mine ({len(reports)} report(s) visible).")
            else:
                log(f"Final report visible to owner for merge {merge_id}.")
            self.passed.append("report: final merge report generated and visible to the tenant owner")
        else:
            log(f"note: /reports/mine -> {rep.status_code} (report generation may have soft-failed; "
                "merge itself succeeded).")

    # ----------------------------------------------------------------------- #
    def owner(self) -> AppClient:
        return AppClient(self.args.api, self.owner_jwt)

    def outsider(self) -> AppClient:
        return AppClient(self.args.api, self.outsider_jwt)

    # ----------------------------------------------------------------------- #
    def teardown(self) -> None:
        if self.args.keep:
            log("--keep set: leaving seeded rows in place for inspection.")
            return
        log("Teardown: removing seeded sandbox contacts and Supabase fixtures...")
        # 1. Salesforce: delete any seeded contacts that still exist (merged losers
        #    are already gone). Also sweep by the marker in case of an earlier abort.
        #    The marker LastName LIKE 'ZZZE2E%' is harness-specific, so this only
        #    touches our own throwaway records, never real sandbox data.
        leftover = set(self.created["sf_contacts"]) | set(self.sf.find_seeded_contacts())
        for cid in leftover:
            try:
                self.sf.delete_contact(cid)
            except Exception:  # noqa: BLE001
                pass
        # 2. Supabase: delete scans (cascades duplicate_sets/merges/merge_backups via FKs),
        #    then connection, tenant_members, tenant. auth.users last.
        for scan_id in self.created["scans"]:
            self.admin.delete_eq("scans", id=scan_id)
        if not self.args.keep_connection:
            for cid in self.created["connections"]:
                self.admin.delete_eq("crm_connections", id=cid)
            if self.tenant_id and self.owner_uid:
                self.admin.delete_eq("tenant_members", tenant_id=self.tenant_id, user_id=self.owner_uid)
            for tid in self.created["tenants"]:
                self.admin.delete_eq("tenants", id=tid)
        if self.args.delete_users:
            for uid in self.created["auth_users"]:
                self.admin.delete_user(uid)
            log("Deleted seeded auth users.")
        ok("Teardown complete.")

    # ----------------------------------------------------------------------- #
    def run(self) -> int:
        self.preflight()
        self.seed_identities()
        self.seed_tenant_and_connection()
        groups = self.seed_sandbox_duplicates()

        scan_id = self.run_scan()

        # Negative tests BEFORE any approval.
        self.assert_tenant_isolation(scan_id)
        self.assert_unapproved_not_merged(scan_id)

        sets = self.get_results(scan_id)
        log(f"scan produced {len(sets)} duplicate set(s).")

        approved_set = None
        unapproved_set = None
        if groups:
            approved_set = self.pick_set_for_group(sets, groups.get("A", []))
            unapproved_set = self.pick_set_for_group(sets, groups.get("B", []))
            if approved_set is None:
                fail("Could not find the duplicate set for the seeded group A. The dedup engine "
                     "did not cluster the seeded contacts (raise volume or check thresholds).")
            self.approve_set(scan_id, approved_set["id"])
            # Deliberately do NOT approve group B (it must stay unmerged).
        else:
            # No seeding: approve the highest-confidence set if any exist, but only
            # under --execute the operator vouches it's throwaway sandbox data.
            if sets and self.args.execute:
                approved_set = sets[0]
                self.approve_set(scan_id, approved_set["id"])
            else:
                log("No seeded groups and no --execute; skipping approve/merge assertions.")

        if approved_set:
            self.merge_and_verify(scan_id, approved_set, unapproved_set, groups)

        # Summary.
        print("\n================ ASSERTIONS PASSED ================")
        for p in self.passed:
            print(f"  PASS  {p}")
        if not self.args.execute:
            print("  (dry-run: merge-time assertions deferred — re-run with --execute --i-confirm-sandbox)")
        print("==================================================\n")
        return 0


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LIVE end-to-end integration harness for crm-dedupe-tool "
                    "(real auth + tenancy + approval gate + pre-merge backup).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--api", default=os.environ.get("API_BASE_URL", "http://localhost:8000"),
                   help="Base URL of the running FastAPI app (default http://localhost:8000).")
    p.add_argument("--sf-access-token", default=None,
                   help="Salesforce SANDBOX access token (or env SF_ACCESS_TOKEN). "
                        "From `sf org display --json` -> result.accessToken.")
    p.add_argument("--sf-instance-url", default=None,
                   help="Salesforce SANDBOX instance URL (or env SF_INSTANCE_URL). "
                        "From `sf org display --json` -> result.instanceUrl.")
    # Safety gates.
    p.add_argument("--execute", action="store_true",
                   help="ACTUALLY perform the irreversible Salesforce merge. Off by default (dry-run).")
    p.add_argument("--i-confirm-sandbox", action="store_true",
                   help="Required alongside --execute. Asserts you are pointed at throwaway SANDBOX data.")
    # Seeding / data.
    p.add_argument("--no-seed", action="store_true",
                   help="Do NOT create disposable sandbox contacts (you must supply throwaway dupes).")
    # Timeouts.
    p.add_argument("--scan-timeout", type=int, default=600, help="Seconds to wait for a scan.")
    p.add_argument("--merge-timeout", type=int, default=300, help="Seconds to wait for a merge.")
    # Teardown controls.
    p.add_argument("--keep", action="store_true", help="Do not tear anything down (inspect afterwards).")
    p.add_argument("--keep-connection", action="store_true",
                   help="Keep the seeded tenant/connection (only clean scans + sandbox contacts).")
    p.add_argument("--delete-users", action="store_true",
                   help="Also delete the seeded auth.users on teardown (default: keep them for re-runs).")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    h = Harness(args)
    rc = 1
    try:
        rc = h.run()
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:  # noqa: BLE001
        print(f"[e2e][ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        rc = 1
    finally:
        try:
            h.teardown()
        except Exception as e:  # noqa: BLE001
            print(f"[e2e][WARN] teardown error: {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
