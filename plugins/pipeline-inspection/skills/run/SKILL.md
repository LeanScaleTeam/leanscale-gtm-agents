---
name: run
description: >-
  Inspect the open pipeline against the team's own rules and produce a ranked, per-deal call
  list — stalled stages measured against their own medians, missing next steps, serial
  close-date pushes, single-threaded deals, past-due close dates, activity silence,
  post-commit amount changes and quarter-end clustering. Read-only. Trigger on
  "/pipeline-inspection:run", "inspect my pipeline", "pipeline scrub", "deal inspection",
  "which deals are stuck", "run the pipeline review", "what's rotten in the pipeline",
  "prep my Monday pipeline meeting", or any request to audit open deals for hygiene and
  risk rather than to forecast a number.
argument-hint: "[--window 24m] [--owner \"Dana Whitfield\"] [--stage Negotiation]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# Pipeline Inspection — run

You are producing a **manager's call list**, not a forecast. The question this answers is
*"which open deals are violating the rules this team set for itself?"* — deterministic,
per-deal, ranked. If the user wants "what will we close", that is the `forecast-agent`
plugin; say so and stop.

**This skill is read-only.** You issue queries. You never create, update or delete a record.
If a CRM write tool resolves, do not call it.

---

## 0. Preflight

1. **Read the config.** `~/.leanscale-gtm/profile.json` and
   `~/.leanscale-gtm/pipeline-inspection.json`.
   If either is missing, stop and tell the user to run `/pipeline-inspection:setup` first —
   the thresholds in this plugin are *their* numbers, and running with defaults produces a
   report they will rightly ignore.

2. **Resolve the CRM tool.** Required capability: `crm.query`. The customer connected their
   own MCP server, so never assume a tool name.

   If `ToolSearch` is available (Claude Code), that is the fastest route:

   ```
   ToolSearch("run_soql_query salesforce query records")
   ToolSearch("hubspot crm search objects deals")
   ```

   Otherwise — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools already
   connected in this client. Typical resolutions: `mcp__salesforce__run_soql_query`;
   `hubspot-search-objects`, `hubspot-batch-read-objects`, `hubspot-list-associations`,
   `hubspot-get-schemas`. Some HubSpot servers expose only a generic request tool — the
   payloads below are the raw REST bodies, so they work either way.

   These names are the common cases, not the contract; the capability is the contract.

3. **Create the run directory** under the project, and note the exact timestamp:

   ```bash
   mkdir -p "./gtm-agents/pipeline-inspection/$(date +%Y-%m-%d-%H%M)/raw"
   ```

   Everything stays local. Never upload a report anywhere.

Honour any arguments: `--window` overrides `history_lookback_months`, `--owner` narrows to
one rep (also settable as `owner_scope` in config), `--stage` narrows the open set.

---

## 1. Fetch — Salesforce

Write each result to `raw/<name>.json` **exactly as the tool returned it**. Do not reshape,
filter or pretty-summarise; `analyze.py` handles Salesforce and HubSpot response shapes and
the raw files are the customer's audit trail.

### 1a. `open_opportunities.json` — REQUIRED

```sql
SELECT Id, Name, StageName, Amount, CloseDate, CreatedDate, LastModifiedDate,
       LastActivityDate, LastStageChangeDate, NextStep, Probability, Type,
       ForecastCategoryName, IsClosed, IsWon, OwnerId, Owner.Name, Owner.IsActive,
       AccountId, Account.Name, Account.Industry
FROM Opportunity
WHERE IsClosed = false
ORDER BY Amount DESC NULLS LAST
LIMIT 2000
```

- If setup recorded a **custom next-step field**, swap `NextStep` for it.
- **Multi-currency orgs** (`CurrencyIsoCode` exists on Opportunity): add `CurrencyIsoCode`
  and wrap the amount as `convertCurrency(Amount)` so everything lands in the corporate
  currency. Mixing record currencies silently is how a pipeline number becomes fiction.
- If `LastStageChangeDate` is not in the org's API version, drop it — stage age then comes
  from `OpportunityHistory` below, and the analyzer says so in the report.
- More than 2000 open opps: page with `WHERE Id > '<lastId>' ORDER BY Id` and concatenate
  into the same `records` array.

### 1b. `closed_opportunities.json` — REQUIRED

This is where the measured stage medians come from. Without it every threshold is a guess.

```sql
SELECT Id, Name, StageName, Amount, CloseDate, CreatedDate, IsWon, IsClosed,
       OwnerId, Owner.Name, AccountId, Account.Name, Type
FROM Opportunity
WHERE IsClosed = true AND CloseDate = LAST_N_MONTHS:24
ORDER BY CloseDate DESC
LIMIT 5000
```

### 1c. `stage_history.json` — strongly recommended

`OpportunityHistory` exists in **every** org and needs no configuration, unlike field history
tracking. It is the primary source for stage durations and the fallback for close-date and
amount changes.

```sql
SELECT OpportunityId, CreatedDate, StageName, Amount, CloseDate, ForecastCategory
FROM OpportunityHistory
WHERE CreatedDate = LAST_N_MONTHS:24
ORDER BY OpportunityId, CreatedDate
LIMIT 20000
```

If that is too large, scope it to the deals in play — chunk the ids **200 at a time**:

```sql
SELECT OpportunityId, CreatedDate, StageName, Amount, CloseDate, ForecastCategory
FROM OpportunityHistory
WHERE OpportunityId IN ('006...','006...')
ORDER BY OpportunityId, CreatedDate
```

### 1d. `field_history.json` — the close-date push count

```sql
SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate
FROM OpportunityFieldHistory
WHERE Field IN ('CloseDate','Amount','StageName')
  AND CreatedDate = LAST_N_MONTHS:24
ORDER BY OpportunityId, CreatedDate
LIMIT 20000
```

**Retention matters and you must report it.** Salesforce keeps field history for **18
months** by default (24 with Field Audit Trail), and history only exists at all if tracking
was enabled on that field. Three outcomes, all of which you handle explicitly:

| What you see | What it means | What you do |
|---|---|---|
| Rows come back for `CloseDate` | Tracking is on and within retention | Nothing — best case |
| Query succeeds, zero `CloseDate` rows | Tracking is **off** for CloseDate | Record the source with `count: 0`; the analyzer adds it to `unavailable`. Tell the user to enable history tracking on CloseDate today — it is the single highest-value 30-second change in this report |
| Query errors | No access to the object | Same as above, and note the permission error verbatim |

Never silently drop this. A pipeline report that omits push counts without saying so is
worse than one that admits the gap.

### 1e. `contact_roles.json` — single-threading

```sql
SELECT OpportunityId, ContactId, Contact.Name, Contact.Title, Contact.Email,
       Role, IsPrimary
FROM OpportunityContactRole
WHERE Opportunity.IsClosed = false
LIMIT 20000
```

If parent-field filtering is rejected, chunk by id instead:
`WHERE OpportunityId IN ('006...', …)` — 200 per call.

### 1f. `activities.json` and `open_tasks.json` — optional

`Opportunity.LastActivityDate` (already in 1a) is a free roll-up and is enough for the
silence check. Pull detail only if the user wants activity *types*, or if `LastActivityDate`
is empty across the board (which itself is a finding — say so).

```sql
SELECT Id, WhatId, Subject, ActivityDate, Status, IsClosed, TaskSubtype,
       CreatedDate, Owner.Name
FROM Task
WHERE WhatId IN ('006...') AND ActivityDate = LAST_N_DAYS:180
LIMIT 20000
```

```sql
SELECT Id, WhatId, Subject, ActivityDate, ActivityDateTime, StartDateTime,
       CreatedDate, Owner.Name
FROM Event
WHERE WhatId IN ('006...') AND ActivityDate = LAST_N_DAYS:180
LIMIT 20000
```

Merge Task + Event rows into one `activities.json` array. If `next_step_mode` is `task` or
`both`, also fetch open tasks:

```sql
SELECT Id, WhatId, Subject, ActivityDate, Status, IsClosed, CreatedDate, Owner.Name
FROM Task
WHERE IsClosed = false AND WhatId IN ('006...')
LIMIT 20000
```

### 1g. `stage_metadata.json` — free stage order

```sql
SELECT MasterLabel, ApiName, IsActive, IsClosed, IsWon, SortOrder,
       DefaultProbability, ForecastCategoryName
FROM OpportunityStage
ORDER BY SortOrder
```

This gives the canonical stage order, which is what makes "this deal moved backwards"
detectable. Always fetch it.

---

## 2. Fetch — HubSpot

Same file names, same required/optional split. HubSpot search returns 100 records per page;
follow `paging.next.after` until it is absent and concatenate into one `results` array.

### 2a. `open_opportunities.json` — REQUIRED

```http
POST /crm/v3/objects/deals/search
```
```json
{
  "filterGroups": [{ "filters": [
    { "propertyName": "hs_is_closed", "operator": "EQ", "value": "false" }
  ]}],
  "properties": [
    "dealname", "amount", "dealstage", "pipeline", "closedate", "createdate",
    "hs_lastmodifieddate", "notes_last_updated", "notes_last_contacted",
    "num_notes", "num_contacted_notes", "hubspot_owner_id", "hs_next_step",
    "hs_deal_stage_probability", "hs_forecast_category", "hs_manual_forecast_category",
    "hs_is_closed", "hs_is_closed_won", "hs_v2_date_entered_current_stage",
    "hs_v2_latest_time_in_current_stage", "dealtype", "hs_priority",
    "associatedcompanyid", "hs_analytics_source"
  ],
  "sorts": [{ "propertyName": "amount", "direction": "DESCENDING" }],
  "limit": 100
}
```

Two required post-steps:

1. **Resolve owner names.** `GET /crm/v3/owners?limit=500`, then inject
   `"hubspot_owner_name": "<firstName lastName>"` into each deal's `properties`. Without it
   the call list says `60007` where a manager needs a person.
2. **Resolve company names** if the customer cares about account grouping:
   `POST /crm/v3/objects/companies/batch/read` with `{"properties":["name"],"inputs":[{"id":"<associatedcompanyid>"}]}`,
   then inject `"associated_company_name"`.

If setup recorded a custom next-step property, add it to `properties` and set
`field_map.next_step` in config.

### 2b. `closed_opportunities.json` — REQUIRED

```json
{
  "filterGroups": [{ "filters": [
    { "propertyName": "hs_is_closed", "operator": "EQ", "value": "true" },
    { "propertyName": "closedate", "operator": "GTE", "value": "<24 months ago, epoch ms>" }
  ]}],
  "properties": ["dealname","amount","dealstage","pipeline","closedate","createdate",
                 "hs_is_closed","hs_is_closed_won","hubspot_owner_id","dealtype",
                 "associatedcompanyid"],
  "limit": 100
}
```

**Bonus that costs nothing:** HubSpot computes per-stage dwell time. Add
`hs_v2_time_in_<stageId>` (or legacy `hs_time_in_<stageId>`) for each stage id from 2g to the
`properties` list on closed deals. If stage history is unavailable, the analyzer measures the
medians from these instead — a fallback Salesforce does not have.

### 2c. `stage_history.json`

```http
POST /crm/v3/objects/deals/batch/read
```
```json
{
  "propertiesWithHistory": ["dealstage"],
  "properties": ["dealname"],
  "inputs": [{ "id": "<dealId>" }]
}
```

Batch **100 ids per call**. Run it over the open deals plus the closed deals you want medians
from. The analyzer reads the `propertiesWithHistory` shape natively.

### 2d. `field_history.json` — the close-date push count

```json
{
  "propertiesWithHistory": ["closedate", "amount"],
  "properties": ["dealname"],
  "inputs": [{ "id": "<dealId>" }]
}
```

**Retention caveat, and you must report it.** HubSpot returns the most recent versions per
property and truncates very old ones on high-churn properties. The analyzer records the
oldest change it actually saw in `push_distribution.coverage.oldest_change_seen`; surface
that number to the user so they know the push counts are a **floor, never an inflation**.

### 2e. `contact_roles.json` — single-threading

```http
POST /crm/v4/associations/deals/contacts/batch/read
```
```json
{ "inputs": [{ "id": "<dealId>" }] }
```

Save the `results` array as-is — the analyzer counts `results[].to[]` per deal. A deal with
`"to": []` is a genuine zero, which is a finding; a deal **missing from the response** is
unknown, which is not. Keep the distinction: include every open deal id in the inputs.

### 2f. `activities.json` and `open_tasks.json` — optional

`notes_last_updated` in 2a already covers the silence check. For detail, search each
engagement type and flatten with the deal id:

```http
POST /crm/v3/objects/calls/search      (also /emails/search, /meetings/search)
```
```json
{
  "filterGroups": [{ "filters": [
    { "propertyName": "hs_timestamp", "operator": "GTE", "value": "<180 days ago, epoch ms>" }
  ]}],
  "properties": ["hs_timestamp", "hs_engagement_type", "hs_activity_type"],
  "limit": 100
}
```

Associate them back with `POST /crm/v4/associations/calls/deals/batch/read` and write each
record with a top-level `"dealId"` inside `properties` — that is the key the analyzer joins on.

Open tasks (only needed when `next_step_mode` is `task` or `both`):

```http
POST /crm/v3/objects/tasks/search
```
```json
{
  "filterGroups": [{ "filters": [
    { "propertyName": "hs_task_status", "operator": "EQ", "value": "NOT_STARTED" }
  ]}],
  "properties": ["hs_task_subject", "hs_task_status", "hs_timestamp"],
  "limit": 100
}
```

Add `"dealId"` to each task's properties from
`POST /crm/v4/associations/tasks/deals/batch/read`.

### 2g. `stage_metadata.json`

```http
GET /crm/v3/pipelines/deals
```

Save the `results` array. This gives stage ids, labels, `displayOrder` (the stage order that
makes regressions detectable) and `metadata.isClosed` / `metadata.probability`. HubSpot stage
values in every other file are internal ids, so without this the report prints
`decisionmakerboughtin` instead of a stage name.

**Multiple pipelines:** ask which pipeline to inspect, or run once per pipeline into separate
run directories. Do not mix pipelines in one run — the stage medians become meaningless.

---

## 3. What is not reachable on each platform

State these plainly rather than letting a check quietly vanish. The analyzer already writes
missing sources into the report's `unavailable` list; your job is to make sure a source that
failed is recorded with `count: 0` rather than omitted.

| Check | Salesforce | HubSpot |
|---|---|---|
| Close-date pushes | `OpportunityFieldHistory`, needs tracking on, 18–24 month retention | Property history, old versions can be truncated |
| Stage dwell time | `OpportunityHistory` (always available) | `hs_v2_time_in_<stage>` + property history |
| Contact count | `OpportunityContactRole` — a real object with roles | v4 associations — **no role/title semantics**, so "who is the economic buyer" cannot be answered from associations alone |
| Contact roles by type | Yes (`Role`, `IsPrimary`) | Only via a custom property; report as unavailable |
| Stage order | `OpportunityStage.SortOrder` | `pipelines[].stages[].displayOrder` |
| Quota | No standard object — supply `quota.json` by hand | No standard object — same |

---

## 4. `raw/meta.json` — write this yourself

The analyzer builds the run manifest from it, so provenance is real rather than invented:

```json
{
  "crm": "salesforce",
  "org_name": "<from profile.json>",
  "as_of": "2026-08-10",
  "instance_label": "<from profile.json>",
  "notes": ["Anything odd you hit while fetching — a truncated page, a permission error."],
  "sources": [
    { "name": "open_opportunities", "tool": "mcp__salesforce__run_soql_query",
      "query": "SELECT Id, Name, StageName, ... FROM Opportunity WHERE IsClosed = false ..." },
    { "name": "field_history", "tool": "mcp__salesforce__run_soql_query",
      "query": "SELECT OpportunityId, Field, ... FROM OpportunityFieldHistory ...",
      "note": "CloseDate tracking enabled 2024-11; nothing before that exists." }
  ]
}
```

Source names must be exactly: `open_opportunities`, `closed_opportunities`, `stage_history`,
`field_history`, `contact_roles`, `activities`, `open_tasks`, `stage_metadata`, `quota`.

**`quota.json` is optional** and unlocks the coverage-ratio score. Shape:

```json
[{ "period": "2026-Q3", "quota": 3200000, "note": "Company new-business target." }]
```

---

## 5. Analyze and report

```bash
"$HOME/.leanscale-gtm/bin/pipeline-inspection" analyze \
  --run-dir "./gtm-agents/pipeline-inspection/<stamp>"

"$HOME/.leanscale-gtm/bin/pipeline-inspection" report \
  --run-dir "./gtm-agents/pipeline-inspection/<stamp>"
```

Produces `findings.json`, `manifest.json`, `report.md`, `report.html`, `call-list.csv`.

**If `analyze.py` exits non-zero with `SourceEmptyError`, do not work around it.** A required
source came back empty, which is a connection or permission problem, not a clean pipeline.
Relay the diagnosis and stop. Never hand the user a report that says "no issues found"
because a query failed.

---

## 6. Present it

Lead with the call list, not the score. In chat:

1. **One sentence of scale** — open deals, open dollars, share flagged.
2. **The top five deals by risk score**, each with the deal name, owner, amount and the rules
   it broke. This is the thing the manager acts on.
3. **The measured stage medians.** Most teams have never seen these. Say them out loud:
   *"Your median Negotiation is 27 days; the 75th percentile is 36. You are flagging above 53."*
4. **The two or three findings that carry the most dollars**, with the fix.
5. **Anything unavailable**, phrased as a specific thing to turn on — not "Slack not
   available" but "field history tracking is off for CloseDate, so push counts are missing;
   switching it on takes 30 seconds and makes the next run the useful one".
6. On a **baseline run**, say plainly that this is the starting point and the comparison
   begins next run. On later runs, lead the deltas: what got fixed, what got worse.

Then point at the files: `report.html` to forward, `call-list.csv` to work from.

**Do not editorialise beyond the data.** Every claim in your summary must be traceable to a
finding with a row table and a query behind it. If you want to say a rep is sandbagging, the
evidence is the push count and the stage age — show it, don't assert it.
