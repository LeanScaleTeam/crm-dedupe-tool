"""Unit tests for the pre-merge backup helpers (app/services/merge_backup.py).

These cover the substantive logic — what gets snapshotted, idempotency on resume, and
that a write failure propagates so run_merge can refuse to merge an un-backed-up set.
The run_merge wiring itself (the try/except precondition) is integration-level and is
exercised against a live stack, consistent with test_phase0_safety's stance.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from app.services.merge_backup import build_backup_row, write_backup  # noqa: E402


# --- minimal in-memory fake (select/eq/insert/execute, the shapes write_backup uses)
class _Result:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, store, table):
        self.store = store
        self.table = table
        self._filters = []
        self._op = "select"
        self._payload = None

    def select(self, *cols):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def insert(self, row):
        self._op, self._payload = "insert", row
        return self

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self._op == "select":
            return _Result([r for r in rows if all(r.get(c) == v for c, v in self._filters)])
        if self._op == "insert":
            rows.append(dict(self._payload))
            return _Result([dict(self._payload)])
        return _Result([])


class FakeSupabase:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _Q(self.store, name)


class _FailInsertQ(_Q):
    def execute(self):
        if self._op == "insert":
            raise RuntimeError("db down")
        return super().execute()


class FailingInsertSupabase(FakeSupabase):
    def table(self, name):
        return _FailInsertQ(self.store, name)


# A run_merge merge_operation for one set mid partial-resume: L1 already merged,
# L2 remaining. The backup must capture the FULL original loser set (L1+L2).
OP = {
    "set_id": "set-1",
    "winner_id": "W1",
    "loser_ids": ["L2"],            # remaining only — NOT what we back up
    "already_merged": ["L1"],
    "all_loser_ids": ["L1", "L2"],  # full original — this is the backup scope
    "blended_properties": {"Name": "Acme"},
    "winner_data": {"Id": "W1", "Name": "Acme Inc"},
    "loser_data": [{"Id": "L1"}, {"Id": "L2"}],
}


def test_build_backup_row_captures_full_pre_merge_state():
    row = build_backup_row("merge-1", "scan-1", "tenant-1", "salesforce", "conn-1", OP)
    assert row["merge_id"] == "merge-1"
    assert row["scan_id"] == "scan-1"
    assert row["set_id"] == "set-1"
    assert row["tenant_id"] == "tenant-1"
    assert row["crm_type"] == "salesforce"
    assert row["connection_id"] == "conn-1"
    assert row["winner_record_id"] == "W1"
    assert row["winner_snapshot"] == {"Id": "W1", "Name": "Acme Inc"}
    # Backs up the FULL original loser set, not just the partial-resume remainder.
    assert row["loser_record_ids"] == ["L1", "L2"]
    assert row["loser_snapshot"] == [{"Id": "L1"}, {"Id": "L2"}]
    assert row["blended_properties"] == {"Name": "Acme"}
    assert row["id"]


def test_write_backup_inserts_when_absent():
    db = FakeSupabase()
    write_backup(db, build_backup_row("m1", "s1", "t1", "salesforce", "c1", OP))
    assert len(db.store["merge_backups"]) == 1


def test_write_backup_idempotent_per_merge_and_set():
    db = FakeSupabase()
    write_backup(db, build_backup_row("m1", "s1", "t1", "salesforce", "c1", OP))
    # A resumed merge re-enters with the same (merge_id, set_id): no duplicate row,
    # and the first (truest) pre-merge snapshot is preserved.
    write_backup(db, build_backup_row("m1", "s1", "t1", "salesforce", "c1", OP))
    assert len(db.store["merge_backups"]) == 1


def test_write_backup_distinguishes_different_sets():
    db = FakeSupabase()
    write_backup(db, build_backup_row("m1", "s1", "t1", "sf", "c1", {**OP, "set_id": "set-1"}))
    write_backup(db, build_backup_row("m1", "s1", "t1", "sf", "c1", {**OP, "set_id": "set-2"}))
    assert len(db.store["merge_backups"]) == 2


def test_write_backup_propagates_failure_so_merge_is_blocked():
    db = FailingInsertSupabase()
    with pytest.raises(Exception):
        write_backup(db, build_backup_row("m1", "s1", "t1", "sf", "c1", OP))
    # Nothing recorded — run_merge's except turns this into "set not merged".
    assert db.store.get("merge_backups", []) == []
