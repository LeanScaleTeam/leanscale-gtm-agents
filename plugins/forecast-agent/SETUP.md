# Forecast Agent — install, connect, first run

Read-only throughout. Nothing in this plugin writes to your CRM.

---

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents      # or the git URL you were given
/plugin install forecast-agent@leanscale-gtm
```

If you were sent a zip: unzip it, then `/plugin marketplace add <path-to-unzipped-folder>` and
install from there. A local directory install always resolves the relative plugin paths.

## 2. Connect your CRM

The plugin calls **your** MCP connectors — it ships no credentials and opens no connections of
its own.

**Salesforce.** Connect the Salesforce MCP server and authenticate the org you forecast from
(production, not a sandbox, unless you are testing). The connected user needs read on
`Opportunity`, `OpportunityHistory`, `OpportunityFieldHistory`, `OpportunityContactRole`,
`OpportunityStage`, `User` and `Organization`. Read-only is enough — a read-only permission set
is the right thing to hand this plugin.

**HubSpot.** Connect the HubSpot MCP server with a private app token carrying `crm.objects.deals.read`,
`crm.objects.contacts.read`, `crm.objects.owners.read`, `crm.schemas.deals.read` and
`crm.pipelines.read`.

**Before you set up — one two-minute admin job that pays for itself.** In Salesforce, turn on
field history tracking for `CloseDate` and `ForecastCategoryName` on Opportunity
(Setup → Object Manager → Opportunity → Fields & Relationships → Set History Tracking). Those
two fields are the difference between "56% of your commit has already slipped once" and "we
can't tell." History **cannot be backfilled** — it starts the day you switch it on. HubSpot
tracks property history automatically, so there is nothing to do there.

## 3. Set up

```
/forecast-agent:setup
```

It probes the connectors, reads your CRM, **measures your real conversion rates, slip
distribution and commit accuracy first**, then asks you the handful of things only a human
knows: your methodology, what counts as the number, the roll-up hierarchy, cadence, quota, and
how far back your history is comparable. It ends with a smoke test and a pass/fail table.

Setup is idempotent. Re-run it any time — it doubles as the health check when a run fails.

It writes two files you can edit by hand at any time:

```
~/.leanscale-gtm/profile.json          shared by every LeanScale GTM agent
~/.leanscale-gtm/forecast-agent.json   this plugin's settings
```

## 4. First run

```
/forecast-agent:run
```

That is the audit, and it is the right place to start. It writes to
`./gtm-agents/forecast-agent/<timestamp>/` — open `report.html` in a browser.

Run one is your baseline. The report says so. The comparison starts on run two.

Once the audit clears the threshold (default 60):

```
/forecast-agent:run --mode forecast
```

Below the threshold the call is withheld, on purpose, with an explanation. `--force` publishes
it anyway; if you use that, show the integrity score next to the number.

## 5. Cadence

Run the audit **monthly**, and again after any change to stages, forecast categories or the
sales team. Run the call the **morning before your forecast submission deadline** — early
enough that the delta can still be worked deal by deal, late enough that the pipeline is what
the reps intend to call.

---

## Troubleshooting

| Symptom | What it means | Fix |
|---|---|---|
| `Run aborted — a required data source returned zero records` | Not an empty CRM. Almost always permissions or a filter. | Read the diagnosis in the message; it names the likely cause and prints the query. Re-run `/forecast-agent:setup`. |
| `No org profile at ~/.leanscale-gtm/profile.json` | Setup has never completed. | `/forecast-agent:setup` |
| Close-date pushes show as zero on every deal | Field history tracking is off for `CloseDate`, or the history is older than the retention window. | Turn on history tracking. It cannot be backfilled. |
| Score is capped and "Date integrity" says *not measurable* | `field_history.json` was empty. | Same as above. |
| "Buying-group coverage — not measurable" | No `OpportunityContactRole` rows, or HubSpot without association data. | Salesforce: check contact roles are actually used. HubSpot: the plugin falls back to `num_associated_contacts` — make sure that property is requested. |
| No coverage ratio | No quota. Deliberate — the plugin will not invent a denominator. | Put `quota.org_quota` or `quota.period_quota_by_owner` in `~/.leanscale-gtm/forecast-agent.json`. |
| `Multi-currency org, but no converted-amount field` | Totals are adding different currencies. | Salesforce: use `convertCurrency(Amount)` (already in the run skill) or add a corporate-currency roll-up field and point `field_map.amount_converted` at it. HubSpot: request `amount_in_home_currency`. |
| Deals missing from the forecast entirely | An unmapped forecast-category or type value. | The report names them. Add the values to `category_map` / `type_map`. |
| Conversion rates look too good | You may be reading the survivorship column. | The report shows entered-cohort and survivorship side by side. The entered-cohort number is the real one. |
| Period is wrong (quarters look like calendar quarters) | `fiscal_year_start_month` is wrong in the profile. | Fix it in `~/.leanscale-gtm/profile.json`; check `fiscal_year_naming` too (`ends_in` vs `starts_in`). |
| `ForecastingQuota` errors on Salesforce | Collaborative Forecasting is off, or the user lacks "Manage Quotas". | Enter quota manually in the config instead. |
| HubSpot search returns only 10,000 deals | API cap per query. | The run skill splits by `createdate`. If you hit it, narrow the history window. |
| Report has no rep names | `redact_pii_in_reports` or `redact_reps` is true. | Set `redact_reps: false` in the plugin config. `findings.json` and `raw/` are never redacted. |

## Testing it offline

Both CRM shapes ship as fixtures, with their own config and profile, so you can see real output
before connecting anything:

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/forecast-agent" --root)"
"$HOME/.leanscale-gtm/bin/forecast-agent" analyze \
  --raw "$AGENT_ROOT/fixtures/salesforce/raw" --out /tmp/fa --mode audit \
  --config "$AGENT_ROOT/fixtures/salesforce/config.json" \
  --profile "$AGENT_ROOT/fixtures/salesforce/profile.json" --no-baseline
"$HOME/.leanscale-gtm/bin/forecast-agent" report --run /tmp/fa
```

Swap `salesforce` for `hubspot` to see the HubSpot path, which also demonstrates how the plugin
degrades when contact associations and quota are missing.

## Privacy

Everything stays local. The only network traffic is the MCP servers you already connected. No
telemetry, no phone-home, no uploaded reports. `report.html` is a single self-contained file
with no external requests — it opens with the wifi off and survives being forwarded to a CFO.
