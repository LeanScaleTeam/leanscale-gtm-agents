---
name: setup
description: >-
  One-time (and re-runnable) setup for the lead-source audit. Probes the connected CRM, inventories
  every field that looks like source/channel/campaign/medium, pulls their picklist values with
  record counts, measures the CURRENT null and "Other" rate before asking anything, then asks only
  the questions the CRM cannot answer — which field the channel report actually uses, first-touch
  versus last-touch intent, where UTMs land, the channel taxonomy they believe they have, whether
  source is supposed to survive conversion, and the self-reported source field. Writes
  ~/.leanscale-gtm/lead-source.json and ends with a smoke test and a pass/fail table. Trigger on
  "/lead-source:setup", "set up the lead source audit", "configure lead source", or when
  /lead-source:run reports missing config. READ-ONLY.
argument-hint: "[--crm salesforce|hubspot]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# Lead Source of Truth — setup

Idempotent. Re-running it is also the health check when a run later fails.

**The rule that governs this whole skill: discover before you ask.** Every question you ask that
the CRM could have answered makes the product feel dumb. Every question you fail to ask that
changes the analysis makes the output wrong. So steps 1–3 are all reading, and by the time you
open your mouth in step 4 you should be able to say *"I found six fields that look like source,
three of them are 90% empty"* rather than *"what fields hold source?"*

**Read-only.** Nothing in this skill writes to the CRM.

---

## 1. Probe

```
ToolSearch("run_soql_query salesforce")        -> crm.query   (Salesforce)
ToolSearch("hubspot crm search objects")       -> crm.query   (HubSpot)
ToolSearch("describe metadata object schema")  -> crm.describe
```

Report exactly which capability each resolved tool provides, by tool name. If nothing resolves
for `crm.query`, stop: this plugin needs it and there is no degraded mode worth shipping.

Probe failures must be specific. Never "HubSpot not available." Instead: *"The HubSpot tools
resolve, but a contacts search returns 403 — the private app token is missing the
`crm.objects.contacts.read` scope."*

---

## 2. Read the shared profile

```bash
cat ~/.leanscale-gtm/profile.json 2>/dev/null || echo "MISSING"
```

**If it exists**, show the customer what is already known and confirm rather than re-ask:
org name, CRM, fiscal year start month, quota-carrying reps. Only ask about what is absent.
Other agents in this suite wrote it; do not interrogate anyone about their fiscal year twice.

**If it is missing**, you are the first agent they have run, so create it. Read what you can:

- Salesforce fiscal year — read it, never assume January:
  ```sql
  SELECT Id, Name, FiscalYearStartMonth, DefaultLocaleSidKey, IsSandbox FROM Organization
  ```
  Then confirm, and ask which naming convention they use: a fiscal year starting in February is
  called FY27 by some companies (named for the year it ends in) and FY26 by others (the year it
  begins). Store as `fiscal_year_naming`: `ends_in` or `starts_in`.
- HubSpot has no fiscal-year setting exposed on the object API — ask directly.
- `quota_carrying_reps` — ask. It is the most load-bearing number in the whole suite and
  headcount is not a substitute.
- `segments` — read the picklist, do not invent one.

Write the profile with `schema_version: 1`.

---

## 3. Discovery — do all of this BEFORE the interview

### 3a. Salesforce — find every field that smells like source

```sql
SELECT QualifiedApiName, Label, DataType, IsCalculated
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Lead'
```

Repeat for `Contact` and `Opportunity`. Then filter the results yourself for anything whose API
name or label contains: `source`, `channel`, `campaign`, `medium`, `utm`, `origin`, `referr`,
`attribut`, `how_did`, `hear`, `first_touch`, `last_touch`, `lead_gen`, `acquisition`.

Cast the net wide here. The field the report is built on is regularly a custom one with a name
nobody would guess, sitting next to the standard `LeadSource` that everyone assumes is in use.

### 3b. Salesforce — measure each candidate before asking about it

Fill rate and top values, per candidate field:

```sql
SELECT LeadSource, COUNT(Id) records
FROM Lead
WHERE CreatedDate = LAST_N_DAYS:540
GROUP BY LeadSource
ORDER BY COUNT(Id) DESC
```

```sql
SELECT COUNT() FROM Lead WHERE CreatedDate = LAST_N_DAYS:540 AND LeadSource = null
```

Also worth running before you ask anything, because each one shapes a question:

```sql
-- how much of the book converts at all, and how much of it produces an opportunity
SELECT COUNT(Id) total, COUNT(ConvertedOpportunityId) with_opp
FROM Lead WHERE IsConverted = true AND CreatedDate = LAST_N_DAYS:540
```

```sql
-- is the source field being changed after creation, and by whom
SELECT Field, COUNT(Id) changes FROM LeadHistory
WHERE CreatedDate = LAST_N_DAYS:540 GROUP BY Field ORDER BY COUNT(Id) DESC
```

```sql
-- the picklist the field is supposed to be constrained to
SELECT Value, Label, IsActive FROM PicklistValueInfo
WHERE EntityParticleId = 'Lead.LeadSource'
```

```sql
-- who or what creates records, which is where the missing source concentrates
SELECT CreatedBy.Name, COUNT(Id) records
FROM Lead WHERE CreatedDate = LAST_N_DAYS:540
GROUP BY CreatedBy.Name ORDER BY COUNT(Id) DESC LIMIT 30
```

### 3c. HubSpot — same discovery, different calls

```
GET /crm/v3/properties/contacts
GET /crm/v3/properties/deals
```

Filter for the same name fragments. Then, for each candidate, get the value distribution — the
Search API's `total` with a filter per value is expensive, so instead pull one page of contacts
with the candidate properties and compute the distribution from the sample, or use:

```
GET /crm/v3/properties/contacts/{propertyName}
```

for the dropdown options, and one filtered search per "is this empty" check:

```json
{ "filterGroups": [{ "filters": [
    { "propertyName": "original_lead_source", "operator": "NOT_HAS_PROPERTY" },
    { "propertyName": "createdate", "operator": "GTE", "value": "1739491200000" }
]}], "limit": 1 }
```

The `total` on the response is the null count. Do the same with
`{"operator": "IN", "values": ["Other","Unknown","N/A"]}` for the placeholder count.

Always check both of these, because on HubSpot they are usually different fields:
`hs_analytics_source` (automatic, enum, near-perfect fill) and whatever custom property the team
actually reports on (manual, duplicated, 30% empty). Also record `hs_object_source_label`, which
gives the capture route natively.

### 3d. Show them what you found, before asking anything

Put it in a table. This is the moment the product earns its price:

| Field | Object | Records | Blank | "Other"/Unknown | Distinct values | Changed after creation |
|---|---|---|---|---|---|---|
| `LeadSource` | Lead | 41,208 | 3.8% | 23.6% | 24 | 1,140 records |
| `Channel__c` | Lead | 41,208 | 91.4% | 0.2% | 6 | 0 |
| `Most_Recent_Source__c` | Lead | 41,208 | 12.1% | 21.0% | 22 | 8,900 records |
| `LeadSource` | Opportunity | 6,140 | 41.9% | 9.2% | 19 | — |

---

## 4. The interview — informed by step 3

Ask in terms of what you found. Every question below is one the CRM genuinely cannot answer.

1. **Which field is the channel report actually built on?**
   > "I found 4 fields that look like source. `Channel__c` is 91% empty, so I assume it is
   > abandoned. `LeadSource` and `Most_Recent_Source__c` are both live. Which one is on the
   > report you present?"
   → `fields.reported_source.primary`. Also ask the equivalent on the Contact and Opportunity,
   because that is what the survival check compares against.

2. **First touch or last touch — what is each field *supposed* to hold?**
   > "Is `LeadSource` meant to be the first time we ever saw them, or the most recent thing they
   > did? And `Most_Recent_Source__c` — is that intended as last touch?"
   → `fields.first_touch`, `fields.last_touch`. Capture the *intent*; the run then measures
   whether the field behaves that way, and the gap is the finding. If they say one field holds
   both, write it down as-is — that is a real finding, not a configuration error to smooth over.

3. **Are UTMs captured, and where do they land?**
   > "I see `UTM_Source__c` and `UTM_Medium__c` on Lead, populated on 41% of records. Do your
   > forms write those on every submission, or only the first? And does anything map them into
   > the source field automatically?"
   → the UTM fields, plus a note on whether they are write-once. If they do not know, that is
   the answer, and the overwrite check will settle it.

4. **What channel taxonomy do you think you have?** Ask for the actual list — the one on the
   report, not the picklist. Paste it in.
   > "Give me the channel list as it appears on your board deck. Not the picklist, the list you
   > report. If they are the same, say so — that would be unusual and worth knowing."
   → `intended_taxonomy`. This is the belief; the run shows the measured reality against it, and
   the gap is a large part of the deliverable. If they cannot produce a list in under two
   minutes, that is a finding, and it goes in the report.

5. **Is source supposed to survive Lead → Contact → Opportunity conversion?**
   > "When a lead converts, should the opportunity carry the lead's source? Is there automation
   > that does that today, or is it whatever the converting rep leaves in the field?"
   → `conversion.*`. Capture whether they *believe* it survives; the measured rate against that
   belief is the sharpest number in the report.

6. **Is there a self-reported source field?** "How did you hear about us?" on a form, a
   qualification question an SDR asks, anything.
   > "I see `How_Did_You_Hear_About_Us__c`, filled on 38% of records. Is that a form field or
   > does an SDR type it? Do you report on it at all?"
   → `fields.self_reported`. If it exists, the run measures how far it diverges from the tracked
   source. Set expectations now: the gap will be large, and it is informative rather than wrong.

7. **Placeholder vocabulary.** Show them the top values and confirm which ones mean "we do not
   know": `Other`, `Unknown`, `N/A`, and any house-specific one such as `Not Provided` or
   `Legacy`.
   → `placeholder_values`.

8. **Capture routes.** Show them the `CreatedBy.Name` (or `hs_object_source_label`) breakdown and
   confirm the mapping: which of these accounts are forms, which are imports, which are
   integrations, which are people.
   → `creation_route.rules`. Getting this right is what turns the biggest finding from "you have
   a data quality problem" into "one integration is producing 60% of the hole".

9. **Reporting window.** Default 540 days for six quarters of trend. Confirm, or shorten it if
   the CRM was migrated recently — data from before a migration will produce a trend break that
   looks like a finding and is not.

---

## 5. Write the config

Write `~/.leanscale-gtm/lead-source.json`, using `${CLAUDE_PLUGIN_ROOT}/config.example.json` as
the template — keep its `_comment` header and every `_<key>_help` line, because customers edit
this file by hand.

```bash
mkdir -p ~/.leanscale-gtm
```

Then **show them the file you wrote** and point out the three keys most worth editing later:
`intended_taxonomy`, `placeholder_values`, `creation_route.rules`.

---

## 6. Smoke test — prove it works before you claim it does

Run the real pipeline against a narrow slice (90 days) into a temporary directory:

```bash
SMOKE="./gtm-agents/lead-source/smoke-$(date +%Y-%m-%d-%H%M)"
mkdir -p "$SMOKE/raw"
# fetch a 90-day slice exactly as /lead-source:run does, then:
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" --run-dir "$SMOKE" --window-days 90
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py"  --run-dir "$SMOKE" --no-baseline
```

`--no-baseline` matters: a smoke test must not consume the customer's baseline slot.

You can also exercise the clustering directly on the value counts you already pulled in step 3b,
which is the fastest way to show a real duplicate group:

```bash
echo '{"Webinar": 118, "webinar": 41, "Webinars": 30, "Paid Search": 96, "PPC": 44, "SEM": 19}' > /tmp/counts.json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/taxonomy.py" --counts-file /tmp/counts.json \
        --taxonomy "Paid Search,Webinar,Email,Referral"
```

**Setup is not finished until you have shown them two real things from their own data:**

1. the actual unattributed rate on their reported source field, stated as a number, e.g.
   *"27.3% of your leads carry no usable source — 3.8% blank, 23.5% sitting on 'Other'"*;
2. one real duplicate cluster with the record counts, e.g. *"'Webinar' (118), 'webinar' (41),
   'Webinars' (30) and 'Web Inar' (7) are almost certainly one channel split four ways — 78
   records are on a spelling that is not the one you report on."*

If you cannot produce both, the setup failed. Say so rather than declaring success.

---

## 7. Pass/fail table

End with this, filled in honestly, plus one plain-English sentence per gap saying what the
customer must do to close it.

| Check | Status | What it means |
|---|---|---|
| `crm.query` resolved | PASS · `run_soql_query` | Records can be read |
| `crm.describe` resolved | PASS | Picklists can be read |
| Shared profile present | PASS · created just now | Other agents will reuse it |
| Reported source field confirmed | PASS · `Lead.LeadSource` | Headline numbers measured against it |
| Downstream source fields | PASS · Contact + Opportunity | Survival measurable at both hops |
| Field history available | **FAIL** · tracking off for `LeadSource` | Overwrite detection and the stability component are skipped. Enable field-history tracking in Setup → Object Manager → Lead → Fields → Set History Tracking. It starts collecting from today; it cannot recover the past. |
| Picklist metadata | PASS · 12 active values | Free-text pollution separable from legitimate values |
| UTM fields | PASS · 3 fields | Capture rate and UTM-vs-source agreement measurable |
| Self-reported source | PASS · `How_Did_You_Hear_About_Us__c` | Disagreement rate measurable |
| Intended taxonomy captured | PASS · 12 values | Off-taxonomy detection active |
| Conversion join | PASS · `ConvertedOpportunityId` | Lead → Opportunity survival measurable |
| Smoke test | PASS · 90-day slice, 11 findings | Pipeline works end to end |

Then tell them what to do next: `/lead-source:run` for the full window, and that run one is the
baseline — the comparison starts on run two.

---

## Things that go wrong here, and what to say

| Symptom | Cause | What to say |
|---|---|---|
| `FieldDefinition` query is rejected | The connected identity lacks View Setup and Configuration | "This is a permission on the integration user, not a licence. It needs View Setup and Configuration to read field metadata." |
| Every candidate field is 90%+ empty | The real field is on a custom object, or reporting runs off a campaign-member join | "None of the source fields on Lead are populated enough to report on. Where does the number on your deck actually come from?" |
| They cannot produce a channel taxonomy | There isn't one, or it lives in a slide | Write down what they say and record `intended_taxonomy` as empty rather than inventing it. The absence is a finding — say so, gently, in the report rather than in the room. |
| HubSpot search returns 403 | Private-app scope missing | Name the exact scope: `crm.objects.contacts.read`, `crm.objects.deals.read`, `crm.schemas.contacts.read`. |
| Sandbox connected instead of production | `Organization.IsSandbox = true` | "You are pointed at a sandbox. The findings would be real, but they would be about test data." |
