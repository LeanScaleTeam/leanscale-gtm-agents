---
name: run
description: >-
  Audit lead source data integrity in Salesforce or HubSpot and produce a findings report:
  null and "Other" rates by capture route, duplicate and near-duplicate source values with a
  proposed canonical taxonomy, whether source survives Lead-to-Opportunity conversion, UTM
  capture and agreement, first-touch versus last-touch confusion, and source values that carry
  volume but never win. Trigger when the user says "/lead-source:run", "audit our lead source",
  "is our channel report real", "why don't marketing and sales channel numbers match", "how much
  of our pipeline says Other", "check lead source data quality", "does lead source survive
  conversion", "clean up our lead source picklist", or asks whether attribution data can be
  trusted. READ-ONLY — it never writes to the CRM.
argument-hint: "[--window 540] [--crm salesforce|hubspot]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# Lead Source of Truth — run

You are auditing whether the source data underneath a channel report is capable of supporting
the claims made on it.

**Scope discipline.** This is not multi-touch attribution and you must not present it as such.
You do not model influence, distribute credit, or touch an ad platform. If the user asks for
multi-touch, say plainly: *"This measures whether the source data is trustworthy. It does not
model multi-touch influence — and building multi-touch on data with a 27% 'Other' rate is how
you get a model everyone stops believing."* Then run this anyway, because it is the prerequisite.

**Read-only.** Every query below is a read. Never call a create/update/delete tool during this
skill, no matter what the user asks for mid-run. Fixes are recommended in the report and applied
by a human.

---

## 0. Preflight

```bash
cat ~/.leanscale-gtm/lead-source.json 2>/dev/null || echo "MISSING"
cat ~/.leanscale-gtm/profile.json 2>/dev/null || echo "MISSING"
```

If either is missing, stop and say: *"Run `/lead-source:setup` first — it discovers your source
fields, measures the current null rate, and asks the handful of things your CRM cannot tell me.
It takes about twenty minutes and you only do it once."* Do not guess field names.

Read both files. Everything below is driven by them: `fields.reported_source`, `fields.first_touch`,
`fields.last_touch`, `fields.self_reported`, the UTM fields, `objects.*.raw_file`, `conversion.*`,
and `window_days`. Where this document writes `{reported_source}` or `{utm_source}`, substitute the
customer's actual field. Never send a query containing a field that is not in their config.

Create the run directory:

```bash
RUN="./gtm-agents/lead-source/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw" && echo "$RUN"
```

Resolve the tools you actually have — never assume a tool name:

Required capabilities: `crm.describe`, `crm.query`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("run_soql_query salesforce")        -> crm.query   (Salesforce)
    ToolSearch("hubspot crm search objects")       -> crm.query   (HubSpot)
    ToolSearch("describe metadata object schema")  -> crm.describe

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    crm.describe  salesforce  run_soql_query over EntityDefinition / FieldDefinition (useToolingApi where noted)
                  hubspot     hubspot-list-properties
    crm.query     salesforce  run_soql_query
                  hubspot     hubspot-search-objects / hubspot-list-objects / hubspot-batch-read-objects

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


---

## 1. Fetch — Salesforce

Run these in order. Write each result to `raw/` **exactly as returned**, no reshaping. If a
result set is too large for one write, write pages as `raw/leads.part01.json`,
`raw/leads.part02.json`, … — the analyzer concatenates `<stem>.part*.json` automatically.

### 1a. Size the job first

```sql
SELECT COUNT() FROM Lead WHERE CreatedDate = LAST_N_DAYS:540
```

If this returns more than ~50,000, tell the user the extract will take a while and offer to
narrow `window_days` to 365. Do not silently truncate.

### 1b. Leads → `raw/leads.json`

Substitute the customer's custom fields; the standard fields are mandatory.

```sql
SELECT Id, FirstName, LastName, Company, Email, Status,
       LeadSource,
       Most_Recent_Source__c, How_Did_You_Hear_About_Us__c,
       UTM_Source__c, UTM_Medium__c, UTM_Campaign__c,
       CreatedDate, CreatedById, CreatedBy.Name,
       IsConverted, ConvertedDate, ConvertedContactId, ConvertedOpportunityId
FROM Lead
WHERE CreatedDate = LAST_N_DAYS:540
ORDER BY CreatedDate DESC
LIMIT 2000
```

**Pagination.** `OFFSET` is capped at 2000 in SOQL and is the wrong tool here. Use a keyset
cursor on `CreatedDate`, taking the oldest `CreatedDate` from the previous page:

```sql
SELECT ... FROM Lead
WHERE CreatedDate = LAST_N_DAYS:540
  AND CreatedDate < 2026-02-14T09:22:11Z
ORDER BY CreatedDate DESC
LIMIT 2000
```

Repeat until a page comes back short. `CreatedBy.Name` is the honest proxy for the capture
route on Salesforce — an integration user, a data-loader account and a rep look completely
different, and that distinction is where most of the missing source turns out to live.

### 1c. Converted Contacts → `raw/contacts.json`

```sql
SELECT Id, FirstName, LastName, Email, LeadSource, CreatedDate
FROM Contact
WHERE Id IN (
  SELECT ConvertedContactId FROM Lead
  WHERE IsConverted = true AND CreatedDate = LAST_N_DAYS:540
)
```

If the semi-join is rejected (very large orgs hit the 100,000-row semi-join limit), fall back to
explicit ID batches of 200 taken from the lead extract:

```sql
SELECT Id, FirstName, LastName, Email, LeadSource, CreatedDate
FROM Contact WHERE Id IN ('0031234567890ABCAA','0031234567890ABDAA', ...)
```

### 1d. Converted Opportunities → `raw/opportunities.json`

```sql
SELECT Id, Name, LeadSource, StageName, IsWon, IsClosed, Amount, CloseDate,
       CreatedDate, CreatedById, CreatedBy.Name, OwnerId, Owner.Name
FROM Opportunity
WHERE Id IN (
  SELECT ConvertedOpportunityId FROM Lead
  WHERE IsConverted = true AND CreatedDate = LAST_N_DAYS:540
)
```

`CreatedBy.Name` matters: Salesforce has no `ConvertedBy` field on Lead, so the user who created
the opportunity **is** the converting user. That is how the report can tell "a rule overwrites
the source" from "one rep's conversion default overwrites the source".

### 1e. Field history → `raw/lead_history.json`

```sql
SELECT Id, LeadId, Field, OldValue, NewValue, CreatedDate, CreatedById, CreatedBy.Name
FROM LeadHistory
WHERE Field IN ('LeadSource','UTM_Source__c','UTM_Medium__c','UTM_Campaign__c')
  AND CreatedDate = LAST_N_DAYS:540
ORDER BY CreatedDate
```

Three things to know, and to tell the user if the result is empty:

1. **History only exists for fields with field-history tracking switched on.** An empty result
   usually means tracking was never enabled on `LeadSource`, not that nothing ever changed.
   That is itself worth reporting: without it, nobody can prove what the source used to say.
2. Salesforce retains field history for 18–24 months depending on edition.
3. `LeadHistory.Field` returns standard field names with a **lowercase initial** (`leadSource`,
   not `LeadSource`). The analyzer matches case-insensitively; do not "fix" the values.

Without history, the run still works — the stability component drops out of the score and the
report says so explicitly rather than scoring it as clean.

### 1f. Picklist values → `raw/field_definitions.json`

One query per source field, results concatenated into one file:

```sql
SELECT Value, Label, IsActive, IsDefaultValue, EntityParticleId
FROM PicklistValueInfo
WHERE EntityParticleId = 'Lead.LeadSource'
```

```sql
SELECT Value, Label, IsActive, IsDefaultValue, EntityParticleId
FROM PicklistValueInfo
WHERE EntityParticleId = 'Opportunity.LeadSource'
```

`PicklistValueInfo` requires the `EntityParticleId` filter — a bare `SELECT ... FROM
PicklistValueInfo` is rejected. If the object is not queryable in this org, use `crm.describe` on
Lead instead, or skip the file: without it the run simply cannot separate free-text pollution
from legitimate values, and it says so.

---

## 2. Fetch — HubSpot

Same rule: write raw responses unmodified, page into `<stem>.partNN.json` when large.

### 2a. Contacts → `raw/contacts.json`

`POST /crm/v3/objects/contacts/search`

```json
{
  "filterGroups": [
    { "filters": [
      { "propertyName": "createdate", "operator": "GTE", "value": "1739491200000" }
    ]}
  ],
  "properties": [
    "createdate", "lifecyclestage", "hs_lifecyclestage_opportunity_date",
    "hs_object_source_label", "hs_object_source",
    "original_lead_source", "hs_analytics_source",
    "hs_analytics_source_data_1", "hs_analytics_source_data_2",
    "hs_latest_source", "hs_latest_source_data_1",
    "how_did_you_hear_about_us",
    "utm_source", "utm_medium", "utm_campaign",
    "email", "firstname", "lastname"
  ],
  "sorts": [{ "propertyName": "createdate", "direction": "ASCENDING" }],
  "limit": 100
}
```

`value` is **epoch milliseconds** for the start of the window. Page with
`"after": "<paging.next.after>"` from the previous response. The Search API caps a single query
at 10,000 results; past that, walk forward in `createdate` windows (raise the `GTE` to the last
`createdate` you saw) rather than trying to page further.

Property notes that decide whether the output is right:

- `hs_analytics_source` is HubSpot's **automatic original source**. It is an enum
  (`PAID_SEARCH`, `ORGANIC_SEARCH`, `PAID_SOCIAL`, `SOCIAL_MEDIA`, `EMAIL_MARKETING`,
  `REFERRALS`, `DIRECT_TRAFFIC`, `OTHER_CAMPAIGNS`, `OFFLINE`) and it has near-perfect fill.
- `hs_latest_source` is the **last touch** equivalent. If a team says "we report on original
  source" and their report is actually reading this, that is a finding in itself.
- The manually-set property is almost always a custom one — `original_lead_source`,
  `lead_source`, `source__c`-style. It is the one with the duplicate values and the "Other"
  problem, and it is usually the one on the report. Set it as `fields.reported_source`.
- `hs_object_source_label` gives the capture route natively: `FORM`, `IMPORT`, `INTEGRATION`,
  `CRM_UI`. Salesforce has nothing this good.
- **The enum cannot represent a webinar, a trade show or an outbound sequence.** Those land on
  `OFFLINE` or `OTHER_CAMPAIGNS`. That is not a bug in this plugin and it should be said out
  loud in the readout: the two fields cannot be fully reconciled, so the org has to decide which
  one the report uses.

### 2b. Deals → `raw/deals.json`

`POST /crm/v3/objects/deals/search`

```json
{
  "filterGroups": [
    { "filters": [
      { "propertyName": "createdate", "operator": "GTE", "value": "1739491200000" }
    ]}
  ],
  "properties": [
    "dealname", "dealstage", "hs_is_closed_won", "amount", "closedate", "createdate",
    "original_lead_source", "hs_analytics_source", "hubspot_owner_id"
  ],
  "sorts": [{ "propertyName": "createdate", "direction": "ASCENDING" }],
  "limit": 100
}
```

**There is no `hubspot_owner_name` property** — a deal carries `hubspot_owner_id`
only. HubSpot silently omits property names it does not recognise rather than
erroring, so asking for a name returns no field and no warning. This analysis
does not key anything on owners, so the id is all it needs; if you extend it to
report per-owner, resolve the ids against `GET /crm/v3/owners` and join locally
rather than expecting a name on the deal.

### 2c. Contact → Deal associations → `raw/associations.json`

HubSpot has no `ConvertedOpportunityId`, so the conversion hop is the association.

`POST /crm/v4/associations/contacts/deals/batch/read`, 100 contact ids per call:

```json
{ "inputs": [ { "id": "50001" }, { "id": "50002" } ] }
```

Merge every response into one file keeping the native shape:

```json
{ "results": [
  { "from": { "id": "50001" },
    "to": [ { "toObjectId": "300012",
              "associationTypes": [ { "category": "HUBSPOT_DEFINED", "typeId": 4 } ] } ] }
] }
```

Only batch the contacts that reached the opportunity lifecycle stage — that is the population
the survival rate is measured on, and it keeps the call count sane.

### 2d. Property history → inline on the contacts

**The Search API does not return property history.** Use batch read, 100 ids per call:

`POST /crm/v3/objects/contacts/batch/read`

```json
{
  "inputs": [ { "id": "50001" }, { "id": "50002" } ],
  "properties": ["original_lead_source", "hs_analytics_source", "utm_source"],
  "propertiesWithHistory": ["original_lead_source", "hs_analytics_source", "utm_source"]
}
```

Write these results into `raw/contacts.json` in place of the search results (they carry the same
`properties` bag plus `propertiesWithHistory`), or as `raw/contacts.part*.json` alongside them.
Leave `history_file` empty in the config — the analyzer reads `propertiesWithHistory` inline.

If the record count makes a full batch-read impractical, pull history for a random sample and
**say so in the readout**: overwrite rates are computed against the full record count, so a
sample makes every overwrite rate a floor, not an estimate. The manifest raises this warning
automatically when coverage is below 90%.

### 2e. Property definitions → `raw/properties.json`

`GET /crm/v3/properties/contacts` — keep the whole response; the analyzer reads `options` off
each property to find values that are not in the dropdown.

---

## 3. Record the queries

Write `raw/_queries.json` so every finding in the report carries the exact query that produced
it. A finding a customer cannot verify in their own CRM in sixty seconds is not shippable.

```json
{
  "primary":      { "tool": "run_soql_query", "query": "SELECT Id, LeadSource, ... FROM Lead WHERE ..." },
  "intermediate": { "tool": "run_soql_query", "query": "SELECT Id, LeadSource FROM Contact WHERE ..." },
  "deal":         { "tool": "run_soql_query", "query": "SELECT Id, LeadSource, IsWon FROM Opportunity WHERE ..." },
  "conversion":   { "tool": "run_soql_query", "query": "SELECT Id, LeadSource, ConvertedOpportunityId FROM Lead WHERE IsConverted = true AND ..." },
  "history":      { "tool": "run_soql_query", "query": "SELECT LeadId, Field, OldValue, NewValue FROM LeadHistory WHERE ..." }
}
```

---

## 4. Analyse and render

```bash
"$HOME/.leanscale-gtm/bin/lead-source" analyze --run-dir "$RUN"
"$HOME/.leanscale-gtm/bin/lead-source" report  --run-dir "$RUN"
```

`analyze.py` writes `manifest.json` and `findings.json`; `report.py` writes `report.md` and
`report.html` and takes the baseline snapshot.

**If `analyze.py` exits 3**, a required source came back empty. Do not retry blindly and do not
report "no issues found" — that would be a lie. Read the diagnosis it printed, then check, in
this order: (1) did the query actually return rows in the tool response; (2) can the connected
identity read the object at all; (3) is the window so narrow that nothing falls inside it. Say
which one it was.

To re-render without taking a second baseline snapshot:
`"$HOME/.leanscale-gtm/bin/lead-source" report --run-dir "$RUN" --no-baseline`

---

## 5. Read the report back to them

Open `findings.json` and lead with the sentence that lands, not with a list. In order:

1. **The headline.** Source Integrity Score out of 100, its band, and which components were
   measured. If it is under 50, say the words: the channel report is fiction. If components were
   dropped, name them — a score built from three of five components is a different number.
2. **The one they have never measured**: the Lead-to-Opportunity survival rate. Walk the
   arithmetic out loud — eligible converted records, how many kept the source, how many arrived
   blank, how many arrived carrying something else, and how many converted with no opportunity
   and were excluded. This is usually the finding that changes the meeting.
3. **Where the missing source concentrates.** One capture route almost always owns most of the
   hole. That turns "we need a data quality programme" into "we need to fix one integration".
4. **The duplicate clusters**, as proposals. Say explicitly: nothing was merged, every group
   needs a human to confirm or reject, and the record counts are there so they can judge the
   blast radius. Never say "we cleaned up your taxonomy".
5. **The self-reported gap**, if measured, framed correctly: this is not an error to fix. It is
   the strongest evidence they have that last-click reporting understates the channels that
   create demand.
6. **Baseline.** On run one, say it plainly: this is the baseline, the comparison starts next
   run, keep the snapshot. On later runs, lead the readout with what moved.

Then point at the files:

```
report.html     open this — every finding carries the query that produced it
report.md       for pasting into a doc or ticket
findings.json   machine-readable, including the full taxonomy mapping proposal
manifest.json   what was read, from where, and how much came back
```

**Do not** offer to apply any fix. This plugin is read-only, the fixes are picklist edits,
validation rules and automation changes, and they belong to a human with a change window.

---

## Failure modes worth naming out loud

| Symptom | What it usually is | What to say |
|---|---|---|
| `LeadHistory` returns 0 rows | Field-history tracking was never enabled on the source field | "Nobody can prove what your source field used to say. Turning tracking on today starts the clock; it does not recover the past." |
| Survival looks suspiciously perfect (100%) | The opportunity source field is populated by a formula or rollup from the lead | "This is inherited, not preserved — check whether it is a formula field before celebrating." |
| Every record has the same UTM | A single hardcoded UTM on a site-wide template | "Your UTMs are decorative. Every record claims the same origin." |
| `hs_analytics_source` disagrees with the manual field on 40%+ | Usually correct, not broken: the enum cannot express webinars, events or outbound | "Both are right about different things. Pick which one the report uses and derive the other." |
| Distinct source values in the hundreds | The field is free text, or an integration writes campaign names into it | "This is not a taxonomy problem, it is a write-access problem. Restrict the picklist first." |
| Zero-conversion values that are all spelling variants | The join is broken, not the channel | "Do not cut this spend. The wins exist, they are filed under a different spelling." |
