# Stage Architect — setup

Read-only. Setup reads your CRM and writes two files in your home directory. It never writes
to your CRM.

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents
/plugin install stage-architect@leanscale-gtm
```

Or from a downloaded zip: unzip it, then `/plugin marketplace add <path-to-unzipped-folder>`
and install from there. A local directory install always resolves correctly.

## 2. Connect a CRM

You need one MCP server providing both capabilities:

| Capability | Salesforce | HubSpot |
|---|---|---|
| `crm.query` | `run_soql_query` or equivalent | deals search / API request |
| `crm.describe` | object + picklist describe | properties + pipelines API |

### Salesforce permissions

The connected user needs read access to:

- `Opportunity` (including any custom loss-reason field)
- `OpportunityStage` — the stage picklist
- **`OpportunityHistory`** — the stage-transition object. This is the one that matters.
  Without it the plugin cannot measure conversion and degrades to a snapshot analysis.
- `RecordType`, `Organization`
- `Lead` — only if you have a separate lead lifecycle

`OpportunityHistory` is a standard object that needs no configuration and is populated for
every org. `OpportunityFieldHistory` is the fallback: it requires field history tracking to
have been switched on for `StageName`, and it is retained **18 months** (24 with Field Audit
Trail). If neither is readable, setup will tell you which one failed and why.

### HubSpot scopes

- `crm.objects.deals.read`
- `crm.schemas.deals.read` (pipelines and stage metadata)
- `crm.objects.contacts.read` — only for the lead lifecycle

## 3. Run setup

```
/stage-architect:setup
```

It will, in order: probe the connectors; read or create the shared profile at
`~/.leanscale-gtm/profile.json`; pull your real stage picklist per pipeline with deal counts
and stage-history coverage; then interview you on the things the CRM cannot answer; write
`~/.leanscale-gtm/stage-architect.json`; run a smoke test on a narrow slice; and finish with a
pass/fail table.

### Have these ready

- **Your written stage definitions.** The wiki page, the enablement doc, the field help text —
  whatever your team is actually told each stage means. Paste it verbatim. The report holds
  your own wording up against the buyer-verifiable standard, so a tidied-up version defeats
  the exercise.
- **Which stage is "sales-accepted."** The boundary between marketing/SDR yield and sales
  execution.
- **Whether a separate lead lifecycle exists** (MQL → SQL → SAL) and which value means
  accepted.
- **Your believed conversion rates.** Setup asks for these **before** showing you any measured
  number, because the delta is the deliverable. Bring the numbers you actually plan with:
  overall win rate, and roughly what share of deals at each stage move to the next.
  If you can, get the answer from two people — the CRO and whoever owns the plan. The spread
  between them is a finding on its own.

Setup is idempotent. Re-run it any time; it doubles as the health check when a run fails.

## 4. First run

```
/stage-architect:run
```

Output lands in `./gtm-agents/stage-architect/<YYYY-MM-DD-HHMM>/`. Open `report.html` — it is
a single self-contained file with no external requests, so it opens offline and survives being
forwarded.

**Run one is a baseline.** It says so on the console and in the report. The comparison starts
on run two.

## 5. Tuning

Edit `~/.leanscale-gtm/stage-architect.json` by hand — every key has a `_<key>_help` line
under it explaining what it does. The two that most change the output:

- `min_cohort_size` (default 30) — the minimum resolved deals a stage needs before its rate is
  allowed into the non-discriminating-pair test. Raise it if you have plenty of deals; do not
  drop it below about 20, or you will start "merging" stages on noise.
- `believed_conversion_rates` — do not backfill these from a report you have already read.
  A belief captured after the measurement is not a belief.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Run aborted — a required data source returned zero records` | The integration user cannot see `Opportunity`, or the date filter excluded everything | Read the diagnosis in the abort message; it names the source and the query. Check object permissions first, then the lookback window. |
| Finding: *"No stage history: every conversion rate below is a guess"* | `OpportunityHistory` not readable, or HubSpot deals were read without `propertiesWithHistory` | Salesforce: grant read on `OpportunityHistory`. HubSpot: use batch read with `propertiesWithHistory: ["dealstage"]`, or add the `hs_date_entered_<stageId>` properties. |
| Finding: *"Only 62% of deals have any stage history"* | Field-history retention (18 months), or a CRM migration where loaded deals have history starting at the load date | Lower `history_lookback_days` to the period with full coverage, or switch from `OpportunityFieldHistory` to `OpportunityHistory`. |
| Every pair says *"not tested — needs 30 resolved deals per stage"* | Too few deals in the window, or too many stages splitting them | Widen `history_lookback_days`, or accept that stages this thin cannot be evaluated yet and say so. Do not lower `min_cohort_size` to force a verdict. |
| Score shows `belief_gap_pp: not captured` | Setup was skipped or the belief question was not answered | Re-run `/stage-architect:setup`. Without a captured belief there is no gap, and the gap is the point. |
| `No such column 'CurrencyIsoCode'` | The org is not multi-currency | Drop that field from the opportunity query. |
| Report shows a stage skipped by 100% | A stage nobody enters — a dead stage, not a skipped one | Expected: the plugin reports these separately as stages with no traffic and excludes them from skip maths. If it appears as a skip, the stage does have some traffic. |
| Conversion rates look far lower than your CRM dashboard | Working as designed | Your dashboard is computing from current stage, which drops lost deals out of the denominator. The report shows both numbers side by side with the gap in percentage points. |
| Person names appear in the report and shouldn't | `redact_pii_in_reports` is false | Set it to `true` in `~/.leanscale-gtm/profile.json`. `raw/` and `findings.json` stay unredacted on your machine; only the rendered reports get stable pseudonyms. |
| Want to test without touching your real config | — | Set `LEANSCALE_GTM_HOME` to a scratch directory before running; all config and baselines move there. |

## What never happens

- No writes to your CRM, under any flag.
- No telemetry, no phone-home, no uploading of reports.
- No report is ever deployed or hosted. Everything stays on your machine.
- The Python scripts have no network access at all — they read and write local files only.
