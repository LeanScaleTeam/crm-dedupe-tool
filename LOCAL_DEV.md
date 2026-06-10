# Local Dev Runbook — stand up the app + test the account dry-run

Goal: run the full Next.js + FastAPI app locally and reach the **Accounts → view-only
dry-run → review** flow on a real Salesforce org. ~30–45 min the first time.

> **Just want to see matches with zero setup?** Skip all of this and run the CLI:
> ```bash
> python3 scripts/dry_run_accounts.py --org <sfOrgAlias> \
>   --profile api/profiles/scandit/account_v3.json --html /tmp/report.html
> ```
> Open `/tmp/report.html`. Needs only the `sf` CLI authed to the org. No app, no DB, no writes.

---

## 0. Prerequisites

- **Node.js 20+**, **Python 3.11+**
- A **Supabase project** (free tier is fine) — for auth + the scan/result tables
- A **Salesforce Connected App** in (or with access to) the org you want to scan
- Redis is **not** required yet — scans currently run in-process via FastAPI
  `BackgroundTasks` (Celery is scaffolded but unused). Leave `REDIS_URL` at its default.

This branch (`feat/accounts-config-match-dryrun`) hardcodes the Salesforce auth domain to
`login.salesforce.com` (production orgs). Sandbox support lives on a separate branch.

---

## 1. Supabase (DB + auth)

1. Create a project at supabase.com. From **Project Settings → API**, copy:
   - Project URL → `SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_URL`
   - `anon` public key → `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   - `service_role` key → `SUPABASE_SERVICE_KEY` (backend only — never ship to the browser)
2. **SQL Editor → New query** → paste and run, in order:
   - `supabase/migrations/001_initial_schema.sql`
   - `supabase/migrations/002_accounts_object_type.sql`  ← enables `accounts`
3. **Authentication → Providers → Email**: enable it (magic-link login). Under
   **Authentication → URL Configuration**, set **Site URL** = `http://localhost:3000`.

---

## 2. Salesforce Connected App (OAuth)

In the target org: **Setup → App Manager → New Connected App** (or **External Client App**):

- Enable OAuth Settings
- **Callback URL:** `http://localhost:3000/api/salesforce/callback`
- **OAuth Scopes:** `Manage user data via APIs (api)`, `Perform requests at any time (refresh_token, offline_access)`
- Save, then copy the **Consumer Key** → `SALESFORCE_CLIENT_ID` (+ `NEXT_PUBLIC_SALESFORCE_CLIENT_ID`)
  and **Consumer Secret** → `SALESFORCE_CLIENT_SECRET`

---

## 3. Environment files

### `.env.local` (repo root — frontend)
```bash
cp .env.local.example .env.local
```
| Var | Value |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon key |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` |
| `NEXT_PUBLIC_SALESFORCE_CLIENT_ID` | Connected App consumer key |
| `SUPABASE_SERVICE_KEY` | Supabase service_role key |
| `SALESFORCE_CLIENT_SECRET` | Connected App consumer secret |

(HubSpot vars can stay as placeholders — not needed for accounts.)

### `api/.env` (backend)
```bash
cp api/.env.example api/.env
# generate the token-encryption key:
python3 -c "import secrets; print(secrets.token_hex(32))"
```
| Var | Value |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service_role key |
| `SALESFORCE_CLIENT_ID` | Connected App consumer key |
| `SALESFORCE_CLIENT_SECRET` | Connected App consumer secret |
| `SALESFORCE_REDIRECT_URI` | `http://localhost:3000/api/salesforce/callback` |
| `ENCRYPTION_KEY` | the 64-char hex string from the command above |
| `ENVIRONMENT` | `development` |
| `REDIS_URL` | leave default (unused) |

---

## 4. Run it

**Backend** (terminal 1):
```bash
cd api
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
Health check: `curl localhost:8000/health` (or whatever the health router exposes).

**Frontend** (terminal 2):
```bash
npm install
npm run dev      # http://localhost:3000
```

---

## 5. Smoke test — the account dry-run

1. Open `http://localhost:3000` → enter your email → click the **magic link** from your inbox.
2. **/connect** → connect **Salesforce** → approve the OAuth screen (the target org).
3. **Configure scan:**
   - Object type → **Accounts**
   - Match profile → **Scandit — … Vertical discriminator (V3)** (or V2 baseline)
   - Start the scan.
4. Watch progress (fetches via `queryAll`, runs the match engine), then land on **Review**.
5. You should see duplicate **clusters** — account Name + `website · country · vertical`,
   confidence, and a **"Dry-run — view only"** badge where the merge button would be.
   **No merge button exists for accounts** — nothing can write.

### Expected (Scandit prod, ~48k accounts)
V3 profile ≈ **311 dupe clusters / 630 accounts**, 84 Vertical-discriminator vetoes.
(V2 ≈ 379 / 768.) These should track the CLI dry-run output.

---

## 6. Notes / known rough edges (this branch)

- **Auth:** the backend currently trusts `user_id` from the request body + uses the
  service-role key (RLS bypass). Fine for **local, single-operator** use; must be fixed
  before any external/multi-tenant deployment (see `06-Cross-Client-Build-Plan.md`).
- **Scale:** 48k accounts loads in memory for the dry-run — fine. 100k+ contacts would
  OOM (out-of-core blocking is a build-plan item, not done).
- **Accounts = Salesforce only** right now (HubSpot accounts later).
- **No writes anywhere in the account path** — `queryAll` read + dry-run review only.
- Magic-link not arriving? Check Supabase **Auth → Email** is enabled and Site URL is
  `http://localhost:3000`.
