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
import json
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


def run_profile(path: str, records: list[dict]):
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
    print(f"  --- verification gate ---")
    print(f"  auto-merge eligible   {s['clusters_auto_merge']:>8,}   (deterministic/exact + activity-safe)")
    print(f"  needs verification    {s['clusters_needs_verification']:>8,}   (must be human-approved first)")

    # a few example clusters for eyeballing
    dupes = sorted((c for c in result.clusters if c.is_dupe),
                   key=lambda c: len(c.member_ids), reverse=True)
    print(f"\n  largest dupe clusters (review sample):")
    for c in dupes[:5]:
        names = [m.get("Name") for m in c.members]
        print(f"    [{c.bucket:12}] {c.hierarchy_class:18} x{len(c.member_ids)}  {names}")
    return profile, result


def write_html(out_path: str, source, n_records: int, results) -> None:
    """Emit a self-contained, browsable HTML review of the dupe clusters."""
    profiles = []
    for profile, result in results:
        clusters = []
        for c in result.clusters:
            if not c.is_dupe:
                continue
            clusters.append({
                "vstatus": c.verification_status,
                "vreason": c.verification_reason,
                "bucket": c.bucket,
                "hierarchy": c.hierarchy_class,
                "path": c.match_path,
                "size": len(c.member_ids),
                "members": [{
                    "name": m.get("Name"),
                    "website": m.get("Website"),
                    "country": m.get("BillingCountryCode"),
                    "vertical": m.get("Vertical__c"),
                    "id": m.get("Id"),
                } for m in c.members],
            })
        clusters.sort(key=lambda x: x["size"], reverse=True)
        profiles.append({"version": profile.version, "stats": result.stats, "clusters": clusters})

    payload = {"source": str(source), "n": n_records, "profiles": profiles}
    html = _HTML_TEMPLATE.replace("/*__DATA__*/", json.dumps(payload))
    with open(out_path, "w") as f:
        f.write(html)


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Account Dedupe — Dry-Run Review</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--mut:#94a3b8;--line:#334155;--accent:#38bdf8}
  *{box-sizing:border-box} body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9;color:#0f172a}
  header{background:var(--bg);color:#fff;padding:18px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  header h1{font-size:17px;margin:0;font-weight:700}
  .badge{padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700}
  .dry{background:#fde68a;color:#78350f}
  .src{color:var(--mut);font-size:12px}
  .wrap{max-width:1080px;margin:0 auto;padding:20px 24px}
  .tabs{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
  .tab{padding:8px 14px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;cursor:pointer;font-weight:600}
  .tab.on{background:#0ea5e9;color:#fff;border-color:#0ea5e9}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:18px}
  .stat{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}
  .stat .n{font-size:22px;font-weight:800} .stat .l{color:#64748b;font-size:12px}
  .controls{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
  .controls input{padding:8px 12px;border:1px solid #cbd5e1;border-radius:8px;flex:1;min-width:200px}
  .chip{padding:6px 12px;border:1px solid #cbd5e1;border-radius:999px;background:#fff;cursor:pointer;font-size:12px;font-weight:600}
  .chip.on{background:#0f172a;color:#fff;border-color:#0f172a}
  .cluster{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin-bottom:10px}
  .ctop{display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap}
  .tag{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px}
  .auto_safe{background:#dcfce7;color:#166534}.needs_review{background:#fef9c3;color:#854d0e}.known_active{background:#e0e7ff;color:#3730a3}
  .hx{background:#f1f5f9;color:#475569} .size{color:#64748b;font-size:12px;margin-left:auto}
  table{width:100%;border-collapse:collapse} td,th{text-align:left;padding:5px 8px;border-top:1px solid #f1f5f9;font-size:13px}
  th{color:#64748b;font-weight:600;font-size:11px;text-transform:uppercase}
  .vurl{color:#0369a1} .muted{color:#94a3b8}
</style></head><body>
<header>
  <h1>Account Dedupe — Review</h1>
  <span class="badge dry">DRY-RUN · no records modified</span>
  <span class="src" id="src"></span>
</header>
<div class="wrap">
  <div class="tabs" id="tabs"></div>
  <div class="stats" id="stats"></div>
  <div class="controls">
    <input id="q" placeholder="Search account name…" oninput="render()">
    <span class="chip on" data-b="all" onclick="setBucket(this)">All</span>
    <span class="chip" data-b="needs_review" onclick="setBucket(this)">Needs review</span>
    <span class="chip" data-b="auto_safe" onclick="setBucket(this)">Auto-safe</span>
    <span class="chip" data-b="known_active" onclick="setBucket(this)">Known-active</span>
  </div>
  <div id="list"></div>
</div>
<script>
const DATA = /*__DATA__*/;
let pi = 0, bucket = "all";
document.getElementById("src").textContent = DATA.source + " · " + DATA.n.toLocaleString() + " accounts";
const tabs = document.getElementById("tabs");
DATA.profiles.forEach((p,i)=>{const b=document.createElement("div");b.className="tab"+(i===0?" on":"");b.textContent=p.version;b.onclick=()=>{pi=i;[...tabs.children].forEach(t=>t.classList.remove("on"));b.classList.add("on");render();};tabs.appendChild(b);});
function setBucket(el){bucket=el.dataset.b;document.querySelectorAll(".chip").forEach(c=>c.classList.remove("on"));el.classList.add("on");render();}
function statCard(n,l){return '<div class="stat"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>';}
function render(){
  const p=DATA.profiles[pi], s=p.stats, q=document.getElementById("q").value.toLowerCase();
  document.getElementById("stats").innerHTML=
    statCard(s.clusters_dupe.toLocaleString(),"dupe clusters")+
    statCard(s.accounts_in_dupe_clusters.toLocaleString(),"accounts in dupes")+
    statCard(s.clusters_needs_review.toLocaleString(),"needs review")+
    statCard(s.clusters_auto_safe.toLocaleString(),"auto-safe")+
    statCard(s.vetoed_pairs.toLocaleString(),"discriminator vetoes")+
    statCard(s.clusters_hierarchy_explained.toLocaleString(),"hierarchy-excluded");
  const rows=p.clusters.filter(c=>(bucket==="all"||c.bucket===bucket) && (!q||c.members.some(m=>(m.name||"").toLowerCase().includes(q))));
  document.getElementById("list").innerHTML = rows.length? rows.map(c=>{
    const mem=c.members.map(m=>'<tr><td>'+(m.name||'<span class=muted>(no name)</span>')+'</td><td class=vurl>'+(m.website||'')+'</td><td>'+(m.country||'')+'</td><td>'+(m.vertical||'<span class=muted>—</span>')+'</td></tr>').join("");
    return '<div class="cluster"><div class="ctop"><span class="tag '+c.bucket+'">'+c.bucket.replace("_"," ")+'</span><span class="tag hx">'+c.hierarchy+'</span><span class="tag hx">'+c.path+'</span><span class="size">'+c.size+' records</span></div><table><tr><th>Name</th><th>Website</th><th>Country</th><th>Vertical</th></tr>'+mem+'</table></div>';
  }).join("") : '<div class="cluster muted">No clusters match this filter.</div>';
}
render();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="Scandit")
    ap.add_argument("--csv", help="replay a cached export instead of querying")
    ap.add_argument("--profile", action="append", required=True, help="repeatable")
    ap.add_argument("--save-csv", help="write the fetched rows here for replay")
    ap.add_argument("--html", help="write a self-contained HTML review report here")
    args = ap.parse_args()

    records = load_csv(args.csv) if args.csv else fetch_via_sf(args.org)
    print(f"[fetch] {len(records):,} account records loaded.")
    if args.save_csv and not args.csv:
        with open(args.save_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=records[0].keys())
            w.writeheader()
            w.writerows(records)
        print(f"[fetch] cached to {args.save_csv}")

    results = [run_profile(p, records) for p in args.profile]

    if args.html:
        write_html(args.html, args.csv or args.org, len(records), results)
        print(f"\n[html] wrote review report -> {args.html}  (open it in a browser)")

    print("\nDRY-RUN ONLY — no records were modified.")


if __name__ == "__main__":
    main()
