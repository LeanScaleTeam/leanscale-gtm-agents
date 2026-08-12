---
name: run
description: >-
  Run a read-only CRM data-quality audit against Salesforce or HubSpot and produce a
  Hygiene Index plus a severity-ranked findings report: duplicate accounts and contacts,
  dead custom fields, required fields nothing enforces, records owned by people who left,
  open deals past their close date, orphaned records, picklist rot and governance gaps.
  Trigger on "/crm-hygiene:run", "audit my CRM", "how dirty is our Salesforce", "find
  duplicate accounts", "which fields are dead", "CRM data quality", "clean up HubSpot",
  "what's wrong with our CRM data", or any request to assess CRM hygiene, data quality
  or duplicates. Nothing is written to the CRM.
argument-hint: "[--window 365d] [--objects Account,Contact,Opportunity] [--quick]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# CRM Hygiene — run

Read-only. This skill issues **SELECT/GET calls only**. It never creates, updates, merges,
deletes or deploys anything. Say that to the user before you start if they have not run it
before; it is the reason this one gets approved.

Three layers, in order. Do not skip a layer or improvise a shortcut through it.

| Layer | Who | What |
|---|---|---|
| 1 | You (Claude) | Fetch from the CRM over MCP, write `raw/*.json` verbatim |
| 2 | `scripts/analyze.py` | Offline transform → `findings.json` + `manifest.json` |
| 3 | `scripts/report.py` | → `report.md`, `report.html`, baseline snapshot |

---

## 0. Preflight

1. Read `~/.leanscale-gtm/crm-hygiene.json` and `~/.leanscale-gtm/profile.json`.
   If **either is missing**, stop and say: *"Run `/crm-hygiene:setup` first — it reads your
   org and writes the config this run needs. It takes about ten minutes and you only do it
   once."* Do not invent a config; a guessed `policy_required_fields` list produces a
   confident, wrong report.
2. Echo the assumptions you are about to run under, in one line: org name, CRM, objects in
   scope, staleness threshold, dedupe keys. The customer should be able to stop you here.
3. Create the run directory in the **current working directory** (never in the plugin, which
   is read-only on a marketplace install):

```bash
RUN="./gtm-agents/crm-hygiene/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw"
echo "$RUN"
```

Arguments: `--window 365d` overrides `window_days`; `--objects` narrows the object list;
`--quick` skips per-property fill-rate and picklist-usage queries (Salesforce: skip the
aggregate batches; HubSpot: skip the `HAS_PROPERTY` sweep), which cuts the run from minutes
to seconds at the cost of sampled rather than exact field counts. Say which mode you used.

---

## 1. Resolve the tools

Required capabilities: `crm.describe`, `crm.query`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("run_soql_query salesforce")        → crm.query   (Salesforce)
    ToolSearch("hubspot crm objects search")       → crm.query   (HubSpot)
    ToolSearch("describe metadata object schema")  → crm.describe

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    crm.describe  salesforce  run_soql_query over EntityDefinition / FieldDefinition (useToolingApi where noted)
                  hubspot     hubspot-list-properties
    crm.query     salesforce  run_soql_query
                  hubspot     hubspot-search-objects / hubspot-list-objects / hubspot-batch-read-objects

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


`crm.query` **and** `crm.describe` are both required — without describe there is no field
inventory and no picklist audit, which is half the value. On Salesforce both capabilities
run through the same `run_soql_query` tool (metadata objects are queryable in SOQL), so one
resolved tool satisfies both.

The Salesforce MCP's `run_soql_query` takes `query`, `usernameOrAlias`, `directory` and an
optional **`useToolingApi: true`** — several queries below need that flag and are marked.
Resolve the username with `get_username` / `list_all_orgs` rather than guessing it.

If a probe fails, be specific about what failed. "Salesforce not available" is useless.
"`run_soql_query` resolves but `SELECT Id FROM Opportunity LIMIT 1` returns 0 rows while the
org reports 3,164 opportunities — the connected user probably lacks read access under a
private org-wide default" is actionable.

---

## 2. Fetch — Salesforce

Run these with `useToolingApi` **false** unless the query says otherwise. Write each result
verbatim into `raw/` using the envelope in §4.

### 2.1 `raw/accounts.json` — required

```sql
SELECT Id, Name, Website, Type, Industry, BillingCountry, BillingState,
       NumberOfEmployees, AnnualRevenue, ParentId, RecordTypeId,
       OwnerId, Owner.Name, Owner.IsActive,
       CreatedDate, LastModifiedDate, LastActivityDate
FROM Account
ORDER BY LastModifiedDate DESC
LIMIT 5000
```

Append the custom fields the customer named in setup (segment, tier, territory) so the
policy-required check can see them. Keep the list under ~90 fields.

### 2.2 `raw/contacts.json` — required

```sql
SELECT Id, FirstName, LastName, Name, Email, Title, Phone, AccountId, Account.Name,
       OwnerId, Owner.Name, Owner.IsActive, MailingCountry, LeadSource,
       CreatedDate, LastModifiedDate, LastActivityDate
FROM Contact
ORDER BY LastModifiedDate DESC
LIMIT 5000
```

### 2.3 `raw/opportunities.json` — required

Every open deal, plus a year of closed history so the analyzer can measure this org's own
median win cycle instead of applying a benchmark:

```sql
SELECT Id, Name, AccountId, Account.Name, StageName, Amount, CloseDate, IsClosed, IsWon,
       Probability, ForecastCategoryName, Type, LeadSource, NextStep, RecordTypeId,
       OwnerId, Owner.Name, Owner.IsActive,
       CreatedDate, LastModifiedDate, LastActivityDate
FROM Opportunity
WHERE IsClosed = false OR CloseDate = LAST_N_DAYS:365
ORDER BY CloseDate DESC
LIMIT 5000
```

### 2.4 `raw/users.json` — required

```sql
SELECT Id, Name, Username, Email, IsActive, UserType, Profile.Name, UserRole.Name,
       LastLoginDate
FROM User
LIMIT 5000
```

`Owner.IsActive` on each record is the primary signal; this query supplies the names and
catches owners whose records you did not pull. Both matter — do not skip it.

### 2.5 `raw/leads.json` — optional, skip if the org does not use Leads

```sql
SELECT Id, FirstName, LastName, Name, Company, Email, Status, IsConverted, LeadSource,
       Industry, OwnerId, Owner.Name, Owner.IsActive,
       CreatedDate, LastModifiedDate, LastActivityDate
FROM Lead
WHERE IsConverted = false
LIMIT 5000
```

### 2.6 `raw/opportunity_contact_roles.json` — optional but high value

```sql
SELECT Id, OpportunityId, ContactId, Role, IsPrimary
FROM OpportunityContactRole
WHERE Opportunity.IsClosed = false
LIMIT 10000
```

### 2.7 `raw/field_metadata.json` — required

`FieldDefinition` is queryable through the standard API but **requires a bounded filter on
`EntityDefinition`**. Run one query per object and concatenate the results into a single
`records` array — some orgs reject a multi-value `IN` here, and one-per-object always works:

```sql
SELECT EntityDefinition.QualifiedApiName, QualifiedApiName, Label, DataType, IsNillable,
       IsCalculated, NamespacePrefix, BusinessStatus, Description, LastModifiedDate
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
```

Repeat for `'Account'`, `'Contact'`, `'Lead'`.

- `IsNillable = false` is the schema-required flag. The gap between that and the customer's
  `policy_required_fields` is the headline governance finding — do not drop this column.
- The analyzer treats a name ending in `__c` as custom. `NamespacePrefix` identifies managed-
  package fields, which are reported but never recommended for deletion.
- `BusinessStatus` is Salesforce's own field-lifecycle flag (`Active` / `DeprecateCandidate` /
  `Hidden`). Where an admin has filled it in, it is the org's own opinion about what is dead.

### 2.8 `raw/field_fill.json` — optional, and the difference between exact and sampled

Aggregate SOQL gives exact fill rates over the whole window without pulling a single record.
`COUNT(Id)` is the denominator, `COUNT(SomeField__c)` counts non-null values:

```sql
SELECT COUNT(Id) total,
       COUNT(NextStep) f_NextStep,
       COUNT(Type) f_Type,
       COUNT(Amount) f_Amount,
       COUNT(Deal_Desk_Notes__c) f_Deal_Desk_Notes__c,
       COUNT(Competitor__c) f_Competitor__c
FROM Opportunity
WHERE CreatedDate = LAST_N_DAYS:365
```

Batch roughly **25 fields per query** and repeat until every field is covered. Three real
constraints, all of which will bite you:

- **Long text area, rich text, multi-select picklist and encrypted fields cannot be
  aggregated.** `COUNT()` on them returns `MALFORMED_QUERY`. Exclude any field whose
  `DataType` contains `Long Text Area`, `Rich Text`, `Picklist (Multi-Select)` or
  `Encrypted`, and let the analyzer measure those from the sampled records instead.
- If a batch errors, halve it and retry rather than abandoning the whole object — one bad
  field should not cost you the other twenty-four.
- Use the same `WHERE CreatedDate = LAST_N_DAYS:365` on every batch for one object so the
  denominators match.

Normalize the results before writing (this is a derived source, not a verbatim one):

```json
{"object": "Opportunity", "field": "NextStep", "total": 3164, "filled": 917}
```

Omit this file entirely under `--quick`; the analyzer falls back to the sampled records and
says so in the method note.

### 2.9 `raw/picklist_metadata.json` — optional

Best route first:

1. **A describe tool**, if one resolved in the probe. One call per object returns every
   picklist value with its `active` flag. Nothing else is this cheap.
2. **Standard picklists** via the Tooling API (`useToolingApi: true`), one query per value set
   — `Metadata` can only be selected one row at a time, which is a hard API limit:
   ```sql
   SELECT Id, MasterLabel, Metadata FROM StandardValueSet WHERE MasterLabel = 'OpportunityStage'
   ```
   Useful `MasterLabel` values: `OpportunityStage`, `OpportunityType`, `LeadSource`,
   `LeadStatus`, `AccountType`, `Industry`, `ForecastCategoryName`.
3. **Custom picklists** via the Tooling API, again one row per query:
   ```sql
   SELECT Id, DeveloperName, TableEnumOrId, Metadata FROM CustomField
   WHERE TableEnumOrId = 'Opportunity' AND DeveloperName = 'Competitor'
   ```
   Values live at `Metadata.valueSet.valueSetDefinition.value[]` as
   `{fullName, label, default, isActive}`.
4. **`retrieve_metadata`** for the object, then read the picklist values out of the XML.

Whatever route you take, normalize to one flat row per value:

```json
{"object": "Opportunity", "field": "StageName", "value": "Proposal/Price Quote",
 "label": "Proposal/Price Quote", "active": true, "default": false}
```

If no route works, skip the file. The analyzer still detects near-duplicate values among the
values that are *in use* — it only loses the "defined but never used" check, and it says so.

### 2.10 `raw/picklist_usage.json` — optional

One `GROUP BY` per audited picklist. This is how you get usage over the whole window without
pulling the records:

```sql
SELECT StageName, COUNT(Id) c FROM Opportunity
WHERE CreatedDate = LAST_N_DAYS:365 GROUP BY StageName ORDER BY COUNT(Id) DESC
```

Multi-select picklists cannot be grouped — skip them. Normalize to:

```json
{"object": "Opportunity", "field": "StageName", "value": "Proposal/Price Quote", "count": 118}
```

### 2.11 `raw/governance.json` — optional, and usually the finding with the most leverage

Concatenate these into one `records` array, each row tagged with a `kind`.

```sql
-- kind: "duplicate_rule"   (standard API)
SELECT Id, DeveloperName, MasterLabel, SobjectType, IsActive, LastModifiedDate
FROM DuplicateRule
```
```sql
-- kind: "matching_rule"    (useToolingApi: true) — a duplicate rule whose matching rule is
-- deactivated does nothing at all, which is the failure mode nobody notices
SELECT Id, MasterLabel, SobjectType, RuleStatus FROM MatchingRule
```
```sql
-- kind: "validation_rule"  (useToolingApi: true — ValidationRule is Tooling-only)
SELECT Id, EntityDefinition.QualifiedApiName, ValidationName, Active, Description, ErrorMessage
FROM ValidationRule
```
```sql
-- kind: "record_type"      (standard API)
SELECT Id, DeveloperName, Name, SobjectType, IsActive FROM RecordType
```
```sql
-- kind: "record_type_usage" — one per object, mapped to {RecordTypeId, SobjectType, count}
SELECT RecordTypeId, COUNT(Id) c FROM Opportunity
WHERE CreatedDate = LAST_N_DAYS:365 GROUP BY RecordTypeId
```

---

## 3. Fetch — HubSpot

HubSpot is a first-class path here, not a fallback: a third of this customer base runs
HubSpot as the CRM of record. The REST shapes below are the contract; pass the equivalent
arguments to whichever tool resolved. The official server names them `hubspot-list-objects`,
`hubspot-search-objects`, `hubspot-batch-read-objects`, `hubspot-list-properties`,
`hubspot-list-associations` and `hubspot-get-user-details`, but do not hard-code those —
resolve them as in §1 and adapt.

**Every HubSpot property value comes back as a string**, booleans and numbers included. Write
them through unchanged; the analyzer coerces them.

### 3.1 `raw/accounts.json` — companies, required

```
GET /crm/v3/objects/companies?limit=100&archived=false
    &properties=name,domain,website,hubspot_owner_id,industry,country,
      numberofemployees,type,lifecyclestage,createdate,hs_lastmodifieddate,
      notes_last_updated,hs_parent_company_id
```
Paginate on `paging.next.after` until it is absent. Concatenate every page's `results` into
one `records` array.

### 3.2 `raw/contacts.json` — required

```
GET /crm/v3/objects/contacts?limit=100&archived=false&associations=companies
    &properties=firstname,lastname,email,jobtitle,phone,hubspot_owner_id,
      lifecyclestage,hs_lead_status,createdate,lastmodifieddate,
      notes_last_contacted,country,associatedcompanyid,hs_additional_emails
```

`hs_additional_emails` matters more than it looks. HubSpot blocks a duplicate *primary*
email at create, so the duplicates that survive are the ones hiding in the secondary email
list — the analyzer reads it, so fetch it.

### 3.3 `raw/opportunities.json` — deals, required

```
GET /crm/v3/objects/deals?limit=100&archived=false&associations=companies,contacts
    &properties=dealname,amount,closedate,dealstage,pipeline,hs_is_closed,
      hs_is_closed_won,dealtype,hubspot_owner_id,createdate,hs_lastmodifieddate,
      hs_last_activity_date,hs_next_step
```

Keep the inline `associations` block — it is the only place the company link and the
contact roles live. A deal's company is an association, not a property, so a deal with no
`associations.companies` genuinely has no account.

If `hs_is_closed` is not available in the portal, the analyzer falls back to the stage label,
which makes `raw/governance.json` (§3.6) load-bearing rather than optional. Fetch it.

### 3.4 `raw/users.json` — owners, required

```
GET /crm/v3/owners/?limit=100&archived=false
GET /crm/v3/owners/?limit=100&archived=true
```

**Run both and concatenate.** The second call is the entire ownership check: a deactivated
HubSpot user becomes an archived owner, and their records keep pointing at them.

### 3.5 `raw/field_metadata.json` — required

```
GET /crm/v3/properties/companies
GET /crm/v3/properties/contacts
GET /crm/v3/properties/deals
```

Add `"object": "companies"` (or `contacts` / `deals`) to every result before writing — the
endpoint is per-object and that context is otherwise lost. `hubspotDefined: false` marks a
custom property.

There is **no object-level required flag in HubSpot**. "Required" is a property of a form or
a workflow, never of the schema, so every field in `policy_required_fields` is unenforced by
construction. Declare that in `raw/_coverage.json` (§4.2) rather than leaving the reader to
assume the check passed.

### 3.6 `raw/governance.json` — deal pipelines

```
GET /crm/v3/pipelines/deals
```

Flatten to one row per stage, `kind: "pipeline_stage"`:

```json
{"kind": "pipeline_stage", "pipeline": "default", "pipeline_label": "Sales Pipeline",
 "pipeline_archived": false, "stage_id": "contractsent", "label": "Contract Sent",
 "probability": 0.9, "is_closed": false, "archived": false}
```

Archived pipelines and archived stages still hold deals. Those deals are open pipeline that
no board view shows, which is a finding the analyzer will make for you.

### 3.7 `raw/field_fill.json` — optional

One search per property, `limit: 1`, and read `total` off the response:

```
POST /crm/v3/objects/deals/search
{"filterGroups":[{"filters":[{"propertyName":"hs_next_step","operator":"HAS_PROPERTY"}]}],
 "limit":1,"properties":["hs_object_id"]}
```

The denominator is the same call with `"filterGroups": []`. Normalize to
`{"object": "deals", "field": "hs_next_step", "total": 2044, "filled": 490}`.

Two cautions: search results are capped at 10,000, so a `total` of exactly `10000` is a
floor — slice by `createdate` and sum if you need the true number. And this is one HTTP call
per property, so skip it under `--quick` and let the analyzer sample instead.

### 3.8 `raw/picklist_metadata.json` and `raw/picklist_usage.json` — optional

Values come from the properties call you already made — `results[].options[]` gives
`{label, value, hidden, displayOrder}`. Deal stages come from the pipelines call. Normalize
to the same flat shape as §2.9, using the HubSpot property name as `field`.

Usage counts, one search per value:

```
POST /crm/v3/objects/deals/search
{"filterGroups":[{"filters":[{"propertyName":"dealstage","operator":"EQ",
  "value":"contractsent"}]}],"limit":1}
```

### 3.9 Not available on HubSpot — say so, do not skip silently

| Check | Why it cannot run |
|---|---|
| Duplicate-rule governance | Duplicate management lives in the Data Quality Command Center with no public API |
| Validation-rule governance | No validation-rule object exists; enforcement is per-form and per-workflow |
| Schema-required fields | Properties have no object-level required flag |
| Contact roles | Read as deal↔contact associations; association *labels* are the closest analogue and only exist if the portal configured them |
| Lead object | No separate Lead object in most portals — audit lead-stage contacts instead |

Every one of these goes into `raw/_coverage.json` so the report marks them **unavailable, not
clean**. A reader must never mistake a missing connector for a passing check.

---

## 4. Write `raw/`

### 4.1 The envelope — every file, no exceptions

```json
{
  "source": "opportunities",
  "crm": "salesforce",
  "tool": "run_soql_query",
  "query": "SELECT Id, Name, ... FROM Opportunity WHERE ...",
  "fetched_at": "2026-08-10T14:02:11Z",
  "truncated": false,
  "note": "",
  "records": [ ... ]
}
```

- `query` is not decoration. It is reprinted under every finding as "verify this yourself",
  and it is the reason a skeptical RevOps lead can check a number in sixty seconds. Paste the
  real query you ran, not a tidied-up version.
- `crm` must be the same in every file. One run reads one CRM.
- Set `truncated: true` the moment you hit a limit and stop short. The report then labels
  those counts as a floor instead of a total.
- **Record sources go in verbatim** — accounts, contacts, opportunities, users, leads,
  contact roles, field metadata. **Derived sources are normalized** to the shapes above —
  `field_fill`, `picklist_metadata`, `picklist_usage`, `governance`. That split is
  deliberate: the raw evidence has to survive unedited, and the aggregates have no single
  vendor shape worth preserving.

### 4.2 `raw/_coverage.json` — what you could not read

```json
{
  "source": "coverage",
  "crm": "hubspot",
  "tool": "n/a",
  "records": [],
  "unavailable": [
    {"check": "Validation-rule governance",
     "reason": "HubSpot has no validation-rule object; enforcement is per-form and per-workflow and is not exposed by the API."}
  ]
}
```

Files beginning with `_` are read as metadata, not as a data source.

### 4.3 Orgs bigger than the limits

If an object exceeds what one call returns, do **not** take the first 5,000 records and call
it a day — that biases every rate in the report toward whatever `ORDER BY` you used. Either:

- slice by `CreatedDate` / `createdate` into ranges and concatenate every slice, or
- keep a genuine sample, set `truncated: true`, and write the sampling rule into `note`.

Fill rates and picklist usage should come from the aggregate paths (§2.8, §2.10, §3.7, §3.8)
in a large org — they are exact regardless of record volume and cost a handful of queries.

---

## 5. Analyze

```bash
"$HOME/.leanscale-gtm/bin/crm-hygiene" analyze --raw "$RUN/raw" --out "$RUN"
```

Writes `manifest.json` and `findings.json`. Exit codes: `0` fine, `2` a required source came
back empty and the run aborted on purpose, `3` a config problem.

**On exit 2, do not retry blindly and do not hand-write a report.** The message names the
source and the likely cause. Read it out, fix the cause — a missing scope, a private
org-wide default, a filter that removed everything — and re-fetch that source. A report that
says "no issues found" because the connector was broken is the worst thing this plugin could
produce, which is why it refuses to produce it.

Useful flags: `--as-of YYYY-MM-DD` to reproduce an earlier run, `--config <path>` for an
alternate config, `--no-baseline` to compare against nothing.

## 6. Report

```bash
"$HOME/.leanscale-gtm/bin/crm-hygiene" report --findings "$RUN/findings.json" --out "$RUN"
```

Writes `report.md` and `report.html` and banks the baseline snapshot. Print the absolute path
to `report.html` and tell the user it opens locally with no network access — it is one
self-contained file they can forward.

Reports stay on the customer's machine. Never upload, deploy or host one.

---

## 7. Present it

Lead with the Hygiene Index and its direction, then the two or three findings that change a
decision this week. Do not read all forty out loud.

- **Run one** says so, plainly: *"This is your baseline. The comparison starts next run."*
  Do not editorialise about whether the score is good — you have nothing to compare it to,
  and inventing a benchmark is how you lose the room.
- **Run two onward**, lead with movement: what improved, what got worse, what is new. That
  is the whole reason the tool runs twice.
- Findings with `effort: quick` and `severity: critical` first — usually open deals past
  their close date and open pipeline owned by someone who left. Those two are visible to an
  executive without any tooling, which is exactly why they are embarrassing.
- Name what was **unavailable** out loud. "We could not read validation rules on HubSpot" is
  a fact the customer needs; silence reads as a pass.
- Offer one concrete next step, not a project plan. The report already carries the fix per
  finding.

## Do not

- Do not write to the CRM. Not a merge, not a field update, not a "quick cleanup". Duplicate
  clusters are **candidates for human review** and the report says so — a merge is
  irreversible and this plugin has no undo.
- Do not name a specific pair of records as "the same company" without the customer
  confirming it. Report the cluster, let a human decide.
- Do not fill a gap in `raw/` by estimating. A missing source is a finding about the
  connection, not a number to guess.
- Do not deploy the report anywhere.
