#!/usr/bin/env python3
"""Inspect / export pre-merge backups (005_merge_backups) for a merge run.

A pre-merge backup is a BACKUP, NOT AN UNDO. A Salesforce merge deletes the loser
records and their Ids cannot be resurrected; a re-created record gets a NEW Id and
loses its relationships. So this tool AUDITS what existed before a merge and EXPORTS
the full snapshot so the lost records can be re-created / reconciled manually. It
deliberately does NOT auto-re-insert into the CRM — that remapping (new Ids, parent/
child links, ownership) is a per-org decision, not something to script blindly.

Usage (from repo root, with the api venv + Supabase env vars set):
    api/venv/bin/python scripts/restore_from_backup.py --merge-id <uuid> [--out backup.json]
"""
import argparse
import json
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API_DIR))

from app.services.supabase_client import get_supabase  # noqa: E402


def fetch_backups(supabase, merge_id: str) -> list:
    res = (
        supabase.table("merge_backups")
        .select("*")
        .eq("merge_id", merge_id)
        .order("created_at")
        .execute()
    )
    return res.data or []


def print_plan(backups: list) -> None:
    if not backups:
        print("No backups found for that merge id.")
        return
    total_losers = 0
    for b in backups:
        losers = b.get("loser_record_ids") or []
        total_losers += len(losers)
        print(f"\nset {b.get('set_id')}  ({b.get('crm_type')})")
        print(f"  winner kept:   {b.get('winner_record_id')}")
        print(f"  losers merged: {', '.join(losers) or '(none)'}")
        print(
            f"  re-create plan: re-insert {len(losers)} loser record(s) from "
            "loser_snapshot, then re-point their relationships to the new Ids."
        )
    print(f"\n{len(backups)} set(s), {total_losers} loser record(s) snapshotted.")
    print(
        "NOTE: this is a backup, not an undo — re-creation is a deliberate manual "
        "step (new Ids + relationship remapping)."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect/export pre-merge backups.")
    ap.add_argument("--merge-id", required=True, help="The merge run to inspect.")
    ap.add_argument("--out", help="Write the full backup JSON to this path.")
    args = ap.parse_args()

    supabase = get_supabase()
    backups = fetch_backups(supabase, args.merge_id)
    print_plan(backups)

    if args.out and backups:
        Path(args.out).write_text(json.dumps(backups, indent=2, default=str))
        print(f"\nFull snapshot written to {args.out}")


if __name__ == "__main__":
    main()
