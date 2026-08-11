---
name: setup
description: >-
  Configure the executive reporting pack — probe the CRM, read the real stage picklist and
  fill-rates, confirm which stage values mean what, capture targets, and prove the pipeline
  produces a real report. Read-only. Trigger on "/executive-reporting:setup", "set up
  executive reporting", "configure the reporting pack", "connect our CRM for board
  reporting", or after a failed run when you need to re-probe.
argument-hint: "[--reset]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# Executive Reporting — setup

**Read-only.** This skill queries the CRM and writes config files on this machine. It never
creates, updates or deletes a record.

Your job is to end with a config the customer has *confirmed*, not one you guessed. The single
most damaging thing this plugin can do is quietly assume which stage means "SQL" — a wrong
guess produces a confident, wrong conversion rate that nobody catches for a quarter.

---

## 1. Probe the connector

```
ToolSearch("run_soql_query salesforce")        → Salesforce path
ToolSearch("hubspot crm search deals")         → HubSpot path
ToolSearch("describe metadata object schema")  → schema path (needed for the picklist)
```

Report exactly which tool resolved for each capability. If neither CRM tool resolves, stop and
say so — do not proceed to the interview.

## 2. Read the shared profile

Read `~/.leanscale-gtm/profile.json`. If it exists, show the customer what is already known
(org name, CRM, fiscal year start month, currency, segments, quota-carrying reps) and ask them
to confirm rather than re-asking. If it does not exist, create it — you are the first plugin
they have set up. Never assume January for `fiscal_year_start_month`; on Salesforce read
`Organization.FiscalYearStartMonth` and confirm it.

## 3. Discovery — do this before you ask anything

This is the part that makes the product feel expensive. Run all of it, then show a summary.

**Salesforce**

```sql
-- stage picklist, in order, with the two flags that matter
SELECT MasterLabel, SortOrder, IsClosed, IsWon, DefaultProbability
FROM OpportunityStage WHERE IsActive = true ORDER BY SortOrder

-- how many deals actually sit in each stage over the window
SELECT StageName, COUNT(Id) n, SUM(Amount) amt
FROM Opportunity WHERE CreatedDate = LAST_N_MONTHS:13 GROUP BY StageName

-- amount-like fields, so you can propose the right one rather than assuming Amount
SELECT Id, Amount, ARR__c, MRR__c, TotalOpportunityQuantity FROM Opportunity LIMIT 200

-- source / channel fill
SELECT LeadSource, COUNT(Id) FROM Opportunity
WHERE CreatedDate = LAST_N_MONTHS:13 GROUP BY LeadSource

-- is there a lifecycle above the opportunity?
SELECT Status, COUNT(Id) FROM Lead WHERE CreatedDate = LAST_N_MONTHS:13 GROUP BY Status
```

**HubSpot** — pull deal pipelines and their stages (`/crm/v3/pipelines/deals`), then a deal
search grouped by `dealstage` over the same window, plus `hs_analytics_source` fill and the
lifecycle-stage distribution on contacts.

Compute and show:

- Every stage value, its record count, and its share of the total.
- **Fill rate over the last 13 months** for: created date, close date, each amount-like field,
  source/channel, owner, segment.
- Which stages have had **no record enter them in 180 days** — candidate dead stages.
- Whether stage counts **decrease monotonically** down the funnel.

## 4. The stage map — the question that matters most

Show the customer their real stage list with counts, then ask them to map it. Do **not** infer
the mapping from the labels.

> I found 9 active stages. Mapping them onto the canonical funnel so the conversion maths is
> unambiguous — please confirm or correct:
>
> | Your stage | Records (13mo) | I think this is |
> |---|---|---|
> | Discovery | 412 | `mql` |
> | Qualified | 240 | `sal` |
> | Proposal | 96 | `sql` — the pipeline stage |
> | Closed Won | 41 | `won` |
> | Closed Lost | 155 | `lost` |

**Two traps to state out loud when you ask:**

1. **The label is not the meaning.** We have seen an org whose displayed "SAL" is the canonical
   SQL stage and whose "SQL" is canonical SAL. Everything downstream routes through this map,
   not through the display names, so a swap here silently reverses two headline rates.
2. **If a later stage holds more records than an earlier one**, say so now and ask why. That is
   almost always a stage that is not being stamped, and it will make conversion read above 100%.

Then ask which canonical stage is **pipeline** (`pipeline_stage`) — usually the last stage
before a win. Everything the pack calls pipeline is measured there.

## 5. The rest of the interview

Ask only what the discovery could not answer:

- **Who owns expansion** — sales, CS, or blended into one bookings number? This decides the
  whole shape of the pack.
- **Targets.** Board, executive and field-level, per metric. They are usually three different
  numbers. If they have none, record that — the pack will say "no goal configured" rather than
  invent one.
- **What they currently quote.** Their conversion rate, their bookings number. Capture these in
  `believed_conversion` / `believed_metrics`. This is not idle curiosity: the pack reconciles
  against them and reports the gap, and finding that gap before the board meeting instead of
  during it is the highest-value thing this plugin does.
- **Which segments matter** — rep, channel, territory, region, industry, product, firmographic.
  Read the picklists, do not invent. Warn them that a segment added here is real work in every
  chart, and one left out means a rebuild.
- **Recurring cadence** — annual or monthly. Ask directly; do not infer from field names. If
  revenue is recognised monthly, the pack must say MRR, and it will never multiply by 12 on its
  own.
- **Sales cycle**, to set `cohort_ripeness_days`. Propose the measured 90th-percentile
  created-to-closed duration, then confirm.

## 6. Write the config

Write `~/.leanscale-gtm/executive-reporting.json` using `config.example.json` as the shape,
and update `~/.leanscale-gtm/profile.json` with anything new. **Show the customer the file you
wrote**, in full. They will edit it by hand later.

## 7. Smoke test — a setup that proves nothing is not done

Run the real pipeline against a narrow slice (one quarter is enough):

```bash
RUN="./gtm-agents/executive-reporting/$(date +%Y-%m-%d-%H%M)-setup"
mkdir -p "$RUN/raw"
# … fetch the same files the run skill fetches, but for a single quarter …
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" --run "$RUN"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --run "$RUN"
```

Show them the Reporting Readiness score and one genuine finding from their own data.

## 8. Pass/fail table

End with a table: capability, resolved tool, status, and — for anything failing — the specific
thing the customer must do. Then one plain-English paragraph: what will work, what will not,
and what the first run will withhold and why.

Setup is **idempotent**. Re-running it is the health check when a run later fails.
