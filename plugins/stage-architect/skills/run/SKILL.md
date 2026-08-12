---
name: run
description: >-
  Measure what your deal stages actually mean, from your own closed history, and contrast it
  with what the team believes they mean. Computes cohort-controlled conversion per stage
  (by stage ENTERED, not current stage), time-in-stage medians and p90s, stage-skip rate,
  backwards movement, stages that do not discriminate, zero-dwell stages, closed-lost reason
  integrity, and lead lifecycle yield, then proposes buyer-verifiable exit criteria. Trigger
  on "/stage-architect:run", "audit our sales stages", "what are our real stage conversion
  rates", "are our pipeline stages right", "do we have too many stages", "which stages should
  we merge", "why is our funnel report wrong", "stage definitions", "exit criteria". Read-only.
argument-hint: "[--window 540d] [--pipeline default] [--as-of 2026-08-10]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# Stage Architect — run

You are measuring what this customer's deal stages **actually mean**, from their own closed
history, and contrasting that with what their team **believes** they mean. The gap is the
deliverable. Lead with it.

**This skill is read-only.** You issue queries. You never create, update, or delete a record.

## Before anything else

1. Read `~/.leanscale-gtm/profile.json` and `~/.leanscale-gtm/stage-architect.json`.
   If either is missing, stop and tell the user to run `/stage-architect:setup` first —
   without `believed_conversion_rates` there is no gap to show, and the gap is the product.
2. Create the run directory:

```bash
RUN="./gtm-agents/stage-architect/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw"
echo "$RUN"
```

3. Print the profile assumptions to the user before fetching, so wrong config is caught
   early: org name, CRM, fiscal year start, material deal floor, pipelines in scope,
   the sales-accepted stage, and the believed rates you are about to test.

## Step 1 — resolve capabilities

Required capabilities: `crm.query` and `crm.describe`.

If `ToolSearch` is available (Claude Code), that is the fastest route —
`ToolSearch("run_soql_query salesforce")` and `ToolSearch("hubspot crm search deals")`.
Otherwise match against the tools already connected in this client: `run_soql_query` on
Salesforce (and the same tool over `EntityDefinition` / `FieldDefinition` for `crm.describe`),
`hubspot-search-objects` / `hubspot-list-objects` plus `hubspot-list-properties` on HubSpot.
Those names are the common cases, not the contract.

If neither CRM resolves, stop — this plugin has no degraded mode that works without a CRM.

## Step 2 — fetch

Write **exactly what came back, unmodified** to `raw/`. Do not summarise, filter, or
pretty-print in transit; `analyze.py` owns all filtering so the numbers stay reproducible.

Files to produce (the analyser reads these names):

| File | Required | Contents |
|---|---|---|
| `raw/stage_metadata.json` | yes | pipelines + ordered stages, in the neutral shape below |
| `raw/opportunities.json` | yes | opportunity/deal records as returned |
| `raw/stage_history.json` | strongly | stage-transition rows as returned |
| `raw/lead_lifecycle.json` | optional | lead/contact lifecycle, if one exists |
| `raw/_sources.json` | yes | the tool + query behind each of the above |

### `raw/stage_metadata.json` — the one shape you must normalise by hand

This is the only file you assemble rather than dump, because Salesforce and HubSpot describe
their ladders completely differently. Build it from the picklist/pipelines call:

```json
{
  "crm": "salesforce",
  "instance_label": "Acme Production",
  "history_source": "OpportunityHistory",
  "loss_reason_field": "Loss_Reason__c",
  "loss_reason_values": ["Price", "Lost to Competitor", "No Decision", "Timing", "Other"],
  "pipelines": [
    {
      "id": "default",
      "label": "New Business",
      "stages": [
        {"id": "Discovery", "label": "Discovery", "order": 0, "is_closed": false, "is_won": false,
         "default_probability": 10, "is_active": true, "forecast_category": "Pipeline",
         "record_count_open": 34},
        {"id": "Closed Won", "label": "Closed Won", "order": 8, "is_closed": true, "is_won": true,
         "default_probability": 100, "is_active": true, "record_count_open": 0},
        {"id": "Closed Lost", "label": "Closed Lost", "order": 9, "is_closed": true, "is_won": false,
         "default_probability": 0, "is_active": true, "record_count_open": 0}
      ]
    }
  ]
}
```

`order` sets the ladder. `is_closed` + `is_won` decide the terminals: **won sits at the top
of the ladder, lost sits off it entirely.** That is deliberate — a lost deal did not advance,
it stopped, and putting Closed Lost at the end of the picklist order is exactly the mistake
that makes stock funnel reports read ~100% conversion at every stage.

`record_count_open` is the all-time count of open deals sitting in that stage (unfiltered by
window). It is what distinguishes "dead stage" from "stage full of deals nobody has touched
since before the window".

---

### Salesforce

**Org fiscal settings** (confirm, do not assume January):

```sql
SELECT Id, Name, FiscalYearStartMonth, DefaultOpportunityAccess FROM Organization
```

**Stage picklist, ordered** — `OpportunityStage` is a queryable standard object:

```sql
SELECT Id, MasterLabel, IsActive, SortOrder, IsClosed, IsWon, DefaultProbability, ForecastCategoryName
FROM OpportunityStage
ORDER BY SortOrder
```

Also describe `Opportunity.StageName` via `crm.describe` — record types can expose different
subsets of the same picklist, and a stage that is inactive on the master list may still hold
deals. If several record types expose different subsets, treat each as its own pipeline.

```sql
SELECT Id, Name, DeveloperName, IsActive FROM RecordType WHERE SobjectType = 'Opportunity'
```

**Open deals parked per stage** (unfiltered — this feeds `record_count_open`):

```sql
SELECT StageName, COUNT(Id) parked FROM Opportunity WHERE IsClosed = false GROUP BY StageName
```

**Opportunities in window:**

```sql
SELECT Id, Name, StageName, Amount, CreatedDate, CloseDate, LastModifiedDate,
       IsClosed, IsWon, Type, LeadSource, Loss_Reason__c,
       OwnerId, Owner.Name, RecordTypeId, RecordType.Name
FROM Opportunity
WHERE CreatedDate = LAST_N_DAYS:540
ORDER BY CreatedDate
LIMIT 5000
```

- Replace `Loss_Reason__c` with the field named in `stage_metadata.loss_reason_field`.
- Add `CurrencyIsoCode` **only** if the org is multi-currency; it does not exist otherwise
  and the query will fail with `No such column`.
- Do **not** filter by Amount here. The analyser applies `material_deal_floor` so it can
  report how many deals the floor excluded.
- Page past 5,000 with `WHERE CreatedDate = LAST_N_DAYS:540 AND Id > '<last id>' ORDER BY Id`
  if the org is large. Merge the pages into one array before writing the file.

**Stage history — the number that makes this plugin work.**

Primary source, `OpportunityHistory`. It is a standard child object, needs no setup, and is
populated for every org:

```sql
SELECT OpportunityId, StageName, Amount, ExpectedRevenue, CloseDate, CreatedDate, SystemModstamp
FROM OpportunityHistory
WHERE CreatedDate = LAST_N_DAYS:540
ORDER BY OpportunityId, CreatedDate
LIMIT 50000
```

Two things to know and to tell the customer:

- A row is also written when **Amount, CloseDate or Probability** change, so consecutive rows
  frequently repeat the same stage. That is not a transition. The analyser collapses
  consecutive duplicates; do not pre-filter them yourself.
- Deals loaded during a CRM migration have history that begins at the **load date**, not the
  real first touch. If coverage is low, the analyser raises `stage-history-incomplete`.

Fallback / cross-check, `OpportunityFieldHistory`:

```sql
SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate
FROM OpportunityFieldHistory
WHERE Field = 'StageName' AND CreatedDate = LAST_N_DAYS:540
ORDER BY OpportunityId, CreatedDate
LIMIT 50000
```

This one requires **field history tracking to be enabled on StageName** and is retained
**18 months**, or 24 with Field Audit Trail. If the org never enabled tracking, it returns
nothing — that is not a bug, and it is why `OpportunityHistory` is the primary. The analyser
accepts either shape.

**If both come back empty:** write `raw/stage_history.json` as `[]` and continue. The
analyser degrades to a snapshot analysis, raises a `critical` finding, and lists the affected
sections under `unavailable`. **Never present snapshot rates as measured conversion.**

**Loss reasons:**

```sql
SELECT Loss_Reason__c, COUNT(Id) losses
FROM Opportunity
WHERE IsClosed = true AND IsWon = false AND CloseDate = LAST_N_DAYS:540
GROUP BY Loss_Reason__c
ORDER BY COUNT(Id) DESC
```

Get the full picklist (including values nobody uses) from `crm.describe` on the field and put
it in `stage_metadata.loss_reason_values` — unused values are a finding.

**Lead lifecycle** (only if `lead_lifecycle_exists` is true):

```sql
SELECT Id, Status, CreatedDate, ConvertedDate, IsConverted, ConvertedOpportunityId, LeadSource
FROM Lead
WHERE CreatedDate = LAST_N_DAYS:540
LIMIT 5000
```

Wrap it with the ordering the customer confirmed at setup:

```json
{
  "stage_field": "Status",
  "ordered_stages": ["Raw", "MQL", "Sales Accepted", "SQL"],
  "accepted_stage": "Sales Accepted",
  "rejected_stages": ["Disqualified", "Recycled"],
  "records": [ ... exactly what the query returned ... ]
}
```

---

### HubSpot

**Pipelines and stages:**

```
GET /crm/v3/pipelines/deals
```

Map each returned stage to the neutral shape: `id` ← `id`, `label` ← `label`,
`order` ← `displayOrder`, `is_closed` ← `metadata.isClosed` (it comes back as the string
`"true"`/`"false"`), `is_won` ← `metadata.probability == "1.0"`,
`default_probability` ← `round(float(metadata.probability) * 100)`.

**Deals in window:**

```
POST /crm/v3/objects/deals/search
{
  "filterGroups": [{ "filters": [
    { "propertyName": "createdate", "operator": "GTE", "value": "1723248000000" }
  ]}],
  "properties": ["dealname", "dealstage", "pipeline", "amount", "createdate", "closedate",
                 "hs_lastmodifieddate", "hs_is_closed_won", "hubspot_owner_id",
                 "closed_lost_reason", "hs_deal_stage_probability"],
  "sorts": [{ "propertyName": "createdate", "direction": "ASCENDING" }],
  "limit": 100
}
```

`value` is **epoch milliseconds**. Page with `"after": "<paging.next.after>"` until
`paging.next` is absent. Search caps at 10,000 results per query — if the org exceeds that,
narrow by `createdate` ranges and concatenate. Write the merged `results` array.

**Stage history — two routes, use the first that works.**

Route A, calculated properties. Fast, one request, no batching. HubSpot maintains
`hs_date_entered_<stageId>` and `hs_time_in_<stageId>` for every stage on every deal, so add
them to the `properties` list above:

```
"hs_date_entered_appointmentscheduled", "hs_time_in_appointmentscheduled",
"hs_date_entered_qualifiedtobuy",       "hs_time_in_qualifiedtobuy",
"hs_date_entered_closedwon",            "hs_date_entered_closedlost"
```

Build the neutral history file from them:

```json
[
  {"dealId": "2400000001", "propertyName": "dealstage",
   "history": [
     {"value": "appointmentscheduled", "timestamp": "2026-02-03T14:22:00Z"},
     {"value": "qualifiedtobuy",       "timestamp": "2026-02-19T09:10:00Z"}
   ]}
]
```

Caveat to state plainly: `hs_date_entered_*` records only the **most recent** entry into each
stage. A deal that entered a stage twice shows one date, so **regression counts are a floor,
not a total**, on this route. Say that in the report if you used it.

Route B, true property history. Exact, includes every re-entry, but batched 100 at a time:

```
POST /crm/v3/objects/deals/batch/read
{
  "propertiesWithHistory": ["dealstage"],
  "properties": ["dealstage"],
  "inputs": [{ "id": "2400000001" }, { "id": "2400000002" }]
}
```

Each result carries `propertiesWithHistory.dealstage`: `[{value, timestamp, sourceType, sourceId}]`,
**newest first**. Write it through unchanged — the analyser sorts ascending. Use Route B
whenever the deal count makes it affordable; prefer it if regressions matter to the customer.

**Loss reason picklist** (for unused-value detection):

```
GET /crm/v3/properties/deals/closed_lost_reason
```

**Lifecycle** (contacts):

```
POST /crm/v3/objects/contacts/search
{ "properties": ["lifecyclestage", "createdate", "hs_lead_status",
                 "hs_date_entered_marketingqualifiedlead", "hs_date_entered_salesqualifiedlead"],
  "limit": 100 }
```

Wrap as `{"stage_field": "lifecyclestage", "ordered_stages": [...], "accepted_stage": "...",
"rejected_stages": [], "records": [...]}`. HubSpot has no lead-conversion object, so the
analyser reports opportunity conversion as not measurable rather than as zero.

---

### `raw/_sources.json`

Provenance for the manifest and for the "verify this yourself" toggles in the report. Record
the query you actually ran, including any edits you made:

```json
{
  "stage_metadata": {"tool": "run_soql_query", "query": "SELECT Id, MasterLabel, ... FROM OpportunityStage ORDER BY SortOrder", "note": ""},
  "opportunities":  {"tool": "run_soql_query", "query": "SELECT Id, Name, StageName, ... FROM Opportunity WHERE CreatedDate = LAST_N_DAYS:540", "note": "paged twice"},
  "stage_history":  {"tool": "run_soql_query", "query": "SELECT OpportunityId, StageName, CreatedDate FROM OpportunityHistory ...", "note": "OpportunityHistory; FieldHistory was empty"},
  "lead_lifecycle": {"tool": "run_soql_query", "query": "SELECT Id, Status, ... FROM Lead ...", "note": ""}
}
```

If a source failed, still record it with the real error in `note`. A silent omission becomes
a silently wrong report.

## Step 3 — analyse

```bash
"$HOME/.leanscale-gtm/bin/stage-architect" analyze --run-dir "$RUN"
```

Exit code 2 means a required source came back empty and the run aborted on purpose. Do not
work around it. Read the diagnosis it printed, fix the connection, re-fetch.

Optional flags: `--as-of YYYY-MM-DD` for a reproducible run, `--config <path>` to test a
different threshold without editing the saved config.

## Step 4 — report

```bash
"$HOME/.leanscale-gtm/bin/stage-architect" report --run-dir "$RUN"
```

Writes `report.md` and `report.html` into the run directory, applies deltas against the last
baseline, and records a new one. On the first run it prints the baseline message — repeat it
to the user verbatim, because run one has no comparison and pretending otherwise is how these
tools lose trust.

**Reports stay local. Never upload, deploy, or host a customer report.**

## Step 5 — interpret

Python computed the numbers; you supply the judgment. In your reply to the user:

1. **Open with the gap**, in one sentence, with the n: *"You believe you win 35% of deals.
   Over 470 resolved deals in the last 18 months you won 21%."* Everything else is support.

2. **Explain why their own funnel report disagrees.** Use the survivorship finding and its
   numbers. The short version: a deal that died in Discovery is sitting in Closed Lost today,
   so it has left the Discovery denominator, and the stock report answers "of the deals still
   alive at or past this stage, how many are past it" — which trends to 100% by construction.
   Name the naive number as wrong every time you cite it.

3. **Lead the recommendations with the merge.** Non-discriminating adjacent stages are the
   highest-value finding this plugin produces. Quote both rates, both n, and the p-value, and
   say plainly that a stage which does not change the odds is not a stage.
   If a pair reads "not tested", say so — *"these two look identical, but on 23 deals we
   cannot tell; ask again next quarter"* — never present an untested pair as a merge candidate.

4. **Sharpen the exit criteria against their own wording.** The proposed criteria in the
   report are archetype defaults. Rewrite them using the customer's actual stage definitions
   from config, their product, and their buying process — but hold the line the plugin exists
   to hold: every criterion must name an artifact **the buyer produced**. If you cannot name
   one, the criterion is rep-asserted and does not ship.

5. **Sequence the work.** The belief gap is repriced this week (it is a planning-model fix).
   Stage merges and criteria rewrites are a quarter of change management. Say which is which
   rather than handing over a flat list.

6. **Never state a rate without its n.** Every table in the report carries it; carry it into
   your prose too.

If any section is in `unavailable`, say so out loud. Those sections are **unavailable, not
clean**, and a customer who reads absence as a pass will be wrong.
