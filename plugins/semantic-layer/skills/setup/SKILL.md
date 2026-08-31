---
name: setup
description: >-
  One-time (and re-runnable) setup for the Semantic Layer builder. Probes the connected
  Salesforce or HubSpot and reads the things your metric definitions will have to reference —
  stage names and their order, every amount field, close-date and stage-history behaviour,
  segment and industry picklists, record types and pipelines, fiscal year start — then asks
  only the handful of questions the CRM cannot answer, and writes ~/.leanscale-gtm/profile.json
  and ~/.leanscale-gtm/semantic-layer.json. Trigger on "/semantic-layer:setup", "set up the
  semantic layer agent", "configure semantic layer", or when a run fails and you need to
  diagnose the connection. Read-only — nothing is written to the CRM.
argument-hint: "[--reconfigure] [--check]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# Semantic Layer — setup

Read-only throughout. Nothing here writes to the CRM.

**Idempotent** — re-run any time. It re-reads the org, shows what is already configured, and
only asks about what is missing or has drifted. `--check` runs steps 1–3 and the pass/fail
table without touching config, which is the right first move when a run has started failing.

The rule that makes this feel expensive: **discover before you ask.** Every question below is
phrased in terms of something you already pulled. Asking a customer "what are your sales
stages" when you could have read them makes the product look dumb — and this plugin in
particular lives or dies on that, because the whole point is that their definitions reference
their *real* fields, not invented ones.

---

## 0. Locate this plugin

`AGENT_ROOT` is this plugin's own directory — the one containing `skills/`, `templates/` and
`.claude-plugin/`. Inside Claude Code `${CLAUDE_PLUGIN_ROOT}` already holds it. On Cursor,
VS Code, Codex CLI or Gemini CLI that variable does not exist — use the directory you loaded
this SKILL.md from, two levels up from `skills/setup/`.

```bash
AGENT_ROOT="${CLAUDE_PLUGIN_ROOT:-<the directory this plugin was loaded from>}"
mkdir -p ~/.leanscale-gtm
echo "$AGENT_ROOT"
```

---

## 1. Read the shared profile first

```bash
cat ~/.leanscale-gtm/profile.json 2>/dev/null || echo "no shared profile yet"
```

If it exists, another LeanScale agent already interviewed this customer. **Show them what is
already known and do not ask again.** Fiscal year, segments, quota-carrying reps, CRM system —
all of it carries over. Only ask for what is missing.

If it does not exist, you are the first agent here and you will create it in step 4.

---

## 2. Resolve the tools

Find the CRM tools available in this session with `ToolSearch`. Salesforce orgs expose a SOQL
query tool; HubSpot exposes CRM object search and properties tools. Name the tool you resolved
back to the customer before you use it — if you cannot find one, stop and say so plainly
rather than guessing at a schema.

Prove the connection with the cheapest possible call before anything else.

---

## 3. Probe — what the definitions will reference

This is the part that makes the interview short. Pull all of it before you ask a single
question, and report counts as you go so the customer can see you actually read their org.

### 3.1 Stages, in order — required
Salesforce: `OpportunityStage` (`MasterLabel`, `SortOrder`, `IsClosed`, `IsWon`, `DefaultProbability`).
HubSpot: deal pipelines and their stages.

Record every stage label verbatim. Every funnel and time definition will name these, and they
must match the customer's spelling exactly or the definitions are fiction.

### 3.2 Every amount field — required
List all currency fields on Opportunity/Deal, with fill rate. There are almost always three or
four (`Amount`, `ExpectedRevenue`, and two custom ones). **Which one means "bookings" is one of
the fights in the interview** — arriving with the actual list is what makes that fight short.

### 3.3 Stage-history behaviour — required, and the one that can stop the project
Determine whether stage transitions are **system-stamped**.

- Salesforce: is history tracking enabled on `Opportunity.StageName`? Is there an
  `OpportunityHistory`/`OpportunityFieldHistory` record for stage changes? Are there custom
  date fields that a human could type into?
- HubSpot: does the deal have `hs_date_entered_<stage>` properties populated?

Record the answer as `stage_transitions_system_stamped: true | false | partial`.

**If false, say so immediately and plainly.** Cycle time is unmeasurable when a human can type
the date, you cannot backfill a timestamp that was never written, and roughly half of what this
suite can do depends on it. That is not a reason to stop — it is a reason they start measuring
from today, and they need to hear it now rather than in month three.

### 3.4 Segment, industry, region picklists — required
Pull the actual values. Segmentation is one of the six definition groups and the fight there is
whose definition of "Enterprise" wins. Arrive with the real picklist.

### 3.5 Record types / pipelines — required
Multiple pipelines mean multiple funnels, and a metric that averages across them is a metric
nobody trusts. Record them.

### 3.6 Fiscal year — required
Salesforce: `Organization.FiscalYearStartMonth`. **Never assume January.** Read it, then confirm.

### 3.7 Lifecycle and source fields — optional, high value
Lead status, lifecycle stage, `LeadSource` values, campaign fields. These feed the funnel and
source-and-channel groups. Note whether source and channel are one field or two — most orgs
have one, and that is its own fight.

---

## 4. Ask only what the CRM could not answer

Every question here is unanswerable from schema alone. Phrase each one against a number you
just pulled.

1. **Which amount field means bookings?** Show the list from 3.2 with fill rates. Do not offer
   an opinion until they have answered.
2. **What makes an opportunity qualified?** Show the stage list from 3.1 and ask which stage
   entry represents it. Push for an observable event rather than a judgment — anything needing
   a rep's opinion drifts within a quarter.
3. **What counts as a customer?** Revenue-based ("has active contracted revenue") is computable;
   a status field is maintained by whoever remembers. Note which they chose.
4. **Who arbitrates when the CRO and the CFO disagree?** One name. This is the executive sponsor
   and the definitions do not survive without one.
5. **Quota-carrying reps** — ask directly if `profile.json` does not have it. Ratios against
   total headcount are wrong and embarrassing.

Do not ask about anything in step 3. You already know it.

---

## 5. Write config

`~/.leanscale-gtm/profile.json` — create it if missing, extend it if present. Never overwrite
keys another agent wrote.

`~/.leanscale-gtm/semantic-layer.json` — this plugin's own settings. Same house shape: a
`_comment` header, and every key followed by a `"_<key>_help"` string explaining it in one
sentence, because customers edit these by hand.

```jsonc
{
  "_comment": "Semantic Layer builder settings. Edit by hand freely; re-run /semantic-layer:setup to refresh the probed values.",

  "repo_path": "./gtm-semantic",
  "_repo_path_help": "Where the generated semantic layer repo is written. Relative to the directory you run from.",

  "first_metrics": ["win_rate", "cycle_time", "pipeline_created"],
  "_first_metrics_help": "The metrics generated in full on the first run. These three are the ones conversion rate and time in stage depend on.",

  "stage_transitions_system_stamped": false,
  "_stage_transitions_system_stamped_help": "Probed in setup. If false, cycle-time metrics are generated with an explicit caveat block and a measure-from-today start date.",

  "qualified_stage": "",
  "_qualified_stage_help": "The stage whose ENTRY means an opportunity is qualified. Cohort metrics use entry into this stage, never close date.",

  "bookings_amount_field": "",
  "_bookings_amount_field_help": "The one currency field that means bookings. Chosen by the customer in setup from the real field list."
}
```

---

## 6. Prove it

Print a pass/fail table: tool resolved, stages read (count), amount fields found (count),
stage-stamping verdict, segments read (count), fiscal year start, config written.

Then state the one thing that matters most, in a sentence: whether their stage transitions are
system-stamped, and what that means for what they can measure.

Finish by telling them to run `/semantic-layer:run`.
