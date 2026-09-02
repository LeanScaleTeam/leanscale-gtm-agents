# LeanScale GTM Agents

Eleven GTM and RevOps agents, packaged as a Claude Code plugin marketplace, that run against a
customer's own Salesforce or HubSpot. Ten are strictly read-only. Every finding carries the
record count and the exact query that produced it.

| Plugin | Command | Needs | What it does |
|---|---|---|---|
| `gtm-brain` | `/gtm-brain:run` | CRM | Your metric definitions and commercial context as a git repo you own — one named owner per file, seeded from your real stages and fields |
| `crm-hygiene` | `/crm-hygiene:run` | CRM | Duplicate records, dead fields, records owned by leavers, stale pipeline, picklist rot → a Hygiene Index |
| `pipeline-inspection` | `/pipeline-inspection:run` | CRM | Open deals violating your own rules: stuck in stage, no next step, pushed 3×, single-threaded |
| `forecast-agent` | `/forecast-agent:run` | CRM | Audits whether the CRM can support a forecast, then calls it three ways with the method shown |
| `stage-architect` | `/stage-architect:run` | CRM + stage history | What your stages actually mean vs. what the team believes; buyer-verifiable exit criteria |
| `lead-source` | `/lead-source:run` | CRM | Source-data integrity: null/Other rate, near-duplicate values, survival through conversion |
| `system-map` | `/system-map:run` | CRM metadata | Integration users, connected apps, flows, orphaned automation, same-field write conflicts |
| `executive-reporting` | `/executive-reporting:run` | CRM | Bookings, created pipeline, coverage, cohort conversion, retention, concentration — each against a goal, each openable to the rows underneath |
| `sales-coach` | `/sales-coach:run` | Transcripts | Scores calls against *their* framework; one manager-facing team pattern report |
| `customer-health` | `/customer-health:run` | CRM (+transcripts) | Two scores — sentiment and commercial risk — because they diverge |
| `meeting-to-crm` | `/meeting-to-crm:run` | CRM + transcripts | Proposes CRM updates as an approvable diff. **The only write-capable plugin.** |

## Repo layout

```
core/
  SPEC.md              the build spec every plugin conforms to — read this first
  PLUGIN-SCHEMA.md     the verified Claude Code plugin/marketplace schema
  lib/                 shared library (config, manifest, findings, baseline, render, crmutil)
  selftest.py          62 checks over the shared library
plugins/<name>/        the eleven plugins
tools/
  vendor.py            copy core/lib -> plugins/*/scripts/lib  (installed plugins can't read ../)
  qa.py                suite-wide gate: schema, leakage, read-only statements, claude plugin validate
  package.py           build the zips into site/dist/
site/                  the public catalog page
.claude-plugin/marketplace.json
```

## Build

```bash
python3 core/selftest.py        # shared library must pass first
python3 tools/vendor.py         # vendor core/lib into every plugin
python3 tools/qa.py             # schema + leakage + claude plugin validate --strict
python3 tools/package.py        # build site/dist/*.zip
```

`tools/vendor.py --check` fails if any plugin's vendored copy has drifted from `core/lib` — run
it before packaging, since a stale vendored lib is invisible until a customer hits it.

## The two rules the whole suite rests on

**Discover before you ask.** A setup skill that opens with "what are your deal stages?" hasn't
looked. Read the schema, picklists, fill-rates and record counts first, then ask only what the
data couldn't answer — phrased in terms of what was found.

**Baseline on run one, value on run two.** Every run snapshots itself into `~/.leanscale-gtm/
baselines/`. Run one says plainly that it is a baseline rather than posing as a verdict.

## Customer-facing invariants

- Config lives in `~/.leanscale-gtm/`, never in the plugin — marketplace installs are a read-only
  cache that is replaced on update.
- One shared `profile.json`: the customer describes their business once, not ten times.
- Python is standard-library only and never touches the network. Claude fetches via MCP; Python
  transforms local files.
- A required source returning zero records **aborts the run** with a diagnosis. A report saying
  "no issues found" because auth failed is worse than a crash.
- Salesforce and HubSpot are both first-class, with real copy-pasteable queries in every skill.
- No agent requires a specific conversation-intelligence vendor.
