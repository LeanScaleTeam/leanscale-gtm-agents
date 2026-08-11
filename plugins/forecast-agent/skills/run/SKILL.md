---
name: run
description: >-
  Run the forecast integrity audit (default) or produce a worst/likely/best forecast call
  with the delta against the rep-called commit, from Salesforce or HubSpot. Read-only.
  Trigger on "/forecast-agent:run", "audit our forecast", "can we even forecast off this CRM",
  "score our forecast integrity", "what's the real number this quarter", "is the commit real",
  "forecast call", "how much of commit is going to slip", "worst case for the quarter",
  "our forecast is always wrong", "board call number", or any request to check whether the
  pipeline supports the number sales leadership is about to say out loud.
argument-hint: "[--mode audit|forecast] [--period current|next|FY27-Q3] [--force]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# Forecast Agent — run

**Read-only.** This skill queries the CRM and writes files on this machine. It never creates,
updates or deletes a record, and it never uploads anything anywhere.

Two modes. **Audit is the default and you should keep it that way**, because the hard part of
forecasting is almost never the arithmetic — it is that close dates are fiction and stages are
not exit-criteria-based. Producing a confident number on top of that is how a board gets
surprised.

| Mode | What it answers | When |
|---|---|---|
| `audit` *(default)* | Can this CRM support a forecast at all? Scored 0–100. | Always run this first |
| `forecast` | Worst / likely / best, and the delta vs the called commit. | Once the audit clears the threshold |

---

## 0. Before anything

1. Read `~/.leanscale-gtm/profile.json` and `~/.leanscale-gtm/forecast-agent.json`.
   **If either is missing, stop and tell the user to run `/forecast-agent:setup` first.** Do not
   guess a fiscal calendar, a commit definition, or a stage list — every one of those changes
   the number.
2. Echo the assumptions you are about to run under, in one block: org, CRM, fiscal year start
   month and naming, methodology, what counts (new / expansion / renewal), commit buckets,
   measure (bookings / ARR / revenue), history window, quota (or "none configured").
3. Resolve the period. `--period current` (default) is the fiscal quarter containing today,
   derived from `fiscal_year_start_month`. **Never assume January.**
4. Create the run directory and remember it as `$RUN`:

```bash
RUN="./gtm-agents/forecast-agent/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw"
```

---

## 1. Probe the connector

```
ToolSearch("run_soql_query salesforce")      → Salesforce path
ToolSearch("hubspot crm search deals")       → HubSpot path
```

Use whichever matches `profile.crm.system`. If the expected tool does not resolve, say exactly
that and stop — do not silently fall back to the other CRM.

---

## 2. Fetch. Write every result to `$RUN/raw/<name>.json`

Every raw file uses the same wrapper so the manifest can record provenance:

```json
{ "source": "open_deals", "tool": "run_soql_query", "query": "SELECT ...",
  "fetched_at": "2026-09-30T07:00:00Z", "note": "", "records": [ ... ] }
```

Write these files. `open_deals` and `closed_deals` are **required** — if either comes back
empty the analysis aborts on purpose. Everything else is optional and degrades with a named
consequence.

| File | Required | What it powers | If missing |
|---|---|---|---|
| `meta.json` | yes | period, fiscal calendar, currency | analysis falls back to the profile |
| `open_deals.json` | yes | the forecast itself | **run aborts** |
| `closed_deals.json` | yes | every measured rate | **run aborts** |
| `stage_history.json` | no | entered-cohort conversion, stage skipping | conversion falls back to current stage (survivorship — inflated) |
| `field_history.json` | no | pushes, slip, commit calibration | Date integrity and Calibration become unmeasurable and the score is capped |
| `contact_roles.json` | no | single-threading | Buying-group component unmeasurable |
| `users.json` | no | rep → manager roll-up, inactive owners | rep level only |
| `stage_meta.json` | no | assigned probability vs measured | no probability-gap finding |
| `activities.json` | no | staleness when there is no next-step field | falls back to `LastActivityDate` |
| `quota.json` | no | coverage ratio | **no coverage ratio is produced** (deliberate) |

### 2a. Salesforce — copy-pasteable SOQL

Substitute the object/field names from `profile.objects` and `config.field_map` if this org has
renamed anything. `LAST_N_FISCAL_QUARTERS:n` respects the org's own fiscal calendar, which is why
it is used instead of a hardcoded date.

**meta / fiscal settings**

```sql
SELECT Id, Name, FiscalYearStartMonth, UsesStartDateAsFiscalYearName,
       DefaultCurrencyIsoCode, LanguageLocaleKey
FROM Organization
```

`UsesStartDateAsFiscalYearName = true` means the `starts_in` naming convention; false means
`ends_in`. Write the result into `meta.json` together with the resolved `period_start`,
`period_end`, `period_label`, `as_of`, `history_quarters`, and `multi_currency`.

**open deals** — the forecast population

```sql
SELECT Id, Name, AccountId, Account.Name, Amount, CurrencyIsoCode, CloseDate, CreatedDate,
       StageName, ForecastCategoryName, IsClosed, IsWon, Probability, Type, OwnerId,
       Owner.Name, Owner.IsActive, NextStep, LastActivityDate, LastModifiedDate,
       LeadSource, HasOpportunityLineItem
FROM Opportunity
WHERE IsClosed = false
  AND Amount >= <profile.material_deal_floor>
ORDER BY CloseDate
```

*Multi-currency orgs:* add the conversion function and rename the returned key to
`ConvertedAmount` when you write the JSON —

```sql
SELECT Id, Amount, convertCurrency(Amount), CurrencyIsoCode, CloseDate FROM Opportunity WHERE IsClosed = false
```

`convertCurrency()` returns the corporate-currency value. There is **no** standard
`ConvertedAmount` field on Opportunity; if you skip this, the totals add euros to dollars and
the plugin will raise a critical finding saying so.

**closed deals** — every measured rate comes from here

```sql
SELECT Id, Name, Amount, CurrencyIsoCode, CloseDate, CreatedDate, StageName,
       ForecastCategoryName, IsClosed, IsWon, Type, OwnerId, Owner.Name
FROM Opportunity
WHERE IsClosed = true
  AND CloseDate = LAST_N_FISCAL_QUARTERS:<config.history_quarters>
ORDER BY CloseDate
```

**stage transitions** — `OpportunityHistory` writes a row whenever Stage, Amount, CloseDate or
Probability changes. This is what makes cohort-by-*entered* possible.

```sql
SELECT OpportunityId, CreatedDate, StageName, Amount, CloseDate, ForecastCategory, Probability
FROM OpportunityHistory
WHERE CreatedDate = LAST_N_DAYS:900
ORDER BY OpportunityId, CreatedDate
```

**field history** — pushes, slip and commit calibration all live here

```sql
SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate, CreatedById
FROM OpportunityFieldHistory
WHERE Field IN ('CloseDate', 'ForecastCategoryName', 'Amount', 'StageName')
  AND CreatedDate = LAST_N_DAYS:900
ORDER BY OpportunityId, CreatedDate
```

> **This is the query that fails most often, and it fails quietly.** `OpportunityFieldHistory`
> only contains fields with **field history tracking switched on**, and standard retention is
> 18–24 months. If it returns zero rows for `CloseDate`, tracking is off — say so plainly:
> *"Close-date history is not being tracked, so nobody in this company can measure whether a
> deal has slipped, including you. Turn on field history tracking for CloseDate and
> ForecastCategoryName today; it starts collecting immediately but it cannot be backfilled."*
> Record it in `manifest.json` with that diagnosis and let the score reflect the gap.

**contact roles** — multi-threading

```sql
SELECT OpportunityId, ContactId, Contact.Name, Contact.Title, Contact.Email, Role, IsPrimary
FROM OpportunityContactRole
WHERE Opportunity.IsClosed = false
```

**users** — the roll-up hierarchy from `profile.team_map.roll_up_field`

```sql
SELECT Id, Name, ManagerId, Manager.Name, IsActive, UserRole.Name, Profile.Name
FROM User
WHERE IsActive = true
   OR Id IN (SELECT OwnerId FROM Opportunity WHERE IsClosed = false)
```

**stage metadata** — the assigned probability, so it can be compared with measured conversion

```sql
SELECT Id, MasterLabel, ApiName, DefaultProbability, ForecastCategoryName, IsActive,
       IsClosed, IsWon, SortOrder
FROM OpportunityStage
ORDER BY SortOrder
```

**quota (only if Collaborative Forecasting is on)**

```sql
SELECT Id, QuotaOwnerId, QuotaOwner.Name, QuotaAmount, StartDate, ForecastingTypeId
FROM ForecastingQuota
WHERE StartDate = THIS_FISCAL_QUARTER
```

If that object does not exist or returns nothing, **do not invent a quota.** Use
`config.quota.org_quota` / `period_quota_by_owner` if the customer entered one; otherwise skip
coverage entirely. Salesforce also blocks `ForecastingQuota` unless the running user has
"Manage Quotas" — a permission error here is a permission finding, not a missing quota.

*Volume note:* SOQL caps a single result set well below what `OpportunityFieldHistory` can
return for a large org. If a query truncates, split it by `CreatedDate` into quarter-sized
chunks and concatenate the `records` arrays before writing the file. Record the true total in
the wrapper's `note`.

### 2b. HubSpot — copy-pasteable CRM search payloads

**open deals** — `POST /crm/v3/objects/deals/search`

```json
{
  "filterGroups": [{ "filters": [
    { "propertyName": "hs_is_closed", "operator": "EQ", "value": "false" },
    { "propertyName": "amount", "operator": "GTE", "value": "<profile.material_deal_floor>" }
  ]}],
  "properties": ["dealname", "amount", "amount_in_home_currency", "deal_currency_code",
    "closedate", "createdate", "dealstage", "pipeline", "hs_forecast_category",
    "hs_manual_forecast_category", "hs_is_closed", "hs_is_closed_won",
    "hs_deal_stage_probability", "hs_next_step", "hubspot_owner_id", "dealtype",
    "num_associated_contacts", "notes_last_updated", "hs_lastmodifieddate",
    "days_to_close", "hs_projected_amount"],
  "sorts": [{ "propertyName": "closedate", "direction": "ASCENDING" }],
  "limit": 100
}
```

Page with the returned `paging.next.after` until it stops coming back. The search endpoint caps
at 10,000 results per query — if you hit it, split by `createdate` ranges.

**closed deals**

```json
{
  "filterGroups": [{ "filters": [
    { "propertyName": "hs_is_closed", "operator": "EQ", "value": "true" },
    { "propertyName": "closedate", "operator": "GTE", "value": "<first day of the history window, epoch ms>" }
  ]}],
  "properties": ["dealname", "amount", "amount_in_home_currency", "closedate", "createdate",
    "dealstage", "pipeline", "hs_forecast_category", "hs_is_closed", "hs_is_closed_won",
    "hubspot_owner_id", "dealtype", "num_associated_contacts"],
  "limit": 100
}
```

**stage + field history** — `POST /crm/v3/objects/deals/batch/read`, 100 ids per call

```json
{
  "propertiesWithHistory": ["dealstage", "closedate", "amount", "hs_forecast_category"],
  "properties": ["dealname"],
  "inputs": [{ "id": "3200000001" }, { "id": "3200000002" }]
}
```

Flatten the response into one row per change and write two files. `stage_history.json` takes
the `dealstage` entries; `field_history.json` takes the rest:

```json
{ "dealId": "3200000001", "property": "closedate", "value": "2026-10-31",
  "timestamp": "2026-08-14T13:22:00Z", "sourceType": "CRM_UI" }
```

HubSpot's history has **no `OldValue`** — the analysis reconstructs the previous value from the
preceding entry in the same series, which is why the entries must stay in timestamp order.

**pipelines and stages** — `GET /crm/v3/pipelines/deals`

Flatten to `{value, label, order (displayOrder), probability (metadata.probability × 100),
is_closed, is_won, forecast_category}` and write `stage_meta.json`.

**owners** — `GET /crm/v3/owners?limit=100`

Write `users.json`. HubSpot owners carry no manager pointer, so unless
`profile.team_map.roll_up_field` names a custom property that holds one, **there is no manager
level** — say so and roll up rep → org only.

**multi-threading** — HubSpot has no `Role` on a deal-contact association the way Salesforce
does. Use the `num_associated_contacts` property already requested above. If association labels
are in use (Sales Hub Pro+), you can enrich via
`POST /crm/v4/associations/deals/contacts/batch/read` and write `contact_roles.json`; otherwise
write nothing and let the Buying-group component report as unmeasurable rather than guessing.

**Not reachable on HubSpot — add each to the run's `unavailable` list:**
- **Quota.** There is no quota object. It must come from `config.quota`.
- **Contact *roles*.** Only association labels, and only on higher tiers. Count, not role.
- **A native forecast category.** `hs_forecast_category` exists only with Sales Hub Enterprise
  forecasting. Many teams keep it in a custom property — point `field_map.forecast_category` at
  theirs during setup, and if there genuinely isn't one, switch `methodology` to `weighted`.

---

## 3. Analyse

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
  --raw "$RUN/raw" --out "$RUN" --mode audit
```

For the call, once the audit has cleared the threshold:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
  --raw "$RUN/raw" --out "$RUN" --mode forecast
```

`--mode forecast` **refuses to publish a number** when the Forecast Integrity Score is below
`config.forecast_threshold`. That refusal is a feature. Only add `--force` when the user has
explicitly asked for the number anyway, and when you do, tell them the score goes on the slide
next to it.

Exit codes: `0` fine · `2` a required source was empty (the message names the likely cause —
relay it verbatim, do not paraphrase it into something softer) · `3` config problem.

## 4. Report

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --run "$RUN"
```

Writes `report.md` and `report.html` into `$RUN`. The HTML is self-contained and opens locally.
**Never upload, deploy or host it.** Give the user the local path.

---

## 5. What to say back

Lead with the thing that changes a decision, not with a list of files.

**Audit mode.** Open with the score, the band, and the one sentence that hurts:

> Forecast Integrity Score **57 / 100 — directional only.** The number that matters: over the
> last eight quarters, dollars called Commit have landed at **54%**, swinging ±22 points. And
> 56% of this quarter's forecast sits on close dates that have already been moved at least once.
> Until those two things change, a point forecast off this CRM is a guess with a decimal place.

Then the three findings a leader could act on this week, each with its record count. Then the
rep table — forecast problems are never evenly distributed, and naming the two reps to sit with
is worth more than the whole score.

**Forecast mode.** Never present a single number. Lead with the delta, because that is the
deliverable:

> The team is calling **$2.14M**. Measured against this company's own closed history the likely
> number is **$1.38M**, inside a range of **$0.70M to $2.31M**. The gap is **$761k — 36%** — and
> two thirds of it sits under one manager.

Then say how it was derived in three sentences: entered-cohort conversion (not current stage),
their own slip distribution for timing, and their own measured penalty for pushed deals. Offer
the deal-by-deal table for the forecast call.

**Always.** On run one, say plainly that this is the baseline and the comparison starts next
run. On later runs, lead the score with its movement.

## Arguments

| Argument | Effect |
|---|---|
| `--mode audit` | Default. Integrity audit only. |
| `--mode forecast` | Adds the three-number call and the delta. Refuses below threshold. |
| `--period current\|next\|FY27-Q3` | Which fiscal period to forecast. Default: the one containing today. |
| `--force` | Publish the call below the integrity threshold. Use only when asked. |

## Do not

- Do not write to the CRM. Nothing in this plugin has a write path.
- Do not present a point estimate. Three numbers or none.
- Do not quote a conversion rate measured on the deals *currently* in a stage. That is
  survivorship: the deals that fell out have left the denominator and every rate comes out high.
  The plugin measures the cohort that **entered** the stage, and the report shows both so the
  customer can see the size of the difference.
- Do not fill a gap with a benchmark. If quota is unknown there is no coverage ratio; if close-
  date history is off there is no push analysis. Missing measurement is a finding, not a blank.
