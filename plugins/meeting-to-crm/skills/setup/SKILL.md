---
name: setup
description: >-
  Configure meeting-to-crm for this workspace — probe the connected CRM and transcript
  tools, discover the real field API names for next step, competitor and each qualification
  dimension, agree the exact field allow-list and overwrite policy, map the customer's
  framework (MEDDPICC/MEDDIC/BANT/SPICED/custom) onto their fields, and finish with a smoke
  test that produces a real diff for a real meeting without writing anything. Trigger on
  "/meeting-to-crm:setup", "set up meeting to crm", "configure the meeting agent",
  "connect my calls to Salesforce/HubSpot", or when a run fails and needs diagnosing.
argument-hint: "[--reconfigure] [--crm salesforce|hubspot]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# meeting-to-crm — setup

Idempotent and re-runnable. It doubles as the health check: when a run breaks, run this.

**Discover before you ask.** Every question you ask that the CRM could have answered makes
the product feel dumb. Read their schema first, then ask the handful of questions only a
human can answer, phrased in terms of what you found.

This is the only write-capable plugin in the suite, so setup has one extra job the others
do not: getting the customer to **say out loud** which fields this thing may touch, and
what happens when a field already has a value. Do not rush that part; it is the whole
safety model.

---

## 0. Locate this plugin

Everything below runs this plugin's scripts through a small shim at
`~/.leanscale-gtm/bin/meeting-to-crm`. Create it before anything else — nothing later works without it.

`AGENT_ROOT` is this plugin's own directory: the one containing `scripts/`, `skills/` and
`.claude-plugin/`. Inside Claude Code, `${CLAUDE_PLUGIN_ROOT}` already holds it. On Cursor,
VS Code, Codex CLI or Gemini CLI that variable does not exist — use the directory you loaded
this SKILL.md from, two levels up from `skills/setup/`.

If the agents were installed with `tools/install-skills.py` (the non-plugin path), this is
already done — skip to the confirmation below. Otherwise:

```bash
AGENT_ROOT="${CLAUDE_PLUGIN_ROOT:-<the directory this plugin was loaded from>}"
python3 "$AGENT_ROOT/scripts/lib/config.py" install-shim --plugin meeting-to-crm --root "$AGENT_ROOT"
```

It verifies the directory really is a plugin root, records it in
`~/.leanscale-gtm/meeting-to-crm.json`, and writes the shim. If it answers *"does not look like a
plugin root"*, the path is wrong — fix it now rather than debugging a later step.

Confirm it works before continuing:

```bash
"$HOME/.leanscale-gtm/bin/meeting-to-crm" --root
```

Re-running this is safe, and is the first thing to try if a run later fails with a missing
script — a plugin update moves the install and the recorded path goes stale.

---

## 1. Probe

Required capabilities: `transcripts.*`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("run_soql_query salesforce")        ToolSearch("hubspot crm search objects")
    ToolSearch("describe object metadata fields")  ToolSearch("create update record")
    ToolSearch("transcripts meetings recordings")  ToolSearch("get transcript")
    ToolSearch("read file content drive")

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    transcripts.*  any vendor  gong / fireflies / chorus / grain / otter / zoom list+get transcript tools
                   fallback    docs.read over a folder of exported transcripts — no vendor is required

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


Report what each resolved tool provides, and be specific about failures. Not "Gong is not
available" but: *"a Gong tool resolves and authenticates, but listing calls for last week
returned 0 while the web app shows 14 — the API key is probably scoped to one user's own
calls."*

`crm.write` not resolving is **not** a blocker for setup. Everything up to and including
the smoke test is read-only; note it and carry on.

## 2. Read the shared profile

```bash
cat ~/.leanscale-gtm/profile.json
```

If it exists, show what is already known and **confirm rather than re-ask**:

> Reading your existing profile: Acme · Salesforce (Acme Production) · fiscal year starts
> February · 14 quota-carrying reps · material deal floor $5,000. Still right?

If it does not exist, create it — you are the first agent they installed. Ask only for what
cannot be discovered: `org_name`, `quota_carrying_reps` (ask directly; it is the most
load-bearing number in the suite), `motion`. Read `fiscal_year_start_month` from
`SELECT FiscalYearStartMonth FROM Organization` and confirm it rather than assuming January.

## 3. Discover — the part that makes this feel expensive

### 3a. Find the fields that already exist for this job

Do not ask "where does next step live?". Go and look.

**Salesforce**

```sql
SELECT QualifiedApiName, Label, DataType, IsUpdatable
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
ORDER BY QualifiedApiName
```

Then pattern-match the labels and API names for the jobs this plugin does:

| Job | Look for | Common finds |
|---|---|---|
| Next step | standard `NextStep` first | `NextStep`, `Next_Steps__c`, `Next_Action__c` |
| Next-step date | date fields near "next" | `Next_Step_Date__c`, `Next_Milestone_Date__c` |
| Competitor | "compet" | `Primary_Competitor__c`, `Competitors__c` (multi-select) |
| Pain / use case | "pain", "use case", "problem", "challenge" | `Identified_Pain__c`, `Use_Case__c` |
| Framework dimensions | the framework's own vocabulary | `Metrics__c`, `Economic_Buyer__c`, `Decision_Criteria__c`, `Decision_Process__c`, `Paper_Process__c`, `Champion__c` |
| Call summary target | Task/Event, or a Notes object | `Task.Description` |

Pull picklist values for every candidate picklist — a competitor field that only accepts
four values changes what can be proposed.

**HubSpot**

```
GET /crm/v3/properties/deals        GET /crm/v3/properties/contacts
```

Standard `hs_next_step` exists on deals, so check it before proposing a custom property.
Flag every property where `calculated` is true or `modificationMetadata.readOnlyValue` is
true — writing to one of those is a 400 that fails the whole batch. Put them in
`read_only_fields`.

### 3b. Measure whether those fields are actually used

Fill-rate over the last 12 months, on open and recently-closed opportunities. This is the
number that sells the plugin and sets the expectations:

```sql
SELECT COUNT(Id) total, COUNT(NextStep) with_next_step, COUNT(Metrics__c) with_metrics,
       COUNT(Economic_Buyer__c) with_eb, COUNT(Decision_Process__c) with_process
FROM Opportunity WHERE CreatedDate = LAST_N_MONTHS:12
```

Report it as a sentence, not a table dump:

> Of 412 opportunities created in the last 12 months, 38% have a next step, 9% have
> metrics, 12% name an economic buyer, and `Paper_Process__c` has been filled in 4 times
> ever. That last one may be a dead field — should I leave it off the allow-list?

Also count opportunities per account, because that is what decides how often matching goes
ambiguous:

```sql
SELECT AccountId, COUNT(Id) FROM Opportunity WHERE IsClosed = false
GROUP BY AccountId HAVING COUNT(Id) > 1
```

> 23 of your 180 open opportunities sit on accounts that have more than one. Calls with
> those accounts will come back ambiguous unless the calendar invite is linked to the deal.

### 3c. Detect the transcript source and sample one real meeting

Whichever tool resolved, pull the **last 7 days** of meetings and show the user what you
found: how many, how many have external attendees, how many carry attendee email
addresses (Zoom, Meet and Otter often do not), and whether any carry a CRM link.

Then take one real recent meeting and show the match you would make, with the signals:

> Your most recent external call is "Northwind Analytics <> Acme — Discovery" (Aug 4, 38
> min). I can see `priya@northwindanalytics.com` on the invite, that email is a Contact on
> Northwind Analytics, and that account has exactly one open opportunity —
> *Northwind Analytics — Platform Expansion*. That is a confident match. Is it the right one?

If the answer is no, you have learned something worth more than the rest of setup.

---

## 4. The interview

Ask these, in this order, informed by what you found. Every question is one only a human
can answer.

1. **Which meetings should this process?**
   Show the meeting types you observed in the last 30 days with counts. Confirm the
   include list, the exclude list, and whether internal-only calls should ever be
   processed (default: never).

2. **The field allow-list — the important one.**
   Walk the fields you found, one at a time, with their fill-rate. For each: in or out.
   Be explicit that a field not on this list is never proposed, no matter how obvious the
   value seems. Read the final list back before writing it.

3. **The overwrite policy, per field.**
   > Default is fill-blanks-only: if a field already has a value, the agent leaves it alone
   > and reports what it would have said. Three fields usually want something different —
   > next step and next-step date are normally 'always' because the newest call *is* the
   > current next step, and long-text notes are normally 'append' so the history builds up
   > instead of being overwritten. Which of yours should be 'always', which 'append', and
   > which stay fill-blanks-only?

4. **Your qualification framework, mapped to real fields.**
   MEDDPICC / MEDDIC / BANT / SPICED / Challenger / Command of the Message / custom.
   If it is custom, capture it properly — the dimensions in their own words, each mapped to
   the field API name you found in 3a. A dimension with no field is fine: it will be
   reported as a blank rather than written anywhere.

5. **May the agent ever propose Amount, CloseDate or stage?**
   Default is no, and recommend keeping it that way:
   > These three are what your forecast is built from. An agent that moves them off a
   > sentence in a call is an agent that quietly re-forecasts your quarter. Most teams
   > leave them off and let the rep make that call. Do you want them on?
   If yes, put the fully-qualified names in `restricted_fields_opt_in` **and** in
   `field_allowlist` — two locks, deliberately — and say clearly that you have done both.

6. **Who approves a batch?**
   Names. One person, a couple of names, or anyone on the team. This name goes in the audit
   log next to every write, so it needs to be a real person, not a role.

7. **Dry-run or apply by default?**
   This is not actually a choice — say so:
   > Dry-run, always. There is no setting that changes it. Applying takes an explicit flag,
   > a named approver, and a token that only matches the exact diff they read. I am telling
   > you rather than asking because it is the thing that makes this safe to install.

8. **How should a meeting be matched to an opportunity?**
   Ranked, based on what you found in 3c: calendar-event link (best, if they use it), a
   meeting-title convention, attendee email domain, or manual only. Show the ambiguity
   count from 3b and be honest that the match strategy determines how much of this actually
   runs unattended.

9. **Anything the agent must never touch.**
   Ask outright. Every team has one field somebody guards. Put it in `read_only_fields`.

---

## 5. Write the config

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/meeting-to-crm" --root)"
mkdir -p ~/.leanscale-gtm
cp "$AGENT_ROOT/config.example.json" ~/.leanscale-gtm/meeting-to-crm.json
# then edit it to the answers above
```

Show the customer the finished file and walk the allow-list section out loud. Keep every
`_<key>_help` line — customers edit this file by hand.

Update `~/.leanscale-gtm/profile.json` with anything newly learned. Do not overwrite keys
another plugin's setup already wrote.

## 6. Smoke test — a real diff, no writes

Run the real pipeline over a short window against real data:

```bash
RUN="./gtm-agents/meeting-to-crm/setup-smoketest"
mkdir -p "$RUN/raw"
# fetch 1–3 real meetings from the last 7 days, follow skills/run/SKILL.md steps 2–5
"$HOME/.leanscale-gtm/bin/meeting-to-crm" analyze --raw "$RUN" --out "$RUN"
"$HOME/.leanscale-gtm/bin/meeting-to-crm" report --run "$RUN"
```

Show the resulting diff table. A setup that ends without a real proposed change on a real
record is not finished.

Then prove the guards on their own data, in front of them:

```bash
"$HOME/.leanscale-gtm/bin/meeting-to-crm" diff selftest
```

38 checks against the bundled fixtures, in a sandbox that does not touch their config or
audit log. Point at the four that matter: a field off the allow-list is dropped, a
populated field is preserved, Amount and CloseDate are refused, and an invented quote is
caught.

If they have no recent meetings, run the bundled fixture instead and say plainly that it is
sample data:

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/meeting-to-crm" --root)"
"$HOME/.leanscale-gtm/bin/meeting-to-crm" analyze \
    --raw "$AGENT_ROOT/fixtures/salesforce" --out "$RUN" \
    --config "$AGENT_ROOT/fixtures/salesforce/config.json"
"$HOME/.leanscale-gtm/bin/meeting-to-crm" report --run "$RUN"
```

## 7. Pass/fail table

End with this, filled in, plus one plain-English sentence per gap saying what the customer
must do to close it:

| Check | Status | What it means |
|---|---|---|
| CRM query | pass / fail | can read opportunities and contacts |
| CRM describe | pass / fail | picklist and read-only validation available; without it, invalid values reach the API |
| CRM write | pass / fail | needed only for the apply step; proposing works regardless |
| Transcript source | pass / fail | which one, and how many meetings in the last 7 days |
| Attendee emails present | pass / fail | if fail, attendee-domain matching is unavailable — Zoom/Meet/Otter strip them |
| Match quality on the sample | pass / partial / fail | how many of the sampled meetings matched confidently |
| Field allow-list agreed | pass / fail | N fields across M objects |
| Framework mapped | pass / partial | N of M dimensions have a field; the rest report as blanks |
| Amount/CloseDate/stage | off (recommended) / on | whether forecast-bearing fields may be proposed |
| Approver named | pass / fail | who appears in the audit log |
| Audit log writable | pass / fail | `~/.leanscale-gtm/audit/meeting-to-crm.log` |
| Guard selftest | pass / fail | 38/38 |
| Smoke test | pass / fail | a real diff on a real record, nothing written |

Close with the honest summary: what will work unattended, what needs a human every time,
and the single change that would most improve match quality (usually: link the calendar
invite to the opportunity).
