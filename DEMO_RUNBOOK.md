# Live UI Demo Runbook — connect + dedupe LSDevBox yourself

Goal: **you** drive the real UI: log in → **Connect Salesforce** (LSDevBox sandbox, real
OAuth) → scan contacts → review duplicate clusters → **click Merge → real sandbox merge**.

Branch: `feat/accounts-config-match-dryrun`.

---

## Status (already done for you)

- ✅ Backend running: http://127.0.0.1:8000 (`/health` healthy, 33/33 tests pass)
- ✅ Frontend running: http://localhost:3000 (login loads)
- ✅ `.env.local` created (Supabase URL + public anon key from the Netlify bundle)
- ✅ **OAuth app registered 100% by CLI** — a Salesforce **External Client App** (ECA) on
  LSDevBox. No Connected App, no Setup wizard, no Support case, no partnership.
  - Consumer key auto-retrieved + wired into `api/.env` + `.env.local`
  - **Public client + PKCE, no secret anywhere** (Salesforce forces PKCE on new apps)
  - Policy: `AllSelfAuthorized` (any user self-authorizes) · verified: authorize request
    returns the consent page (client id valid)
- ✅ **Tool now implements PKCE** (S256) end to end — connect page → callback → backend token exchange
- ✅ **14 demo contacts seeded in LSDevBox** → 5 clusters: Smith/Lee/Chen/Johnson ×2, **Martinez ×3**
  (tagged `Department='DEMOSEED0707'`). Reusable: `scripts/demo_seed_contacts.apex`
- ✅ Review modal no longer says "HubSpot"
- ✅ Fallback ready: `scripts/seed_demo_connection.py` (seeds the connection if live OAuth wobbles)

LSDevBox org id `00DVA00000Aezbh2AB` · login host `https://leanscale--devbox.sandbox.my.salesforce.com`
ECA source: `/tmp/eca` (deployable) · consumer key in the env files.

---

## The ONLY remaining setup — allow localhost login in Supabase (~1 min)

Supabase project `bpjgstwayjhsmaaxrwru` → **Authentication → URL Configuration**:
- **Redirect URLs** must include `http://localhost:3000/**` (the magic-link login lands at
  `http://localhost:3000/auth/callback`). Likely already there from earlier local dev — just confirm.

Everything else is wired. No Salesforce steps remain.

---

## Drive the demo (you, in the browser)

1. **http://localhost:3000** → enter **your email** → **Send magic link** → click the link → lands on `/connect`.
   *(Use your own email — a fresh user with zero connections, so `/scan` stays clean.)*
2. **Connect Salesforce** → the sandbox login/consent screen → **Allow** → back to `/connect` showing **"Salesforce Connected"** (Org `00DVA00000Aezbh2AB`). This is the real ECA OAuth + PKCE flow.
3. **Start Deduplication Scan** → Object type **Contacts** → winner rules (Oldest Created / Most Associations) → confidence 90% → **Start**.
4. Auto-advances to **Review**: **5 duplicate clusters** with confidence pills. Open **Details** on **Martinez (3 records)** — the Winner/Loser/Merged table; click any cell to pick the surviving value.
5. **Select** clusters → **Merge N selected** → confirm. (Selection = approval; merge gate only runs `approved` sets.)
6. **Merge progress** screen → completion tiles. Losers are really merged in LSDevBox.

Verify (optional): `sf data query -o LSDevBox --query "SELECT COUNT(Id) c FROM Contact WHERE Department='DEMOSEED0707'"` — count drops as losers absorb (Martinez 3→1).

---

## If live OAuth wobbles (fallback — you still drive scan→review→merge)

After logging in once (step 1):
```
cd "/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool"
./api/venv/bin/python scripts/seed_demo_connection.py --email <your-login-email> --org LSDevBox
```
Refresh `/connect` → "Salesforce Connected" → continue from step 3. (Skips only the OAuth click.)

Known low-risk snags:
- If a connect throws an **IP error**, relax the ECA policy: in `/tmp/eca` set
  `ipRelaxationPolicyType` → `Relax` and `sf project deploy start -o LSDevBox -d force-app/main/default/extlClntAppOauthPolicies`.
- If the sandbox token in the fallback expires (~2h), just re-run the seed script.

---

## Re-seed demo dupes (if a rehearsal already merged them away)
```
sf apex run -o LSDevBox -f scripts/demo_seed_contacts.apex
```

## Cleanup after the demo
```
sf data delete bulk -o LSDevBox -s Contact -q "SELECT Id FROM Contact WHERE Department='DEMOSEED0707'"
```

## Productionizing the OAuth (the real answer to "no manual setup")
The ECA above is the model: **one** vendor-owned app, created + managed as code (Metadata API),
every client self-authorizes with one click — no per-customer setup. For production, deploy the
same ECA into a **stable LeanScale-owned org** (not a refreshable sandbox) and point the login host
at `login.salesforce.com` (prod) / `test.salesforce.com` (sandbox). HubSpot follows the same pattern:
one app in the LeanScale HubSpot dev account. `SALESFORCE_LOGIN_URL` / `NEXT_PUBLIC_SALESFORCE_LOGIN_URL`
already make the host configurable.

## Not pushed
7 prior commits + today's changes are local-only. `.env.local` / `api/.env` are gitignored.
