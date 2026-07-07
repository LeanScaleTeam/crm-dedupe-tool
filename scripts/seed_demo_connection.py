#!/usr/bin/env python3
"""
Break-glass demo helper: seed a Salesforce crm_connections row for a REAL login
user, straight from an `sf` CLI sandbox token — no browser OAuth / Connected App.

This is the FALLBACK for the live UI demo. Normally you click "Connect Salesforce"
in the UI (real OAuth). If that wobbles on stage, run this AFTER logging into the
app once (so the auth user exists); then refresh /connect and it shows "Salesforce
Connected", and you continue scan -> review -> merge yourself in the UI.

It mirrors scripts/integration_e2e.py's seeding exactly (encrypted token via the
app's Fernet key, tenant + owner membership, portal_id='<orgId>|<instanceUrl>',
expires_at far future so get_connection never tries an OAuth refresh).

Usage (from repo root, with the api venv):
    ./api/venv/bin/python scripts/seed_demo_connection.py --email you@leanscale.team --org LSDevBox

Re-runnable (upserts on user_id,crm_type). Requires: api/.env populated, `sf`
authed to the sandbox org.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API_DIR = REPO / "api"


def load_api_env() -> None:
    """Load api/.env into os.environ BEFORE importing app.* (app.config caches settings)."""
    env_path = API_DIR / ".env"
    if not env_path.exists():
        sys.exit(f"ERROR: {env_path} not found.")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def sf_session(org_alias: str) -> tuple[str, str, str]:
    """Return (access_token, instance_url, org_id) from `sf org display`."""
    out = subprocess.run(
        ["sf", "org", "display", "--target-org", org_alias, "--json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"ERROR: `sf org display -o {org_alias}` failed:\n{out.stderr}")
    r = json.loads(out.stdout)["result"]
    token = r.get("accessToken")
    instance = r.get("instanceUrl")
    org_id = r.get("id") or "00Dxx0000000SANDBOX"
    if not token or not instance:
        sys.exit("ERROR: could not read accessToken/instanceUrl from sf org display.")
    if "sandbox" not in instance.lower() and "test.salesforce" not in instance.lower():
        # Guardrail: this helper is for sandbox demos only.
        sys.exit(f"REFUSING: instance_url does not look like a sandbox: {instance}")
    return token, instance, org_id


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True, help="Login user's email (must exist / will be created+confirmed).")
    ap.add_argument("--org", default="LSDevBox", help="sf CLI sandbox alias (default LSDevBox).")
    args = ap.parse_args()

    load_api_env()
    sys.path.insert(0, str(API_DIR))

    # Imported AFTER env is loaded so app.config picks up the right settings.
    from app.services.encryption import encrypt_token  # type: ignore
    from app.services.supabase_client import get_supabase  # type: ignore
    from app.services.tenancy import resolve_tenant_for_save  # type: ignore

    sb = get_supabase()
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    import httpx

    # 1) Resolve (or create+confirm) the auth user by email.
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = httpx.get(f"{url}/auth/v1/admin/users", headers=headers, params={"page": 1, "per_page": 200})
    users = r.json().get("users", []) if r.status_code == 200 else []
    uid = next((u["id"] for u in users if (u.get("email") or "").lower() == args.email.lower()), None)
    if not uid:
        cr = httpx.post(
            f"{url}/auth/v1/admin/users", headers=headers,
            json={"email": args.email, "email_confirm": True},
        )
        if cr.status_code not in (200, 201):
            sys.exit(f"ERROR creating user: {cr.status_code} {cr.text}")
        uid = cr.json()["id"]
        print(f"  created+confirmed auth user {args.email} -> {uid}")
    else:
        print(f"  found auth user {args.email} -> {uid}")

    # 2) sf sandbox session.
    token, instance, org_id = sf_session(args.org)
    print(f"  sandbox: {instance}  (org {org_id})")

    # 3) Tenant + owner membership (same helper the real OAuth save_connection uses).
    tenant_id = resolve_tenant_for_save(sb, uid, "salesforce", org_id)
    print(f"  tenant -> {tenant_id}")

    # 4) Upsert the encrypted connection (far-future expiry => no OAuth refresh attempt).
    row = {
        "user_id": uid,
        "tenant_id": tenant_id,
        "crm_type": "salesforce",
        "access_token_encrypted": encrypt_token(token),
        "refresh_token_encrypted": encrypt_token(token),
        "portal_id": f"{org_id}|{instance}",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat(),
    }
    res = sb.table("crm_connections").upsert(row, on_conflict="user_id,crm_type").execute()
    conn = (res.data or [None])[0]
    print(f"  connection -> {conn.get('id') if conn else '(none)'}")
    print("\nDONE. Log into the app as this user, open /connect (refresh), and you'll")
    print("see 'Salesforce Connected'. Click 'Start Deduplication Scan' -> contacts.")
    print("NOTE: the sandbox token lives ~2h; re-run this right before the demo.")


if __name__ == "__main__":
    main()
