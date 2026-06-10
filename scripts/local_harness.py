#!/usr/bin/env python3
"""
Local end-to-end harness — connect -> scan -> review, with ZERO external setup.

This is a self-contained way to test the full product flow on your machine without
Supabase, a Salesforce Connected App, OAuth, or any deploy. The "connection" step
is your existing `sf` CLI auth (whatever orgs you're already logged into), so there
are no credentials to create and nothing to commit.

  Connect  ->  GET  /            lists your authed `sf` orgs (real connection)
  Configure->  the form picks an org + one or more match profiles
  Scan     ->  POST /scan        pulls Accounts (live via sf, or a cached CSV) and
                                  runs the real MatchEngine
  Review   ->  GET  /review      the production-style cluster review UI, view-only

It NEVER writes to Salesforce. It is the same engine the app uses; only the auth/
storage plumbing (Supabase + Connected App) is swapped for your local sf session.

Run:
  cd api && source venv/bin/activate && cd ..
  python3 scripts/local_harness.py            # -> http://localhost:8765
  # then open the browser, pick an org + profile(s), Run scan.

Reuses a cached export at /tmp/scandit_accounts_cache.csv when present (so testing
doesn't re-hit prod); tick "force live" to re-pull.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from app.services.match_engine import MatchEngine, MatchProfile  # noqa: E402
from dry_run_accounts import fetch_via_sf, load_csv, _HTML_TEMPLATE  # noqa: E402

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402
import uvicorn  # noqa: E402

app = FastAPI(title="Dedupe local harness")

CACHE = "/tmp/scandit_accounts_cache.csv"
PROFILE_GLOB = os.path.join(ROOT, "api", "profiles", "**", "*.json")

# in-memory result store (single operator, local only)
STATE: dict = {"results": None, "source": None, "n": 0}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def list_orgs() -> list[dict]:
    """Real connection state: the orgs your `sf` CLI is authed to."""
    try:
        proc = subprocess.run(
            ["sf", "org", "list", "--json"], capture_output=True, text=True, timeout=30
        )
        data = json.loads(proc.stdout)
        out = []
        for group in data.get("result", {}).values():
            if not isinstance(group, list):
                continue
            for o in group:
                out.append({
                    "alias": o.get("alias") or o.get("username"),
                    "username": o.get("username"),
                    "status": o.get("connectedStatus") or o.get("status") or "",
                })
        # de-dupe by alias, keep connected first
        seen, uniq = set(), []
        for o in sorted(out, key=lambda x: x["status"] != "Connected"):
            key = o["alias"]
            if key and key not in seen:
                seen.add(key)
                uniq.append(o)
        return uniq
    except Exception as e:  # noqa: BLE001
        return [{"alias": f"(sf org list failed: {e})", "username": "", "status": ""}]


def list_profiles() -> list[dict]:
    out = []
    for path in sorted(glob.glob(PROFILE_GLOB, recursive=True)):
        try:
            d = json.load(open(path))
            out.append({"path": path, "rel": os.path.relpath(path, ROOT),
                        "version": d.get("version", os.path.basename(path)),
                        "object": d.get("object_type", "?")})
        except Exception:  # noqa: BLE001
            continue
    return out


def build_review_payload(source, n, results) -> str:
    profiles = []
    for profile, result in results:
        clusters = []
        for c in result.clusters:
            if not c.is_dupe:
                continue
            clusters.append({
                "vstatus": c.verification_status, "vreason": c.verification_reason,
                "bucket": c.bucket, "hierarchy": c.hierarchy_class, "path": c.match_path,
                "size": len(c.member_ids),
                "members": [{"name": m.get("Name"), "website": m.get("Website"),
                             "country": m.get("BillingCountryCode"),
                             "vertical": m.get("Vertical__c"), "id": m.get("Id")}
                            for m in c.members],
            })
        clusters.sort(key=lambda x: x["size"], reverse=True)
        profiles.append({"version": profile.version, "stats": result.stats, "clusters": clusters})
    payload = {"source": str(source), "n": n, "profiles": profiles}
    return _HTML_TEMPLATE.replace("/*__DATA__*/", json.dumps(payload))


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
CONNECT_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>Dedupe harness — Connect</title><style>
body{{font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9;color:#0f172a;margin:0}}
header{{background:#0f172a;color:#fff;padding:18px 26px}}header h1{{margin:0;font-size:18px}}
.wrap{{max-width:760px;margin:0 auto;padding:26px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px 22px;margin-bottom:18px}}
h2{{font-size:14px;text-transform:uppercase;color:#64748b;letter-spacing:.04em;margin:0 0 12px}}
label{{display:block;padding:9px 12px;border:1px solid #e2e8f0;border-radius:9px;margin-bottom:8px;cursor:pointer}}
label:hover{{border-color:#38bdf8}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px}}
.ok{{background:#22c55e}}.bad{{background:#cbd5e1}}
.mut{{color:#64748b;font-size:13px}}
button{{background:#0ea5e9;color:#fff;border:0;border-radius:9px;padding:12px 22px;font-size:15px;font-weight:700;cursor:pointer}}
.banner{{background:#fef9c3;color:#854d0e;border:1px solid #fde68a;border-radius:9px;padding:10px 14px;font-size:13px;margin-bottom:18px}}
code{{background:#f1f5f9;padding:1px 5px;border-radius:4px}}
</style></head><body>
<header><h1>CRM Dedupe — Local Harness</h1></header>
<div class=wrap>
<div class=banner><b>Dry-run only.</b> Connection = your existing <code>sf</code> CLI auth. No Supabase, no OAuth,
nothing written to Salesforce — ever.</div>
<form method=post action=/scan>
<div class=card><h2>1 · Connection — your authed Salesforce orgs</h2>
{orgs}
</div>
<div class=card><h2>2 · Match profile(s)</h2>
{profiles}
</div>
<div class=card><h2>3 · Data source</h2>
<label><input type=checkbox name=force_live value=1> Force a live pull from Salesforce
<span class=mut>(default: reuse the cached export at <code>/tmp/scandit_accounts_cache.csv</code> if it exists)</span></label>
</div>
<button type=submit>Run scan &rarr;</button>
</form></div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def connect():
    orgs = list_orgs()
    org_html = "".join(
        f'<label><input type=radio name=org value="{o["alias"]}"'
        f'{" checked" if i == 0 else ""}>'
        f'<span class="dot {"ok" if o["status"]=="Connected" else "bad"}"></span>'
        f'<b>{o["alias"]}</b> <span class=mut>{o["username"]} · {o["status"] or "unknown"}</span></label>'
        for i, o in enumerate(orgs)
    )
    profs = list_profiles()
    prof_html = "".join(
        f'<label><input type=checkbox name=profile value="{p["path"]}"'
        f'{" checked" if "v3" in p["version"] else ""}> '
        f'<b>{p["version"]}</b> <span class=mut>· {p["object"]} · {p["rel"]}</span></label>'
        for p in profs
    ) or '<p class=mut>No profiles found under api/profiles/.</p>'
    return CONNECT_PAGE.format(orgs=org_html, profiles=prof_html)


@app.post("/scan")
def scan(org: str = Form(...), profile: list[str] = Form(default=[]),
         force_live: str = Form(default="")):
    if not profile:
        return HTMLResponse("<p>Pick at least one profile. <a href=/>back</a></p>")
    use_cache = (not force_live) and os.path.exists(CACHE) and org.lower() == "scandit"
    if use_cache:
        records = load_csv(CACHE)
        source = f"{org} (cached export)"
    else:
        records = fetch_via_sf(org)
        source = f"{org} (live)"
    results = []
    for path in profile:
        p = MatchProfile.from_json(path)
        results.append((p, MatchEngine(p).find_clusters(records)))
    STATE.update(results=results, source=source, n=len(records))
    return RedirectResponse("/review", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review():
    if not STATE["results"]:
        return RedirectResponse("/", status_code=303)
    return build_review_payload(STATE["source"], STATE["n"], STATE["results"])


if __name__ == "__main__":
    print("Dedupe local harness -> http://localhost:8765  (Ctrl-C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
