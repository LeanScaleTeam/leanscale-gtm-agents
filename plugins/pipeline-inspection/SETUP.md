# Setting up Pipeline Inspection

Read-only throughout. Nothing below asks your CRM for write access, and the plugin has no
code path that modifies a record.

---

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents      # or your git URL / GitHub shorthand
/plugin install pipeline-inspection@leanscale-gtm
```

If you downloaded a zip, unzip it first and point `marketplace add` at the unzipped
directory — relative plugin paths do not resolve from a bare URL.

## 2. Connect your CRM

You need **one** of these MCP servers connected, with **read** scopes only.

### Salesforce

Any Salesforce MCP that exposes a SOQL query tool. The connected identity needs read access
to: `Opportunity`, `OpportunityStage`, `OpportunityHistory`, `OpportunityFieldHistory`,
`OpportunityContactRole`, `Account`, `User`, `Task`, `Event`, `Organization`.

Two things are worth checking before you start, because they decide how good the report is:

- **Field history tracking on `CloseDate`.** Setup → Object Manager → Opportunity → Fields &
  Relationships → **Set History Tracking** → tick Close Date (and Amount). Without it the
  highest-signal check in the plugin — how many times a deal has pushed — is unavailable.
  Salesforce retains 18 months of history by default, 24 with Field Audit Trail, so turning
  it on today starts a clock rather than recovering the past. Turn it on anyway.
- **Sharing rules.** If the connected user only sees their own records, the report only
  covers their own records. Use an identity with org-wide read, or accept a partial view and
  say so.

### HubSpot

Any HubSpot MCP, or a private app token with these read scopes:
`crm.objects.deals.read`, `crm.objects.contacts.read`, `crm.objects.companies.read`,
`crm.objects.owners.read`, `crm.schemas.deals.read`, and — if you want activity detail —
`sales-email-read` plus the engagement scopes.

If you run more than one deal pipeline, decide which one to inspect. Blending two pipelines
produces stage medians that describe neither.

### Neither?

The plugin requires a CRM. There is no manual-paste path here, because the whole product is
per-deal evidence at volume.

## 3. Run setup

```
/pipeline-inspection:setup
```

It probes your tools, reads your stages and fiscal settings, **measures your real
days-in-stage medians, close-date push distribution and contacts-per-deal**, then asks you to
confirm or override the thresholds derived from them. It ends with a smoke test that produces
a real finding and a pass/fail table.

Expect it to take 10–15 minutes and to ask you about ten things:

1. Expected days in each stage — shown against your measured medians first
2. Where "next step" lives: a field, an open task, either, or nowhere
3. How many close-date pushes is too many — shown against your actual distribution
4. The single-threading bar by deal size
5. Which stages mean "commit"
6. Your deal-size bands and the floor below which deals are noise
7. The activity-silence window, and any per-stage overrides
8. Whether you inspect weekly or monthly
9. What to do about any dead stages discovery found
10. Scope (whole pipeline or one team) and whether to pseudonymise rep names in reports

Config is written to `~/.leanscale-gtm/pipeline-inspection.json` and the shared org profile to
`~/.leanscale-gtm/profile.json`. Both are yours to hand-edit; every key has a `_help` line
next to it, and both survive plugin updates.

## 4. Run it

```
/pipeline-inspection:run
```

Output lands in `./gtm-agents/pipeline-inspection/<timestamp>/`. Forward `report.html`; work
from `call-list.csv`.

The first run is your baseline and says so. The value shows up on run two.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SourceEmptyError: open_opportunities returned 0 records` | Connection or permission problem, not an empty pipeline | Re-run `:setup`. Check the connected identity can read Opportunity and is not restricted by sharing rules. The plugin aborts deliberately — a report saying "no issues" because auth failed is worse than a crash |
| `SourceEmptyError: closed_opportunities returned 0 records` | No closed deals in the lookback window, or the date filter excluded them | Widen `history_lookback_months`. If the org genuinely has no closed deals yet, set `require_closed_history: false` — and accept that every threshold is then an assumption, not a measurement |
| Report says close-date pushes are unavailable | Field history tracking is off for `CloseDate` (Salesforce) or the batch read ran without `propertiesWithHistory` (HubSpot) | Turn on history tracking; re-run. Existing deals will start accumulating history from today |
| Push counts look too low | History retention: Salesforce drops rows past 18–24 months, HubSpot truncates old property versions | Nothing to fix — counts are a floor. The report prints the oldest change it saw so you can judge coverage |
| Stage medians say "too few" for a stage | Fewer than `min_closed_deals_for_median` completed intervals | Normal for a rare stage. The threshold falls back to your expected days, then the all-stage median; the report names which basis it used |
| Everything is flagged | Thresholds inherited defaults instead of your numbers | Re-run `:setup`, or edit `~/.leanscale-gtm/pipeline-inspection.json` directly. A first inspection flagging 60–70% of pipeline is common and usually correct |
| Owner shows as a number | HubSpot owner ids were not resolved | The run skill should call `GET /crm/v3/owners` and inject `hubspot_owner_name`. Re-run `:run` |
| Stage shows as `decisionmakerboughtin` | `stage_metadata.json` is missing | Fetch `GET /crm/v3/pipelines/deals` into `raw/stage_metadata.json` and re-run `analyze.py` |
| `No configuration found` | Setup has not run | `/pipeline-inspection:setup`. To try the scripts offline instead, pass `--config ./config.example.json` |
| Deltas never appear | Every run is being treated as a baseline | Check `~/.leanscale-gtm/baselines/pipeline-inspection/` has snapshots. `report.py --no-baseline` skips saving; a run directory already carrying `.baseline-saved` will not save twice |
| Names in the report need hiding | | Set `redact_pii_in_reports: true` in `~/.leanscale-gtm/profile.json`. `report.md` / `report.html` get stable pseudonyms; `raw/` and `findings.json` stay unredacted locally so you can still act on them |

## Data handling

Everything stays on your machine. Raw CRM extracts sit in the run directory under your
project — treat them as you would any CRM export, and add `gtm-agents/` to `.gitignore` if
your project is a repository.
