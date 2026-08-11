# Executive Reporting — install, connect, first run

Read-only throughout. Nothing in this plugin writes to your CRM, and nothing leaves your machine.

---

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents      # or the git URL you were given
/plugin install executive-reporting@leanscale-gtm
```

If you were sent a zip: unzip it, then `/plugin marketplace add <path-to-unzipped-folder>` and
install from there. A local directory install always resolves the relative plugin paths.

## 2. Connect your CRM

The plugin calls **your** MCP connectors — it ships no credentials and opens no connections of
its own.

**Salesforce.** Connect the Salesforce MCP server and authenticate the org you report from.
The connected user needs read on `Opportunity`, `OpportunityStage`, `Account`, `Lead`, `User`
and `Organization`. A read-only permission set is exactly right here.

**HubSpot.** Connect the HubSpot MCP server with a private app token carrying
`crm.objects.deals.read`, `crm.objects.companies.read`, `crm.objects.owners.read`,
`crm.schemas.deals.read` and `crm.pipelines.read`.

**Before you set up — have three answers ready.** These are the only things the CRM cannot
tell us, and each one changes a headline:

1. **Who owns expansion** — sales, CS, or is it stacked into one bookings number?
2. **Your targets** — board, executive and field-level. They are usually three different
   numbers. If you have none, say so; the pack will report actuals and label them "no goal
   configured" rather than invent a denominator.
3. **The numbers you quote today** — your conversion rate, your bookings figure. The pack
   reconciles against them and reports the gap. This is the single most valuable thing you can
   bring to setup.

## 3. Set up

```
/executive-reporting:setup
```

It probes the connectors, reads your real stage picklist, record counts and field fill-rates
**first**, then asks you the handful of things only a human knows. It ends with a smoke test
against your own data and a pass/fail table.

**The one question to answer carefully: the stage map.** Setup shows you your real stages with
record counts and proposes a mapping onto canonical keys (`lead`, `mql`, `sal`, `sql`, `won`,
`lost`). Everything downstream routes through that map — not through your stage labels. We have
seen an org whose displayed "SAL" is the canonical SQL stage and whose "SQL" is canonical SAL;
taking the names at face value reported the wrong conversion rate for months and nothing looked
broken. Read the proposed map against what those stages actually mean in your process.

Setup is idempotent. Re-run it any time — it doubles as the health check when a run fails.

## 4. First run

```
/executive-reporting:run
```

Expect the first run to be a **baseline**, and expect it to withhold something. That is the
plugin working, not failing.

## Troubleshooting

| What you see | What it means | Fix |
|---|---|---|
| `ABORT: stage_map is empty` | The plugin will not guess which stage is an SQL. | Run `/executive-reporting:setup`. |
| `ABORT: opportunities.json returned 0 records` | The CRM returned nothing — usually a permission or a date-filter problem, not an empty pipeline. | Re-run setup to re-probe; check the connected user can read Opportunity. |
| `Reporting readiness NN/100 — headlines withheld` | The data cannot support the pack yet. | Work the critical and high findings, re-run. `--force` publishes anyway; use it knowingly. |
| A conversion rate shows as `NO — exceeds 100%` | A stage is not being stamped on every record. | Make that stage an automatic stamp on entry. The rate stays withheld until it is. |
| `suppressed — not ripe` on recent cohorts | Those cohorts are still mostly open. | Nothing to fix. Lower `cohort_ripeness_days` only if your cycle is genuinely shorter. |
| Most bookings sit in an "Unallocated" channel | Source is not being captured at creation. | Make source required at creation and backfill closed-won. Below ~10% unallocated before the channel view is publishable. |
| The report says MRR and you expected ARR | `recurring_cadence` is set to `monthly`. | Change it in `~/.leanscale-gtm/executive-reporting.json`. The plugin will never convert between them on its own. |

## Where your configuration lives

```
~/.leanscale-gtm/
    profile.json                 shared across every LeanScale agent
    executive-reporting.json     this plugin's settings — every key has a _help line
    baselines/executive-reporting/   dated snapshots, so run two can show deltas
```

Config lives in your home directory, never inside the plugin — plugin installs are a read-only
cache that is replaced on update. Edit these files by hand any time.
