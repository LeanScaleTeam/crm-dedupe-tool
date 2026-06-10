<!-- Generated 2026-06-10 via requirements-recheck workflow (11 agents). Safety findings in the intro + §E were code-verified against merge.py/scan.py/salesforce_merge.py. Design sections (§C–§F) are proposals pending the §H decisions. -->

# Cross-Client Salesforce Dedupe + Merge Tool — Consolidated Requirements & Design Spec

**Status:** V1 design, rules-only. Repo: `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool` (branch `feat/accounts-config-match-dryrun`). First tenant: Scandit. Object focus this pass: **Accounts → Contacts → Leads** (Opportunities = V2).

> **READ THIS FIRST — the load-bearing correction.** Every design doc assumed "the verification gate guards the merge button." **It does not.** `execute_merge` (`merge.py:144-187`) and `run_merge` (`merge.py:48-50`) select sets by `excluded=False AND merged=False` only — `verification_status`, `bucket`, and `certainty` are never read in the merge path. A `MergeRequest` with `set_ids=None` (the default) bulk-merges *every* non-excluded set. Combined with the account winner being an arbitrary `members[0]` (`scan.py:82`), the auth bypass (`user_id` from request body + service-role key, `scan.py:35`/`merge.py:17`), and the total absence of any pre-merge backup, **today's code can irreversibly destroy the wrong master records across any tenant from one unauthenticated request.** These four items (R1–R4 in §E) are absolute prerequisites before account merge or any external client goes live. Nothing else in this spec ships before them.

---

## A. Reconciled Requirements

The user's 9 new requirements map onto (and are a subset of) the committed methodology. They are restated below, merged with the original-doc requirements, with conflicts flagged for your confirmation in §H.

### A.1 The 9 new requirements (north-star), reconciled

| # | New requirement (user's words → reconciled) | Merged with original-doc requirement |
|---|---|---|
| 1 | **Standard matching modes** (exact/fuzzy per field) | Per-field mode = `fuzzy` \| `exact` \| `picklist` (#8). The account engine has this; **the contact engine does not** — it's still a hardcoded email-60/name-40 blend (`dedup_engine.py:115`). |
| 2 | **Custom-field selection + per-field method** | Config-as-data exists (`MatchProfile`); a **field-picker UI does not** — profiles are hand-edited JSON. Custom SF fields (`Vertical__c`, `SCD_NetSuite_*`) are first-class (#7, #25). |
| 3 | **Rule / formula composition** (AND/OR) | Composite rules with required/optional fields (#7), discriminators (#9), conditional matching (#11). The full AND/OR clause tree is **new design** (§C). |
| 4 | **Login / auth** | Frontend auth BUILT; backend auth is the **bypass** (#30). The "multi-tenant" rework riding on this is an **undecided** architecture call (§H.1). |
| 5 | **"Set up your model" UI** | The matching-rule builder (§C). Does **not** yet cover the master-record / owner-reassignment rules (#15–#17) — those are not in the engine. |
| 6 | **See your batches** | A runs/batches index — **missing** (only get-by-id today). |
| 7 | **Auto-merge vs manual-check by accuracy** | The verification gate (#16, #18) — but must reconcile the user's **binary** ask with the committed **three-bucket** output (safe / needs-review / known-active) + **escalation** as a distinct terminal state (§H.4). |
| 8 | **Merge button** ("merge all the records") | Contact merge BUILT; **account merge missing**. "Merge all" can only mean "merge all *auto-eligible, non-escalated* sets" (#15.3/#16/#18) — confirm this interpretation (§H.5). |
| 9 | **Backup in case it's needed** | Pre-merge snapshot (#19). **Hard constraint: true undo is impossible** (#20) — "backup" = forensic snapshot + recreate-as-stub, never a reversible merge. Clients must be told this in writing. |

### A.2 Original-doc requirements the 9 don't name but are committed (do not drop)

- **Discriminators / negative matches** (#9): "if Vertical differs → NOT a dupe"; "same first+last name, different email → NOT a dupe." First-class, never a weight.
- **Hierarchy classification** over ParentId (#10): tags `hierarchy_explained` / `disconnected_dupe` / `sibling_dupe` / `mixed`; only `disconnected_dupe` + `sibling_dupe` surface for merge.
- **Master-record RULES ENGINE with escalation** (#15–#16): AccountNumber → NetSuite flag → **escalate if both flags on multiple members** → owner fallback. *The methodology's "biggest behavioral change" — and currently owned by no design and built by no one.*
- **Owner reassignment on merge** (#17): SAM→Core→ISM fallback; inactive-owner → RevOps queue (Claire's amendment); Marketing/BDR → Core SDs. **Provenance unconfirmed** (cited email chain missing) — confirm before building.
- **Three-bucket eligibility** via CampaignMember + `queryAll` soft-deleted activity (#18, #23, #24).
- **Custom-field boolean plumbing** (#25): `has_account_number`, `has_netsuite_sync` — derived-boolean expressions. Generalizes to all clients.
- **CSV export to per-scan column template** (#26) — the V3 column contract.
- **Persistent exclusion list** (#22): promote per-scan exclusions to a permanent (record_a, record_b) registry.
- **Configurable false-positive filters** (#12): placeholder names, freemail domains.
- **`run with and without domain`** dual-output (#7).

### A.3 Conflicts you must confirm (full list in §H)

1. **Traction Complete: replicate vs. buy.** The north star says "replicate the config UX of SF Matching Rules + Traction Complete." The original docs **never mention TC**, decision #28 says the tool does **not** own prevention, and your own memory says "not a Traction Complete clone." **Reconciliation:** replicate TC's *matching config UX only* (field picker, per-field method, compose-into-rule, run/review/merge). Do **not** build TC's *prevention* (block-on-create) or *hierarchy management* (auto-maintaining ParentId trees) — that's RingLead/native SFDC/the TC purchase. This keeps us detection + merge + audit, consistent with #28.
2. **"Backup" ≠ "undo."** A Salesforce merge is destructive and not API-reversible. Confirm clients accept *forensic snapshot + recreate-as-stub*, not undo.
3. **"Merge all the records" vs. escalation-as-outcome.** "Merge all" = merge all auto-eligible non-escalated sets, never a literal merge-everything.
4. **Binary auto/manual vs. three-bucket + escalation.** The committed output is three-way + escalation, not binary.
5. **Multi-tenant vs. per-client** is formally undecided and four designs assume it's decided.

---

## B. Current State → Target Gap

| # | Requirement | Built today | What's missing |
|---|---|---|---|
| 1 | Standard matching modes (exact/fuzzy/picklist) | **Account engine** (`match_engine.py:228`): det/exact-fingerprint/fuzzy/picklist/veto, Union-Find, hierarchy | Contact/Lead engine still hardcoded blend (`dedup_engine.py:115`); contact discriminator (same-name/diff-email) has nowhere to run |
| 2 | Custom-field + per-field method | Config-as-data (`MatchProfile`, `account_v3.json`); custom fields bound by API name | **No field-picker UI**; no backend `describe` endpoint over the *stored OAuth token* (only the throwaway `sf`-CLI harness introspects) |
| 3 | Rule/formula composition | Profile buckets (fingerprint/det/discriminators/fuzzy); `WinnerSelector` priority rules (contacts only) | AND/OR clause tree; conditional `only_when`; optional fingerprint components; per-field fuzzy thresholds |
| 4 | Login / auth | Magic-link OTP, middleware, RLS policies written | **Backend trusts body `user_id` + service-role key (RLS bypassed)**; no tenant model |
| 5 | "Set up your model" UI | Config *selector* (`ScanConfigClient.tsx:33`, dropdown of 2 JSON files) | The builder itself; survivorship/owner-reassignment UI (and the engine behind it) |
| 6 | See your batches | Scan→results→review with pagination; merge pause/resume | **No runs/batches index**; no `GET /scan/list`; no dual-variant (with/without domain) result display |
| 7 | Auto/manual gating | `_verify` (`match_engine.py:436`) emits `verification_status` + reason + buckets | Gate **gates nothing executable** (merge path ignores it); confidence is a **flat constant** (`conf=1.0/0.9`, `:356`), not a real accuracy |
| 8 | Merge button | Contact merge BUILT (`salesforce_merge.py:53`), pause/resume | **Account/Lead merge** (REST shape same, sObject differs); winner from master-record engine; gate enforcement |
| 9 | Pre-merge backup / rollback | Nothing — no table, no snapshot, no restore | **Entire backup layer** (table, fresh pre-merge snapshot, child refs); restore tiers; "not undo" honesty in UI |
| — | Master-record rules engine (#15) | Nothing — account winner = `members[0]` (`scan.py:82`) | The full Scandit chain + escalation-as-outcome. **Blocks account merge.** |
| — | Owner reassignment (#17) | Nothing | SAM→Core→ISM / inactive→RevOps / Mkt→Core SDs. **Provenance unconfirmed.** |
| — | CampaignMember + queryAll eligibility (#18,24) | `queryAll` used on account read path | CampaignMember cross-ref query (cost at 48k scale undesigned); contact-path `queryAll` |
| — | Derived booleans (#25), CSV export (#26), exclusion registry (#22) | Nothing | All three undesigned/dropped by prior designs — now in scope (§G) |

---

## C. The Matching-Rule / "Model" Config Schema

The user authors `fields` + a `rule` formula; the engine still runs its proven four-pass spine. A **`compile()`** step bridges the new front-of-house schema down to the legacy attributes (`fingerprint`, `deterministic_keys`, `fuzzy_rules`, `discriminators`) the passes already consume. This is what lets us add a builder UI **without rewriting the Union-Find core.** Backward-compat: any profile lacking a `rule` block synthesizes one from its existing fields.

### C.1 Full example (Scandit Accounts, schema_version 4)

```json
{
  "object_type": "account",
  "version": "scandit-v4-name-country-state-vertical",
  "id_role": "Id",
  "schema_version": 4,
  "scoring": "per_field",

  "fields": {
    "name":      {"api": "Name",                       "method": "fuzzy",    "normalizer": "legal_name", "threshold": 0.88, "weight": 0.5, "label": "Account Name"},
    "domain":    {"api": "Website",                     "method": "exact",    "normalizer": "domain",   "weight": 0.4, "label": "Website Domain"},
    "country":   {"api": "BillingCountryCode",          "method": "picklist", "normalizer": "picklist", "label": "Billing Country"},
    "state":     {"api": "BillingStateCode",            "method": "picklist", "normalizer": "picklist", "label": "Billing State"},
    "phone":     {"api": "Phone",                       "method": "exact",    "normalizer": "digits",   "weight": 0.1, "label": "Phone"},
    "vertical":  {"api": "Vertical__c",                 "method": "picklist", "normalizer": "picklist", "label": "Vertical"},
    "netsuite":  {"api": "SCD_NetSuite_ID__c",          "method": "deterministic", "normalizer": "as_is", "label": "NetSuite ID"},
    "parent":    {"api": "ParentId",                    "method": "ignore",   "normalizer": "as_is"},
    "activity":  {"api": "LastActivityDate",            "method": "ignore",   "normalizer": "as_is"},
    "acct_num":  {"api": "AccountNumber",               "method": "ignore",   "normalizer": "as_is", "label": "Account Number"},
    "ns_sync":   {"api": "SCD_NetSuite_Sync_Active__c", "method": "ignore",   "normalizer": "as_is"}
  },

  "rule": {
    "op": "OR",
    "clauses": [
      {"kind": "deterministic", "field": "netsuite", "require_nonblank": true,
       "label": "Same NetSuite ID → certain match"},
      {"op": "AND", "id": "firmographic", "label": "Same name + country/state (+ optional domain)",
       "clauses": [
         {"kind": "match", "field": "name",    "required": true},
         {"kind": "match", "field": "domain",  "required": false, "run_with_and_without": true},
         {"kind": "match", "field": "country", "required": true},
         {"kind": "match", "field": "state",   "required": false,
          "only_when": {"field": "country", "in": ["US"]}}
       ]}
    ]
  },

  "discriminators": [
    {"field": "vertical", "blank_handling": "skip", "label": "Different Vertical → NOT a duplicate"}
  ],

  "derived_booleans": {
    "has_account_number": {"expr": "nonblank", "field": "acct_num"},
    "has_netsuite_sync":  {"expr": "truthy",   "field": "ns_sync"}
  },

  "filters": {
    "require_eligible": ["name"],
    "filter_placeholder_names": ["name"],
    "filter_freemail_domains": ["domain"]
  },

  "hierarchy": {"parent_role": "parent"},
  "safety":    {"activity_role": "activity", "protect_role": "ns_sync", "campaign_member_check": true},

  "survivorship": {
    "master_chain": [
      {"prefer_nonblank": "acct_num", "label": "Record with Account Number"},
      {"prefer_truthy":   "ns_sync",  "label": "Record with NetSuite sync flag"}
    ],
    "collision_rule": "escalate_if_multiple_carry_any_flag",
    "owner_chain": [
      {"if_inactive_owner": "queue:RevOps"},
      {"if_owner_role_in": ["Marketing","Marketo","BDR"], "assign": "Core SDs"},
      {"fallback_order": ["SAM","Core","ISM"]}
    ],
    "owner_chain_provenance": "UNCONFIRMED — email chain missing; do not enable until confirmed"
  },

  "verification": {
    "auto_paths": ["deterministic", "exact_fingerprint"],
    "auto_merge_threshold": 0.97,
    "require_safe_bucket": true,
    "require_discriminators_conclusive": true
  }
}
```

> **Schema-binding is mandatory.** All `api` names come from live `sf describe` of the connected org, never hardcoded. Scandit uses `AccountNumber` (standard, ~6% fill), **not** `Account_Number__c` (which does not exist in their org). The example uses Scandit's State/Country **code** picklists (`BillingCountryCode`/`BillingStateCode`); Anrok stores them as free text (`BillingCountry`/`BillingState`) — the `only_when.in` value list is therefore **org-specific** and the builder should populate it from sampled live values.

### C.2 Clause grammar

- `{"kind":"match","field":<role>,"required":<bool>,"only_when":<cond>,"run_with_and_without":<bool>}` — uses the field's own `method`.
- `{"kind":"deterministic","field":<role>,"require_nonblank":<bool>}` — exact short-circuit, confidence 1.0.
- nested group `{"op":"AND"|"OR","clauses":[...]}` — AND/OR composition.
- `only_when`: `{"field":<role>,"in":[...]}` or `{"field":<role>,"equals":<role2>}` — the conditional primitive ("State counts only when Country = US").

### C.3 UI control → schema mapping (the "Set Up Your Model" screen)

| UI control | Writes to | Notes |
|---|---|---|
| Field-picker dropdown | new `fields{}` entry | Populated by live `sf describe` over the **stored OAuth token** (new backend endpoint — see §B gap) |
| Method selector (Exact / Fuzzy / Picklist / Unique-ID / Ignore) | `fields[role].method` | Auto-suggests normalizer (Fuzzy+name→`legal_name`; Exact+website→`domain`); override allowed |
| Fuzzy threshold slider (only if Fuzzy) | `fields[role].threshold` | Falls back to top-level threshold |
| "Required" toggle | `clauses[].required` | Required = fingerprint component; optional = contributes, absence doesn't fail |
| "Run with and without this field" | `clauses[].run_with_and_without` | Emits two labeled result sets |
| AND/OR toggle at group header | `rule.op` / nested op | Top OR = "any scenario"; AND group = "all fields must match" |
| "+ Add match scenario" | appends AND-group to top-level OR | Traction-style scenario |
| "This field is a discriminator/veto" | moves field to `discriminators[]` + blank-handling (`skip`/`veto`) | First-class negative match, not a weight |
| "Only count when…" | `clauses[].only_when` | Condition dropdown populated from live picklist values |
| False-positive filters panel | `filters.*` | Placeholder names + freemail |
| Survivorship tab (separate) | `survivorship.*` | Master chain + escalation + owner chain (owner chain disabled pending provenance) |
| Auto-merge gate panel | `verification.*` | Threshold slider + safety checkboxes |

### C.4 How it compiles to the engine (the only real engine work)

1. **Deterministic clauses → `deterministic_keys`** (PASS 0, unchanged).
2. **AND-group of `required` exact/picklist clauses → a `fingerprint` variant.** **Engine change A:** `_fingerprint()` (`match_engine.py:189`) becomes `_fingerprints()` returning a *list of variants* — `[["name","country","domain"],["name","country"]]` for `run_with_and_without`. A component guarded by `only_when` evaluating false is dropped per-record (US records key on state, non-US don't).
3. **`fuzzy` clauses → `fuzzy_rules` + `blocking`.** **Engine change B:** per-field thresholds — each fuzzy field must clear *its own* `token_sort_ratio ≥ field.threshold`; the group fires when all required fuzzy fields clear their bars. Blocking auto-derived from required exact/picklist fields in the same group.
4. **Discriminators → `discriminators`** (PASS 3, unchanged).
5. **OR across scenarios → multiple edge passes into one Union-Find.** **Engine change C:** each top-level scenario runs its own edge-generation pass; all feed the same `try_union` and the same `cannot_link`. AND-within-group enforced at edge creation; OR-across-groups enforced by Union-Find.

> **Engine-change honesty:** the prior design called this "~a day." It is **not** — it rewrites fingerprint construction, replaces the blended fuzzy score with per-field thresholds, and adds a multi-scenario edge loop. Budget **3–5 days** plus tests, on a stable core. Also fix the **discriminator transitivity hole** (R8, §E) as part of this work: compute vetoes for *all* intra-cluster pairs, not just anchor-vs-member, or split clusters containing any vetoed pair at assembly time.

> **Scoring-core decision (must pick one — §H.3):** `design:rule-model` wants per-field thresholds; `design:confidence-gating` (§D) depends on a weighted blend. They are mutually exclusive. **Recommendation:** adopt **per-field thresholds for the *match decision*** (does this pair link?) and a **weighted blend for the *displayed accuracy number*** (how strong is the link?). The `scoring: "per_field"` flag selects match behavior; §D's `A(a,b)` blend is used only for the confidence percentage and the auto-merge threshold. This resolves the contradiction without rewriting either.

---

## D. Confidence → Auto-Merge vs Manual-Check Gating

Today `confidence` is a flat constant (`conf = 1.0 if fingerprint else 0.9`, `match_engine.py:356`) — it cannot tell a strong fuzzy match from a weak one, so a numeric threshold on it is meaningless. Replace it with a real per-cluster accuracy.

### D.1 The accuracy math

**Per-field strength `s_f`** (pair-level), in [0,1]:

| Mode | `s_f` |
|---|---|
| deterministic key match | 1.0 |
| exact fingerprint component (picklist/exact/legal_name equality) | 1.0 |
| fuzzy | `token_sort_ratio(va,vb)/100` |
| field blank on either side | `null` — **excluded from the average, not scored 0** (mirrors `_fuzzy_score`, so optional/missing domain doesn't tank accuracy) |

**Pair accuracy** (weighted mean over participating fields):
```
A(a,b) = Σ_f (w_f · s_f) / Σ_f w_f      over fields where s_f ≠ null
```
`w_f` reuses `fuzzy_rules[].weight` plus optional weights on fingerprint/det roles (default 1.0). This is the home for Scandit's name .5 / domain .4 / phone .1 weighting. **These weights are expert-set, not fitted — no labeled ground-truth set exists. Acceptable for rules-only V1; tell the client the number is a heuristic.**

**Cluster accuracy = weakest internal link:**
```
C = min over all matched edges (a,b) in the cluster of A(a,b)
```
The weakest edge is the one most likely to be wrong; this is the numeric generalization of the existing weakest-type `_cluster_path` logic (`:427`). Pure deterministic/fingerprint clusters → `C = 1.0` (unchanged). Promote `edge_type` from `dict[pair,str]` to `dict[pair,{type,score}]` so `min` is computable at assembly with no extra pass.

**Discriminators do NOT enter the score.** A vetoed pair never becomes an edge, so it never contributes to `C`. Accuracy answers "how strong is this match"; discriminators answer "is this even allowed." Two separate questions — do not collapse them.

### D.2 The gate (logical AND of four, plus hard overrides)

```
auto_merge  IFF
   C ≥ verification.auto_merge_threshold        (numeric — NEW)
   AND match_path ∈ auto_paths                  (category guard — fuzzy excluded by default)
   AND bucket == "auto_safe"                    (if require_safe_bucket)
   AND discriminators conclusive                (if require_discriminators_conclusive)
   AND NOT (any hard override fires)
otherwise → needs_verification  (or → escalated, see below)
```

`C ≥ threshold` and `path ∈ auto_paths` are **not redundant**: a fuzzy edge can score 0.99 yet be epistemically weaker than a deterministic match (two distinct "Apple" entities can be string-identical). **Recommendation for an auditable V1: fuzzy is permanently `needs_verification` — never in `auto_paths`.** Default auto-merge bar **0.97**, clustering bar 0.92 (loose about *surfacing*, strict about *acting*).

**Hard overrides (force review/escalate even at `C = 1.0`):**
- **Activity bucket** ≠ `auto_safe` — any `known_active` member (`LastActivityDate`), or **any CampaignMember** (the campaign cross-ref folds into `_bucket` as a `needs_review` trigger).
- **Protected record** — `SCD_NetSuite_Sync_Active__c` truthy on any member.
- **Blank discriminator** (when `require_discriminators_conclusive: true`) — Scandit should set this **true** for Vertical so a blank forces review rather than silently allowing a cross-vertical merge.
- **Master-record collision** — ≥2 members carry a protection flag (AccountNumber populated OR NetSuite-synced) → cannot pick a master → **escalate**.

### D.3 Output is THREE-WAY, not binary (resolving §H.4)

Reconcile the user's "auto vs manual" with the committed three-bucket + escalation by making the terminal state:
- **`auto_merge`** — all gates pass.
- **`needs_review`** — failed a soft gate; a human approves/excludes.
- **`escalated`** — master-record collision or an explicitly escalatable rule (routes to RevOps; distinct queue, not ordinary manual). This is "escalation as a valid outcome" (#16) as a *first-class state*, not collapsed into binary.

Materialize this as a `duplicate_sets.queue` column so the two/three-queue UI and the bulk-auto-merge SQL are trivial and the gate is enforced server-side (not via a caller-supplied `set_ids`).

### D.4 What the reviewer sees per cluster (explainability)

- **Confidence `{C·100}%`** + band (High ≥97 / Med 90–97 / Low <90).
- **Decision badge** AUTO-MERGE / MANUAL CHECK / ESCALATED.
- **Plain-language `verification_reason`** (already built, `:473`) — e.g. "deterministic match, activity-safe" / "approximate match (fuzzy) — verify" / "NetSuite-synced on 2 members — escalated."
- **Field-by-field match table** — per role: each member's value, `s_f`, and a chip: `MATCHED` / `FUZZY 0.94` / `DIFFERS — VETO` (e.g. Vertical "T&L" vs "Retail" → why it's *not* a dupe) / `BLANK — skipped`.
- **Weakest-link callout** — the specific field+pair that produced `C`.
- **Hierarchy + bucket chips** (`hierarchy_class`, `bucket`).
- **Intentionally-not-merged list** — surface discriminator-vetoed pairs read-only ("these looked identical but were split by Vertical") for audit.

### D.5 Tuning

`auto_merge_threshold` + `field_weights` live in the profile's `verification` block. A single slider in the model/scan UI with a **live count preview** reusing the existing `clusters_auto_merge` vs `clusters_needs_verification` rollup (`:384`): "At 0.97 → 142 auto / 169 manual. At 0.95 → 168 / 143." Risk-free to explore while the account path is dry-run.

---

## E. Merge Execution + Backup / Rollback

> **A Salesforce merge is destructive and NOT natively reversible.** Losers are deleted (Recycle Bin ~15 days, then purged). Children re-parent to the winner in place; reparenting is not reversibly logged. "Backup in case it's needed" = **forensic snapshot + best-effort recreate-as-stub**, never one-click undo. This statement goes in the merge-confirm UI **and** the client contract (#20).

### E.1 The four ship-blockers (must close before account merge or any external org)

- **R1 — Enforce the verification gate in the merge path.** `run_merge` must hard-filter on the stored `duplicate_sets.queue = 'auto_merge'` for the auto path and require explicit per-set approval (`decision='approved'`) for anything else. Refuse any set whose `verification_status != auto_merge` without a human approval row. Never trust a caller-supplied `set_ids`; never allow `set_ids=None` to mean "merge everything."
- **R2 — Real winner selection.** Remove `members[0]` (`scan.py:82`). The account scan must populate `winner_record_id` from the **master-record rules engine** (§C.1 `survivorship`), and treat an escalated/unresolved master as **non-mergeable** (skip + surface), never fall back to index 0.
- **R3 — Close the auth bypass.** Verify the Supabase JWT in a FastAPI dependency, derive `user_id` from the verified `sub`, delete `user_id` from all request bodies, and use a per-request user-scoped client (or explicit `WHERE user_id = <sub>`) so RLS applies. Without this, tenant isolation does not exist and nothing else matters.
- **R4 — Backup is a precondition.** No `merge_backups` table exists today. The executor must **refuse to call merge if the backup write failed.**

### E.2 Merge mechanics

| Object | Endpoint | Records/call | Status |
|---|---|---|---|
| Contact | `POST /sobjects/Contact/merge` | master + 2 losers | **BUILT** (`salesforce_merge.py:53`) |
| Account | `POST /sobjects/Account/merge` | master + 2 losers | **Build** — same REST shape, different sObject |
| Lead | `POST /sobjects/Lead/merge` | master + 2 losers | **Build** |

Generalize the hardcoded `/sobjects/Contact/merge` to `f"/sobjects/{object_type}/merge"`; generalize `update_contact` → `update_record(object_type,…)`. A cluster of N needs `ceil((N-1)/2)` sequential calls (the existing recursion is the right pattern).

- **Survivorship gap-fill is OURS.** Native SF merge keeps the master's values wholesale; it does not gap-fill. We PATCH the winner with the blended map **first**, then merge (existing two-step, `merge_duplicate_set:155`). V1 rule: winner value wins; blank winner field takes first non-blank loser value in deterministic loser order; record every override in the backup.
- **Children re-parent automatically** (SF does it) — but reparent collisions silently drop data (e.g. duplicate CampaignMembers) and orgs with >~10k child records fail the merge. Capture child reference IDs in the backup to detect what survived; catch the child-cap failure and route to `failed` with a clear reason, not blind retry.

**Fix the partial-success bug (R5).** `merge_contacts` (`salesforce_merge.py:73-75`) recurses and **returns only the last batch's result**, discarding earlier ones; `run_merge` flips `merged=True` per-**cluster** only on overall success — so a cluster that merges 2 losers then fails on losers 3–4 is recorded as fully-failed while 2 records are already gone. **Mitigation:** return per-batch results; write backup + status **per merge call** with a `merge_step` index; on resume, never re-merge already-absorbed (deleted) loser IDs.

**Stale-decision guard (R9).** The pre-merge fresh snapshot doubles as change-detection: compare each member's `SystemModstamp` to scan time; if changed, skip and re-surface.

### E.3 The backup artifact

**New table `merge_backups`** (reconciles the two conflicting prior definitions — superset, includes `tenant_id`):
```sql
CREATE TABLE merge_backups (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  merge_id UUID NOT NULL REFERENCES merges(id) ON DELETE CASCADE,
  duplicate_set_id UUID NOT NULL REFERENCES duplicate_sets(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  object_type TEXT NOT NULL,                 -- 'Account' | 'Contact' | 'Lead'
  winner_id TEXT NOT NULL,
  loser_ids TEXT[] NOT NULL,
  member_snapshots JSONB NOT NULL,           -- {sfId: {ALL field values incl system fields}}
  child_references JSONB NOT NULL,           -- {loserId: {childType: [childIds...]}}
  winner_field_overrides JSONB,              -- gap-filled fields, old→new
  merge_map JSONB NOT NULL,                  -- {winner, losers[], step}
  merge_step INT NOT NULL DEFAULT 0,
  captured_at TIMESTAMPTZ DEFAULT NOW(),
  merge_executed BOOLEAN DEFAULT FALSE,      -- true only after SF confirms
  sf_api_version TEXT,
  restore_status TEXT DEFAULT NULL           -- null | 'recreated_stub' | 'audit_only' | 'undeleted'
);
```

Snapshot is a **fresh live query immediately before merge** (scan-time `winner_data`/`loser_data` are stale and only ~14 fields — insufficient): all fields of every member (incl. system fields) via `SELECT FIELDS(ALL)` / introspected field list; child reference IDs for every loser (Account→Contacts/Opps/Cases/child Accounts/CampaignMembers/Tasks; Contact→Opps via OCR/CampaignMembers/Tasks/Cases; Lead→CampaignMembers/Tasks). Also emit one flat JSON file per run to Supabase Storage (`backups/{merge_id}.json`) as the portable client-held artifact.

> **PII (R13):** these snapshots copy full PROD records (names, emails, phones, custom PII) into LeanScale Supabase. Define a per-tenant retention/deletion policy; encrypt or field-mask PII in backup payloads; **RLS must be enforced (R3) before any cross-tenant data coexists.** The multi-tenant-vs-per-client decision (§H.1) should weigh PII residency — per-client deployment sidesteps commingling.

### E.4 Honest rollback tiers

- **Tier 1 — Audit-only** (always; default). The backup proves what existed and what we did. No SF writes. Satisfies "explainable/auditable."
- **Tier 2 — Recycle-Bin undelete** (only within ~15 days, only if not purged; `queryAll WHERE IsDeleted=true` → `undelete`). Restores **original IDs** but does *not* undo the winner's field changes or re-parented children — you get the loser back as a duplicate, not the original graph. Offer as "emergency restore" with a countdown. **Recommend fast-follow, not V1.**
- **Tier 3 — Recreate-as-stub** (always; lossy). Insert new records from `member_snapshots`. **New IDs** (every external reference to the old ID breaks), no children. `restore_status='recreated_stub'`.
- **The one clean undo:** revert the winner's gap-filled fields (we have old→new). Safely reversible — offer it.

**Cannot ever:** reverse the merge, restore original IDs (stub path), un-re-parent children, recover deleted child collisions, or restore field history.

---

## F. App Flow + Auth + Batches Data Model

### F.1 Screen map (`login → model → batches → review → verify → merge`)

| Step | Route | Today | Action |
|---|---|---|---|
| Login | `src/app/login/page.tsx` | BUILT | REUSE |
| Connect org | `src/app/connect/` + OAuth callbacks | BUILT | REUSE; add tenant/org label |
| **Set up your model** | **NEW** `src/app/models/` + `models/[modelId]/` | MISSING (dropdown of 2 JSON files) | NEW builder → writes the §C `MatchProfile` JSON; "test this model" shells the same `find_clusters` path over OAuth-fetched records |
| Start a run | `src/app/scan/` | BUILT (mixes config+model) | EXTEND: reduce to "pick connection + saved model + Run" |
| Run progress | `src/app/scan/[scanId]/` | BUILT (polls status) | REUSE |
| **See your batches** | **NEW** `src/app/runs/` | MISSING | NEW page + `GET /scan/list` (mirror `reports.py:list_user_reports:25`) |
| **Review (auto / manual / escalated queues)** | `src/app/review/[scanId]/` | PARTIAL (flat list + confidence filter) | EXTEND: replace filter with tabs driven by `queue` column; the engine already emits the signal |
| Cluster detail / verify | `src/components/DuplicateDetail.tsx` | PARTIAL | EXTEND: surface `verification_reason`/`hierarchy_class`/field-table (§D.4); Approve/Exclude/Escalate via `PATCH /scan/{id}/duplicate-sets/{set_id}` (`scan.py:331`) |
| Merge confirm + execute | `src/app/merge/[mergeId]/` + `POST /merge/execute` | Contact BUILT / **Account MISSING** | EXTEND: build account merge, gate on queue, backup-before-each-call; reuse pause/resume |
| Report | `src/app/reports/` | BUILT (PDF) | REUSE; also emit V3 **CSV** (#26) and generate the audit on *every* terminal state incl. failed/partial (R14) |

**Net new surface:** 3 pages (`models/`, `models/[modelId]/`, `runs/`), 1 review refactor, backend endpoints (`scan/list`, model CRUD, `describe`-over-OAuth), the backup table+write, account/lead merge methods. The largest real build is **account merge execution + backup + the master-record engine** — the rest is plumbing existing engine signals into the UI.

### F.2 Auth debt to close (ordered)

1. **JWT verification + drop body `user_id`** (`scan.py:35`, `merge.py:17`) — blocks any external connection (#30, R3).
2. **Tenant model** (if §H.1 = multi-tenant): NEW `tenants` + `tenant_members(tenant_id, user_id, role)`; re-point `crm_connections.tenant_id` + add to `scans/models/merges/merge_backups`. RLS shifts from `auth.uid()=user_id` to `EXISTS(SELECT 1 FROM tenant_members WHERE tenant_id=<row>.tenant_id AND user_id=auth.uid())` — same shape as the existing `duplicate_sets` join policy. **Do not run these migrations until §H.1 is ratified** — reversing is a migration on every table.
3. **Per-request user-scoped Supabase client** (retire service-role key from request paths; keep it only for background workers).
4. **Secrets/CORS hardening** + resolve the README Netlify-vs-Railway hosting inconsistency.

### F.3 Batches data model deltas (`001_initial_schema.sql`)

- **`scans`:** ADD `tenant_id`, `model_id`, `model_snapshot JSONB` (snapshot the full profile at run time so later model edits don't silently change history); WIDEN `object_type` CHECK to include `accounts`, `leads` (currently `contacts|companies|deals`).
- **`duplicate_sets`:** ADD `queue TEXT CHECK (queue IN ('auto_merge','needs_review','escalated','known_active'))`; `decision TEXT CHECK (decision IN ('pending','approved','excluded','escalated','merged'))`; `decided_by`, `decided_at`; `backup_id UUID REFERENCES merge_backups(id)`; `variant TEXT` (which with/without-domain fingerprint produced the cluster — resolves the dual-output display gap).
- **NEW `models`:** `id, tenant_id, name, object_type, profile_json JSONB, version INT, created_by, created_at`. The two `api/profiles/scandit/*.json` files become seed rows; add `MatchProfile.from_dict` alongside `from_json`.
- **NEW `merge_backups`** (§E.3).
- **NEW `not_duplicate_pairs`** (#22): `tenant_id, object_type, record_a, record_b, created_by, created_at` — future scans skip these.
- `merges`, `reports`: ADD `tenant_id`.

**Local harness** (`scripts/local_harness.py`) stays a **test rig, not product** — it deliberately swaps auth (`sf` session) and storage (in-memory) for the exact two things this design adds. The *engine* graduates (already shared); the harness web server does not. The model-builder's "test" button points at the same `find_clusters` path. Do not let "trust the sf session, store in memory" leak into the backend — that pattern *is* the bypass we're closing.

---

## G. Recommended Build Sequence (debt-first)

Each phase ends shippable. Effort is rough engineer-days.

**Phase 0 — Safety prerequisites (BLOCKS everything; ~5–7d).** Close R3 (JWT auth + drop body `user_id` + RLS enforcement); fix R1 (wire the gate into the merge path via a `queue` column, kill implicit `set_ids=None` merge-all); fix R5 (per-call merge status + step index). *Outcome: the existing contact merge path becomes safe and gated. No external org connects before this.*

**Phase 1 — Master-record engine + winner selection (~4–6d).** Build the `survivorship` rule chain (AccountNumber → NetSuite flag → escalate-on-collision) and the derived-boolean plumbing (#25). Remove `members[0]` (R2); escalated/unresolved → non-mergeable. *Owner-reassignment chain stays disabled pending provenance (§H.6).* *Outcome: accounts get a real, auditable winner + escalation-as-outcome.*

**Phase 2 — Rule-engine compile + per-field accuracy (~5–7d).** Implement `compile()` (fingerprint variants + `only_when` + per-field fuzzy thresholds + multi-scenario OR), the discriminator transitivity fix (R8), and the real per-cluster accuracy `C` replacing the flat constant (§D). Fold CampaignMember + `queryAll` eligibility into `_bucket` (#18,23,24). *Outcome: the engine actually does what §C/§D claim, on a tested core.*

**Phase 3 — Backup layer (~4–5d).** `merge_backups` table + fresh pre-merge snapshot (fields + child refs) + backup-as-precondition (R4) + portable JSON export + winner-field-revert + audit-on-every-terminal-state (R14). PII retention/encryption policy. *Outcome: every merge is auditable and partially recoverable; honest "not undo" copy in UI.*

**Phase 4 — Account/Lead merge execution (~3–4d).** Generalize the REST merge to `/sobjects/{object_type}/merge`; object-type plumbing end-to-end; child-cap + API-limit-floor handling; stale-decision guard (R9). *Outcome: gated account merge ships behind the verification gate + backup.*

**Phase 5 — Model builder UI (~6–8d).** `models/` + `models/[modelId]/` builder (field picker via `describe`-over-OAuth, per-field method, AND/OR scenarios, discriminators, `only_when`, threshold slider with live counts), survivorship tab. Replaces the hardcoded dropdown. *Outcome: "set up your model" without editing JSON.*

**Phase 6 — Batches + review UX + CSV (~4–5d).** `runs/` index + `GET /scan/list`; review tabs (auto/manual/escalated) + field-by-field table + intentionally-not-merged list; dual-variant display; V3 CSV export (#26); persistent exclusion registry (#22). *Outcome: the full `login→model→batches→review→verify→merge→backup` loop.*

**Deferred / fast-follow (not V1):** Recycle-Bin undelete (Tier 2); Celery migration + out-of-core blocking (force into V1 only if 48k+CampaignMember scans OOM — R10/R11); contact-engine migration onto the multi-mode engine (needed for the Contact same-name/diff-email discriminator); HubSpot accounts; Opportunities.

**Total V1: ~31–42 engineer-days** to the full loop, safety-first.

---

## H. Open Decisions For You

1. **Multi-tenant vs. per-client deployment.** The single biggest unresolved architecture question, and four designs assume "multi-tenant, decided." Ratify before any `tenant_id` migration (reversing = a migration on every table). PII residency (§E.3) is a factor — per-client sidesteps cross-tenant commingling. **Recommendation: single multi-tenant app, tenant = connected org, `tenants`/`tenant_members`.**
2. **Traction Complete: replicate the *config UX* only, not prevention/hierarchy-management.** Confirm the tool stays detection + merge + audit (consistent with #28, your "not a TC clone" memory, and the ~$27k TC hierarchy-automation buy). If you actually want TC-style prevention, that's a scope expansion that collides with #28.
3. **Scoring core.** Per-field thresholds (match decision) + weighted blend (displayed accuracy) — confirm the hybrid in §C.4, or pick one. This decides whether §D's accuracy math stands.
4. **Output shape: three-way (auto / manual / escalated), not binary.** Confirm escalation is a first-class terminal state (RevOps queue), reconciling your "auto vs manual" ask with committed #16/#18.
5. **"Merge all the records" = merge all auto-eligible, non-escalated sets.** Confirm this interpretation; a literal merge-everything violates #15.3/#16/#18.
6. **Owner-reassignment chain provenance.** The SAM→Core→ISM / inactive→RevOps / Mkt→Core SDs cascade has **no primary-source support** (Kylee↔Anca↔Claire email chain cited but unverifiable). Confirm with Claire/Anca before building; it stays disabled in the schema until then.
7. **Discriminator blank-handling default.** `skip` (Scandit's stated choice, blanks never veto) vs forcing `require_discriminators_conclusive: true` so blanks → review. **Recommendation: `skip` for clustering but `require_discriminators_conclusive: true` for Vertical at the auto-merge gate** — so a blank Vertical can't silently auto-merge a cross-vertical pair (R7).
8. **Recover Eduardo's `scripts/audit/analyze_dupes.py`** — the "live spec" for exact V3 thresholds, not in any accessible repo, author left end of May. Every numeric default here (0.92/0.97, name .5/domain .4/phone .1) is **expert-set, not fitted**. Decide whether to hunt the script down (his old machine/Drive/git stash) or accept calibrated-by-judgment defaults for V1.
9. **Recycle-Bin emergency restore in V1 or fast-follow?** Recommend fast-follow (audit-only + stub + winner-field-revert in V1).

---

**Key files (all absolute):**
- Engine: `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/api/app/services/match_engine.py` (`MatchProfile:93`, `_fingerprint:189`, `_fuzzy_score:213`, `find_clusters:228`, flat conf `:356`, `_verify:436`, veto `199-210`/`260-263`)
- Merge: `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/api/app/services/salesforce_merge.py:53` (hardcoded Contact endpoint), `:73-75` (partial-success recursion bug); `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/api/app/routers/merge.py:48-50,144-187` (ungated merge path)
- Winner placeholder: `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/api/app/routers/scan.py:82` (`members[0]`); auth bypass `scan.py:35` + `merge.py:17`
- Profiles → DB seeds: `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/api/profiles/scandit/account_v2.json`, `account_v3.json`
- UI to replace/extend: `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/src/app/scan/ScanConfigClient.tsx:33`, `src/app/review/[scanId]/ReviewClient.tsx`
- Schema: `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/supabase/migrations/001_initial_schema.sql`
- Harness (test rig, not product): `/Users/kaveangobal/Documents/GitHub/crm-dedupe-tool/scripts/local_harness.py`

**External-dependency risk to capture now:** Eduardo's `scripts/audit/analyze_dupes.py` (live spec for exact thresholds) and the missing `06-Cross-Client-Build-Plan.md` / `02-Changes-Made-and-Rollback.md` docs — cited as authoritative, not in any accessible repo. Recover or formally reconstruct before context fully decays.
