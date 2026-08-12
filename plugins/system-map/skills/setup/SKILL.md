---
name: setup
description: >-
  One-time (and re-runnable) setup for the System Map agent. Probes which CRM metadata surfaces
  the connected identity can actually read, reports that first, discovers the org's objects,
  packages, users and automation counts, then asks only the questions the CRM cannot answer —
  production or sandbox, what to include, the dormancy threshold, and which tools the team
  believes are connected. Ends with a smoke test and a pass/fail table naming every metadata
  surface. Trigger on "/system-map:setup", "set up system map", "configure the system map
  agent", "why did the system map run fail", "check my system map permissions", or when a
  /system-map:run reports missing config.
argument-hint: "[--reconfigure] [--sandbox]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# System Map — setup

Idempotent. Re-run it any time; it doubles as the health check when a run fails.

**Rule for this whole skill: discover before you ask.** Every question the CRM could have
answered and you asked anyway makes the product feel dumb. Every question you skip that changes
the analysis makes the output wrong. Read first, then ask — and phrase each question in terms of
what you found.

**Read-only.** Setup queries metadata and writes two files in the user's home directory. It
changes nothing in the CRM.

---

## Step 0 — Locate this plugin

Everything below runs this plugin's scripts through a small shim at
`~/.leanscale-gtm/bin/system-map`. Create it before anything else — nothing later works without it.

`AGENT_ROOT` is this plugin's own directory: the one containing `scripts/`, `skills/` and
`.claude-plugin/`. Inside Claude Code, `${CLAUDE_PLUGIN_ROOT}` already holds it. On Cursor,
VS Code, Codex CLI or Gemini CLI that variable does not exist — use the directory you loaded
this SKILL.md from, two levels up from `skills/setup/`.

If the agents were installed with `tools/install-skills.py` (the non-plugin path), this is
already done — skip to the confirmation below. Otherwise:

```bash
AGENT_ROOT="${CLAUDE_PLUGIN_ROOT:-<the directory this plugin was loaded from>}"
python3 "$AGENT_ROOT/scripts/lib/config.py" install-shim --plugin system-map --root "$AGENT_ROOT"
```

It verifies the directory really is a plugin root, records it in
`~/.leanscale-gtm/system-map.json`, and writes the shim. If it answers *"does not look like a
plugin root"*, the path is wrong — fix it now rather than debugging a later step.

Confirm it works before continuing:

```bash
"$HOME/.leanscale-gtm/bin/system-map" --root
```

Re-running this is safe, and is the first thing to try if a run later fails with a missing
script — a plugin update moves the install and the recorded path goes stale.

---

## Step 1 — Probe the connectors

Required capabilities: `crm.describe`, `crm.query`, `crm.metadata`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("run_soql_query salesforce")       -> crm.query   (Salesforce)
    ToolSearch("hubspot crm search objects")      -> crm.query   (HubSpot)
    ToolSearch("describe metadata object schema") -> crm.describe
    ToolSearch("retrieve metadata package")       -> metadata retrieve (Salesforce)

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    crm.describe  salesforce  run_soql_query over EntityDefinition / FieldDefinition (useToolingApi where noted)
                  hubspot     hubspot-list-properties
    crm.query     salesforce  run_soql_query
                  hubspot     hubspot-search-objects / hubspot-list-objects / hubspot-batch-read-objects
    crm.metadata  salesforce  the server's metadata retrieve tool (Flow, WorkflowRule, ApexTrigger, ValidationRule)
                  hubspot     no equivalent — HubSpot exposes no automation metadata API

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


Report exactly which resolved tool provides which capability. If neither CRM resolves, stop
here and tell the user which MCP server to connect — everything downstream is pointless.

---

## Step 2 — Probe the metadata surfaces, and report that FIRST

This is the part that makes this plugin trustworthy. Before any interview, find out what the
connected identity can actually see, one surface at a time. Each probe is a `LIMIT 1` query or a
single GET, so the whole sweep is cheap.

### Salesforce probe set

| # | Probe | Surface | If it fails, the fix is |
|---|---|---|---|
| 1 | `SELECT Id, IsSandbox FROM Organization LIMIT 1` | Org / sandbox flag | API Enabled on the identity |
| 2 | `SELECT Id FROM User LIMIT 1` | Users | Read on User |
| 3 | `SELECT Id FROM LoginHistory LIMIT 1` | Login history | 'Manage Users' or 'View Setup and Configuration' |
| 4 | `SELECT Id FROM OauthToken LIMIT 1` | OAuth grants | 'Manage Users' — without it you see only your own tokens |
| 5 | `SELECT Id FROM PermissionSetAssignment LIMIT 1` | Permission assignments | 'View Setup and Configuration' |
| 6 | `SELECT Id FROM ObjectPermissions LIMIT 1` | Object write permissions | 'View Setup and Configuration' |
| 7 | `SELECT Id FROM FlowDefinitionView LIMIT 1` | Flows (standard API) | 'View Setup and Configuration' or 'Manage Flow' |
| 8 | `SELECT Id FROM FlowVersionView LIMIT 1` | Flow versions | same as 7 |
| 9 | `SELECT Id FROM AssignmentRule LIMIT 1` | Assignment rules | Read access |
| 10 | `SELECT Id FROM CronTrigger LIMIT 1` | Scheduled jobs | 'View Setup and Configuration' |
| 11 | `SELECT QualifiedApiName FROM EntityDefinition LIMIT 1` | Object inventory | API Enabled |
| 12 | `SELECT Id FROM SetupAuditTrail LIMIT 1` | Setup audit trail | 'View Setup and Configuration' |
| 13 | `SELECT Id FROM ApexTrigger LIMIT 1` | **Tooling gate** | see below |
| 14 | `SELECT Id FROM ConnectedApplication LIMIT 1` | Connected apps (Tooling) | 'Customize Application' + 'Manage Connected Apps' |
| 15 | `SELECT Id FROM InstalledSubscriberPackage LIMIT 1` | Packages (Tooling) | 'Download AppExchange Packages' |
| 16 | `SELECT Id FROM ValidationRule LIMIT 1` | Validation rules (Tooling) | 'View Setup and Configuration' |
| 17 | `SELECT Id FROM WorkflowRule LIMIT 1` | Workflow rules (Tooling) | 'View Setup and Configuration' |
| 18 | `SELECT Id, Metadata FROM Flow WHERE Id = '<one active flow id>'` | **Field-write detection** | 'Manage Flow' + Tooling routing |
| 19 | `SELECT Id FROM ApiEvent LIMIT 1` | API volume by consumer | Shield Event Monitoring (paid) |

**Read probe 13 carefully — it is the single most informative result in this skill.**

- `INVALID_TYPE: sObject type 'ApexTrigger' is not supported` means the query tool is bound to
  the **standard** endpoint. That is a routing limitation, not a permission problem, and no
  amount of granting will fix it. Say so plainly and go looking for a tooling-capable tool, a
  metadata-retrieve tool, or an authenticated `sf` CLI.
- `INSUFFICIENT_ACCESS` means the routing is fine and the permission is missing. Name it.

Probe 18 deserves its own note: `Flow.Metadata` is only selectable when the query filters to a
single Id. If a batched query fails but a single-Id one works, that is expected — the run loops.

### HubSpot probe set

| # | Probe | Surface | Scope needed |
|---|---|---|---|
| 1 | `GET /account-info/v3/details` | Portal / sandbox flag | any valid token |
| 2 | `GET /settings/v3/users?limit=1` | Users | `settings.users.read` |
| 3 | `GET /crm/v3/owners?limit=1&archived=true` | Archived owners | `crm.objects.owners.read` |
| 4 | `GET /automation/v4/flows?limit=1` | Workflows | `automation` — and Operations/Marketing Hub Pro+ |
| 5 | `GET /crm/v3/properties/contacts` | Property fingerprints | `crm.schemas.contacts.read` |
| 6 | `GET /crm/v3/schemas` | Custom objects | `crm.schemas.custom.read` (Enterprise) |
| 7 | `POST /crm/v3/objects/deals/search` `{"limit":1}` | Record counts | `crm.objects.deals.read` |
| 8 | `GET /account-info/v3/api-usage/daily` | API usage | any valid token |
| 9 | `GET /account-info/v3/activity/login` | Login activity | `account-info.security.read` (Enterprise) |

Probe 4 returning 403 on a Starter portal is not a permission you can grant — the feature does
not exist at that tier. Say that instead of sending someone to look for a checkbox.

**Connected apps on HubSpot have no API at all.** Tell the user now, during setup, not at the
end of a run: to get app-orphan detection they must export Settings → Integrations → Connected
Apps and → Private Apps by hand into `raw/hs_connected_apps.json`. Offer to walk them through it.

**Report the probe results before asking anything.** A short table: surface, readable yes/no,
and the fix. Then say in one sentence what will and will not work.

---

## Step 3 — Read the shared profile

Read `~/.leanscale-gtm/profile.json`.

- **Present:** show what is already known — org name, CRM, fiscal year start, quota-carrying
  reps — and ask only for confirmation. Do not re-interrogate someone who has already set up
  another agent in this suite.
- **Absent:** create it. Pull what the CRM can tell you first:
  - Salesforce: `SELECT Name, FiscalYearStartMonth, IsSandbox, OrganizationType FROM Organization`,
    and `SELECT COUNT(Id) FROM User WHERE IsActive = true AND UserType = 'Standard'`.
  - HubSpot: `GET /account-info/v3/details` for currency, time zone and account type;
    `GET /crm/v3/owners` for the owner count.

  Then ask only what the CRM genuinely cannot answer:
  - **`quota_carrying_reps`** — ask directly, and say why: it is the most load-bearing number in
    the suite and a ratio computed against total headcount is embarrassing. Offer the active
    standard-user count as context, not as the answer.
  - **`fiscal_year_start_month`** — read it, then confirm; never assume January.
  - **`redact_pii_in_reports`** — this plugin names people (who last edited a flow, who
    installed an app, whose service account is unowned). Ask whether the report may name them.
  - `org_name`, `currency`, `segments`, `material_deal_floor` — fill from the CRM where you can.

Write it with the shape in the suite spec and `schema_version: 1`. Show the user the file.

---

## Step 4 — Automatic discovery

Now go wide on the surfaces that passed. Show real numbers — this is what makes the interview
feel informed rather than interrogative.

1. **Objects and volume.** Object list plus record counts. Flag any object over the
   `object_surface_min_records` floor.
2. **Automation census.** Counts by type: active/inactive flows, Process Builder processes
   (`ProcessType = 'Workflow'`), Apex triggers, validation rules, workflow rules, assignment
   rules, scheduled jobs. On HubSpot: enabled vs disabled workflows by object.
3. **Objects carrying the most automation.** The top five. This is where conflicts will be.
4. **Integration identities.** Salesforce: users on an Integration licence or an API-only
   profile, plus anything matching `svc_`, `api_`, `integration`, `sync`, `connector`,
   `noreply`. HubSpot: private apps, plus seats named like service accounts.
5. **Packages and namespaces.** Every installed managed package and its namespace prefix — the
   strongest tool-detection signal there is.
6. **Departed editors.** Distinct `LastModifiedBy` names on active automation, cross-referenced
   against `IsActive = false` users (HubSpot: archived owners). Have the count ready; it
   previews the orphan finding.

Show a compact summary. Something like: *"41 flows (32 active), 6 of them last edited by
someone whose account is inactive. 5 Apex triggers across 3 objects, two of them on Lead. 9
managed packages. 6 integration identities, 5 with write access."*

---

## Step 5 — The interview

Ask these, informed by Step 4. Every question is one the CRM genuinely cannot answer.

1. **Production or sandbox?**
   *"`Organization.IsSandbox` says this is production and it is named Acme Production. Confirm —
   findings from a sandbox are worthless in a production conversation, and I label the report
   either way."*

2. **Managed packages — include them?**
   *"I found 9 installed packages. Including them gives the most reliable tool detection, since a
   namespace prefix is registered and unique. Any reason to exclude them?"*

3. **Integration users — include them?**
   *"6 identities look like service accounts, and 5 of them can write. This is the section a
   security review asks for. Include?"*

4. **Connected apps and OAuth grants — include them?**
   Salesforce: *"I can read 10 connected apps and their last-use dates. Include?"*
   HubSpot: *"There is no API for this. If you export Connected Apps and Private Apps by hand I
   can find the orphans; otherwise that section stays blank and the report will say it is blind
   rather than clean. Which do you want?"*

5. **Automation — include it?** (Flows, Process Builder, workflow rules, Apex triggers,
   validation rules, assignment rules.)
   *"This is where the field-conflict detection comes from — the same field written by two
   automations. Turning it off removes the most valuable finding, so I would keep it on."*

6. **Scheduled jobs — include them?**
   *"6 scheduled jobs. These are the only ones with a true last-fired timestamp, so they are
   the most reliable dormancy signal available."*

7. **Dormancy threshold, in days.**
   *"Default 90 — one full quarter, so seasonal processes don't get flagged. Your oldest active
   automation was last touched 641 days ago and your median is 118, so 90 will flag about 26 of
   them. Want 30, 90 or 180?"* Compute those numbers first; do not offer a bare default.

8. **Flag orphaned automation?**
   *"I found 6 active automations last edited by someone whose account is now inactive. Flag
   these? Say no only if your platform deactivates leavers in a way that makes it noisy."*

9. **Which tools do you believe are connected to this CRM?** — **the important one.**

   Ask this **before** showing them any detection results. The whole point is to capture the
   belief and then measure against it; showing your answer first destroys the finding.

   *"Off the top of your head, which tools are connected to this CRM right now? Do not check
   anything — I want the list your team would give in a meeting. I will compare it against what
   the instance actually shows, and the gap is usually the most interesting part of the report."*

   Prompt by category if they stall — conversation intelligence, sales engagement, enrichment,
   forecasting, routing and scheduling, marketing automation, CPQ or billing, e-signature,
   customer success, support, iPaaS or reverse ETL, BI. Record verbatim in `believed_tools`.

10. **Anything to exclude?** Fields that are deliberately written by several systems
    (`conflict_ignore_fields`), vendor-managed automation you cannot change
    (`ignore_automation_names`), namespaces to skip (`ignore_namespaces`), and any extra naming
    convention that marks a service account here (`extra_integration_user_patterns`).

---

## Step 6 — Write the config

Copy the plugin's `config.example.json` to `~/.leanscale-gtm/system-map.json` and fill
in the answers. Keep every `_<key>_help` line — customers edit this file by hand.

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/system-map" --root)"
mkdir -p ~/.leanscale-gtm
cp "$AGENT_ROOT/config.example.json" ~/.leanscale-gtm/system-map.json
```

Then edit it with the interview answers and **show the user the finished file**. Point out
`believed_tools` explicitly and confirm it is their answer, not the shipped sample.

---

## Step 7 — Smoke test

A setup that ends without proving output is not done. Run the real pipeline against one object.

1. Pick the object carrying the most automation (usually Opportunity, or Deal on HubSpot).
2. Pull the real inventory for just that object and write it to a temporary run directory:

```bash
SMOKE="./gtm-agents/system-map/smoke-$(date +%Y-%m-%d-%H%M)"
mkdir -p "$SMOKE/raw"
```

Salesforce, scoped to one object:
```sql
SELECT ApiName, Label, ProcessType, TriggerType, IsActive, VersionNumber,
       LastModifiedDate, LastModifiedBy.Name
FROM FlowDefinitionView
WHERE TriggerObjectOrEvent.QualifiedApiName = 'Opportunity'
```
```sql
SELECT Name, TableEnumOrId, Status, LastModifiedBy.Name FROM ApexTrigger
WHERE TableEnumOrId = 'Opportunity'                                   -- Tooling
```
HubSpot, scoped to deals: `GET /automation/v4/flows?limit=100`, keep `objectTypeId == "0-3"`.

3. Write `_sources.json` for the slice (same rules as the run skill), then:

```bash
"$HOME/.leanscale-gtm/bin/system-map" analyze --run-dir "$SMOKE"
"$HOME/.leanscale-gtm/bin/system-map" report  --run-dir "$SMOKE"
```

4. **Show a genuine finding, in their own words.** Not "the pipeline ran." Something like:

   > *"On Opportunity alone: `Forecast_Category__c` is written by four active automations —
   > a before-save flow, a workflow field update, and two after-save flows. The two after-save
   > flows are at the same point in the order of execution and neither has trigger ordering set,
   > so Salesforce does not define which one wins. That is almost certainly why that field
   > argues with itself."*

If the smoke test finds nothing on that object, say so and name the next object to try — do not
present an empty result as a pass.

### If the scripts fail
- `No ..._sources.json found` — you did not write it. It is mandatory by design.
- `Run aborted — a required data source returned zero records` — the automation surfaces were
  all unreadable. The abort message lists each one and its permission. That is the correct
  behaviour, not a bug.
- `No org profile at ...` — Step 3 did not complete.

---

## Step 8 — Pass/fail table

Close with this table, filled in from the Step 2 probes. One row per metadata surface. No
summarising, no omitting the rows that failed — the failures are the point.

| Metadata surface | Object / endpoint | Readable | Permission needed if not |
|---|---|---|---|
| Org / sandbox flag | `Organization` | PASS | — |
| Users and integration users | `User` | PASS | — |
| Login history | `LoginHistory` | PASS | — |
| OAuth grants and last use | `OauthToken` | PASS | — |
| Permission set assignments | `PermissionSetAssignment` | PASS | — |
| Object write permissions | `ObjectPermissions` | PASS | — |
| Flows and Process Builder | `FlowDefinitionView` | PASS | — |
| Flow versions | `FlowVersionView` | PASS | — |
| Flow field writes | `Flow.Metadata` (Tooling) | **FAIL** | 'Manage Flow' + a Tooling-capable query route |
| Apex triggers | `ApexTrigger` (Tooling) | **FAIL** | 'Author Apex', via the Tooling API |
| Apex classes | `ApexClass` (Tooling) | **FAIL** | 'Author Apex', via the Tooling API |
| Validation rules | `ValidationRule` (Tooling) | **FAIL** | 'View Setup and Configuration', via Tooling |
| Workflow rules | `WorkflowRule` (Tooling) | **FAIL** | 'View Setup and Configuration', via Tooling |
| Workflow field updates | `WorkflowFieldUpdate` (Tooling) | **FAIL** | 'View Setup and Configuration', via Tooling |
| Connected apps | `ConnectedApplication` (Tooling) | **FAIL** | 'Customize Application' + 'Manage Connected Apps' |
| Installed packages | `InstalledSubscriberPackage` (Tooling) | **FAIL** | 'Download AppExchange Packages' |
| Assignment rules | `AssignmentRule` | PASS | — |
| Scheduled jobs | `CronTrigger` | PASS | — |
| Object inventory | `EntityDefinition` | PASS | — |
| Record counts | `/limits/recordCount` | PASS | — |
| Field inventory | `FieldDefinition` | PASS | — |
| Setup audit trail | `SetupAuditTrail` | PASS | — |
| API volume by consumer | `ApiEvent` | **FAIL** | Shield Event Monitoring (paid add-on) |
| Flow execution counts | `EventLogFile` / FlowExecution | **FAIL** | Shield Event Monitoring (paid add-on) |

*(The PASS/FAIL values above are illustrative. Fill in the real ones.)*

Use the HubSpot surface list from Step 2 when the CRM is HubSpot, and always include the
`hubspot_connected_apps` row with "no public API — manual export required" in the fix column.

Then state, in plain English:

- **What will work.** e.g. *"Full automation inventory, orphan detection, dormancy, integration
  users, the stack map, and object surface."*
- **What will not, and why.** e.g. *"Field-write conflict detection needs `Flow.Metadata`
  through the Tooling API. Right now your query tool only reaches the standard endpoint, so I
  can list your flows but not see which fields they write — and that is the single most
  valuable finding here."*
- **The shortest path to fixing each gap.** For Salesforce that is usually one permission set
  on the connected identity: **View Setup and Configuration**, **API Enabled**, and read
  access — never Modify All Data, which this plugin does not need and should not have.
- **What to do next:** `/system-map:run`.
