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

Find the CRM tools available in this session with `ToolSearch`. Name the tool you resolved back
to the customer before you use it.

**Lead with the describe/schema tool, not the query tool.** Probes 3.1 through 3.5 below all come
out of a single object-describe call — stage picklists, every currency field, every date field,
segment picklists. You only need a query tool for fill rates (3.7) and fiscal year (3.6). Order
your work that way: one describe call gets you most of the interview.

**If a tool's parameter schema comes back empty, stop.** Do not try candidate parameter names
until one works — you will burn the customer's patience and possibly their API limits, and a
wrong guess against a write-capable tool is how a read-only agent stops being read-only. Say
which tool you could not resolve, say what you could not probe as a result, and continue with
what the describe call gave you. A probe reported as **blocked** is a good outcome. A probe
reported as passed when you never ran it is not.

Prove the connection with the cheapest possible call before anything else.

---

## 3. Probe — what the definitions will reference

This is the part that makes the interview short. Pull all of it before you ask a single
question, and report counts as you go so the customer can see you actually read their org.

### 3.1 Stages, in order — required, and check for more than one funnel
Salesforce: the `StageName` picklist on Opportunity, plus `OpportunityStage`
(`MasterLabel`, `SortOrder`, `IsClosed`, `IsWon`, `DefaultProbability`).
HubSpot: deal pipelines and their stages.

Record every stage label verbatim. Every funnel and time definition will name these, and they
must match the customer's spelling exactly or the definitions are fiction.

**Then read the list again and ask whether it is one funnel or several.** A single picklist
routinely carries a new-business funnel *and* a renewal or expansion motion — stages like
`Open Renewal`, `Review Complete` or `Expansion Identified` sitting after `Closed Won`. This is
the common case, not the exception.

It matters because a win rate computed across a mixed stage set silently blends new business
with renewals, and renewals win at a completely different rate. The number that comes out is
meaningless and nobody can see why.

If the stage list looks like more than one motion, **stop and get a scope decision before
continuing**: which funnel are we defining metrics for first? Do not offer to do both — one
funnel, finished, beats two half-defined. Record it as `primary_funnel_stages` and
`excluded_stages`, and say plainly that the excluded motion needs its own definitions later.

### 3.2 Every amount field — required
List every currency field on Opportunity/Deal. **Expect ten to fifteen, not three or four.** A
real org accumulates `Amount`, then ARR, ACV, TCV, MRR, Net ARR, Net New ARR, and a handful more
from qualification frameworks (BANT budget, MEDDIC economic buyer amounts) that are not revenue
at all and must not be offered as candidates for bookings.

Present them grouped — plausible bookings candidates first, framework and scratch fields last —
with fill rate where you have it. **Which one means "bookings" is one of the fights in the
interview**, and arriving with a sorted real list rather than a raw dump of fifteen is what
makes that fight short.

### 3.3 Stage-history behaviour — required, and the one that can stop the project
Determine whether stage transitions are **system-stamped**, and **for which stages**.

- Salesforce: is history tracking enabled on `Opportunity.StageName`? Is there an
  `OpportunityHistory`/`OpportunityFieldHistory` record for stage changes? Is there a
  `LastStageChangeDate`? Are there per-stage datetime fields (a `Date_Time_Stage_N__c` pattern
  is common), and are they written by automation or typeable by a rep?
- HubSpot: does the deal have `hs_date_entered_<stage>` properties, and are they populated?

**Do not record this as a single boolean.** Per-stage timestamp fields very often cover only the
new-business funnel and stop before the renewal and expansion stages from 3.1 — which means
cycle time is measurable for one motion and not the other. Record it per funnel:

```jsonc
"stage_timestamps": {
  "primary_funnel":   "system_stamped",   // system_stamped | typeable | absent
  "renewal_funnel":   "absent",
  "coverage_note":    "Date_Time_Stage_0..6 cover new business only; no fields after Closed Won"
}
```

**If the primary funnel is `typeable` or `absent`, say so immediately and plainly.** Cycle time
is unmeasurable when a human can type the date, you cannot backfill a timestamp that was never
written, and roughly half of what this suite can do depends on it. That is not a reason to stop —
it is a reason they start measuring from today, and they need to hear it now rather than in
month three.

### 3.4 Segment, industry, region picklists — required
Pull the actual values. Segmentation is one of the six definition groups and the fight there is
whose definition of "Enterprise" wins. Arrive with the real picklist.

### 3.5 Record types / pipelines — required
Multiple pipelines mean multiple funnels, and a metric that averages across them is a metric
nobody trusts. Record them, and reconcile against the mixed-stage check in 3.1 — a single
pipeline with a mixed stage list is the same problem wearing different clothes.

### 3.6 Lifecycle and source fields — optional, high value
Lead status, lifecycle stage, `LeadSource` values, campaign fields. These feed the funnel and
source-and-channel groups. Note whether source and channel are one field or two — most orgs
have one, and that is its own fight.

---

**Everything above comes from the describe call. Everything below needs a query tool.** If you
could not resolve one in step 2, mark both as `blocked`, tell the customer which questions stay
open as a result, and carry on — you already have enough for the interview.

### 3.7 Fill rates — query tool required
Row counts for the candidate amount fields from 3.2 and the stage timestamp fields from 3.3. A
field that exists and is 4% populated is not a candidate for anything, and the fill rate is
what turns a list of fifteen currency fields into a shortlist of two.

### 3.8 Fiscal year — query tool required
Salesforce: `Organization.FiscalYearStartMonth`. **Never assume January.** Read it, then confirm.
If blocked, ask the customer directly — it is one question and they know the answer.

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

  "stage_timestamps": {
    "primary_funnel": "absent",
    "renewal_funnel": "absent",
    "coverage_note": ""
  },
  "_stage_timestamps_help": "Probed in setup, PER FUNNEL — system_stamped | typeable | absent. Timestamp fields often cover new business only, so cycle time is measurable for one motion and not another. Anything other than system_stamped generates cycle-time metrics with a caveat block and a measure-from-today start date.",

  "primary_funnel_stages": [],
  "_primary_funnel_stages_help": "The stages of the ONE funnel being defined first. Set when setup finds a mixed stage list (new business plus renewal or expansion in the same picklist).",

  "excluded_stages": [],
  "_excluded_stages_help": "Stages deliberately out of scope for this pass. They need their own definitions later; metrics generated now must filter them out or they blend two motions.",

  "qualified_stage": "",
  "_qualified_stage_help": "The stage whose ENTRY means an opportunity is qualified. Cohort metrics use entry into this stage, never close date.",

  "bookings_amount_field": "",
  "_bookings_amount_field_help": "The one currency field that means bookings. Chosen by the customer in setup from the real field list."
}
```

---

## 6. Prove it

Print a table with three states per probe — **pass / blocked / fail** — never just pass or fail.
A probe you could not run because no query tool resolved is `blocked`, and saying so is the
honest result. Never report a probe as passed when you did not run it.

| Probe | Report |
|---|---|
| Tool resolved | which describe tool, which query tool (or `blocked`) |
| 3.1 Stages | count, **and whether it is one funnel or several** |
| 3.2 Amount fields | count, and the shortlist you presented |
| 3.3 Stage timestamps | **per funnel**, with the coverage note |
| 3.4 Segments | count |
| 3.5 Pipelines | count |
| 3.7 Fill rates | pass or `blocked` |
| 3.8 Fiscal year | value, or `asked the customer` |
| Config | files written |

Then state the two things that matter most, in a sentence each:

1. **Whether their stage transitions are system-stamped, and for which funnel** — this decides
   whether cycle time is measurable at all, and for which motion.
2. **Whether their stage list is one funnel or several** — because if it is several and they
   have not scoped it, every metric generated next will blend two motions.

Finish by telling them to run `/semantic-layer:run`.
