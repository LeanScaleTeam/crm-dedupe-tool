# Deployment Runbook — crm-dedupe-tool (live, distributable)

Target: a single shared, **invite-only multi-tenant** deployment for a select few clients.

| Layer | Host | Domain |
|---|---|---|
| Frontend (Next.js) | **Netlify** | `dedupe.leanscale.team` |
| Backend + worker (FastAPI) | **Railway** (Docker) | `api.dedupe.leanscale.team` |
| DB + Auth | **Supabase** (fresh prod project) | — |

The OAuth **callback lands on the frontend** (`/api/{crm}/callback` is a Next.js route), then calls the backend to exchange the code. So every redirect URI is on the **frontend** domain:
`https://dedupe.leanscale.team/api/hubspot/callback` and `.../api/salesforce/callback`.

---

## Step 1 — Supabase (fresh project)
1. Create a new Supabase project (a prod-tier one; note the region).
2. SQL Editor → paste **`supabase/migrations/apply_all_fresh.sql`** → Run. (Migrations 001–007 in order; clean schema, no dev data.)
3. Authentication → Providers/Settings → **disable public sign-ups** (invite-only). Add the allowed redirect/site URL `https://dedupe.leanscale.team`.
4. Project Settings → API → copy: **Project URL**, **anon key**, **service_role key**, and **JWT secret**.

## Step 2 — Backend on Railway
1. New project → Deploy from the GitHub repo → set **root directory = `api/`** (it has the `Dockerfile` + `railway.toml`; healthcheck `/health`).
2. Set env vars (see table below). Generate a **fresh** encryption key — do NOT reuse dev:
   `python -c "import secrets; print(secrets.token_hex(32))"`
3. Deploy → confirm the healthcheck passes → add custom domain `api.dedupe.leanscale.team` (Railway gives you a CNAME target).
4. Add the **worker** as a second Railway service from the same repo if/when background scans move off in-process (uses `Dockerfile.worker`). Not required for launch (scans run in-process today).

## Step 3 — Frontend on Netlify
1. New site → connect the GitHub repo (`netlify.toml` is picked up automatically: `npm run build`, Next.js plugin).
2. Set env vars (table below).
3. Deploy → add custom domain `dedupe.leanscale.team`.

## Step 4 — DNS
- `dedupe.leanscale.team` → CNAME to the Netlify site target.
- `api.dedupe.leanscale.team` → CNAME to the Railway domain target.
- Wait for HTTPS certs to issue on both.

## Step 5 — OAuth apps (point redirect URIs at prod)
**HubSpot** (`leanscale-dedupe-app/src/app/app-hsmeta.json`):
- Add `https://dedupe.leanscale.team/api/hubspot/callback` to `redirectUrls` (keep localhost for dev).
- `hs project upload` to redeploy the app on portal 39681069.

**Salesforce** (the "LeanScale Dedupe" classic connected app):
- Setup → App Manager → the connected app → add `https://dedupe.leanscale.team/api/salesforce/callback` to Callback URLs.

Both `*_REDIRECT_URI` backend env vars must match these exact strings.

## Step 6 — Invite clients (self-serve, controlled)
- Supabase → Authentication → Users → invite each client user by email (magic-link/password). Public signup stays OFF.
- Each client then: log in → Connect HubSpot/Salesforce (authorizes their own org) → scan → review → merge.

## Step 7 — Smoke test (before handing to a client)
1. Log in on `dedupe.leanscale.team`.
2. Connect a **test** org (HubSpot test portal / SF sandbox).
3. Run a contacts scan → review → **dry-run first**; do a single real merge on the test org → confirm the report + restore CSV.
4. Confirm a forced failure shows the real reason (error visibility).

---

## Env vars

### Railway (backend) — all are **secrets**, set in Railway (never commit)
| Var | Value / source |
|---|---|
| `SUPABASE_URL` | fresh project URL |
| `SUPABASE_SERVICE_KEY` | fresh project **service_role** key |
| `SUPABASE_JWT_SECRET` | fresh project JWT secret |
| `ENCRYPTION_KEY` | freshly generated 32-byte hex (see Step 2) |
| `SALESFORCE_CLIENT_ID` / `SALESFORCE_CLIENT_SECRET` | LeanScale Dedupe connected app |
| `SALESFORCE_REDIRECT_URI` | `https://dedupe.leanscale.team/api/salesforce/callback` |
| `SALESFORCE_LOGIN_URL` | `https://login.salesforce.com` |
| `HUBSPOT_CLIENT_ID` / `HUBSPOT_CLIENT_SECRET` | LeanScale Dedupe HubSpot app |
| `HUBSPOT_REDIRECT_URI` | `https://dedupe.leanscale.team/api/hubspot/callback` |
| `ENVIRONMENT` | `production` |

### Netlify (frontend) — `NEXT_PUBLIC_*` ship to the browser (anon key is public by design)
| Var | Value / source |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | fresh project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | fresh project **anon** key |
| `NEXT_PUBLIC_API_URL` | `https://api.dedupe.leanscale.team` |
| `NEXT_PUBLIC_SALESFORCE_LOGIN_URL` | `https://login.salesforce.com` |
| `NEXT_PUBLIC_SALESFORCE_CLIENT_ID` | connected app consumer key |
| `NEXT_PUBLIC_HUBSPOT_CLIENT_ID` | HubSpot app client id |

---

## Security checklist (before real client data)
- [ ] All secrets in host env managers — **no `.env` in the deploy** (gitignored).
- [ ] Fresh `ENCRYPTION_KEY` (tokens are Fernet-encrypted at rest); never reuse dev.
- [ ] Public signup OFF (invite-only).
- [ ] HTTPS on both domains; redirect URIs are `https://`.
- [ ] Merge defaults: approval-gated, dry-run available, pre-merge backups + restore CSV in place.
- [ ] Tenant isolation verified (service-role bypasses RLS; access enforced in `app/services/tenancy.py`).
- [ ] HubSpot unlisted app install cap is 25 — fine for a few clients; listing/review needed beyond that.

## Deferred hardening (not blocking launch)
- Structured error codes (transient vs permanent → auto-retry).
- Server-side validation of picked merge fields.
- Move scans/merges to the Railway **worker** for long-running jobs (currently in-process).
- Per-merge audit log surfaced to clients.
