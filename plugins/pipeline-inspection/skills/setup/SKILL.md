---
name: setup
description: >-
  Connect and configure pipeline inspection: probe the CRM tools, read the org's stages and
  fiscal settings, MEASURE the real median days-in-stage, close-date-push distribution and
  contacts-per-deal before asking anything, then confirm the thresholds with a human, write
  the config, and prove it with a smoke test that produces a real finding. Trigger on
  "/pipeline-inspection:setup", "set up pipeline inspection", "configure the pipeline agent",
  "connect my CRM to pipeline inspection", "what are my stage medians", or whenever a
  `/pipeline-inspection:run` fails and needs diagnosing — this skill doubles as the health
  check.
argument-hint: "[--reconfigure] [--pipeline \"Sales Pipeline\"]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# Pipeline Inspection — setup

Two rules govern this whole skill.

**Discover before you ask.** Any question the CRM could have answered makes the product feel
cheap. You are going to *measure* this team's stage medians, push distribution and
contacts-per-deal, and then ask them to react to their own numbers. Nobody has ever shown
them these numbers before; that moment is the reason they will trust the report.

**Prove it before you finish.** Setup ends with a real finding on real data and a pass/fail
table, or it isn't done.

Setup is **idempotent**. Re-running it is the supported way to diagnose a failed run.

---

## Step 0 — Locate this plugin

Everything below runs this plugin's scripts through a small shim at
`~/.leanscale-gtm/bin/pipeline-inspection`. Create it before anything else — nothing later works without it.

`AGENT_ROOT` is this plugin's own directory: the one containing `scripts/`, `skills/` and
`.claude-plugin/`. Inside Claude Code, `${CLAUDE_PLUGIN_ROOT}` already holds it. On Cursor,
VS Code, Codex CLI or Gemini CLI that variable does not exist — use the directory you loaded
this SKILL.md from, two levels up from `skills/setup/`.

If the agents were installed with `tools/install-skills.py` (the non-plugin path), this is
already done — skip to the confirmation below. Otherwise:

```bash
AGENT_ROOT="${CLAUDE_PLUGIN_ROOT:-<the directory this plugin was loaded from>}"
python3 "$AGENT_ROOT/scripts/lib/config.py" install-shim --plugin pipeline-inspection --root "$AGENT_ROOT"
```

It verifies the directory really is a plugin root, records it in
`~/.leanscale-gtm/pipeline-inspection.json`, and writes the shim. If it answers *"does not look like a
plugin root"*, the path is wrong — fix it now rather than debugging a later step.

Confirm it works before continuing:

```bash
"$HOME/.leanscale-gtm/bin/pipeline-inspection" --root
```

Re-running this is safe, and is the first thing to try if a run later fails with a missing
script — a plugin update moves the install and the recorded path goes stale.

---

## Step 1 — Probe the connectors

Required capabilities: `transcripts.*`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("run_soql_query salesforce query records")
    ToolSearch("hubspot crm search objects deals")
    ToolSearch("describe metadata object schema fields")
    ToolSearch("transcripts meetings recordings")     # optional enrichment only

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    transcripts.*  any vendor  gong / fireflies / chorus / grain / otter / zoom list+get transcript tools
                   fallback    docs.read over a folder of exported transcripts — no vendor is required

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


Report what each resolved tool actually provides, and be specific about failure. Not
"HubSpot not available" but *"`hubspot-search-objects` resolves, but a search on `deals`
returns 403 — the private app token is missing the `crm.objects.deals.read` scope."*

Confirm the CRM is reachable with the cheapest possible call before doing anything else:

- Salesforce: `SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false`
- HubSpot: `GET /crm/v3/pipelines/deals`

Transcripts are **not required** by this plugin. If a conversation-intelligence tool happens
to be connected, note it — a later run can use it to explain *why* a deal stalled — but never
block on it.

---

## Step 2 — Read the shared profile

Read `~/.leanscale-gtm/profile.json`. If it exists, **show the user what is already known and
ask them to confirm**, rather than re-interrogating them:

> I already have: Northwind Systems · Salesforce · fiscal year starts February · 14
> quota-carrying reps · material deal floor $5,000. Still right?

If it does not exist, create it — you are the first agent in the suite to run. Fill what the
CRM can tell you, then ask only for the rest:

- `fiscal_year_start_month` — **read it, never assume January**:
  `SELECT FiscalYearStartMonth, Name, OrganizationType FROM Organization` (Salesforce).
  HubSpot has no equivalent; ask, and offer January as the default rather than assuming it.
  Also ask whether their FY is named for the year it **ends in** (Feb-2026 start → FY2027,
  the Salesforce/NVIDIA convention) or **starts in** — write `fiscal_year_naming`.
- `quota_carrying_reps` — **ask directly.** Do not infer it from active user count; ratios
  against total headcount are wrong and embarrassing. Show the count of distinct owners of
  open opportunities as a prompt: *"38 people own an open deal. How many of those carry a
  quota?"*
- `material_deal_floor` — propose the **10th percentile of closed-won amount**, measured:
  pull closed-won amounts and compute it, then confirm.
- `segments` — read the picklist, don't invent it.

---

## Step 3 — Automatic discovery

Run all of this before you ask a single question.

### 3a. Salesforce

**Stages, in order, with won/closed semantics — free:**

```sql
SELECT MasterLabel, ApiName, IsActive, IsClosed, IsWon, SortOrder,
       DefaultProbability, ForecastCategoryName
FROM OpportunityStage
ORDER BY SortOrder
```

**Where the open pipeline actually sits:**

```sql
SELECT StageName, COUNT(Id) deals, SUM(Amount) amount
FROM Opportunity
WHERE IsClosed = false
GROUP BY StageName
ORDER BY SUM(Amount) DESC
```

**Dead stages — stages nobody has entered in six months:**

```sql
SELECT StageName, COUNT(Id) entries
FROM OpportunityHistory
WHERE CreatedDate = LAST_N_DAYS:180
GROUP BY StageName
```

Any active stage absent from that result is a stage the process no longer uses. Ask about it
explicitly (see Step 5).

**Field fill rates on the fields this plugin depends on** — run the pair and divide:

```sql
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND NextStep != null
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND Amount != null AND Amount > 0
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND LastActivityDate != null
SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = false AND LastStageChangeDate != null
```

**Custom-field inventory** — find the fields they built that this plugin should use instead
of the standard ones (a `Next_Step__c`, an `ACV__c`, a `Renewal_Date__c`):

```sql
SELECT QualifiedApiName, Label, DataType, IsCustom
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
ORDER BY QualifiedApiName
```

**Is close-date history even available?**

```sql
SELECT COUNT(Id) FROM OpportunityFieldHistory WHERE Field = 'CloseDate'
```

Zero means tracking is off. That is the single most valuable thing you will tell them today —
see Step 6.

**Record types and multi-currency**, if present, so you know whether one set of medians is
even meaningful:

```sql
SELECT RecordType.Name, COUNT(Id) FROM Opportunity WHERE IsClosed = false GROUP BY RecordType.Name
SELECT CurrencyIsoCode, COUNT(Id) FROM Opportunity WHERE IsClosed = false GROUP BY CurrencyIsoCode
```

### 3b. HubSpot

```http
GET /crm/v3/pipelines/deals          -> pipelines, stage ids, labels, displayOrder, isClosed
GET /crm/v3/properties/deals         -> every deal property, custom ones included
GET /crm/v3/owners?limit=500         -> owner id -> name
```

Open pipeline by stage: search with `hs_is_closed EQ false`, page through, and group by
`dealstage` locally — HubSpot search has no GROUP BY.

Fill rates: run the same search with `{"propertyName":"hs_next_step","operator":"NOT_HAS_PROPERTY"}`
and compare `total` against the unfiltered `total`. Repeat for `amount` and
`notes_last_updated`.

**Multiple pipelines:** if more than one has open deals, ask which to inspect and record it.
Never blend pipelines — medians across two different sales processes describe neither.

### 3c. Measure the numbers — this is the differentiator

Fetch a real dataset exactly as `skills/run/SKILL.md` describes (open + closed + stage
history + field history + contact roles + stage metadata), write it to a discovery run
directory, and run the analyzer with the shipped defaults:

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/pipeline-inspection" --root)"
mkdir -p "./gtm-agents/pipeline-inspection/discovery/raw"
# ... write raw/*.json and raw/meta.json exactly as the run skill specifies ...
"$HOME/.leanscale-gtm/bin/pipeline-inspection" analyze \
  --run-dir "./gtm-agents/pipeline-inspection/discovery" \
  --config "$AGENT_ROOT/config.example.json"
```

Then read `findings.json` → `sections` and pull out the three distributions that drive the
whole interview:

- `sections.stage_medians.rows` — closed samples, median, p75, p90 per stage
- `sections.push_distribution.rows` and `.by_owner` — how often close dates actually move
- `sections.contact_roles.rows` and `.by_band` — contacts per deal, by deal size
- `sections.cycle_time` — median create-to-won, and remaining runway from each stage
- `sections.close_date_clustering.rows` — the period-end spike

You now know more about their pipeline than they do. Ask accordingly.

---

## Step 4 — The interview

Ask these as **reactions to measurements**, one topic at a time, in this order. Never open
with a blank question.

### 1. Expected days in stage — per stage

> Measured from your own 218 closed deals over the last 24 months:
>
> | Stage | Median | 75th | 90th | Open deals |
> |---|---|---|---|---|
> | Discovery | 10d | 16d | 21d | 9 |
> | Qualification | 17d | 27d | 35d | 10 |
> | Negotiation | 27d | 36d | 42d | 8 |
>
> Default is to flag at 2× the median — so 54 days in Negotiation. Your 75th percentile is
> 36 and your 90th is 42, so 2× is a genuinely unusual deal rather than a slow one. Take
> the default, or set your own bar per stage?

Write their answer to `expected_days_in_stage` and, if they want their expectation to govern
rather than the measurement, set `stagnation_basis` to `expected` or `max_of_both`.

Call out the interesting gap explicitly: if they say Negotiation "should take two weeks" and
the measured median is 27 days, that gap is a finding in its own right — tell them.

### 2. Where does "next step" live?

> `NextStep` is filled on 41% of open deals, and 12 open deals have an open task with a due
> date. So it looks like some of the team uses the field and some use tasks. Which is the
> standard: the field, an open task, either, or neither?

Sets `next_step_mode` (`field` | `task` | `both` | `none`) and `next_step_field`. If they
have a custom field, put it in `next_step_field` and mirror it into `field_map.next_step`.
If they answer "neither", say plainly that you are switching the cheapest check in the
plugin off, and that it will appear in every report as unavailable.

### 3. How many close-date pushes is too many?

> Of 45 open deals: 30 have never moved their close date, 7 moved it once, 5 twice, and 3
> have moved it three or more times — one of them four times, slipping 118 days in total.

> Default flags at 3+. Given your distribution, 3 catches the genuine outliers. Keep it?

Sets `push_threshold` and `push_watch_threshold`. If field history is unavailable, say so
here and skip the question rather than asking them to guess.

### 4. Single-threading, by deal size

> Contacts per open deal today: 2 deals have none at all, 11 have exactly one, 18 have two,
> 14 have three or more. Broken out by size, your $250k+ deals average 2.4 contacts.

> Default bar: 1 contact under $25k, 2 above $25k, 3 above $100k, 5 above $250k. Where do
> you want the line for a six-figure deal?

Sets `single_thread_thresholds`.

### 5. What does "commit" mean to you?

> Your stages are Discovery → Qualification → Solution Review → Proposal → Negotiation →
> Contracting. Which of those means "we have told leadership this is happening"?

Sets `commit_stages`. Used for post-commit amount changes. If they say commit is a forecast
category rather than a stage, capture the stages that map to it — and note that
`ForecastCategoryName` (Salesforce) or `hs_manual_forecast_category` (HubSpot) is already in
the raw data if they want it used instead.

### 6. Deal-size bands

> Your open deals run from $6k to $610k; the 10th percentile of closed-won is $8,400.
>
> Proposed bands: Small (under $50k), Mid-Market ($50k–$250k), Enterprise ($250k+). And
> should I ignore open deals under $8,400 as noise?

Sets `deal_size_bands` and `material_deal_floor`. Deals with **no amount at all** are never
filtered out — they are their own finding.

### 7. Activity silence

> Median gap since last logged activity on an open deal is 8 days; 10 deals have gone over
> 21 days and 3 have never had an activity logged at all.
>
> Default silence window is 21 days. Late-stage deals usually deserve a tighter one — 10
> days in Negotiation, 7 in Contracting?

Sets `activity_silence_days` and `activity_silence_days_by_stage`.

### 8. Cadence

> Do you inspect the pipeline weekly or monthly? It sets the rhythm the baseline compares
> against — weekly inspection with a monthly report is the usual mismatch.

Sets `inspection_cadence`.

### 9. Dead stages, if discovery found any

> `Technical Validation` and `Legal` are active stages, but no deal has entered either in
> 180 days, and 3 open deals are sitting in them. Are those dead stages I should treat as
> parked, or real stages that are just rare?

Fold the answer into `stage_order` / `commit_stages` and mention it in the readout.

### 10. Scope and privacy

> Inspect the whole pipeline, or one manager's team? And should reports pseudonymise rep
> names — useful if this gets forwarded to a board?

Sets `owner_scope`, and `redact_pii_in_reports` in the **shared profile**.

---

## Step 5 — Write the config

Write `~/.leanscale-gtm/pipeline-inspection.json` (create/extend `~/.leanscale-gtm/profile.json`
first if it was missing). Start from the plugin's `config.example.json` — find the directory
with `"$HOME/.leanscale-gtm/bin/pipeline-inspection" --root` — keep every `_<key>_help` line,
and replace the sample values with theirs.

Then **show the user the file you wrote**, in full, and tell them it is theirs to hand-edit —
it lives in their home directory and survives plugin updates.

---

## Step 6 — Smoke test

Run the real pipeline on a small slice — one stage or one owner, or just the discovery run
you already fetched — with the config you just wrote:

```bash
"$HOME/.leanscale-gtm/bin/pipeline-inspection" analyze --run-dir "./gtm-agents/pipeline-inspection/discovery"
"$HOME/.leanscale-gtm/bin/pipeline-inspection" report  --run-dir "./gtm-agents/pipeline-inspection/discovery"
```

**Show them one real finding, with the deal name, the owner, the amount and the rule it
broke.** A setup that ends without producing output has not proved anything. Then open
`report.html` and point at `call-list.csv`.

If `analyze.py` aborts with `SourceEmptyError`, that is the setup result — relay the
diagnosis verbatim, fix the connection, and re-run. Do not lower `require_closed_history` to
make the error go away unless the org genuinely has no closed deals.

---

## Step 7 — Pass/fail table

Finish with this, filled in honestly:

| Check | Status | What it means | To fix |
|---|---|---|---|
| CRM reachable | PASS | 45 open deals, 218 closed in 24 months | — |
| Stage metadata | PASS | 8 stages with sort order | — |
| Stage history | PASS | 556 transitions, medians measured on 335 completed intervals | — |
| Close-date field history | **FAIL** | Tracking is off for CloseDate — push counts unavailable | Setup → Object Manager → Opportunity → Fields & Relationships → Set History Tracking → tick Close Date. Takes 30 seconds; the next run gets the highest-signal check in the plugin |
| Contact roles | PASS | 109 roles across 43 of 45 open deals | — |
| Activity signal | WARN | `LastActivityDate` empty on 22% of open deals | Those reps are not logging; the silence check will over-report them |
| Quota | SKIP | No quota supplied | Drop a `quota.json` in the run's `raw/` to get a coverage ratio |
| Baseline | PASS | First snapshot written; next run shows movement | — |

Then, in plain English: **what will work, what will not, and what they must do about each.**
Be specific about the cost of each gap — "no push counts" is abstract; "you cannot tell a
deal that has slipped four times from one that has never moved, which is the single best
predictor you have" is not.

Close by telling them the command they will actually use from now on:
`/pipeline-inspection:run`.
