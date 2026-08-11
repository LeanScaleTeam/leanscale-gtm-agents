---
name: setup
description: >-
  One-time (and re-runnable) setup for the CRM Hygiene agent. Probes the connected
  Salesforce or HubSpot tools, reads the org automatically — objects, record counts,
  custom-field inventory, fill rates, picklists, record types, active vs deactivated
  users, fiscal settings — then asks only the handful of questions the CRM cannot
  answer, writes ~/.leanscale-gtm/profile.json and ~/.leanscale-gtm/crm-hygiene.json,
  and proves the pipeline with a smoke test. Trigger on "/crm-hygiene:setup", "set up
  crm hygiene", "configure the CRM audit", "connect my Salesforce to the hygiene agent",
  or when a run fails and you need to diagnose the connection. Read-only.
argument-hint: "[--reconfigure] [--check]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# CRM Hygiene — setup

Read-only throughout. Nothing here writes to the CRM.

Run it in this order. **Idempotent** — re-run it any time; it re-reads the org, shows what is
already configured, and only asks about what is missing or has drifted. `--check` runs steps
1–3 and the pass/fail table without touching config, which is the right first move when a run
has started failing.

The rule that makes this feel expensive: **discover before you ask.** Every question below is
phrased in terms of a number you already pulled. A question the CRM could have answered makes
the product look dumb, and a question you skip that changes the analysis makes it wrong.

---

## 1. Probe the connectors

```
ToolSearch("run_soql_query salesforce")        → crm.query    (Salesforce)
ToolSearch("hubspot crm objects search")       → crm.query    (HubSpot)
ToolSearch("describe metadata object schema")  → crm.describe
```

Report the mapping explicitly — tool name, the capability it satisfies, and the call you used
to prove it:

| Capability | Tool | Proof | Result |
|---|---|---|---|
| `crm.query` | `run_soql_query` | `SELECT COUNT() FROM Account` | 3,411 |
| `crm.describe` | `run_soql_query` (FieldDefinition) | `SELECT COUNT() FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName = 'Opportunity'` | 214 |

On Salesforce, `run_soql_query` satisfies both capabilities: metadata objects are queryable
in SOQL, and the tool takes `useToolingApi: true` for the Tooling-only ones. Resolve the org
with `get_username` or `list_all_orgs` — never guess a username or alias.

**Failures must be specific.** Not "HubSpot not available" but: *"`hubspot-list-objects`
resolves and returns companies, but `/crm/v3/properties/deals` returns 403 — the private app
token is missing the `crm.schemas.deals.read` scope, so field and picklist analysis cannot
run."* Name the scope, the object, the setting.

If `crm.query` does not resolve at all, stop here and say what to connect. If `crm.query`
works but `crm.describe` does not, you can still finish setup — say plainly which checks will
be missing (dead fields, picklist rot, schema-vs-policy gap: roughly half the value).

---

## 2. Read the shared profile

```bash
cat ~/.leanscale-gtm/profile.json 2>/dev/null || echo "no profile yet"
```

If it exists, **show it back and confirm** — do not re-interrogate. This file is shared by
every LeanScale GTM agent; asking about the fiscal year for the fourth time is how a suite
starts feeling like nine separate tools.

> "You already have a profile: Acme, Salesforce, fiscal year starts February, 14 quota-carrying
> reps, material deal floor $5,000, segments SMB / Mid-Market / Enterprise. Still right?"

If it does not exist, you are the first agent here and you create it in step 5.

---

## 3. Read the org — automatically, before any question

### 3.1 Salesforce

**Org settings.** `FiscalYearStartMonth` is the number that drives every period calculation in
the suite, and `UsesStartDateAsFiscalYearName` decides which of the two fiscal-naming
conventions this company uses. Read both; do not assume January and do not assume the
convention.

```sql
SELECT Id, Name, OrganizationType, InstanceName, IsSandbox,
       FiscalYearStartMonth, UsesStartDateAsFiscalYearName, DefaultCurrencyIsoCode
FROM Organization
```

`IsSandbox = true` means you are pointed at a sandbox. Stop and confirm that is intended —
auditing a sandbox and reporting it as production is a credibility-ending mistake.

Multi-currency: `SELECT COUNT() FROM CurrencyType`. If the object does not exist, the org is
single-currency; that error is the answer, not a failure.

**Volume.**

```sql
SELECT COUNT() FROM Account
SELECT COUNT() FROM Contact
SELECT COUNT() FROM Opportunity
SELECT COUNT() FROM Lead WHERE IsConverted = false
SELECT COUNT() FROM Opportunity WHERE IsClosed = false
```

**Object inventory**, including custom objects that may hold the real pipeline:

```sql
SELECT QualifiedApiName, Label, KeyPrefix FROM EntityDefinition
WHERE IsCustomizable = true AND IsQueryable = true ORDER BY QualifiedApiName
```

**Field inventory** — the number that usually starts the conversation:

```sql
SELECT EntityDefinition.QualifiedApiName, QualifiedApiName, Label, DataType, IsNillable,
       NamespacePrefix, BusinessStatus
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
```
Repeat per object. Count total, custom (`__c`), managed-package (`NamespacePrefix` set), and
schema-required (`IsNillable = false`).

**Fill rates** over the last 12 months, ~25 aggregates per query (see the run skill §2.8 for
the batching rules and the long-text-area constraint):

```sql
SELECT COUNT(Id) total, COUNT(NextStep) f1, COUNT(Type) f2, COUNT(Amount) f3
FROM Opportunity WHERE CreatedDate = LAST_N_DAYS:365
```

**Picklists** — read them, never invent them. `StageName`, `Type`, `LeadSource`,
`ForecastCategoryName`, `Account.Type`, `Account.Industry`, `Lead.Status`, plus any custom
segment field. Both the defined value set (§2.9 of the run skill) and usage:

```sql
SELECT StageName, COUNT(Id) c FROM Opportunity
WHERE CreatedDate = LAST_N_DAYS:365 GROUP BY StageName ORDER BY COUNT(Id) DESC
```

**Users.**

```sql
SELECT IsActive, COUNT(Id) c FROM User WHERE UserType = 'Standard' GROUP BY IsActive
```

**Do deactivated users still own things?** Answer it yourself before you ask about it:

```sql
SELECT COUNT() FROM Opportunity WHERE IsClosed = false AND Owner.IsActive = false
SELECT COUNT() FROM Account WHERE Owner.IsActive = false
```

**Record types and their usage:**

```sql
SELECT Id, DeveloperName, Name, SobjectType, IsActive FROM RecordType
SELECT RecordTypeId, COUNT(Id) c FROM Opportunity
WHERE CreatedDate = LAST_N_DAYS:365 GROUP BY RecordTypeId
```

**Governance posture:**

```sql
SELECT DeveloperName, MasterLabel, SobjectType, IsActive FROM DuplicateRule
SELECT EntityDefinition.QualifiedApiName, ValidationName, Active
FROM ValidationRule                              -- useToolingApi: true
```

**Who closes deals** — the evidence behind the quota-carrying-reps question:

```sql
SELECT OwnerId, Owner.Name, COUNT(Id) c FROM Opportunity
WHERE IsWon = true AND CloseDate = LAST_N_DAYS:365 GROUP BY OwnerId, Owner.Name
```

**Deal-size distribution** for the material deal floor — pull the amounts and take the 10th
percentile:

```sql
SELECT Amount FROM Opportunity
WHERE IsWon = true AND CloseDate = LAST_N_DAYS:365 AND Amount != null
ORDER BY Amount ASC
```

### 3.2 HubSpot

**Portal.** `GET /account-info/v3/details` → portal id, currency, time zone, and whether this
is a sandbox. HubSpot exposes no reliable fiscal-year setting through the CRM API, so read
what you can and ask for the rest (§4.0).

**Volume** — one search per object with an empty filter, `limit: 1`, read `total`:

```
POST /crm/v3/objects/companies/search   {"filterGroups":[],"limit":1}
POST /crm/v3/objects/contacts/search    {"filterGroups":[],"limit":1}
POST /crm/v3/objects/deals/search       {"filterGroups":[],"limit":1}
POST /crm/v3/objects/deals/search
  {"filterGroups":[{"filters":[{"propertyName":"hs_is_closed","operator":"EQ","value":"false"}]}],"limit":1}
```

**Objects**, including custom ones: `GET /crm/v3/schemas`.

**Field inventory:** `GET /crm/v3/properties/companies|contacts|deals`. Count total, custom
(`hubspotDefined: false`), calculated, and hidden. Note out loud that HubSpot has **no
object-level required flag** — it will matter in §4.2.

**Fill rates:** one `HAS_PROPERTY` search per property, `limit: 1`, read `total` (run skill
§3.7). In a portal with hundreds of properties this is the slow part of setup; say so, and
offer to sample the top 60 by group instead.

**Picklists:** `results[].options[]` from the properties call, plus
`GET /crm/v3/pipelines/deals` for stages, including `archived` pipelines and stages.

**Owners:** `GET /crm/v3/owners/?archived=false` and `?archived=true`. The archived list is
the deactivated-user list.

**Do archived owners still own records?**

```
POST /crm/v3/objects/deals/search
{"filterGroups":[{"filters":[{"propertyName":"hubspot_owner_id","operator":"IN",
  "values":["<archived owner ids>"]},
  {"propertyName":"hs_is_closed","operator":"EQ","value":"false"}]}],"limit":1}
```

### 3.3 Show your work

Before asking anything, put the discovery on screen — the customer should learn something in
the first two minutes:

```
Salesforce · Acme Production (not a sandbox) · fiscal year starts February, named for the year it ends

  Accounts       3,411        Custom fields   Account 61 · Contact 38 · Opportunity 214 · Lead 44
  Contacts      18,760        Under 5% filled                    ... 39 · 22 · 128 · 27
  Opportunities  2,044        Never populated                    ... 21 · 14 ·  71 · 19
  Open deals       317        Schema-required custom fields: 3 of 357
  Open leads     1,902        Duplicate rules: 4, of which 1 active
                              Validation rules: 22, of which 14 active
  Users: 61 active, 19 deactivated — and 41 open opportunities are owned by the 19.
  Opportunity stages: 9 defined, 7 used in the last 12 months.
```

---

## 4. The interview

Ten questions. Every one of them is something the CRM genuinely cannot tell you, and every
one changes the analysis. Ask them with your numbers in the sentence.

### 4.0 Profile questions — only if `profile.json` is missing or incomplete

- **Org name.** Propose `Organization.Name` / the HubSpot portal name.
- **Fiscal year start + naming.** *"Your org has `FiscalYearStartMonth = 2` and
  `UsesStartDateAsFiscalYearName = false`, so FY2027 runs Feb 2026 → Jan 2027. Confirm?"*
  On HubSpot you have to ask — the API will not tell you.
- **Quota-carrying reps.** Ask directly, with the evidence: *"17 people closed at least one
  deal in the last 12 months, and you have 61 active users. How many carry a quota?"* This is
  the single most load-bearing number in the suite — ratios computed against total headcount
  are wrong and embarrassing.
- **Material deal floor.** Propose the 10th percentile of closed-won amount: *"The 10th
  percentile of your closed-won deals is $4,200. Ignore anything below that as noise?"*
- **Segments.** Read the picklist and confirm which field holds it. Do not invent segments.
- **Motion and competitors.** Short, and only if unknown.
- **PII redaction.** *"Should reports replace person names and emails with pseudonyms?
  findings.json stays unredacted on your machine either way."*

### 4.1 Objects in scope

> "I can audit Account, Contact, Opportunity and Lead. You have 1,902 unconverted leads, so
> Lead looks live — but I also see a custom object `Deal_Registration__c` with 4,100 records.
> Should that be in scope, and is there anything here you deliberately do not manage?"

→ `objects_in_scope`

### 4.2 Policy-required fields — the most important question in this interview

> "Only 3 of your 357 custom fields are required in the schema. That is normal, and it is also
> the reason fields go empty. Which fields is your team *told* are mandatory? Name the ones a
> manager would push back on if they were blank — `NextStep`, `Type`, `Competitor__c`?"

On HubSpot, lead with the constraint: *"HubSpot has no object-level required flag at all —
'required' only exists on a form or in a workflow. So whatever your team believes is
mandatory, the schema does not enforce any of it. Which fields do you tell them to fill?"*

The gap between this answer and `IsNillable = false` **is** the headline finding. Push for
specificity: "everything on the layout" is not an answer, and a list of thirty fields is a
policy nobody follows. Six or fewer per object is a real policy.

→ `policy_required_fields`, and confirm `policy_required_scope`: judge open records plus
anything created in the window (default), open records only, or everything ever.

### 4.3 The dedupe key you trust

> "I can cluster duplicates four ways: shared website domain, normalized company name,
> exact contact email, and same person name on the same account. Domain is the strongest, but
> 412 of your accounts have no website — so name-matching will catch things domain cannot.
> Which of these do you trust enough to look at? And are there partner, reseller or agency
> domains that legitimately front many accounts and should be excluded?"

Also ask about hierarchy: *"Do you use the parent-account field for subsidiaries? If you do,
I will treat a shared domain across a linked parent and child as a hierarchy rather than a
duplicate — that removes the most common false positive."*

→ `dedupe_keys`, `ignore_domains`, `exclude_hierarchy_clusters`

### 4.4 Staleness threshold for an open opportunity

Discover first, then ask:

```sql
SELECT COUNT() FROM Opportunity WHERE IsClosed = false AND LastActivityDate < LAST_N_DAYS:30
SELECT COUNT() FROM Opportunity WHERE IsClosed = false AND LastActivityDate < LAST_N_DAYS:60
SELECT COUNT() FROM Opportunity WHERE IsClosed = false AND LastActivityDate < LAST_N_DAYS:90
```

> "At 30 days, 128 of your 317 open deals are stale. At 60 it is 74; at 90 it is 41. Where do
> you want the line — what is the longest a real deal in your cycle goes quiet?"

Ask the same for leads if Lead is in scope.

→ `open_opp_staleness_days`, `lead_staleness_days`

### 4.5 Record types in use

> "You have 7 record types. Two have had no record created in 12 months
> (`Opportunity.Services_Deal`, `Account.Legacy_Prospect`). Dead, or dormant on purpose?"

On HubSpot the equivalent is pipelines: *"Your `Partner Pipeline (2023)` is archived but still
holds 9 deals."*

→ `record_types_in_scope`

### 4.6 Fields you already know are dead

> "128 Opportunity custom fields are under 5% filled. Are any of those deliberately sparse —
> a field that only applies to one motion — or is that all rot? Anything you already know is
> dead and have decided to keep, I will count but stop reporting."

This is the question that separates a useful report from a nagging one.

→ `known_dead_fields`

### 4.7 Who owns data quality

> "When this report says a field is 40% empty, who is the person who decides what happens
> next?"

If the honest answer is "nobody", that is worth writing down — it is usually the real finding.

→ `data_quality_owner`

### 4.8 Do deactivated users still own records on purpose?

> "19 deactivated users still own 41 open opportunities and 260 accounts. Some teams park a
> departed rep's history deliberately. Is that a policy here, or a gap?"

If deliberate, those findings drop from critical to low and the report says you told us so —
rather than shouting at them every run about a decision they already made.

→ `expect_inactive_users_own_records`

### 4.9 How often will you run this?

> "Monthly is the usual cadence — often enough to show movement, rare enough that the list
> changes between runs."

Not stored; it sets the expectation that run one is a baseline and run two is the product.

---

## 5. Write the config

Two files. Show both back to the customer in full — they will edit these by hand.

```bash
mkdir -p ~/.leanscale-gtm
```

`~/.leanscale-gtm/profile.json` — shared, per SPEC §2. **Merge, never overwrite:** read the
existing file, add only the keys that are missing, keep everything another agent wrote.

```json
{
  "schema_version": 1,
  "org_name": "Acme",
  "crm": {"system": "salesforce", "mcp_probe": "run_soql_query",
          "instance_label": "Acme Production", "secondary": null},
  "fiscal_year_start_month": 2,
  "fiscal_year_naming": "ends_in",
  "currency": {"corporate": "USD", "multi_currency": true},
  "motion": ["inbound_led", "enterprise"],
  "quota_carrying_reps": 14,
  "segments": ["SMB", "Mid-Market", "Enterprise"],
  "segment_field": "Account.Segment__c",
  "material_deal_floor": 4200,
  "team_map": {"roll_up_field": "User.ManagerId"},
  "objects": {"opportunity": "Opportunity", "account": "Account", "lead": "Lead"},
  "competitors": [],
  "redact_pii_in_reports": false
}
```

`~/.leanscale-gtm/crm-hygiene.json` — start from
`${CLAUDE_PLUGIN_ROOT}/config.example.json`, keep the `_<key>_help` lines, and replace the
values with the interview answers. On HubSpot, remember the field names are lower_snake_case
internal property names, not Salesforce API names — see
`${CLAUDE_PLUGIN_ROOT}/fixtures/config.hubspot.json` for a worked example.

Then print the file and say: *"This lives in your home directory, not in the plugin, so it
survives updates. Edit it directly any time — every key has a help line under it."*

---

## 6. Smoke test — prove it produces a real finding

A setup that ends without output is not done. Run the whole pipeline against a deliberately
small slice.

```bash
SMOKE="./gtm-agents/crm-hygiene/setup-smoke"
mkdir -p "$SMOKE/raw"
```

Fetch the five required sources with `LIMIT 300` (HubSpot: one page of 100 each), plus field
metadata for a single object. Same envelope as the run skill §4.1. Then:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" --raw "$SMOKE/raw" --out "$SMOKE"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --findings "$SMOKE/findings.json" \
        --out "$SMOKE" --no-baseline
```

**`--no-baseline` is not optional.** A smoke test runs on a truncated slice; banking it as a
baseline would make the first real run's deltas meaningless.

Then quote one genuine finding back with its record count and offer the record ids, so the
customer can open one in their CRM right now. That single moment is what setup is for.

If you want to check the machinery without touching their CRM at all, the bundled fixtures
run offline:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
        --raw "${CLAUDE_PLUGIN_ROOT}/fixtures/raw" --out /tmp/crm-hygiene-selftest
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" \
        --findings /tmp/crm-hygiene-selftest/findings.json --out /tmp/crm-hygiene-selftest
```

---

## 7. Pass/fail table

End with this, filled in, plus one plain-English sentence per failure saying exactly what the
customer must do.

| Check | Status | Detail |
|---|---|---|
| `crm.query` resolves | PASS | `run_soql_query` → 3,411 accounts |
| `crm.describe` resolves | PASS | `FieldDefinition` → 357 custom fields across 4 objects |
| Read access to Opportunity | PASS | 2,044 records, 317 open |
| Read access to User | PASS | 61 active, 19 deactivated |
| Duplicate rules readable | PASS | 4 rules, 1 active |
| Validation rules readable | FAIL | Tooling API returned `INSUFFICIENT_ACCESS` |
| Contact roles readable | PASS | 1,180 roles on open deals |
| Picklist value sets readable | PASS | via `StandardValueSet` + Tooling `CustomField` |
| Field fill-rates exact | PARTIAL | 12 long-text fields fall back to sampling |
| Profile written | PASS | `~/.leanscale-gtm/profile.json` |
| Plugin config written | PASS | `~/.leanscale-gtm/crm-hygiene.json` |
| Smoke test produced a finding | PASS | 23 open deals past their close date |

Then, in words:

> **What will work:** duplicates, dead fields, ownership, staleness, orphans, picklists.
> **What will not:** the inactive-validation-rule finding, because the connected user cannot
> read the Tooling API. **To fix it:** grant the integration user the "View Setup and
> Configuration" permission, then re-run `/crm-hygiene:setup --check`.
> **What is unavailable rather than clean:** anything on that list shows in every report as
> unavailable, so nobody reads a missing connector as a pass.

Close by telling them run one is a baseline: *"The first `/crm-hygiene:run` sets the
comparison point. From the second run on, every number carries a delta — that is where this
starts being worth the calendar slot."*
