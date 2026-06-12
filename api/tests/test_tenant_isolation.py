"""Multi-tenant isolation unit tests (no live Supabase needed).

Exercises the backend's tenant access rule (app/services/tenancy.py), which is the
REAL enforcement: the backend talks to Postgres with the service-role key and
BYPASSES RLS, so the SQL policies in 004_multi_tenant.sql are only second-line.

Covers the locked decisions + the two rollback levers from 004_multi_tenant.sql:
  * client access gated by tenants.client_access_enabled, default OFF (lever #1);
  * owner / operator access never depends on that flag (so flipping it off never
    locks out the operator);
  * platform-staff cross-tenant access (no per-tenant membership required);
  * tenant auto-provisioning on connect (tenant = connected org), client OFF.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from app.services import tenancy  # noqa: E402
from app.services.tenancy import (  # noqa: E402
    accessible_tenant_ids,
    assert_tenant_access,
    can_access_tenant,
    is_platform_staff,
    resolve_tenant_for_save,
)


# --------------------------------------------------------------------------- #
# Minimal in-memory fake of the PostgREST query builder tenancy.py uses.
# Supports exactly the call shapes in that module: select/eq/in_/single/execute,
# insert, and upsert(on_conflict=...).
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, store, table):
        self.store = store
        self.table = table
        self._filters = []
        self._single = False
        self._count = False
        self._op = "select"
        self._payload = None
        self._on_conflict = None

    def select(self, *cols, count=None):
        self._op = "select"
        self._count = count == "exact"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def single(self):
        self._single = True
        return self

    def insert(self, row):
        self._op, self._payload = "insert", row
        return self

    def upsert(self, row, on_conflict=None):
        self._op, self._payload, self._on_conflict = "upsert", row, on_conflict
        return self

    def _match(self, r):
        for op, col, val in self._filters:
            if op == "eq" and r.get(col) != val:
                return False
            if op == "in" and r.get(col) not in val:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self._op == "select":
            hits = [dict(r) for r in rows if self._match(r)]
            if self._single:
                return _Result(hits[0] if hits else None)
            return _Result(hits, len(hits) if self._count else None)
        if self._op in ("insert", "upsert"):
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                if self._op == "upsert" and self._on_conflict:
                    keys = [k.strip() for k in self._on_conflict.split(",")]
                    existing = next(
                        (r for r in rows if all(r.get(k) == p.get(k) for k in keys)), None
                    )
                    if existing:
                        existing.update(p)
                        out.append(dict(existing))
                        continue
                rows.append(dict(p))
                out.append(dict(p))
            return _Result(out)
        return _Result([])


class FakeSupabase:
    def __init__(self, **tables):
        self.store = {k: [dict(r) for r in v] for k, v in tables.items()}

    def table(self, name):
        return _Query(self.store, name)


T1, T2 = "tenant-1", "tenant-2"
OWNER, CLIENT, STAFF, STRANGER = "u-owner", "u-client", "u-staff", "u-stranger"


def _db(**overrides):
    tables = {
        "tenants": [
            {"id": T1, "name": "Acme (salesforce)", "client_access_enabled": False},
            {"id": T2, "name": "Beta (hubspot)", "client_access_enabled": False},
        ],
        "tenant_members": [
            {"tenant_id": T1, "user_id": OWNER, "role": "owner", "is_active": True},
            {"tenant_id": T1, "user_id": CLIENT, "role": "client", "is_active": True},
        ],
        "platform_staff": [{"user_id": STAFF}],
        "crm_connections": [],
    }
    tables.update(overrides)
    return FakeSupabase(**tables)


# --- core access rule ------------------------------------------------------ #
def test_owner_always_has_access():
    assert can_access_tenant(_db(), T1, OWNER) is True


def test_stranger_denied():
    assert can_access_tenant(_db(), T1, STRANGER) is False


def test_client_denied_when_flag_off():
    # Rollback lever #1 default state: client access OFF -> denied.
    assert can_access_tenant(_db(), T1, CLIENT) is False


def test_client_allowed_when_flag_on():
    db = _db(tenants=[
        {"id": T1, "name": "Acme", "client_access_enabled": True},
        {"id": T2, "name": "Beta", "client_access_enabled": False},
    ])
    assert can_access_tenant(db, T1, CLIENT) is True


def test_owner_unaffected_by_client_flag():
    # Flipping the client lever off must NEVER lock out the owner/operator.
    db = _db(tenants=[{"id": T1, "name": "Acme", "client_access_enabled": False}])
    assert can_access_tenant(db, T1, OWNER) is True


def test_inactive_member_denied():
    db = _db(tenant_members=[
        {"tenant_id": T1, "user_id": OWNER, "role": "owner", "is_active": False},
    ])
    assert can_access_tenant(db, T1, OWNER) is False


def test_none_tenant_denied():
    assert can_access_tenant(_db(), None, OWNER) is False


def test_no_cross_tenant_leak_for_member():
    # OWNER of T1 must not reach T2 (the isolation guarantee).
    assert can_access_tenant(_db(), T2, OWNER) is False


# --- platform staff -------------------------------------------------------- #
def test_staff_reaches_any_tenant_without_membership():
    db = _db()
    assert is_platform_staff(db, STAFF) is True
    assert can_access_tenant(db, T1, STAFF) is True
    assert can_access_tenant(db, T2, STAFF) is True  # no membership needed


def test_accessible_tenant_ids_staff_is_none():
    # None == "all tenants" — list endpoints must not filter for staff.
    assert accessible_tenant_ids(_db(), STAFF) is None


def test_accessible_tenant_ids_owner_only_when_client_off():
    db = _db(tenant_members=[
        {"tenant_id": T1, "user_id": OWNER, "role": "owner", "is_active": True},
        {"tenant_id": T2, "user_id": OWNER, "role": "client", "is_active": True},
    ])
    assert accessible_tenant_ids(db, OWNER) == [T1]  # T2 client membership gated off


def test_accessible_tenant_ids_includes_enabled_client():
    db = _db(
        tenants=[
            {"id": T1, "name": "Acme", "client_access_enabled": False},
            {"id": T2, "name": "Beta", "client_access_enabled": True},
        ],
        tenant_members=[
            {"tenant_id": T1, "user_id": OWNER, "role": "owner", "is_active": True},
            {"tenant_id": T2, "user_id": OWNER, "role": "client", "is_active": True},
        ],
    )
    assert sorted(accessible_tenant_ids(db, OWNER)) == sorted([T1, T2])


def test_accessible_tenant_ids_empty_for_stranger():
    assert accessible_tenant_ids(_db(), STRANGER) == []


# --- assert_tenant_access -> 404 (don't reveal existence) ------------------ #
def test_assert_raises_404_for_stranger():
    with pytest.raises(tenancy.HTTPException) as e:
        assert_tenant_access(_db(), T1, STRANGER)
    assert e.value.status_code == 404


def test_assert_ok_for_owner():
    assert_tenant_access(_db(), T1, OWNER)  # no raise


# --- provisioning (tenant = connected org) --------------------------------- #
def test_resolve_creates_tenant_client_off_and_owner():
    db = _db()
    tid = resolve_tenant_for_save(db, OWNER, "salesforce", "00Dxx")
    created = next(t for t in db.store["tenants"] if t["id"] == tid)
    # The locked decision: new tenants default client access OFF.
    assert created["client_access_enabled"] is False
    # The connecting user is its OWNER and can act immediately.
    assert can_access_tenant(db, tid, OWNER) is True


def test_resolve_reuses_existing_tenant_for_same_crm_type():
    db = _db()
    first = resolve_tenant_for_save(db, OWNER, "salesforce", "00Dxx")
    # Simulate the saved connection now carrying that tenant.
    db.store["crm_connections"].append(
        {"user_id": OWNER, "crm_type": "salesforce", "tenant_id": first}
    )
    second = resolve_tenant_for_save(db, OWNER, "salesforce", "00Dxx")
    assert second == first  # no duplicate tenant on token re-save


def test_deactivated_owner_stays_deactivated_across_token_refresh():
    # Regression (provisioning#1): resolve_tenant_for_save runs on EVERY token-refresh
    # re-save, not just first connect. A deliberate staff deactivation (is_active=False)
    # must NOT be silently undone by that re-save — otherwise revocation never sticks.
    db = _db()
    tid = resolve_tenant_for_save(db, OWNER, "salesforce", "00Dxx")
    db.store["crm_connections"].append(
        {"user_id": OWNER, "crm_type": "salesforce", "tenant_id": tid}
    )
    # Staff offboards the owner from this tenant.
    for m in db.store["tenant_members"]:
        if m["tenant_id"] == tid and m["user_id"] == OWNER:
            m["is_active"] = False
    assert can_access_tenant(db, tid, OWNER) is False

    # A routine token-refresh re-save must leave the revocation in place.
    again = resolve_tenant_for_save(db, OWNER, "salesforce", "00Dxx")
    assert again == tid
    assert can_access_tenant(db, tid, OWNER) is False
