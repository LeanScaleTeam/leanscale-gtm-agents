---
name: run
description: >-
  Inventory everything wired into the CRM — integration users, connected apps, OAuth grants,
  managed packages, flows, workflow rules, Apex triggers, validation rules and scheduled jobs —
  then find the orphans, the fields two automations fight over, and the tools nobody named.
  Read-only: it reads metadata and changes nothing. Trigger on "/system-map:run", "run the
  system map", "audit our automation", "what's connected to our CRM", "what integrations do we
  have", "who owns this flow", "why did this field change", "find orphaned automation",
  "which automations write the same field", "map our stack", "we're doing a security review of
  Salesforce", or any request to inventory CRM automation, integrations or connected apps.
argument-hint: "[--dormancy 90] [--sandbox] [--skip-metadata]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# System Map — run

You are producing an inventory of everything actually wired into this customer's CRM, and
contrasting it with what their team believes is wired in. The gap is the deliverable.

**This run is read-only.** You issue SELECT-shaped queries and GET requests against metadata.
You never create, update, delete or deploy anything. If a tool you resolve can write, do not
use it.

Three layers, in order. Do not skip and do not merge them.

```
YOU (this skill)      call MCP tools  ->  write ./gtm-agents/system-map/<stamp>/raw/*.json
scripts/analyze.py    raw/*.json      ->  findings.json + manifest.json    (offline, stdlib)
scripts/report.py     findings.json   ->  report.md + report.html + baseline delta
```

---

## 0. Preflight

1. Read `~/.leanscale-gtm/system-map.json`. If it is missing, stop and tell the user to run
   `/system-map:setup` first — the run needs `believed_tools` (captured before measurement) and
   the dormancy threshold, and neither can be invented after the fact.
2. Read `~/.leanscale-gtm/profile.json` for `org_name`, `crm.system` and
   `redact_pii_in_reports`.
3. Create the run directory:

```bash
RUN_DIR="./gtm-agents/system-map/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN_DIR/raw"
```

4. **Confirm production vs sandbox out loud before fetching.** Findings from a sandbox are
   worthless in a production conversation and vice versa. Salesforce reports it as
   `Organization.IsSandbox`; HubSpot as `accountType` on `/account-info/v3/details`
   (`STANDARD` / `SANDBOX` / `DEVELOPER_TEST`). If it disagrees with the configured
   `environment`, say so and ask which to trust before continuing.

---

## 1. Resolve the tools you have

Do not assume tool names — customers connect their own MCP servers.

```
ToolSearch("run_soql_query salesforce")          -> crm.query (Salesforce)
ToolSearch("hubspot crm search objects")          -> crm.query (HubSpot)
ToolSearch("describe metadata object schema")     -> crm.describe
ToolSearch("retrieve metadata package")           -> metadata retrieve (Salesforce)
```

Then run the **Tooling probe**, which is the single most important thing this skill does:

```sql
SELECT Id FROM FlowDefinitionView LIMIT 1        -- standard API; usually works
SELECT Id FROM ApexTrigger LIMIT 1               -- TOOLING API only
```

- If both succeed → the resolved query tool reaches the Tooling API. Full inventory available.
- If the first succeeds and the second fails with `sObject type 'ApexTrigger' is not supported`
  → the tool is bound to the **standard** query endpoint. This is common and is not a
  permission problem. Use the fallback ladder in §2.
- If both fail → this is a permission problem. Record it and degrade; do not abandon the run.

**Never report "Tooling API not available" and stop.** Report which specific objects were
unreachable, by which route, and what would fix each.

### Fallback ladder when the Tooling API is out of reach

1. **A second query tool with a tooling flag.** Some servers expose `use_tooling_api` or a
   separate tool. `ToolSearch("tooling api query")`.
2. **A metadata retrieve tool.** `retrieve_metadata` with types `Flow`, `WorkflowRule`,
   `ApexTrigger`, `ValidationRule` returns the same definitions as XML. Parse and write the
   equivalent JSON shape into `raw/`.
3. **The Salesforce CLI, if the user already has it authenticated.** Ask first, then:
   ```bash
   sf data query --use-tooling-api --target-org <alias> --json \
     --query "SELECT Id, Name, TableEnumOrId, Status, Body FROM ApexTrigger" \
     > "$RUN_DIR/raw/sf_apex_triggers.json"
   ```
4. **Standard-API substitutes**, which cover more than people expect:
   | Tooling-only object | Standard-API substitute | What you lose |
   |---|---|---|
   | `Flow` | `FlowDefinitionView` + `FlowVersionView` | the metadata body, so no field-write detection |
   | `InstalledSubscriberPackage` | `PackageLicense` | packages with no licence rows |
   | `ConnectedApplication` | `OauthToken` (app name + last use) | install date and installer |
   | `ValidationRule` / `WorkflowRule` | nothing | record them as unavailable |
5. **Give up on that surface, loudly.** Set `ok: false` in `_sources.json` with the verbatim
   error. The report turns it into an "unavailable, not clean" line with the exact permission.

---

## 2. Fetch — Salesforce

Write each response to `raw/<file>.json` exactly as returned. Do not reshape, filter or
pretty-summarise: `analyze.py` handles `{"records": [...]}`, `{"results": [...]}`,
`{"sObjects": [...]}` and bare lists.

> Adjust the API version to the org's current one. `v62.0` below is illustrative.

### 2.1 Org and environment → `sf_org.json`
```sql
SELECT Id, Name, IsSandbox, InstanceName, OrganizationType, FiscalYearStartMonth,
       TrialExpirationDate, LanguageLocaleKey, CreatedDate
FROM Organization
```

### 2.2 Users → `sf_users.json` *(required source)*
```sql
SELECT Id, Name, Username, Email, IsActive, UserType, ProfileId,
       Profile.Name, Profile.UserLicense.Name,
       ManagerId, Manager.Name, Manager.IsActive,
       CreatedById, CreatedBy.Name, CreatedBy.IsActive,
       LastLoginDate, CreatedDate
FROM User
ORDER BY IsActive DESC, Name
```
`Manager` then `CreatedBy` is how the run establishes who *owns* a service account. When both
are inactive the credential is unowned — that is the critical finding, so pull both.

### 2.3 Login history → `sf_login_history.json`
```sql
SELECT UserId, LoginTime, Application, Status, SourceIp
FROM LoginHistory
WHERE LoginTime = LAST_N_DAYS:90
ORDER BY LoginTime DESC
```
`Application` is the closest thing to per-consumer API volume without Event Monitoring. Say so
rather than implying it is a call count.

### 2.4 OAuth grants → `sf_oauth_tokens.json`
```sql
SELECT Id, AppName, UserId, User.Name, LastUsedDate, UseCount
FROM OauthToken
ORDER BY LastUsedDate NULLS FIRST
```
Without **Manage Users** this returns only the running identity's own tokens, which looks
identical to a clean org. If the row count is suspiciously small, say so explicitly.

### 2.5 Permissions → `sf_permset_assignments.json`, `sf_object_permissions.json`
```sql
SELECT AssigneeId, PermissionSetId, PermissionSet.Name,
       PermissionSet.PermissionsModifyAllData, PermissionSet.PermissionsViewAllData,
       PermissionSet.PermissionsApiEnabled, PermissionSet.IsOwnedByProfile
FROM PermissionSetAssignment
```
```sql
SELECT ParentId, SobjectType, PermissionsCreate, PermissionsEdit,
       PermissionsDelete, PermissionsModifyAllRecords
FROM ObjectPermissions
WHERE PermissionsEdit = true
```

### 2.6 Connected apps and packages *(Tooling)* → `sf_connected_apps.json`, `sf_installed_packages.json`
```sql
SELECT Id, Name, CreatedDate, CreatedBy.Name, LastModifiedDate, LastModifiedBy.Name,
       OptionsAllowAdminApprovedUsersOnly
FROM ConnectedApplication
```
```sql
SELECT Id, SubscriberPackage.Name, SubscriberPackage.NamespacePrefix,
       SubscriberPackage.PublisherName, SubscriberPackageVersion.Name,
       SubscriberPackageVersion.MajorVersion, SubscriberPackageVersion.MinorVersion
FROM InstalledSubscriberPackage
```
Standard-API fallback if Tooling is unreachable:
```sql
SELECT NamespacePrefix, Status, AllowedLicenses, UsedLicenses, ExpirationDate FROM PackageLicense
```

### 2.7 Flows → `sf_flows.json`, `sf_flow_versions.json` *(standard API — no Tooling needed)*
```sql
SELECT DurableId, ApiName, Label, ProcessType, TriggerType,
       TriggerObjectOrEvent.QualifiedApiName, IsActive, VersionNumber,
       LastModifiedDate, LastModifiedBy.Name, NamespacePrefix, IsOutOfDate, Description
FROM FlowDefinitionView
ORDER BY Label
```
```sql
SELECT DurableId, FlowDefinitionViewId, ApiName, Label, VersionNumber, Status, ProcessType
FROM FlowVersionView
```
`ProcessType = 'Workflow'` on `FlowDefinitionView` means **Process Builder**, not a flow. Both
of those views live on the standard API, which is why flow inventory usually survives even when
nothing else Tooling-shaped does.

### 2.8 Flow definitions — the field writes *(Tooling)* → `sf_flow_metadata.json`
This is what makes conflict detection possible.
```sql
SELECT Id, FullName, Metadata FROM Flow WHERE Id = '301xx0000000001'
```
**One Id per query.** The Tooling API refuses `Metadata` unless the filter narrows to a single
record; a batched `WHERE Id IN (...)` fails. Loop over the active flows from §2.7, collect the
responses into one array, and write:
```json
{"records": [ {"Id": "301...", "FullName": "Opportunity_Stage_Hygiene", "Metadata": { ... }}, ... ]}
```
If this is expensive, restrict it to active record-triggered flows — that is where conflicts
live. Note the restriction in `_sources.json`.

`analyze.py` reads field writes from `Metadata.recordUpdates[].inputAssignments[].field`,
`Metadata.recordCreates[].inputAssignments[].field`, and
`Metadata.assignments[].assignmentItems[].assignToReference` beginning `$Record.`. It also
reads `Metadata.triggerOrder` (or `Metadata.start.triggerOrder`) so the report can tell you
whether flow trigger ordering has been set.

### 2.9 Apex *(Tooling)* → `sf_apex_triggers.json`, `sf_apex_classes.json`
```sql
SELECT Id, Name, TableEnumOrId, Status, ApiVersion,
       UsageBeforeInsert, UsageBeforeUpdate, UsageAfterInsert, UsageAfterUpdate,
       UsageBeforeDelete, LengthWithoutComments, LastModifiedDate, LastModifiedBy.Name, Body
FROM ApexTrigger
```
```sql
SELECT Id, Name, NamespacePrefix, Status, ApiVersion, LengthWithoutComments,
       LastModifiedDate, LastModifiedBy.Name
FROM ApexClass
```
Keep `Body` — field writes are parsed out of it. That parse is a labelled heuristic: it finds
variables bound to `Trigger.new` and assignments onto them, so it misses writes made inside a
helper class. The report says so.

### 2.10 Validation, workflow and assignment rules
```sql
-- Tooling -> sf_validation_rules.json
SELECT Id, ValidationName, EntityDefinition.QualifiedApiName, Active, ErrorMessage,
       ErrorDisplayField, LastModifiedDate, LastModifiedBy.Name
FROM ValidationRule
```
```sql
-- Tooling -> sf_workflow_rules.json
SELECT Id, Name, TableEnumOrId, LastModifiedDate, LastModifiedBy.Name, Metadata FROM WorkflowRule
```
```sql
-- Tooling -> sf_workflow_field_updates.json   (this is where the field name lives)
SELECT Id, Name, TableEnumOrId, LastModifiedDate, Metadata FROM WorkflowFieldUpdate
```
```sql
-- standard -> sf_assignment_rules.json
SELECT Id, Name, SobjectType, Active, LastModifiedDate, LastModifiedBy.Name FROM AssignmentRule
```

### 2.11 Scheduled jobs → `sf_scheduled_jobs.json`
```sql
SELECT Id, CronJobDetail.Name, CronJobDetail.JobType, CronExpression, State,
       NextFireTime, PreviousFireTime, StartTime, TimesTriggered,
       CreatedDate, CreatedBy.Name, CreatedBy.IsActive
FROM CronTrigger
ORDER BY PreviousFireTime NULLS FIRST
```
`PreviousFireTime` is a **true** last-fired timestamp — the only one Salesforce gives you
without Event Monitoring. Everything else falls back to a labelled last-modified proxy.

### 2.12 Object and field surface
```sql
-- sf_entities.json
SELECT DurableId, QualifiedApiName, Label, KeyPrefix, IsCustomSetting, NamespacePrefix
FROM EntityDefinition
WHERE IsQueryable = true
```
```
GET /services/data/v62.0/limits/recordCount?sObjects=Account,Contact,Lead,Opportunity,Case,Task,Event,<custom objects>
-> sf_record_counts.json
```
```sql
-- sf_field_definitions.json  (must filter by entity; repeat per object)
SELECT QualifiedApiName, EntityDefinition.QualifiedApiName, NamespacePrefix, DataType
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
```
`EntityDefinition.DurableId` is what turns `TableEnumOrId` on a trigger or workflow rule back
into a real object name for custom objects — pull it or custom-object automation shows up
against a raw Id.

### 2.13 Recent changes → `sf_setup_audit_trail.json`
```sql
SELECT Id, Action, Section, CreatedDate, CreatedBy.Name, Display
FROM SetupAuditTrail
WHERE CreatedDate = LAST_N_DAYS:30
ORDER BY CreatedDate DESC
```

### 2.14 Optional, Shield only
```sql
SELECT Client, ApiType, COUNT(Id) FROM ApiEvent
WHERE EventDate = LAST_N_DAYS:30 GROUP BY Client, ApiType     -- sf_api_events.json
```
```sql
SELECT Id, EventType, LogDate, LogFileLength FROM EventLogFile
WHERE EventType = 'FlowExecution'                              -- sf_flow_executions.json
```
Most orgs do not have Event Monitoring. When these fail, that is expected — record
`ok: false` with the verbatim error and move on. Do not treat it as a run failure.

---

## 3. Fetch — HubSpot

Every call is a GET or a search POST. Use the resolved HubSpot MCP tool if it exposes generic
requests; otherwise ask the user for a private-app token and use `Bash` + `curl`, never echoing
the token into the transcript:

```bash
curl -sS -H "Authorization: Bearer $HUBSPOT_TOKEN" \
  "https://api.hubapi.com/automation/v4/flows?limit=100" > "$RUN_DIR/raw/hs_workflows.json"
```

| File | Call | Scope |
|---|---|---|
| `hs_account.json` | `GET /account-info/v3/details` | any valid token |
| `hs_users.json` *(required)* | `GET /settings/v3/users?limit=100` | `settings.users.read` |
| `hs_owners.json` | `GET /crm/v3/owners?limit=500&archived=false` **and** `&archived=true` | `crm.objects.owners.read` |
| `hs_workflows.json` | `GET /automation/v4/flows?limit=100` (page on `paging.next.after`) | `automation` |
| `hs_properties.json` | `GET /crm/v3/properties/contacts`, `/companies`, `/deals`, `/tickets` | `crm.schemas.*.read` |
| `hs_schemas.json` | `GET /crm/v3/schemas` | `crm.schemas.custom.read` |
| `hs_object_counts.json` | `POST /crm/v3/objects/{type}/search` `{"limit":1}` → read `total` | `crm.objects.{type}.read` |
| `hs_api_usage.json` | `GET /account-info/v3/api-usage/daily` | any valid token |
| `hs_login_activity.json` | `GET /account-info/v3/activity/login` | `account-info.security.read` |

**Merge the two owner calls** into one `{"results": [...]}` — archived owners are how the run
detects a departed automation editor and a departed app installer.

**Object counts.** Label each row with the same display name the automation inventory uses —
`Contact`, `Company`, `Deal`, `Ticket`, `LineItem`, and `<Label> (custom)` for custom objects —
so the object-surface comparison lines up:
```json
{"results": [{"objectType": "Contact", "total": 412884}, {"objectType": "Equipment (custom)", "total": 60533}]}
```

### The honest gap: connected apps
**HubSpot publishes no API that lists installed apps or private apps.** There is nothing to
grant. Two options, in order:

1. Ask the user to export it by hand — Settings → Integrations → **Connected Apps**, then
   Settings → Integrations → **Private Apps** — and save it as `hs_connected_apps.json`:
   ```json
   {"results": [
     {"name": "Warehouse Reverse Sync", "type": "private_app",
      "createdBy": "Jordan Wu", "installedBy": "Jordan Wu",
      "installedAt": "2025-03-11", "lastUsedAt": "2026-08-10", "callsLast30Days": 96204,
      "scopes": ["crm.objects.contacts.write", "crm.objects.deals.write"]}
   ]}
   ```
   `type` must be `private_app` or `marketplace_app` — private apps are HubSpot's real
   integration users, and that is how the run finds tokens whose owner has left.
2. If they will not, omit the file and set `ok: false` with
   `"error": "no public API; manual export not provided"`. The report will say the app-orphan
   section is blind, which is the truth.

### Field writes in HubSpot workflows
`/automation/v4/flows` returns each action inline. A set-property action looks like:
```json
{"actionId": "1", "type": "SINGLE_CONNECTION", "actionTypeId": "0-5",
 "fields": {"property_name": "hs_lead_status", "value": {"staticValue": "OPEN", "type": "STATIC_VALUE"}}}
```
`analyze.py` reads `fields.property_name` from those. `objectTypeId` maps `0-1` Contact,
`0-2` Company, `0-3` Deal, `0-5` Ticket. Dormancy uses `lastEnrollmentAt` when the payload
carries it and `updatedAt` as a labelled proxy otherwise — true enrollment recency only exists
in the workflow UI's performance tab, so do not claim more precision than you have.

---

## 4. Write `raw/_sources.json`

**This file is mandatory.** It is how the pipeline tells "clean" apart from "blind". Without it
`analyze.py` refuses to run, on purpose.

```json
{
  "crm": "salesforce",
  "environment": "production",
  "org_label": "Acme Production",
  "fetched_at": "2026-08-10T14:05:00Z",
  "sources": [
    {"name": "salesforce_flows", "file": "sf_flows.json", "tool": "run_soql_query",
     "ok": true, "required": false, "query": "SELECT ... FROM FlowDefinitionView"},
    {"name": "salesforce_apex_triggers", "file": "sf_apex_triggers.json", "tool": "run_soql_query",
     "ok": false, "required": false,
     "error": "sObject type 'ApexTrigger' is not supported",
     "query": "SELECT ... FROM ApexTrigger"}
  ]
}
```

Rules, and they matter:

- **`ok` records whether the CALL succeeded, not whether rows came back.** A 200 with an empty
  list is `ok: true` plus a `note` saying so. A 403 is `ok: false` with the verbatim error.
  Getting this backwards is how a permissions failure becomes a clean bill of health.
- Use the exact `name` values from the tables above — they key the built-in permission map, so
  the report can print the precise grant that fixes each gap.
- Set `required: true` on `salesforce_users` / `hubspot_users` only. `analyze.py` adds its own
  required gate: if the union of every automation surface is zero, the run aborts rather than
  emitting a confident empty report.
- Include the real query text. It ends up in the report as the customer's verification path.

---

## 5. Analyse and report

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" --run-dir "$RUN_DIR"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py"  --run-dir "$RUN_DIR"
```

`analyze.py` exits `3` if a required source came back empty. It prints the diagnosis and every
unreadable surface with its permission — relay that to the user and stop. Do not re-run with
`--allow-empty-automation` to make the error go away; that flag exists only for an instance the
user has personally confirmed has no automation.

Outputs land in `$RUN_DIR`: `findings.json`, `manifest.json`, `report.md`, `report.html`.
The baseline snapshot goes to `~/.leanscale-gtm/baselines/system-map/`. Run one says
"this is your baseline"; every later run shows the deltas.

**Reports are local files. Never upload, deploy or host one.**

---

## 6. Brief the user

Lead with the finding that changes behaviour, not with a count. Cover, in this order:

1. **Field-write conflicts.** Name the specific field, the automations, and whether the order
   is guaranteed. "`Opportunity.Forecast_Category__c` is written by four active automations and
   two of them are after-save flows, so Salesforce does not define which one wins" is the
   sentence that earns the install.
2. **Orphans.** Integration users whose owner has left, connected apps installed by someone
   inactive, automation last touched by a departed admin. Name the departed person — that is
   what makes it real.
3. **The stack gap.** What is connected that nobody named, and what they named that leaves no
   trace.
4. **Coverage.** Which surfaces you could not read and the exact permission for each. Say the
   words: *unavailable, not clean.*

Then: open `report.html`, and offer the two next steps that follow — pick the top contested
field and decide its owner, and reassign or revoke the unowned credentials.

Never say "your org is clean" when a section failed to load. Say "I could not see X; here is
the permission that would let me."
