---
name: setup
description: >-
  Connect the Forecast Agent to Salesforce or HubSpot, measure this company's real conversion
  rates, slip distribution and commit accuracy, then interview the user about forecast
  methodology, what counts, roll-up, cadence and quota — and prove the pipeline with a live
  smoke test. Trigger on "/forecast-agent:setup", "set up the forecast agent",
  "configure forecasting", "connect my CRM to the forecast agent", "the forecast run failed",
  "forecast agent isn't working", or any first-time or health-check request for this plugin.
argument-hint: "[--reconfigure]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# Forecast Agent — setup

**Read-only.** Setup queries the CRM and writes two config files in the user's home directory.
It never modifies a record.

Idempotent and re-runnable. When a run later fails, run this again — it doubles as the health
check.

**The rule for this skill: discover before you ask.** Do not open with "what are your forecast
categories?" Read them. Then measure what they actually deliver, and ask the question that only
a human can answer, phrased in terms of what you found.

---

## Step 1 — Probe the connectors

```
ToolSearch("run_soql_query salesforce")      → crm.query, crm.describe (Salesforce)
ToolSearch("hubspot crm search deals")       → crm.query (HubSpot)
ToolSearch("describe metadata object schema")→ crm.describe
ToolSearch("transcripts meetings recordings")→ optional; deal-context colour only
```

Report exactly which resolved tool provides which capability. Be specific about failures.
Not *"Salesforce not available"* but *"`run_soql_query` resolves and returns Account rows, but
`SELECT Id FROM OpportunityFieldHistory` returns a MALFORMED_QUERY error — the connected user's
profile is missing 'View All Data' or field history tracking has never been enabled."*

`crm.query` is required. Everything else degrades with a stated consequence.

## Step 2 — Read what is already known

Read `~/.leanscale-gtm/profile.json`. If it exists, show the user what is already set and
**confirm rather than re-ask** — this profile is shared by every LeanScale GTM agent and nobody
should describe their fiscal year twice.

If it does not exist, you will create it at step 5. The keys this plugin needs from it:
`org_name`, `crm`, `fiscal_year_start_month`, `fiscal_year_naming`, `currency.multi_currency`,
`quota_carrying_reps`, `material_deal_floor`, `team_map.roll_up_field`, `redact_pii_in_reports`.

Read `~/.leanscale-gtm/forecast-agent.json` too. With `--reconfigure`, treat every answer as
up for revision; without it, only fill what is missing.

## Step 3 — Discovery. Measure first, in this order

### 3a. Structure and fiscal calendar

**Salesforce**

```sql
SELECT Id, Name, FiscalYearStartMonth, UsesStartDateAsFiscalYearName, DefaultCurrencyIsoCode
FROM Organization
```

```sql
SELECT Id, MasterLabel, ApiName, DefaultProbability, ForecastCategoryName, IsActive,
       IsClosed, IsWon, SortOrder
FROM OpportunityStage ORDER BY SortOrder
```

```sql
SELECT StageName, ForecastCategoryName, COUNT(Id) deals, SUM(Amount) dollars
FROM Opportunity WHERE IsClosed = false GROUP BY StageName, ForecastCategoryName
```

```sql
SELECT FISCAL_YEAR(CloseDate) fy, FISCAL_QUARTER(CloseDate) fq, IsWon,
       COUNT(Id) deals, SUM(Amount) dollars
FROM Opportunity WHERE IsClosed = true AND CloseDate = LAST_N_FISCAL_QUARTERS:12
GROUP BY FISCAL_YEAR(CloseDate), FISCAL_QUARTER(CloseDate), IsWon
ORDER BY FISCAL_YEAR(CloseDate), FISCAL_QUARTER(CloseDate)
```

```sql
SELECT Type, COUNT(Id), SUM(Amount) FROM Opportunity
WHERE IsClosed = true AND IsWon = true AND CloseDate = LAST_N_FISCAL_QUARTERS:8 GROUP BY Type
```

Is close-date history even being kept? This one question decides half the score:

```sql
SELECT COUNT(Id) FROM OpportunityFieldHistory
WHERE Field = 'CloseDate' AND CreatedDate = LAST_N_DAYS:180
```

Custom-field inventory — a company that forecasts on ARR usually has a field for it:

```
describe Opportunity → list every field ending __c, plus its type and label.
Look specifically for: ARR, MRR, ACV, TCV, Recognized Revenue, Next Step, Forecast Category,
Commit flag, Renewal Date, Segment.
```

Fill rates on the fields the analysis depends on:

```sql
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND (NextStep = null OR NextStep = '')
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND (Amount = null OR Amount = 0)
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND CloseDate < TODAY
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND Owner.IsActive = false
SELECT COUNT(Id) FROM User WHERE IsActive = true AND ManagerId = null
```

**HubSpot**

- `GET /crm/v3/properties/deals` — the full property inventory including the picklist options
  for `dealstage`, `dealtype` and any custom forecast-category property. This is the equivalent
  of describe.
- `GET /crm/v3/pipelines/deals` — stages, display order, and `metadata.probability`.
- `POST /crm/v3/objects/deals/search` with `"limit": 1` per filter and read the `total` —
  cheapest way to get counts by stage, by closed/won, by quarter, and to measure fill rates
  (e.g. `hs_next_step` `NOT_HAS_PROPERTY`).
- HubSpot has no org-level fiscal setting exposed on the deals API. **Ask for the fiscal year
  start month and say why you are asking**, rather than defaulting to January.
- `GET /crm/v3/owners` — note that owners carry no manager pointer.

### 3b. Now measure the things that make the questions worth asking

Pull a **discovery slice** — the last 8 fiscal quarters of closed deals, current open pipeline,
plus stage and field history — into a scratch directory using the exact queries in
`skills/run/SKILL.md`, then run the analysis without touching the baseline:

```bash
DISC="./gtm-agents/forecast-agent/_setup-$(date +%Y-%m-%d-%H%M)"
mkdir -p "$DISC/raw"
# ... write raw/*.json from the queries above ...
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
  --raw "$DISC/raw" --out "$DISC" --mode audit --no-baseline
```

Read `$DISC/findings.json` → `sections.measured`. You now have, from their own data:

- **conversion by stage entered**, with the 95% band and the cohort size for each
- **the survivorship version** of the same rates, so you can show the difference
- **commit attainment per quarter** and its swing
- **the slip distribution** — how late deals actually land versus the date stated 30 days out
- **median and p25 sales cycle**
- **the measured penalty on pushed deals**
- **how many closed deals and how many comparable quarters exist**

Everything in step 4 is phrased against these numbers.

## Step 4 — The interview

Ask these. Do not ask anything the queries above already answered.

**1. Methodology.**
> I found 4 forecast-category values in use (Commit, Best Case, Pipeline, Omitted) and 6 open
> stages carrying probabilities from 10% to 95%. Which one is the number you actually call —
> categories, weighted stage probability, or a hybrid where Commit and Best Case are called by
> category and everything below is weighted?

**2. What Commit means — and what it costs.** Lead with the gap.
> Your measured Commit→Won rate over the last 8 quarters is **71%**. Your current Commit total
> is $4.1M, and the way it's being presented implies you're calling it at essentially 100%.
> Which of those is the number you want me to forecast against?

Then, because this changes how the whole audit reads:
> What does a missed commit cost a rep here — nothing much, an uncomfortable Monday, or their
> credibility for the quarter? If it's expensive, Commit is probably sandbagged and the delta
> will run the other way. If it's free, it's inflated. I read the same numbers differently
> depending on your answer.

**3. What you forecast, and how the pieces count.**
> Bookings, ARR, or recognised revenue? I can see an `ARR__c` field populated on 78% of
> closed-won deals, so ARR is available if that's the number.
>
> And renewals are **22% of closed-won dollars** over the last 8 quarters, expansion another
> 19%. Does each of those count toward the forecast number, or are renewals run on a separate
> cadence? Getting this wrong makes every number in the report wrong, so I'd rather be annoying
> about it now.

**4. Roll-up hierarchy.**
> `ManagerId` is populated for 12 of your 14 opportunity owners; Wes Carmody and one other have
> no manager set. Is `ManagerId` the real forecast hierarchy, or do you roll up by territory or
> role instead? (HubSpot: owners have no manager field at all — name the property that holds it,
> or I'll roll up rep → org only and say so in the report.)

**5. Cadence and deadline.**
> When is the forecast submitted, and what's the cut-off? "Wednesday 5pm ET, weekly" tells me
> which snapshot to compare against and when it's worth scheduling this run — ideally the
> morning before the deadline, not after.

**6. Quota.**
> Quota is almost never in the CRM, and it isn't in yours — `ForecastingQuota` returned nothing
> (or: HubSpot has no quota object). Give me this period's quota per rep, or one number for the
> org, and I'll show coverage. If you'd rather not, I'll skip coverage entirely rather than
> compute it against a denominator I made up.

**7. How far back history is credible.**
> You have 11 quarters of closed deals. But three of your stages have only existed since March,
> and I can see the win rate step-change in FY26-Q4. How far back is genuinely comparable —
> 8 quarters, or should I stop at the re-stage?

**8. Audit first, or the call.**
> **My recommendation, and the default: audit first.** Your close-date history shows 56% of
> current forecast deals have already been pushed, which means a precise number would be a
> precise number built on dates that have been wrong at least once. Run the audit, fix the two
> or three things it names, then the call means something. Want me to do it that way, or do you
> need a number today regardless?

**Confirm, quickly, don't interrogate:**
- Fiscal year: *"`FiscalYearStartMonth` is 2, and `UsesStartDateAsFiscalYearName` is false —
  so February 2026 is FY2027 Q1. Right?"*
- Material deal floor: propose the 10th percentile of closed-won amount, then confirm.
- Closed-lost mapping: *"You have `Closed Lost` and `Closed Lost - No Decision`. I'm counting
  no-decision as a loss — for forecasting it is one, even though the CRM is being polite."*
- Single-thread threshold (default: 1 contact role), staleness window (default 21 days; use 7 if
  the median cycle is under 45), and the minimum days that make a close-date change a "push"
  (default 7, so housekeeping edits don't read as slippage).
- PII: does `report.html` get forwarded to people who shouldn't see rep names?

## Step 5 — Write config

Create `~/.leanscale-gtm/` if absent. Write or update:

- **`profile.json`** — only the keys that are missing, plus anything the user corrected. Never
  overwrite a key another plugin's setup already established without saying so.
- **`forecast-agent.json`** — start from `${CLAUDE_PLUGIN_ROOT}/config.example.json`, keep the
  `_comment` header and every `_<key>_help` line, and fill in the answers. On HubSpot, rewrite
  `field_map` to the HubSpot property names and `closed_won_stages` / `closed_lost_stages` to
  the internal stage ids (`closedwon`, `closedlost`), not the display labels.

Then **show the user both files** and say in one line what each key will do to the numbers.

## Step 6 — Smoke test

Run the real pipeline on a small slice — the current quarter plus 4 quarters of history — and
show a genuine finding:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
  --raw "$DISC/raw" --out "$DISC" --mode audit --no-baseline
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --run "$DISC"
```

Show the Forecast Integrity Score, its band, and one real finding with its record count and the
query that produced it — then invite the user to run that query themselves. A setup that ends
without proving output is not finished.

If you have no CRM access at all yet, prove the machinery instead, and say clearly that this is
sample data:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
  --raw "${CLAUDE_PLUGIN_ROOT}/fixtures/salesforce/raw" --out /tmp/fa-smoke --mode audit \
  --config "${CLAUDE_PLUGIN_ROOT}/fixtures/salesforce/config.json" \
  --profile "${CLAUDE_PLUGIN_ROOT}/fixtures/salesforce/profile.json" --no-baseline
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --run /tmp/fa-smoke
```

## Step 7 — Pass / fail table

End with this table filled in, then a plain-English paragraph. No hedging.

| Check | Status | What it means | To fix |
|---|---|---|---|
| CRM query tool resolves | ✅ / ❌ | | |
| Open deals readable | ✅ / ❌ | n = | |
| Closed history ≥ 4 quarters | ✅ / ⚠️ / ❌ | n quarters, n deals | |
| Stage transition history | ✅ / ❌ | cohort-by-entered conversion | `OpportunityHistory` / batch-read `dealstage` |
| Close-date change history | ✅ / ❌ | push + slip + calibration | enable field history tracking on CloseDate |
| Forecast-category history | ✅ / ❌ | commit attainment by quarter | track `ForecastCategoryName` |
| Contact roles / associations | ✅ / ❌ | single-threading | |
| Manager hierarchy | ✅ / ⚠️ / ❌ | rep → manager → org roll-up | |
| Stage probabilities readable | ✅ / ❌ | measured-vs-assigned gap | |
| Multi-currency conversion | ✅ / ⚠️ / n/a | totals are comparable | `convertCurrency()` / `amount_in_home_currency` |
| Quota | ✅ / ⚠️ | coverage ratio | enter it in config |
| Config written | ✅ | | |
| Smoke test produced a finding | ✅ / ❌ | | |

Then say, in plain words, what will work, what will not, and what the customer has to do about
each gap. For example:

> Everything works except close-date history: field history tracking has never been switched on
> for `CloseDate`, so I cannot tell you which deals have slipped or by how much. That costs you
> the Date-integrity and Calibration components — about a third of the score — and it caps what
> the audit can tell you. It takes an admin about two minutes to turn on (Setup → Object Manager
> → Opportunity → Fields & Relationships → Set History Tracking), it starts collecting
> immediately, and it **cannot be backfilled**. Turn it on today and this report gets materially
> better in one quarter.

## Do not

- Do not ask a question the CRM could have answered. Every one of those makes the product feel
  cheap.
- Do not default the fiscal year to January.
- Do not accept "we forecast bookings" without asking how renewals and expansion count.
- Do not invent a quota to produce a coverage ratio.
- Do not save a baseline from a discovery or smoke run — always pass `--no-baseline`. The
  customer's evidence trail starts with their first real run.
