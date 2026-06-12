# Live Integration Test — connect → scan → approve → merge (real auth + tenancy + gate + backup)

This runbook stands up the app against a **live Supabase project** and a **Salesforce
sandbox** and runs the full end-to-end flow through the harness
`scripts/integration_e2e.py`, asserting the safety invariants — not just that calls
return 200. Until now nothing was integration-tested (only unit + static); this closes
that gap.

> **What's different from `LOCAL_DEV.md`:** that runbook is for manually clicking through
> the account **dry-run**. This one drives the **contacts merge** path programmatically
> through real `require_user` auth, the tenant-isolation guard, the approval gate, and the
> pre-merge backup — with **no bypass** (unlike `scripts/local_harness.py`, which swaps out
> auth/storage).

---

## Safety model (read first)

- **Default mode is DRY-RUN.** The harness does everything *except* the irreversible
  Salesforce merge. It exercises auth, tenancy, scan, results, approval, and the
  negative tests with **zero Salesforce writes**.
- A real merge requires **all** of: `--execute` **and** `--i-confirm-sandbox` **and** a
  live, **positive** `Organization.IsSandbox = true` confirmation **and** an
  instance_url that does **not** look like a production login domain. A failed/ambiguous
  org query *aborts* `--execute` — there is no path to a production merge.
- The harness seeds its own disposable duplicate contacts (`LastName LIKE 'ZZZE2E%'`)
  and cleans them up. It only ever deletes rows it created. **Point it at a throwaway
  developer sandbox, never production.**

---

## Prerequisites

| Need | How |
|---|---|
| Migrations 003–005 applied to the live DB | Step 1 below |
| `SUPABASE_JWT_SECRET` in `api/.env` | Step 2 (the API fails **closed** with a 500 without it) |
| `ENCRYPTION_KEY` in `api/.env` | Already present (64-hex). Must match the running API |
| `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` | Already in `api/.env` |
| A Salesforce **sandbox** the `sf` CLI can reach | Step 4 |
| The running SF user has the **Merge Contacts** perm | Only needed for `--execute`; otherwise the merge ends `failed` and the harness reports `error_log` (not a false pass) |

---

## Step 1 — Apply the schema to the live Supabase project

In the Supabase dashboard for the target project → **SQL Editor → New query** → paste the
**entire** contents of:

```
supabase/migrations/LIVE_apply_003-005.sql
```

…and **Run**. This bundles migrations 003 + 004 + 005 (the live DB is assumed to be on
**001 only**). It is **idempotent** — if the SQL editor times out or you're unsure it
finished, just paste and Run it again; it will not error or create duplicate tenants.

> **002 is intentionally skipped.** It only widens `scans.object_type` to allow
> `accounts`/`leads`. This integration test uses the **contacts** path (the only object
> with a real merge — accounts are a view-only dry-run). If you *also* want to exercise the
> accounts dry-run against this DB later, separately apply
> `supabase/migrations/002_accounts_object_type.sql` (it just re-adds a widened CHECK;
> nothing destructive). Without 002, an `accounts` scan is correctly rejected by the DB.

---

## Step 2 — Provision `api/.env`

The backend reads these (pydantic settings → env names):

```bash
# in api/.env — the harness and the API must see the SAME values
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service_role key>          # bypasses RLS; seeds + backend
SUPABASE_JWT_SECRET=<Project Settings → API → JWT Settings>   # HS256 secret auth.py verifies
ENCRYPTION_KEY=<64+ hex chars>                   # already set; must match the running API
```

`SUPABASE_JWT_SECRET` is the one most likely to be missing — copy it from
**Supabase → Project Settings → API → JWT Settings** and **restart the API** after setting it.

---

## Step 3 — Start the API

```bash
cd api
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt   # first time
uvicorn app.main:app --reload --port 8000
```

Leave it running. The harness talks to it at `http://localhost:8000` (override with
`--api` / `API_BASE_URL`). The API process must see the same `SUPABASE_*` / `ENCRYPTION_KEY`
env as the harness (it auto-loads `api/.env`).

---

## Step 4 — Get a Salesforce **sandbox** session

No Connected App / OAuth dance needed — the harness seeds an encrypted connection directly
from a CLI session token:

```bash
# log the sf CLI into a SANDBOX (test.salesforce.com), once:
sf org login web --instance-url https://test.salesforce.com --alias <sandboxAlias>

# then export the session for the harness:
SF_JSON=$(sf org display --json --target-org <sandboxAlias>)
export SF_ACCESS_TOKEN=$(echo "$SF_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["accessToken"])')
export SF_INSTANCE_URL=$(echo "$SF_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["instanceUrl"])')
```

> `sf` session tokens expire (~2h). If a run fails mid-way with an auth error, re-export and
> re-run (the harness is idempotent).

---

## Step 5 — Run the harness

Run it with the **api venv** active so `from app.services.encryption import encrypt_token`
resolves (a pure-Fernet fallback covers environments where it can't):

```bash
# from the repo root, api venv active
# 1) DRY RUN first — full flow through approve, NO Salesforce merge:
python3 scripts/integration_e2e.py

# 2) Then the real thing against the sandbox (irreversible merge of the seeded dupes):
python3 scripts/integration_e2e.py --execute --i-confirm-sandbox
```

Useful flags: `--keep` (leave seeded rows for inspection), `--keep-connection` (reuse the
seeded tenant/connection across runs), `--no-seed` (you supply throwaway dupes),
`--scan-timeout` / `--merge-timeout`, `--delete-users` (also remove the seeded `auth.users`).
See `--help`.

---

## What it asserts

- **Auth is real, not bypassed** — an unauthenticated `/scan/start` is rejected 401/403; every
  request carries a JWT minted to match `app/auth.py` exactly (HS256, `aud=authenticated`,
  `exp`+`sub`, `sub`=`auth.users` id), self-verified before use.
- **Tenant isolation** — a second user with no membership (and not `platform_staff`) gets
  403/404 on `/scan/{id}/status`, `/results`, and `/merge/execute`.
- **Approval gate (request time)** — `/merge/execute` with nothing approved returns 400.
- **Approval gate (execution time)** — with set A approved and set B left unapproved, only A
  merges; B stays `decision != 'merged'`, `merged=False`, and its sandbox loser still exists.
- **Only the approved set merges** — A ends `merged=True`, `decision='merged'`.
- **Backup-as-precondition** — a `merge_backups` row exists for the merged set with the correct
  `winner_record_id` and the full original `loser_record_ids` snapshot.
- **Full vs partial coverage** — `merged=True` only when `merged_loser_ids == loser_record_ids`.
- **Salesforce ground truth** — every loser is gone (404) and the winner survives.
- **Final report** — auto-generated and located via `report_data['merge']['id']` in `/reports/mine`.

On success it prints `ASSERTIONS PASSED`; any failure exits non-zero with a `[FAIL]` line.

---

## Teardown & troubleshooting

- **Teardown is automatic** and safe: it deletes only the ids it captured plus a
  `ZZZE2E%`-marked contact sweep (harness-specific — cannot match real data); seeded scans
  cascade-delete their `duplicate_sets`/`merges`/`merge_backups`. `auth.users` are kept unless
  `--delete-users`. If hard-killed before teardown, the next run cleans up via the marker.
- **`500 Auth not configured`** → `SUPABASE_JWT_SECRET` isn't set / the API wasn't restarted.
- **`Expected 401/403 for an unauthenticated /scan/start`** → `require_user` isn't wired; the
  harness refuses to continue (it would not be a real test).
- **`Organization.IsSandbox is ... (not a confirmed sandbox)`** → you're pointed at a non-sandbox
  (or the org query failed). The harness will not `--execute` against it. This is the gate working.
- **Merge ends `failed` with `error_log`** → the sandbox user lacks the **Merge Contacts**
  permission. Grant it (or read the snapshot — the harness reports honestly, no false pass).
- **Slow scan** → `SalesforceContactsService` fetches *all* contacts (no WHERE clause); use a
  small/empty developer sandbox or raise `--scan-timeout`.
