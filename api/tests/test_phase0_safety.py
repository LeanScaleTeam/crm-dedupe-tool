"""Phase 0 safety unit tests — the parts that don't need a live Supabase/Salesforce.

Covers the two highest-risk pieces of the safety work:
  1. JWT verification (auth.py) — valid / expired / bad-signature / missing-secret.
  2. Partial-merge correctness (salesforce_merge.py) — a multi-batch merge that
     fails midway must report exactly which losers were absorbed, and stop.

The DB-gated paths (gate enforcement, ownership checks) are integration-level and
need Supabase; they are exercised manually / in a future integration suite.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from app import auth  # noqa: E402
from app.services.salesforce_merge import SalesforceMergeService  # noqa: E402

SECRET = "test-jwt-secret-0123456789"


# --------------------------------------------------------------------------- #
# 1. JWT verification
# --------------------------------------------------------------------------- #
@pytest.fixture
def with_secret(monkeypatch):
    monkeypatch.setattr(
        auth, "get_settings",
        lambda: types.SimpleNamespace(supabase_jwt_secret=SECRET),
    )


def _token(sub="user-123", secret=SECRET, exp_delta=timedelta(hours=1), aud="authenticated"):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": sub, "aud": aud, "exp": now + exp_delta, "iat": now},
        secret, algorithm="HS256",
    )


def test_valid_token_returns_sub(with_secret):
    assert auth.verify_supabase_jwt(_token(sub="abc-789")) == "abc-789"


def test_expired_token_rejected(with_secret):
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_supabase_jwt(_token(exp_delta=timedelta(hours=-1)))
    assert e.value.status_code == 401


def test_bad_signature_rejected(with_secret):
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_supabase_jwt(_token(secret="someone-elses-secret"))
    assert e.value.status_code == 401


def test_wrong_audience_rejected(with_secret):
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_supabase_jwt(_token(aud="not-authenticated"))
    assert e.value.status_code == 401


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.setattr(
        auth, "get_settings",
        lambda: types.SimpleNamespace(supabase_jwt_secret=""),
    )
    with pytest.raises(auth.HTTPException) as e:
        auth.verify_supabase_jwt(_token())
    assert e.value.status_code == 500  # fail closed, not open


# --------------------------------------------------------------------------- #
# 2. Partial-merge correctness
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "" if status_code in (200, 201, 204) else "error body"


class _FakeAsyncClient:
    """Returns a programmed sequence of status codes, one per .post() call."""
    sequence: list[int] = []
    calls: list[list[str]] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.calls.append(list(json["recordToMergeIds"]))
        idx = len(_FakeAsyncClient.calls) - 1
        return _FakeResponse(_FakeAsyncClient.sequence[idx])


def _service():
    conn = types.SimpleNamespace(access_token="t", instance_url="https://x.my.salesforce.com")
    return SalesforceMergeService(conn)


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    import app.services.salesforce_merge as m
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(m.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)


async def _no_sleep(*_a, **_k):
    return None


@pytest.mark.asyncio
async def test_all_batches_succeed_absorbs_all():
    _FakeAsyncClient.sequence = [204, 204]  # 4 losers -> 2 calls, both ok
    res = await _service().merge_contacts("MASTER", ["l1", "l2", "l3", "l4"])
    assert res["success"] is True
    assert res["absorbed_ids"] == ["l1", "l2", "l3", "l4"]
    assert _FakeAsyncClient.calls == [["l1", "l2"], ["l3", "l4"]]


@pytest.mark.asyncio
async def test_second_batch_fails_reports_partial_and_stops():
    # The exact bug: batch 1 (l1,l2) deleted, batch 2 fails. Must report l1,l2 as
    # absorbed (they're GONE), report failure, and NOT attempt batch 3.
    _FakeAsyncClient.sequence = [204, 500, 204]
    res = await _service().merge_contacts("MASTER", ["l1", "l2", "l3", "l4", "l5", "l6"])
    assert res["success"] is False
    assert res["absorbed_ids"] == ["l1", "l2"]      # batch 1 only
    assert _FakeAsyncClient.calls == [["l1", "l2"], ["l3", "l4"]]  # stopped, no batch 3
    assert res["errors"]


@pytest.mark.asyncio
async def test_duplicate_set_reports_merged_loser_ids():
    _FakeAsyncClient.sequence = [204]
    res = await _service().merge_duplicate_set("MASTER", ["l1", "l2"])
    assert res["success"] is True
    assert res["merged_loser_ids"] == ["l1", "l2"]
    assert res["merged_count"] == 2


# --------------------------------------------------------------------------- #
# 3. HubSpot merge honors the same contract (merged_loser_ids + stop-on-failure)
# --------------------------------------------------------------------------- #
def _hs_service():
    from app.services.hubspot_merge import HubSpotMergeService
    conn = types.SimpleNamespace(access_token="t")
    return HubSpotMergeService(conn)


@pytest.mark.asyncio
async def test_hubspot_stops_on_failure_and_reports_absorbed(monkeypatch):
    import app.services.hubspot_merge as hm
    monkeypatch.setattr(hm.asyncio, "sleep", _no_sleep)
    svc = _hs_service()

    async def fake_merge(winner_id, loser_id):
        ok = loser_id != "l2"  # l2 fails
        return {"success": ok} if ok else {"success": False, "error": "boom"}

    monkeypatch.setattr(svc, "merge_contacts", fake_merge)
    res = await svc.merge_duplicate_set("MASTER", ["l1", "l2", "l3"])
    assert res["success"] is False
    assert res["merged_loser_ids"] == ["l1"]      # stopped at l2, l3 never tried
    assert res["merged_count"] == 1


@pytest.mark.asyncio
async def test_hubspot_winner_update_fail_aborts_merge(monkeypatch):
    import app.services.hubspot_merge as hm
    monkeypatch.setattr(hm.asyncio, "sleep", _no_sleep)
    svc = _hs_service()
    merged_any = {"called": False}

    async def fake_update(cid, props):
        return {"success": False, "error": "nope"}

    async def fake_merge(winner_id, loser_id):
        merged_any["called"] = True
        return {"success": True}

    monkeypatch.setattr(svc, "update_contact", fake_update)
    monkeypatch.setattr(svc, "merge_contacts", fake_merge)
    res = await svc.merge_duplicate_set("MASTER", ["l1"], blended_properties={"x": 1})
    assert res["success"] is False
    assert res["merged_loser_ids"] == []
    assert merged_any["called"] is False           # never archived a loser
