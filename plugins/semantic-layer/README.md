# Semantic Layer

Walks you through defining your go-to-market metrics, then writes them out as a real semantic
layer — a git repo of versioned metric files, each with one named owner and a declared source
of truth, seeded from your actual CRM stage names and fields.

**Read-only against your CRM.** It writes files to your working directory only.

## Two commands

```
/semantic-layer:setup    reads your org — stages, amount fields, stage-history behaviour,
                         segment picklists, fiscal year — then asks only what it can't read
/semantic-layer:run      drafts the three core metrics from your schema, walks you through
                         every assumption it made, and generates the repo
```

Run one ends with a draft semantic layer, not a findings list: the three metrics arrive
already filled in from your real stages, fields and fill rates, with every guessed value
numbered as an assumption in `DRAFTS.md`. The interview is you correcting a draft — which is
faster, and harder to leave unfinished.

## What you get

```
gtm-semantic/
├─ semantic/metrics/win_rate.yml         cohort-based, not close-date
├─ semantic/metrics/cycle_time.yml
├─ semantic/metrics/pipeline_created.yml
├─ definitions-worksheet.md              the other six groups, for your team
├─ CODEOWNERS                            metric file -> named human
└─ .github/pull_request_template.md
```

Three metrics in full, because conversion rate and time in stage are the two numbers
go-to-market operations actually moves, and `pipeline_created` is the denominator they're both
argued about with. The rest comes as a worksheet you take to your team — thirty definitions is
the ceiling, not the target.

## The one thing setup will tell you that you may not want to hear

Whether your stage transitions are **system-stamped**. If a human can type the date a deal
entered a stage, cycle time is unmeasurable and you cannot backfill a timestamp that was never
written. That isn't a reason to stop — it's a reason you start measuring from today, and you
should know it now rather than in month three.

## Why the owner field is not decoration

`CODEOWNERS` maps each metric file to one named human, so a pull request touching that metric
cannot merge without their approval. Definitions owned by "the RevOps team" are owned by
nobody, and within a quarter there are two versions of win rate and a standing agenda item
about which one is right.
