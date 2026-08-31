---
name: run
description: >-
  Walk through defining your go-to-market metrics and write them out as a real semantic layer:
  a git repo of versioned metric files, each with one named owner, a plain-English description
  and a declared source of truth, seeded from your actual CRM stage names and fields. Generates
  the three metrics your yield depends on in full — win rate, cycle time, pipeline created —
  plus a definitions worksheet for the rest. Trigger on "/semantic-layer:run", "build my
  semantic layer", "define our metrics", "what should we define", "set up metric definitions",
  "our dashboards disagree", "two versions of win rate", "how do we define qualified", or any
  request to standardise, document or govern GTM metric definitions. Read-only against the CRM
  — it writes files to your working directory only.
argument-hint: "[--group identity|funnel|time|money|source|segmentation] [--metrics-only] [--worksheet-only]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# Semantic Layer — run

**Read-only against the CRM.** This skill issues SELECT/GET calls only. Everything it writes
goes to a new directory in the customer's working directory — never to Salesforce, HubSpot, or
anything else. Say that before you start.

What it produces: a git repo they own, containing their metrics as versioned files with a named
owner each, and a worksheet for the definitions still to settle.

---

## 0. Preflight

```bash
cat ~/.leanscale-gtm/profile.json ~/.leanscale-gtm/semantic-layer.json 2>/dev/null \
  || echo "not configured"
"$HOME/.leanscale-gtm/bin/semantic-layer" --root || echo "shim missing — run /semantic-layer:setup"
```

If either is missing, stop and tell them to run `/semantic-layer:setup` first. Do **not**
interview someone about their stages when you could have read them.

Re-read the probed values. You will reference their real stage names, their real amount fields
and their real segment picklist throughout — never a placeholder, never an invented example.

## 0.5 Resolve the capabilities

**The capability is the contract, not the tool name, and not `ToolSearch`** — that is a Claude
Code tool, and on Cursor, VS Code, Codex CLI and Gemini CLI it does not exist.

| Capability | Need | Probe (Claude Code) | Otherwise, match by name |
|---|---|---|---|
| `crm.describe` | **required** | `ToolSearch("describe metadata object schema")` | sf: a SOQL tool over `EntityDefinition`/`FieldDefinition`, or a describe tool · hs: `hubspot-list-properties` |
| `crm.query` | optional | `ToolSearch("run_soql_query salesforce")` / `ToolSearch("hubspot crm search")` | sf: `run_soql_query` · hs: `hubspot-search-objects`, `hubspot-list-objects` |

Otherwise — on any client without `ToolSearch` — match on what a tool *does* rather than giving
up. The right-hand column is the common case, not an allow-list.

`crm.describe` is the only hard requirement. Without `crm.query` you lose fill rates and fiscal
year; everything else still runs. Name what you resolved onto each capability before using it.

## 1. Snapshot the schema and produce the readiness report

Create the run directory in the **current working directory** — never in the plugin, which is
read-only on a marketplace install:

```bash
RUN="./gtm-agents/semantic-layer/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw"
echo "$RUN"
```

Write the describe snapshot to `$RUN/raw/describe.json` with this shape: `stages.values[]`
(`value`, `sort`, `is_won`, `is_closed`), `currency_fields[]` (`name`, `label`, `fill_rate`),
`stage_date_fields[]`, `segment_picklists{}`, `record_types[]`, `fiscal_year_start_month`,
`source_channel_fields[]`.

**Salesforce**, via `crm.describe`:

```sql
SELECT QualifiedApiName, Label, DataType
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
```

```sql
SELECT MasterLabel, SortOrder, IsClosed, IsWon, DefaultProbability
FROM OpportunityStage
WHERE IsActive = true
ORDER BY SortOrder
```

Fill rates and fiscal year need `crm.query`; skip both if it did not resolve:

```sql
SELECT COUNT(Id) total, COUNT(Amount) amount_filled, COUNT(ACV__c) acv_filled
FROM Opportunity
```

```sql
SELECT FiscalYearStartMonth FROM Organization
```

**HubSpot**, via `crm.describe`: `hubspot-list-properties` on `deals` — take `type: "number"`
with a currency `fieldType` as the currency fields, `hs_date_entered_*` as the stage date
fields, and the `dealstage` property's options as the stages.

Then run the pipeline through the shim — analyze, then **draft**, then report, chained so
a failed step stops the run instead of letting a partial report pose as the result:

```bash
"$HOME/.leanscale-gtm/bin/semantic-layer" analyze --raw "$RUN/raw" --out "$RUN" \
  && "$HOME/.leanscale-gtm/bin/semantic-layer" draft  --raw "$RUN/raw" --out "$RUN" \
  && "$HOME/.leanscale-gtm/bin/semantic-layer" report --findings "$RUN/findings.json" --out "$RUN"
```

If the chain stops at `draft`, do not continue to the report — read the stderr diagnosis
and fix the cause first. Two known cases: a missing describe snapshot (re-run
`/semantic-layer:setup --check`), and `no such script 'draft'`, which means the installed
plugin predates 1.2.0 — have the customer run `claude plugin update semantic-layer` (or
their client's equivalent), then re-run `/semantic-layer:setup` to refresh the shim.

The draft step is what makes run one end with something to react to instead of a findings
list: `$RUN/draft/gtm-semantic/` now holds the three core metrics as YAML, seeded from their
real stages, fields and fill rates, with every guessed value recorded as a numbered
assumption, and `$RUN/draft/DRAFTS.md` is the review sheet. Values the customer already
settled in setup are used silently; only genuine unknowns become assumptions.

Walk the customer through `$RUN/report.md` first — the readiness findings — then open the
drafts. **The report says what stands in the way; DRAFTS.md is the agenda for the next twenty
minutes.** Never end the run here: a readiness report with unreviewed drafts behind it is a
run that stopped at step one (see "Definition of done" below).

---

## 1. Frame it in one paragraph, then start

Say this, in your own words, before the first question:

> Most companies have their metric definitions in people's heads, and they find out the
> definitions disagree when two dashboards disagree in front of the board. What we're doing is
> writing them down once, giving each one a named owner, and putting them somewhere a machine
> can read. That's the whole thing. We'll do the three that matter most in full, then leave you
> a worksheet for the rest.

Then: **the questions come before the metrics.** Ask what questions their exec team actually
asks every week — aim for ten, accept five. Write them down. Everything generated below is
scoped to that list, and anything not on it does not get built. A team that models its business
instead of its questions ends up eighteen months later with a beautiful warehouse and nothing
that answers anything.

---

## 2. The three metrics, in full

Generate these first, always, unless `--group` narrows it. They are the two numbers the whole
argument rests on, plus the denominator they both need.

| Metric | Why this one |
|---|---|
| `win_rate` | Conversion rate. One point of it is worth about a million dollars on a $20M plan |
| `cycle_time` | Time in stage. One day off it is worth about a quarter of that, per day, forever |
| `pipeline_created` | The denominator both of the above are argued about with |

All three are **already drafted** in `$RUN/draft/gtm-semantic/semantic/metrics/`. The
interview is therefore a review, not authorship from a blank page: walk `DRAFTS.md` top to
bottom, one assumption at a time. For each assumption present the drafted value, the
evidence, and the alternatives table, then get a decision — confirmed or corrected. When a
decision lands, edit the draft file: set the value, delete that assumption's line from the
file's header block, and tick it off in `DRAFTS.md`. An assumption the customer explicitly
postpones stays in the header — a draft that still carries assumption lines is visibly
unfinished, which is the point.

Still run the loop below per metric — one metric finished, then the next — but anchored to
the draft: the "fight" for each metric is its assumption #1, and the draft already names a
defensible side of it.

### 2.1 Name the fight first
Every metric has one decision that determines the rest of it. Put it on the table before
anything else, using their real values from setup:

- **`win_rate`** — cohort or close date? Show both. *Close date* takes every deal that closed in
  the period; the mix shifts as the funnel grows, so the number moves without anything about
  their selling changing. *Cohort* takes every deal that entered qualified in the period and
  asks what share eventually won — stable, comparable, defensible. **Recommend cohort**, and be
  honest that a recent cohort isn't finished yet, so it either matures or gets reported partial.
- **`cycle_time`** — where does the clock start and stop? Name the actual stages from setup. Read
  `stage_timestamps` **for the funnel you are defining**, not the org as a whole: if that funnel
  is `typeable` or `absent`, say plainly that this metric measures from today forward and cannot
  be backfilled. If another funnel has no timestamps at all, say that too — they will ask for
  renewal cycle time eventually and it is better they know now.
- **`pipeline_created`** — created when the opportunity is created, or when it reaches qualified?
  These differ by a lot in most orgs and the gap is usually the argument.

### 2.2 Get the four things every definition needs
1. **Plain-English description** — in their words, one or two sentences. This is what lets the
   definition survive the person who wrote it.
2. **One named human owner.** Not a team. Two owners produce two definitions inside a quarter,
   and then a standing agenda item about which is right. Push back if they say "RevOps."
3. **The source of truth** — which system, which object, which field. Use the real field names
   from setup.
4. **The filter and the grain** — which records count, and what one row represents.

### 2.3 Finish the file
The draft already has their actual field names in it — finishing means: every assumption in
its header resolved, the owner filled in (§2.2), and the plain-English description in their
words. Keep the `# cohort, not close` comment on the time block where it applies — that one
line is the difference between a win rate they can defend and one that reshuffles every
quarter. If a corrected decision changes a field, update every occurrence (source_of_truth,
filter, measure, time) — the draft is consistent; keep it that way.

**If setup recorded `excluded_stages`, every metric must filter them out explicitly**, and the
filter must appear in the file where a human can see it — not left implicit. A win rate that
silently averages new business with renewals is exactly the kind of confidently wrong number
this whole exercise exists to prevent.

Show them the finished file and ask if it's right before moving on.

---

## 3. The six groups — the worksheet

The remaining definitions come in six groups. Do **not** try to settle all of them in one
sitting; the target is a worksheet they take to their team, not a completed model. Thirty is
the ceiling, not the goal — past forty they're modelling the business instead of the questions.

For each group, present: the fight, the defensible default, and their real values from setup.

| Group | The fight | The default to offer |
|---|---|---|
| **Identity** | Does a subsidiary roll up — and the same way for reporting and for comp? | Domain is the spine. Hierarchy is an attribute on top of identity, never identity itself. Decide reporting and comp separately |
| **Funnel & lifecycle** | What makes an opportunity qualified? | An observable event, never a judgment. "Accepted by sales and a discovery meeting occurred" |
| **Time** | Who stamps the date a deal entered a stage? | System-stamped entry and exit, always. If a human can type it, cycle time is unmeasurable — the one prerequisite with no workaround |
| **Money** | What counts as a customer, and who decides? | Define on revenue, not status. "Has active contracted revenue" is computable; a status field is maintained by whoever remembers |
| **Source & channel** | One field or two? Most teams have one | Two. Source is immutable first touch; channel is mutable and is what marketing spends against. Conflate them and history can never be restated |
| **Segmentation** | Whose definition of Enterprise wins — sales', marketing's or finance's? | One segment field, computed from firmographics on the resolved account. Never a picklist three teams maintain differently |

Write the worksheet to `definitions-worksheet.md` in the repo: one section per group, their real
picklist values inline, the fight stated, the default pre-filled, and blank owner and
source-of-truth fields for them to complete with their team.

---

## 4. Generate the repo

Promote the reviewed drafts: copy `$RUN/draft/gtm-semantic/` to the repo path (default
`./gtm-semantic`), with each metric file carrying only the assumptions the customer
explicitly postponed — everything else resolved and marker-free. Then add the governance
files below. A file promoted with open assumptions must say so in `definitions-worksheet.md`
too, with the owner who'll settle it.

```
gtm-semantic/
├─ README.md                  what this is, how to change a definition
├─ semantic/
│  └─ metrics/
│     ├─ win_rate.yml
│     ├─ cycle_time.yml
│     └─ pipeline_created.yml
├─ definitions-worksheet.md   the six groups, for their team
├─ CODEOWNERS                 file path -> named human
└─ .github/
   └─ pull_request_template.md
```

**CODEOWNERS is the point, not decoration.** It is what turns "one named owner per metric" from
a good intention into something the tooling enforces on every pull request. One line per metric
file, mapped to the human they named in 2.2. If they gave you a GitHub handle use it; if not,
write the name and a `# TODO: replace with GitHub handle` comment.

Initialise it as a git repo and make the first commit — but **only after confirming the path**,
and never inside an existing repo's tree. Check with `git rev-parse --is-inside-work-tree`
first; if they're already in a repo, put it in a subdirectory and say so.

---

## 5. Close with what they actually do next

Three things, and they map to what the definitions still need:

1. **Take the worksheet to the owners.** Each definition needs one human's name against it.
2. **Open a pull request to change anything.** No editing definitions in a dashboard — there is
   no other door. That rule is the whole governance model.
3. **Book thirty minutes a month.** Every metric that moved more than expected, and every metric
   nobody queried at all.

Then the honest checkpoint, which is the most useful thing you can leave them with:

> In about six weeks, ask for one number from this layer that contradicts something in your
> current board deck. If this is real, there will be one — and finding it is the point. If
> everything matches perfectly, nothing has actually been defined yet.

---

## Definition of done for this run

The readiness report is step one, not the deliverable. **Do not end the run at the report.**
The run is complete when all of these are true:

1. `$RUN/draft/` exists — three metric drafts, `DRAFTS.md`, `assumptions.json`.
2. Every assumption in `DRAFTS.md` was walked with the customer — confirmed, corrected, or
   explicitly postponed by them (not by you).
3. Each metric has one named human owner, or is listed in the worksheet with who will name one.
4. `gtm-semantic/` exists as a git repo with CODEOWNERS — **or** the customer chose to stop
   early, in which case say exactly what was completed, what remains, and that re-running
   `/semantic-layer:run` resumes from the drafts.

If the customer has to leave mid-run, close by naming the state out loud: *"Drafts are
written and two of five assumptions are confirmed. Nothing is adopted yet — the repo gets
generated when we've walked the other three."* Never let a readiness report pose as the
finished product.

## Safety

- **Never write to the CRM.** No creates, updates, deletes, merges or deploys. Not even a
  custom field. If the customer asks you to, tell them this plugin is read-only by design and
  point them at their admin.
- **Never `git init` without confirming the directory**, and never inside an existing repo.
- **Never invent a field name, stage name or picklist value.** If setup didn't probe it, ask —
  a definition that references a field which doesn't exist is worse than no definition.
