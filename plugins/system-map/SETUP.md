# System Map — setup

Read-only throughout. Setup and run both read metadata and change nothing in your CRM.

---

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents      # or the git URL you were given
/plugin install system-map@leanscale-gtm
```

Installing from a downloaded zip: unzip it first, then
`/plugin marketplace add <path-to-unzipped-folder>`. A local directory install always resolves.

---

## 2. Connect your CRM

You need one MCP server that can query your CRM. The plugin discovers the tool names at
runtime, so anything that exposes a query capability will work.

### Salesforce

Any Salesforce MCP server providing a SOQL query tool. Two things decide how much of this
plugin works:

**a) Can it reach the Tooling API?** Some servers only route to the standard query endpoint.
Test it:

```
SELECT Id FROM FlowDefinitionView LIMIT 1     -- standard API
SELECT Id FROM ApexTrigger LIMIT 1            -- Tooling API only
```

If the first works and the second returns `sObject type 'ApexTrigger' is not supported`, you are
on the standard endpoint. Flow inventory still works (`FlowDefinitionView` and `FlowVersionView`
are exposed on the standard API), but Apex, validation rules, workflow rules, connected apps,
installed packages and **field-write conflict detection** will not. Setup tells you this before
you commit to anything.

**b) What can the connected identity see?** The cleanest grant is a single permission set
assigned to the identity behind the MCP connection:

| Permission | Unlocks |
|---|---|
| **API Enabled** | everything |
| **View Setup and Configuration** | login history, permission sets, object permissions, scheduled jobs, validation rules, workflow rules, setup audit trail, field inventory |
| **Manage Users** | OAuth grants for *all* users — without it you see only your own tokens, which looks identical to a clean org |
| **Manage Flow** *(or View All Data)* | flow definitions, which is where field-write detection comes from |
| **Author Apex** | Apex triggers and classes |
| **Customize Application** + **Manage Connected Apps** | connected apps |
| **Download AppExchange Packages** | installed managed packages |

It never needs **Modify All Data**, and should not have it. This plugin does not write.

Two surfaces cannot be granted at all without a paid add-on: per-consumer API call volume
(`ApiEvent`) and true flow execution counts (`EventLogFile` / FlowExecution) both require
**Salesforce Shield Event Monitoring**. Without them, dormancy is measured from last-modified
date and the report labels it as a proxy.

### HubSpot

A HubSpot MCP server, or a private-app token used through `curl`. Scopes:

| Scope | Unlocks |
|---|---|
| `settings.users.read` | users *(required)* |
| `crm.objects.owners.read` | owners, including archived ones — how departed editors are detected |
| `automation` | workflows *(needs Operations Hub or Marketing Hub Professional and above)* |
| `crm.schemas.contacts.read`, `.companies.read`, `.deals.read` | property fingerprints |
| `crm.schemas.custom.read` | custom objects *(Enterprise)* |
| `crm.objects.contacts.read`, `.companies.read`, `.deals.read`, `.tickets.read` | record counts |
| `account-info.security.read` | login and security activity *(Enterprise)* |

**One honest gap:** HubSpot publishes **no API** that lists installed apps or private apps. There
is nothing to grant. To get app-orphan detection, export it once by hand:

1. Settings → Integrations → **Connected Apps** — note name, who installed it, install date,
   last use.
2. Settings → Integrations → **Private Apps** — note name, creator, scopes, last use.
3. Save as `raw/hs_connected_apps.json` in the run directory:

```json
{"results": [
  {"name": "Warehouse Reverse Sync", "type": "private_app",
   "createdBy": "Jordan Wu", "installedBy": "Jordan Wu",
   "installedAt": "2025-03-11", "lastUsedAt": "2026-08-10", "callsLast30Days": 96204,
   "scopes": ["crm.objects.contacts.write", "crm.objects.deals.write"]}
]}
```

`type` must be `private_app` or `marketplace_app`. Private apps are HubSpot's real integration
users — that is how the run finds a live token whose owner has left. Without the file, the
report says the section is blind rather than clean.

---

## 3. Run setup

```
/system-map:setup
```

It will, in order:

1. Probe every metadata surface one at a time and **show you the results before asking
   anything**.
2. Read `~/.leanscale-gtm/profile.json`, or create it if this is your first agent from this
   suite. It asks only what your CRM cannot tell it.
3. Discover your objects, record counts, automation census, integration identities and installed
   packages.
4. Interview you — production or sandbox, what to include, the dormancy threshold, whether to
   flag orphans, and **which tools you believe are connected**.
5. Write `~/.leanscale-gtm/system-map.json`.
6. Run a smoke test on your busiest object and show you a real finding.
7. Print a pass/fail table naming every metadata surface and, for each failure, the exact
   permission that fixes it.

### Answer question 9 honestly

*"Which tools do you believe are connected to this CRM?"* — answer from memory, before checking
anything. That list is compared against measured reality, and the gap is the most useful thing
in the report. Checking first destroys the finding.

---

## 4. First run

```
/system-map:run
```

Ten to fifteen minutes on a large Salesforce org, mostly because flow definitions must be
fetched one at a time — a Tooling API constraint, not a bug. HubSpot is faster.

Output lands in `./gtm-agents/system-map/<date-time>/`. Open `report.html`.

Run one is your baseline and says so. Run two onward shows deltas.

---

## 5. Troubleshooting

| Symptom | What it means | Fix |
|---|---|---|
| `No org profile at ~/.leanscale-gtm/profile.json` | Setup never completed. | Run `/system-map:setup`. |
| `No .../_sources.json found` | The fetch step did not record which surfaces were readable. The pipeline refuses to run without it, so a permissions failure cannot masquerade as a clean org. | Re-run `/system-map:run`; it writes the file. |
| `Run aborted — a required data source returned zero records` | Every automation surface came back empty. Almost always permissions, not an empty CRM. | Read the listed surfaces and their permissions, grant them, re-run. Only use `--allow-empty-automation` if you have personally confirmed the instance has no automation. |
| `sObject type 'ApexTrigger' is not supported` | Your query tool is bound to the standard endpoint, not the Tooling API. Not a permission problem. | Connect a tooling-capable query tool, or a metadata-retrieve tool, or authenticate the `sf` CLI and let the skill shell out to `sf data query --use-tooling-api`. |
| `INSUFFICIENT_ACCESS` on a Tooling object | Routing is fine; the permission is missing. | Grant the permission named in the report's "not covered" list. |
| Only 1–2 OAuth grants found, on an org you know has more | The identity lacks **Manage Users**, so `OauthToken` returns only its own rows. | Grant Manage Users. This one is dangerous precisely because it looks like a clean result. |
| `Flow.Metadata` query fails when batched | Expected. The Tooling API only returns `Metadata` when the query filters to a single Id. | The skill loops one Id at a time. If you wrote the query by hand, add `WHERE Id = '301...'`. |
| HubSpot workflows return 403 | Either the `automation` scope is missing, or the portal is on Starter, where the API does not exist. | Add the scope; if the portal is Starter there is nothing to grant. |
| Connected apps section empty on HubSpot | There is no API for it. | Do the manual export in step 2. |
| Zero conflicts reported but you know a field argues with itself | Field-write detection needs flow definitions. If `salesforce_flow_metadata` is in the report's "not covered" list, the run listed your flows but never saw inside them. | Grant 'Manage Flow' and ensure Tooling routing. |
| Apex writes look wrong | Apex field writes are parsed from source and labelled `apex_body_heuristic`. Writes made inside a helper class are invisible to it. | Treat Apex rows in the conflict table as leads, and confirm in the class. |
| Report names people you would rather it did not | PII redaction is off. | Set `redact_pii_in_reports: true` in `~/.leanscale-gtm/profile.json`. Person names become stable pseudonyms and every email address is scrubbed in `report.md` / `report.html`. Service-account and app **display** names stay — "ZoomInfo Integration" is a system, not a person, and pseudonymising it leaves a finding nobody can act on. Setup-audit descriptions are withheld entirely, because that text is free-form and cannot be safely scrubbed field by field. `raw/` and `findings.json` keep the real values locally. |
| Too many dormancy flags | Threshold too tight for your seasonality. | Raise `dormancy_days` to 180 in `~/.leanscale-gtm/system-map.json`. |
| A tool you use is missing from the stack map | Its signature is not in the fingerprint table, or it connects through middleware and leaves no trace of its own. | Check the "installed packages this run could not identify" finding — it is probably there under a namespace. Nothing is silently dropped. |
| A tool appears that you do not use | A name-only match. Check the "Detected via" column: a managed-package namespace is proof, a matching service-account name is a lead. | Low-confidence rows are labelled. Ignore or add it to `ignore_namespaces`. |

---

## 6. Tuning

`~/.leanscale-gtm/system-map.json` — every key has a `_<key>_help` line beside it. The ones you
will actually change:

- `dormancy_days` — 90 by default. 30 for a fast-moving instance, 180 if you have annual jobs.
- `believed_tools` — keep it current. It is the other half of the most interesting finding.
- `conflict_ignore_fields` — fields you have deliberately decided may be written by several
  systems. Keep the list short; every entry is a finding you have chosen not to see.
- `extra_integration_user_patterns` — your own service-account naming convention.
- `object_surface_min_records` — how many records an object needs before "carries no
  automation" is worth reporting.

---

## 7. Offline check

The plugin ships fixtures for both CRM shapes, so you can confirm the scripts work before
pointing them at anything real:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
  --raw "${CLAUDE_PLUGIN_ROOT}/fixtures/salesforce/raw" --out /tmp/system-map-demo
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --run-dir /tmp/system-map-demo
open /tmp/system-map-demo/report.html
```

Swap `salesforce` for `hubspot` to see the other shape. Both fixtures contain a real
same-field conflict, an orphaned connected app and an integration identity whose owner has
left, so you can see exactly what those findings look like before they are about you.
