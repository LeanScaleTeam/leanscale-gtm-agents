---
name: setup
description: >-
  One-time (re-runnable) setup for stage-architect. Probes the connected CRM, pulls the real
  stage picklist per pipeline with record counts and stage-history coverage, then asks only
  what the CRM cannot answer - the written stage definitions, which stage is sales-accepted,
  whether a separate lead lifecycle exists, whether stages are meant to be buyer-verifiable
  or rep-asserted, and what the team BELIEVES their conversion rates are. Writes config to
  ~/.leanscale-gtm/ and ends with a smoke test plus a pass/fail table. Trigger on
  "/stage-architect:setup", "set up stage architect", "configure stage analysis",
  "stage architect isn't working", or when :run reports missing config.
argument-hint: "[--reconfigure]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# Stage Architect — setup

Re-runnable and idempotent. It doubles as the health check when a run fails.

**Read-only.** Setup reads the CRM and writes two files in the user's home directory. It
never writes to the CRM.

**The rule that governs this whole skill:** never ask the CRM a question the CRM can answer.
Discover first, then ask only what lives in a human's head — and phrase every question in
terms of what you found.

**The one question that cannot be recovered later:** what the team believes their conversion
rates are. Ask it **before** you show any measured number. Once someone has seen the real
rate they cannot un-see it, and the belief you capture afterwards is worthless. If you
accidentally reveal a measured rate first, say so in the config note rather than pretending.

---

## Step 0 — Locate this plugin

Everything below runs this plugin's scripts through a small shim at
`~/.leanscale-gtm/bin/stage-architect`. Create it before anything else — nothing later works without it.

`AGENT_ROOT` is this plugin's own directory: the one containing `scripts/`, `skills/` and
`.claude-plugin/`. Inside Claude Code, `${CLAUDE_PLUGIN_ROOT}` already holds it. On Cursor,
VS Code, Codex CLI or Gemini CLI that variable does not exist — use the directory you loaded
this SKILL.md from, two levels up from `skills/setup/`.

If the agents were installed with `tools/install-skills.py` (the non-plugin path), this is
already done — skip to the confirmation below. Otherwise:

```bash
AGENT_ROOT="${CLAUDE_PLUGIN_ROOT:-<the directory this plugin was loaded from>}"
python3 "$AGENT_ROOT/scripts/lib/config.py" install-shim --plugin stage-architect --root "$AGENT_ROOT"
```

It verifies the directory really is a plugin root, records it in
`~/.leanscale-gtm/stage-architect.json`, and writes the shim. If it answers *"does not look like a
plugin root"*, the path is wrong — fix it now rather than debugging a later step.

Confirm it works before continuing:

```bash
"$HOME/.leanscale-gtm/bin/stage-architect" --root
```

Re-running this is safe, and is the first thing to try if a run later fails with a missing
script — a plugin update moves the install and the recorded path goes stale.

---

## Step 0b — The LeanScale corpus (optional)

**Optional. Skip it and everything still works.** This plugin can cite the stage-design playbook from the
LeanScale corpus when a finding matches one. Without a key it runs exactly as before and
simply says so where a finding could have been enriched.

Check what is already there:

```bash
python3 "$AGENT_ROOT/scripts/lib/config.py" mcp-key-status
```

If it reports `"present": false`, offer — do not assume:

> "I can fetch a LeanScale key so this run can cite the matching playbook. That sends your
> work email to mcp.leanscale.team and nothing else — no CRM data, now or ever. Want me to,
> or would you rather grab one yourself at https://mcp.leanscale.team/ ?"

Only if they say yes, and only with an email they give you:

```bash
curl -sS -X POST https://mcp.leanscale.team/api/access \
     -H 'Content-Type: application/json' \
     -d '{"email":"THEIR@EMAIL"}'
```

Store the returned key — via stdin, so it never lands in shell history or the process list:

```bash
printf '%s' 'THE_KEY' | python3 "$AGENT_ROOT/scripts/lib/config.py" save-mcp-key
```

That writes `~/.leanscale-gtm/mcp.json` at mode 0600 and prints one `export` line. **Show
that line and ask them to add it to their shell profile themselves.** A `.mcp.json` can only
read a real environment variable, and this setup does not edit files it does not own.

Two things to say plainly, because both will otherwise confuse them later:

- `claude mcp list` shows this server as **connected even with no key**, because the endpoint
  answers unauthenticated requests with server info. Green there is not proof of a working key.
  This check is the real one.
- The key only takes effect after they restart the client.

---

## Step 1 — probe connectors

| Capability | Required | Used for |
|---|---|---|
| `crm.query` | yes | stages, opportunities, stage history |
| `crm.describe` | yes | picklist values, record types, loss-reason options |

If `ToolSearch` is available (Claude Code), that is the fastest route:

```
ToolSearch("run_soql_query salesforce")
ToolSearch("hubspot crm search deals")
ToolSearch("describe metadata object schema")
```

Otherwise — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools already
connected in this client:

    crm.query     salesforce  run_soql_query
                  hubspot     hubspot-search-objects / hubspot-list-objects
    crm.describe  salesforce  run_soql_query over EntityDefinition / FieldDefinition
                  hubspot     hubspot-list-properties

These names are the common cases, not the contract; the capability is the contract.

Report which resolved tool provides which capability, by name. Be specific about failures:
not "Salesforce unavailable" but *"`run_soql_query` resolves and `SELECT Id FROM Opportunity
LIMIT 1` returns a row, but `SELECT ... FROM OpportunityHistory` returns 0 rows while 4,100
opportunities exist — the connected user most likely lacks read access to opportunity history."*

If neither CRM resolves, stop here and tell the user what to connect. There is no useful
degraded mode.

## Step 2 — read the shared profile

Read `~/.leanscale-gtm/profile.json`. If it exists, **show the user what is already known and
confirm it** rather than re-asking:

> Using your existing profile: Acme · Salesforce · fiscal year starts February · 14
> quota-carrying reps · material deal floor $5,000. Anything changed?

If it does not exist, you are the first agent in the suite to run. Create it. Ask only what
you cannot read, and read what you can:

- `fiscal_year_start_month` — read `SELECT FiscalYearStartMonth FROM Organization`
  (Salesforce) and confirm. Never assume January.
- `fiscal_year_naming` — `ends_in` (a FY starting Feb 2026 is FY2027, the Salesforce/NVIDIA
  style) or `starts_in`. Ask; companies feel strongly and both are common.
- `quota_carrying_reps` — ask directly. Nothing in the CRM answers it reliably.
- `material_deal_floor` — compute the 10th percentile of closed-won amount and propose it:
  *"10% of your won deals are under $4,200. Shall I ignore anything below that as noise?"*
- `segments` / `segment_field` — read the picklist, do not invent.
- `org_name`, `crm.system`, `crm.instance_label`, `redact_pii_in_reports`.

## Step 3 — automatic CRM discovery

This is the part that makes the product feel expensive. Run all of it before you ask a single
stage question, and show the user a table of what you found.

**Salesforce**

```sql
SELECT Id, MasterLabel, IsActive, SortOrder, IsClosed, IsWon, DefaultProbability, ForecastCategoryName
FROM OpportunityStage ORDER BY SortOrder
```
```sql
SELECT StageName, COUNT(Id) deals, MIN(CreatedDate) oldest, MAX(CreatedDate) newest
FROM Opportunity WHERE CreatedDate = LAST_N_DAYS:180 GROUP BY StageName ORDER BY COUNT(Id) DESC
```
```sql
SELECT StageName, COUNT(Id) parked FROM Opportunity WHERE IsClosed = false GROUP BY StageName
```
```sql
SELECT COUNT(Id) total FROM Opportunity WHERE CreatedDate = LAST_N_DAYS:540
```
```sql
SELECT COUNT(Id) history_rows, MIN(CreatedDate) earliest
FROM OpportunityHistory WHERE CreatedDate = LAST_N_DAYS:540
```
```sql
SELECT Id, Name, DeveloperName, IsActive FROM RecordType WHERE SobjectType = 'Opportunity'
```
```sql
SELECT Status, COUNT(Id) leads FROM Lead WHERE CreatedDate = LAST_N_DAYS:180 GROUP BY Status
```

Then `crm.describe` on `Opportunity` to inventory custom fields, and specifically to find the
loss-reason field. Look for API names matching `loss`, `lost`, `reason`, `competitor`,
`churn`. Report the candidates and their fill rate over closed-lost deals rather than picking
one silently.

**HubSpot**

```
GET  /crm/v3/pipelines/deals
GET  /crm/v3/properties/deals
POST /crm/v3/objects/deals/search   { "filterGroups": [], "properties": ["dealstage","pipeline"], "limit": 100 }
```

Count deals per pipeline and per stage from the search results. Check whether
`hs_date_entered_<stageId>` properties are present in the property list — if they are,
history is available cheaply via Route A in the run skill.

**Compute and show, per pipeline:**

| What | Why it matters |
|---|---|
| every stage, in order, with `is_closed` / `is_won` / probability | the ladder |
| deals **entered** per stage over 180 days | finds dead stages |
| open deals **parked** per stage, all time | distinguishes dead from abandoned |
| total opportunities in 540 days | tells you whether cohorts will be big enough |
| stage-history rows, and the earliest one | tells you whether conversion is measurable |
| share of in-window deals with at least one history row | the coverage number |
| loss-reason field candidates and their fill rate | terminal integrity |
| whether a lead lifecycle field is populated | whether there is a funnel ahead of the funnel |

**Say the hard thing about history now, not later.** If history coverage is under ~80%, or
the earliest history row is much later than the earliest opportunity, tell the user
immediately: Salesforce field history is retained 18 months (24 with Field Audit Trail), and
deals loaded during a migration have history starting at the load date. Recommend shortening
`history_lookback_days` to the period with full coverage. Conversion rates over partially
covered data skew optimistic, because the missing deals are the old, long, lost ones.

If there is **no** stage history at all, say so plainly and set expectations before the
interview: without it this plugin produces a snapshot analysis and cannot measure conversion,
dwell, skips, or regressions. It will still run, and it will label those sections unavailable.

## Step 4 — the interview

Ask these. Every question is phrased against what step 3 found. Do not ask any of them
generically.

1. **The stage list and their written definitions.** Show the ladder you discovered, then:
   > *"Here are your 9 stages in order. Is there a written definition for each one — a wiki
   > page, an enablement doc, a field help text? Paste it, or point me at it."*
   Capture the wording **verbatim** into `stage_definitions[<stage>].written_definition`.
   Do not paraphrase and do not improve it — the report holds their sentence up against the
   buyer-verifiable standard, and a cleaned-up version defeats the exercise. If no definition
   exists for a stage, record it as absent; that is itself the finding.

2. **Buyer-verifiable or rep-asserted, per stage.** For each definition, ask which side it
   falls on, and explain the distinction with their own text:
   > *"'Rep has confirmed budget' is rep-asserted — nothing outside your CRM has to change
   > for it to be true. 'Buyer confirmed the budget owner in writing' is buyer-verifiable —
   > there is an artifact. Which is each of yours meant to be?"*
   Set `verifiability` per stage, and `stages_are_meant_to_be` for the team's overall intent.

3. **Dead stages.** Phrase it with the counts you measured:
   > *"You have 9 stages. 4 have had no deal enter them in 180 days — Technical Validation,
   > Legal, Verbal, Pilot. Three of them have deals parked in them right now. Dead stages I
   > should ignore, or real stages that are just slow?"*
   Anything confirmed dead still gets analysed and reported — the customer should see it —
   but you will not treat it as a skipped stage.

4. **Which stage is sales-accepted.** The boundary between marketing/SDR yield and sales
   execution. Set `sales_accepted_stage` to the exact CRM value.

5. **Is there a separate lead lifecycle?** Show the `Lead.Status` (or `lifecyclestage`)
   distribution you pulled, then confirm the ordering, which value means sales-accepted, and
   which values are rejections/recycles. Set `lead_lifecycle_exists`, and record
   `ordered_stages`, `accepted_stage`, `rejected_stages` for the run skill to use.

6. **The beliefs — ask before showing anything measured.**
   > *"Before I measure anything: what do you believe your conversion rates are? Two numbers.
   > First, your overall win rate — of the deals that close, what share do you win? Second,
   > for each stage, what share of deals that reach it go on to the next one? Rough is fine;
   > I want the number you plan with."*
   Store as `believed_conversion_rates.believed_headline_pct` and `believed_by_stage_pct`,
   and set `headline_metric` to `overall_win_rate` or `win_rate_from_sales_accepted` depending
   on which one they actually plan with.

   **Ask two people if you can** — the CRO and whoever owns the plan. The spread between
   their answers is a finding on its own, and worth recording in the config `_comment`.

7. **Thresholds, proposed from their data rather than asked cold.**
   - `min_cohort_size` — propose 30. If a pipeline has under ~300 deals in 540 days, say that
     several stages will be too small to test and that the report will mark them untested
     rather than guess.
   - `history_lookback_days` — propose the period with full history coverage, not the default.
   - `pipelines_in_scope` — list the pipelines with their deal counts and let them choose.
     Note that the largest becomes the primary and gets the full analysis.
   - `material_deal_floor` — inherit from the profile unless they want a different one here.

## Step 5 — write config

Write `~/.leanscale-gtm/stage-architect.json`, using `config.example.json` as the template.
Keep the `_comment` header and every `_<key>_help` line — customers edit this by hand.

```bash
mkdir -p ~/.leanscale-gtm
# write the file, then show it
cat ~/.leanscale-gtm/stage-architect.json
```

Show the user the file you wrote and name the two keys that most change the output:
`min_cohort_size` and `believed_conversion_rates`.

## Step 6 — smoke test

Prove it works on real data before declaring success. Run the real pipeline over a narrow
slice — one pipeline, a shorter window — into a temporary run directory:

```bash
RUN="./gtm-agents/stage-architect/setup-smoketest"
mkdir -p "$RUN/raw"
# fetch a reduced slice per the run skill (LAST_N_DAYS:180 is enough), then:
"$HOME/.leanscale-gtm/bin/stage-architect" analyze --run-dir "$RUN"
"$HOME/.leanscale-gtm/bin/stage-architect" report --run-dir "$RUN" --no-save-baseline
```

`--no-save-baseline` matters: a smoke test on a narrow slice must not become the baseline the
customer's real runs are measured against.

If no CRM is connected yet, fall back to the bundled fixtures so the customer still sees the
machinery work end to end:

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/stage-architect" --root)"
"$HOME/.leanscale-gtm/bin/stage-architect" analyze \
  --raw "$AGENT_ROOT/fixtures/salesforce/raw" \
  --run-dir "$RUN" \
  --config "$AGENT_ROOT/fixtures/salesforce/config.json" \
  --as-of 2026-08-10
```

Say clearly when you have used fixtures — that is sample data, not their org.

**End the smoke test by showing one real measured conversion rate beside the believed one.**
This is the moment the product lands:

> **Smoke test — your first measured number**
>
> | Stage | You believed | Measured | n (resolved deals) |
> |---|---|---|---|
> | Qualification → next | 75% | 72.5% | 320 |
> | Overall win rate | 35% | 21.0% | 470 |
>
> Measured on deals that **entered** the stage, not deals sitting in it today. The full run
> explains why your CRM's own funnel report shows a much higher number.

## Step 7 — pass/fail table

Close with this, filled in, plus one plain-English sentence per failing row saying exactly
what the customer must do:

| Check | Status | What it means |
|---|---|---|
| `crm.query` resolved | pass / fail | can read opportunities |
| `crm.describe` resolved | pass / fail | can read picklists and custom fields |
| Stage picklist read (n stages, m pipelines) | pass / fail | the ladder is known |
| Opportunities in window (n) | pass / fail | cohorts exist |
| Stage history available | pass / **degraded** | conversion is measured vs guessed |
| Stage-history coverage ≥ 80% | pass / warn | rates are unbiased vs optimistic |
| Loss-reason field identified | pass / warn | terminal integrity is checkable |
| Lead lifecycle configured | pass / n/a | the pre-opportunity funnel is covered |
| Written stage definitions captured | pass / warn | exit criteria can be held to their own standard |
| Believed rates captured | pass / **fail** | **the gap is the deliverable; without this there is no gap** |
| `~/.leanscale-gtm/profile.json` present | pass / fail | shared across all agents |
| `~/.leanscale-gtm/stage-architect.json` written | pass / fail | settings persist across updates |
| Smoke test produced a report | pass / fail | the pipeline works end to end |

Then state plainly what will and will not work. For example: *"Conversion, dwell, skips and
regressions will all be measured. Backwards movement will be a floor rather than a total,
because we are reading HubSpot's `hs_date_entered_*` properties, which only keep the most
recent entry into each stage. Switch to batch property history if regression counts matter."*

Finish by telling them the next command is `/stage-architect:run`, and that the first run is a
baseline — the comparison starts on run two.
