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
```

If either is missing, stop and tell them to run `/semantic-layer:setup` first. Do **not**
interview someone about their stages when you could have read them.

Re-read the probed values. You will reference their real stage names, their real amount fields
and their real segment picklist throughout — never a placeholder, never an invented example.

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

For each, run this loop. Do not batch the questions — one metric at a time, finished, then the
next. A finished definition they've agreed to beats three half-argued ones.

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

### 2.3 Write the file
Use `templates/metric.yml` as the shape. Fill it with their actual field names. Include the
`# cohort, not close` comment on the time block where it applies — that one line is the
difference between a win rate they can defend and one that reshuffles every quarter.

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

## Safety

- **Never write to the CRM.** No creates, updates, deletes, merges or deploys. Not even a
  custom field. If the customer asks you to, tell them this plugin is read-only by design and
  point them at their admin.
- **Never `git init` without confirming the directory**, and never inside an existing repo.
- **Never invent a field name, stage name or picklist value.** If setup didn't probe it, ask —
  a definition that references a field which doesn't exist is worse than no definition.
