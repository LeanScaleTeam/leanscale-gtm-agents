# Semantic Layer — setup

Read-only against your CRM. It writes files to your machine and nothing else.

## 1. Install

```
/plugin marketplace add LeanScaleTeam/leanscale-gtm-agents
/plugin install gtm-brain@leanscale-gtm
```

Restart your client so the skills load.

## 2. Connect a CRM

The plugin needs one capability: **`crm.describe`** — the ability to read your object schema.
It does not need write access, and it does not need a query tool to produce a readiness report
(a query tool only adds fill rates and fiscal year).

| Your CRM | Connect | Capability satisfied by |
|---|---|---|
| Salesforce | any Salesforce MCP server | a SOQL tool over `EntityDefinition` / `FieldDefinition`, or a describe tool |
| HubSpot | any HubSpot MCP server | `hubspot-list-properties` |

Salesforce is CRM-of-record for roughly two thirds of companies this is built for, HubSpot for
most of the rest. Both are first-class.

## 3. Run setup

```
/gtm-brain:setup
```

It reads your org — stages, currency fields, stage-history behaviour, segment picklists,
record types — then asks only the handful of questions your schema cannot answer, and writes
`~/.leanscale-gtm/profile.json` and `~/.leanscale-gtm/gtm-brain.json`.

`--check` re-runs the probes and prints the pass/blocked/fail table without touching config.
That is the right first move if a run starts failing.

## 4. First run

```
/gtm-brain:run
```

Produces a readiness report **and a draft semantic layer** — the three core metrics
pre-filled from your real stages, fields and fill rates, with every guessed value numbered
as an assumption in `draft/DRAFTS.md`. The interview walks those assumptions, then writes
the `gtm-brain/` repo. The run is not finished at the report: if you have a report but
no `draft/` directory and no repo, the run stopped early — re-run it.

Output lands in `./gtm-agents/gtm-brain/<timestamp>/` in whatever directory you ran from.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "No `crm.describe` tool resolved" | No CRM MCP server connected, or it exposes only write tools | Connect a Salesforce or HubSpot MCP server, then `/gtm-brain:setup --check` |
| Setup asks about stages it should have read | The describe call returned but was empty | The connected identity may not see the Opportunity object. Check its profile/permissions |
| "Probe blocked: fill rates" | No query tool resolved, only describe | Expected and fine. Fill rates and fiscal year are the only things you lose; the report still runs |
| Report says 0 findings | Genuinely clean, or the describe snapshot was thin | Check `manifest.json` in the run directory — it lists every source read and its record count |
| `analyze.py` aborts with a source error | A required source came back empty | That is deliberate. An empty required source produces no report rather than a confident empty one. The manifest names which source and why |
| `no such script 'draft'` mid-run | Installed plugin predates 1.2.0 | `claude plugin update gtm-brain@leanscale-gtm`, then `/gtm-brain:setup` to refresh the shim |
| Report exists but no `draft/` directory | The draft step failed or never ran | Read the run's stderr; a missing describe snapshot is the usual cause — `/gtm-brain:setup --check` |
| Skills don't appear after install | Client not restarted | Restart. Plugin skills load at session start |
| Fixed a bug but behaviour is unchanged | The installed cache still holds the old version | `claude plugin update gtm-brain@leanscale-gtm`. If it says "already at the latest version", the version was not bumped — uninstall and reinstall |

## What it never does

- No writes to your CRM. No creates, updates, merges, deletes or deploys.
- No `git init` without confirming the directory, and never inside an existing repo.
- No invented field names. If a probe did not read it, the skill asks rather than guessing.
