#!/usr/bin/env python3
"""
Dry-run account dedupe against a live Salesforce org — VIEW matches, commit nothing.

Fetches Account records via the `sf` CLI, runs the config-driven MatchEngine with
one or more client profiles, and prints the duplicate clusters for review. No
writes to Salesforce, ever.

Usage:
  python3 scripts/dry_run_accounts.py --org Scandit \
      --profile api/profiles/scandit/account_v2.json \
      --profile api/profiles/scandit/account_v3.json
  # or replay a cached export:
  python3 scripts/dry_run_accounts.py --csv /tmp/scandit_accounts.csv --profile ...
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys

# make `app.services.match_engine` importable from the api/ package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
from app.services.match_engine import MatchEngine, MatchProfile  # noqa: E402

# Verified audit baseline (duplicate-audit.md, 2026-04-09) for sanity-checking the engine.
AUDIT_BASELINE = {
    "total_accounts": 48175,
    "eligible": 39977,
    "true_dupe_accounts": 729,
    "true_dupe_clusters": 343,
    "hierarchy_explained_clusters": 60,
    "consolidation_safe_accounts": 129,
}

# Fields the profiles bind to (only what we need — keeps 48k rows light).
SOQL_FIELDS = [
    "Id", "Name", "Website", "BillingCountry", "BillingCountryCode",
    "Vertical__c", "ParentId", "LastActivityDate",
    "SCD_NetSuite_Sync_Active__c", "SCD_NetSuite_ID__c", "AccountNumber",
    "OwnerId", "CreatedDate",
]


def fetch_via_sf(org: str) -> list[dict]:
    soql = f"SELECT {', '.join(SOQL_FIELDS)} FROM Account"
    print(f"[fetch] querying {org} via sf CLI (this pulls ~48k rows, ~30-60s)...", flush=True)
    proc = subprocess.run(
        ["sf", "data", "query", "--query", soql, "--target-org", org, "--result-format", "csv"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"sf query failed:\n{proc.stderr}")
    out = proc.stdout
    # strip any leading non-CSV chatter; CSV starts at the header line containing "Id"
    lines = out.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Id,") or ln.startswith('"Id"')), 0)
    return list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))


def load_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def pct(n, d):
    return f"{(100.0 * n / d):.2f}%" if d else "—"


def run_profile(path: str, records: list[dict]) -> None:
    profile = MatchProfile.from_json(path)
    result = MatchEngine(profile).find_clusters(records)
    s = result.stats
    base = AUDIT_BASELINE

    print(f"\n{'='*72}\nPROFILE: {profile.version}\n{'='*72}")
    print(f"  records in            {s['total_records']:>8,}   (audit total {base['total_accounts']:,})")
    print(f"  eligible              {s['eligible']:>8,}   (audit {base['eligible']:,})")
    print(f"  ineligible (filtered) {s['ineligible']:>8,}")
    print(f"  --- matching ---")
    print(f"  deterministic pairs   {s['deterministic_pairs']:>8,}   (tier-3 external-id short-circuit)")
    print(f"  fingerprint pairs     {s['fingerprint_pairs']:>8,}   (exact Name+Domain+Country)")
    print(f"  fuzzy pairs           {s['fuzzy_pairs']:>8,}")
    print(f"  vetoed by discriminator {s['vetoed_pairs']:>6,}   (tier-2 custom-field veto)")
    print(f"  --- clusters ---")
    print(f"  dupe clusters         {s['clusters_dupe']:>8,}   (audit {base['true_dupe_clusters']:,})")
    print(f"  accounts in dupes     {s['accounts_in_dupe_clusters']:>8,}   (audit {base['true_dupe_accounts']:,})")
    print(f"  hierarchy-excluded    {s['clusters_hierarchy_explained']:>8,}   (audit {base['hierarchy_explained_clusters']:,})")
    print(f"  auto_safe / review    {s['clusters_auto_safe']:>4,} / {s['clusters_needs_review']:<4,}")

    # a few example clusters for eyeballing
    dupes = sorted((c for c in result.clusters if c.is_dupe),
                   key=lambda c: len(c.member_ids), reverse=True)
    print(f"\n  largest dupe clusters (review sample):")
    for c in dupes[:5]:
        names = [m.get("Name") for m in c.members]
        print(f"    [{c.bucket:12}] {c.hierarchy_class:18} x{len(c.member_ids)}  {names}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="Scandit")
    ap.add_argument("--csv", help="replay a cached export instead of querying")
    ap.add_argument("--profile", action="append", required=True, help="repeatable")
    ap.add_argument("--save-csv", help="write the fetched rows here for replay")
    args = ap.parse_args()

    records = load_csv(args.csv) if args.csv else fetch_via_sf(args.org)
    print(f"[fetch] {len(records):,} account records loaded.")
    if args.save_csv and not args.csv:
        with open(args.save_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=records[0].keys())
            w.writeheader()
            w.writerows(records)
        print(f"[fetch] cached to {args.save_csv}")

    for p in args.profile:
        run_profile(p, records)

    print("\nDRY-RUN ONLY — no records were modified.")


if __name__ == "__main__":
    main()
