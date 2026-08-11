# System Map

**This plugin is read-only. It reads metadata — integration users, connected apps, installed
packages, flows, workflow rules, Apex triggers, validation rules, assignment rules and scheduled
jobs — and it changes nothing. No records are created, updated or deleted. No metadata is
deployed. Nothing is uploaded anywhere: every report is a local file on your machine, and the
only network traffic is to the MCP servers you already connected.**

It answers a question most teams cannot answer about their own CRM: **what is actually wired
into this thing, and who owns it?**

The median GTM stack in this market is about ten tools. Almost nobody can name everything
holding a write connection to their CRM. The recurring finding — and it is genuinely alarming
the first time you see it — is automation running in production that was last touched by
someone who left two years ago, writing to a field that a second automation also writes, with
no defined order between them.

---

## What it inventories

| Area | What you get |
|---|---|
| **Integration users and API consumers** | Every service account, its last login, its write surface object by object, and whether the person who owns it still works there. |
| **Connected apps, OAuth grants, packages** | Name, installer, install date, last recorded use — and which are **orphaned**: installed by someone now inactive, or unused past your dormancy threshold. |
| **Automation inventory** | Salesforce: flows and their versions, Process Builder processes, workflow rules, Apex triggers and classes, validation rules, assignment rules, scheduled jobs. HubSpot: workflows, enrollment triggers, last-modified and last-enrolled dates. For each: active or not, which object, who last changed it, when. |
| **Field-write conflicts** | The finding that earns the install: the same field on the same object written by more than one active automation, with the order they fire in where the platform defines one. |
| **Dormant automation** | Active but idle past your threshold — and the inverse, everything changed in the last two weeks that nobody flagged. |
| **Object surface** | Which objects carry automation and which carry none. A custom object with 84,000 records and zero automation is usually a dumping ground. |
| **The stack map** | Third-party tools inferred from package namespaces, connected-app publishers, integration-user naming and field-name fingerprints (`gong__`, `zi_`, `SBQQ__`, `mkto_si__`, `chilipiper_`), clustered by what they do — and contrasted against the tools your team said were connected. |

### Headline scores

Automations counted · orphaned automations · fields written by more than one automation ·
integration users with write access · tools detected vs tools believed.

---

## Why the "believed" list matters

Setup asks your team which tools they think are connected, **before** anything is measured. The
run measures reality. The gap is the deliverable — and it goes both ways:

- **Detected but not named.** Something is writing to your CRM that nobody budgeted for,
  nobody reviews, and nobody will notice breaking.
- **Named but not detected.** Either the connection is not live — so whatever depends on that
  sync is quietly broken — or it runs through middleware you have not accounted for.

---

## Sample output

```
Automations counted                      44   38 active across 7 objects
Orphaned automations                     15   active, last changed by someone whose account is gone
Fields written by 2+ automations          5   3 with no guaranteed firing order
Integration users with write access       5   6 integration identities found in total
Tools detected vs believed                12   you named 6 · 7 undisclosed · 1 named but not found

[CRITICAL] 3 integration users are owned by someone who has left
[CRITICAL] 5 fields are written by more than one active automation
[HIGH]     15 active automations were last changed by a departed user
[HIGH]     4 connected apps are orphaned
[HIGH]     5 integration users hold write access, 1 of them unused
[HIGH]     7 connected tools nobody named in setup
[MEDIUM]   4 active Workflow Rules / Process Builder processes remain
[MEDIUM]   26 active automations look dormant
[MEDIUM]   8 objects hold records but carry no automation
[LOW]      53 inactive flow versions across 7 flows
```

A conflict, expanded:

```
Opportunity.Forecast_Category__c — 4 active automations, order NOT guaranteed

  Set Opportunity Defaults            Before-save record-triggered flow
  Update Forecast Category on Close   Workflow rule field update
  Opportunity Forecast Category Sync  After-save record-triggered flow
  Opportunity Stage Hygiene           After-save record-triggered flow

  The last two sit at the same point in the order of execution and neither has flow
  trigger ordering set, so the platform does not define which one wins.
```

Every finding ships with the record count and the exact query that produced it, so a skeptical
RevOps lead can verify it in their own CRM in under a minute.

---

## Permissions, handled honestly

Much of this needs metadata access, not record access, and the identity behind your MCP
connection may not have it. So the plugin **probes each surface separately and degrades per
section**. Anything it could not read is listed in the report as *unavailable, not clean*, with
the exact permission that would fix it:

> **Flow field writes** — `Flow.Metadata` (Tooling API, one Id per query). Fix: 'Manage Flow'
> (or 'View All Data') + API Enabled, via the Tooling API. The Metadata field can only be
> selected when the query filters on a single Id — batching it fails.

A permissions failure never renders as a clean bill of health. If every automation surface comes
back empty, the run **stops** with a diagnosis instead of publishing a confident, empty, wrong
report.

On Salesforce the shortest path is usually one permission set on the connected identity:
**View Setup and Configuration**, **API Enabled**, and read access. It never needs Modify All
Data, and should not have it. On HubSpot the scopes are `settings.users.read`,
`crm.objects.owners.read`, `automation`, `crm.schemas.*.read` and the per-object read scopes —
with one honest gap: HubSpot publishes no API for installed apps, so app-orphan detection needs
a one-time manual export, and the report says so when it is missing.

---

## Both CRMs, first class

Salesforce and HubSpot are both fully written out — real Tooling API SOQL, real HubSpot
endpoints, real fallbacks. Sandbox versus production is asked during setup and labelled on the
report, because a sandbox finding is worthless in a production conversation.

---

## Baseline and delta

Run one writes a baseline and says so: *"This is your baseline. The comparison starts next
run."* Every run after it shows what moved — fewer conflicts, fewer orphans, more automation.
Snapshots live in `~/.leanscale-gtm/baselines/system-map/` and are never pruned. They are your
evidence trail: the proof that the cleanup actually happened.

---

## Install and run

```
/plugin install system-map@leanscale-gtm
/system-map:setup      # probe, discover, interview, smoke test — do this first
/system-map:run        # the full inventory
```

Setup is idempotent. Re-run it any time; it doubles as the health check when a run fails.

Full instructions and a troubleshooting table are in `SETUP.md`.

---

## Files it writes

```
./gtm-agents/system-map/<YYYY-MM-DD-HHMM>/
    raw/            exactly what each source returned, plus _sources.json
    findings.json   machine-readable result
    manifest.json   provenance, per-source record counts, failures
    report.md       the findings doc
    report.html     self-contained, opens locally, no external requests

~/.leanscale-gtm/
    profile.json                     shared org profile, read by every agent in this suite
    system-map.json                  this plugin's settings
    baselines/system-map/*.json      dated snapshots
```

Reports stay on your machine. Nothing is hosted, uploaded, or phoned home.
