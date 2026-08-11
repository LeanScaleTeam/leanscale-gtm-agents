# Setting up CRM Hygiene

Read-only. Nothing in this document, and nothing the plugin does, writes to your CRM.

Budget about fifteen minutes: ten for setup, five for the first run.

---

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents      # or the git URL you were given
/plugin install crm-hygiene@leanscale-gtm
```

If you were sent a zip, unzip it first and add the **local directory** — a bare URL to a
`marketplace.json` cannot resolve the relative plugin paths inside it.

Confirm it landed:

```
/plugin
```

You should see `crm-hygiene` with two skills, `run` and `setup`. Plugin skills are always
namespaced, so you invoke them as `/crm-hygiene:run` and `/crm-hygiene:setup` — there is no
bare `/crm-hygiene`.

## 2. Connect your CRM

You need one MCP server, already authenticated as a user who can **read** your CRM.

### Salesforce

The official Salesforce MCP server exposes `run_soql_query`, which is all this plugin needs —
records and metadata both come through SOQL. Authenticate with the Salesforce CLI first
(`sf org login web`), then confirm Claude can see the org:

> "List my Salesforce orgs and run `SELECT COUNT() FROM Account`."

**Permissions the connected user needs**

| Need | Why | If missing |
|---|---|---|
| Read on Account, Contact, Opportunity, Lead, User | the audit itself | required — nothing runs |
| Field-level read on the fields you audit | fill rates | fields you cannot see read as 0% filled, which is a false finding |
| `View Setup and Configuration` | Tooling API: validation rules, custom picklist value sets | those checks are marked unavailable |
| Read on `OpportunityContactRole` | contact-role coverage | that check is marked unavailable |

A dedicated read-only integration user is the cleanest setup, but watch the org-wide defaults:
if Opportunity is private, an integration user with no role sees **nothing**, and the run will
abort with a zero-record diagnosis rather than reporting a suspiciously clean org.

### HubSpot

Connect the HubSpot MCP server with a private app token. Required scopes:

```
crm.objects.companies.read
crm.objects.contacts.read
crm.objects.deals.read
crm.objects.owners.read
crm.schemas.companies.read
crm.schemas.contacts.read
crm.schemas.deals.read
```

Confirm:

> "List 5 HubSpot companies and fetch the deal properties."

Two HubSpot-specific things worth knowing before you start:

- **`GET /crm/v3/owners/?archived=true` is the deactivated-user list.** Without
  `crm.objects.owners.read`, every ownership check is blind.
- **HubSpot has no schema-level required flag.** "Required" only exists on a form or in a
  workflow. The plugin reports that as an explicit finding rather than pretending the check
  passed.

## 3. Run setup

```
/crm-hygiene:setup
```

It will:

1. probe the connected tools and print exactly which capability each one satisfies;
2. read your org — objects, record counts, custom-field inventory, fill rates, picklists,
   record types, active vs deactivated users, fiscal settings — and show you the summary;
3. ask about ten things the CRM genuinely cannot tell it, each phrased in terms of what it
   just found;
4. write `~/.leanscale-gtm/profile.json` and `~/.leanscale-gtm/crm-hygiene.json` and show you
   both files;
5. run the full pipeline against a small slice and quote a real finding back at you;
6. print a pass/fail table plus a plain-English list of what will and will not work.

**Have one answer ready before you start:** which fields your team is *told* are mandatory.
Not what the schema requires — what a manager would push back on if it were blank. Six or
fewer per object. The gap between that list and what the schema actually enforces is the
single most useful thing this plugin measures, and it is the one thing no CRM can tell it.

Setup is idempotent. Re-run it any time, and use `/crm-hygiene:setup --check` as a health
check when a run starts failing.

## 4. First run

```
/crm-hygiene:run
```

Open the `report.html` path it prints. One self-contained file; no network needed.

**Run one is a baseline, and the report says so.** Do not benchmark yourself against it —
there is nothing to compare it to yet. Fix two or three things, run it again in a month, and
the second report will show you the movement. That comparison is the product.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Run aborted — a required data source returned zero records` | This is the plugin working correctly. A connector broke, and a clean-looking empty report would be a lie. | Read the diagnosis it printed — it names the source and the likely cause. Usually a missing scope or a private org-wide default. |
| Salesforce returns 0 opportunities but the org has thousands | Private org-wide default and the integration user has no role or sharing rule | Give the user a role at the top of the hierarchy, or a "View All" permission on Opportunity |
| `INSUFFICIENT_ACCESS` on `ValidationRule` | Tooling API needs setup access | Grant `View Setup and Configuration`, then re-run `/crm-hygiene:setup --check`. Until then those findings show as unavailable, not clean. |
| `MALFORMED_QUERY` on an aggregate fill-rate batch | Long text area, rich text, multi-select picklist and encrypted fields cannot be aggregated in SOQL | Expected. Exclude those `DataType`s from the batch; the analyzer measures them from the sampled records instead |
| `FieldDefinition` query fails | It requires a bounded filter on `EntityDefinition` | Run one query per object rather than a multi-value `IN` |
| HubSpot 403 on `/crm/v3/properties/deals` | Missing `crm.schemas.deals.read` | Add the scope to the private app and reconnect |
| Every HubSpot policy-required field reads as "not found" | You used Salesforce API names. HubSpot property names are lower_snake_case internal names (`next_step`, not `NextStep`) | Fix `policy_required_fields`; see `fixtures/config.hubspot.json` for a worked example |
| A HubSpot count is exactly `10000` | The search API caps results at 10,000 | Slice by `createdate` and sum. The report labels truncated counts as a floor if `truncated` was set |
| Report says a field is 0% filled and you know it is used | Field-level security hides it from the connected user, or the field is a long text area measured from a small sample | Check FLS for the integration user's profile; check the finding's `basis` line for exact vs sampled |
| Duplicate clusters full of legitimate subsidiaries | Parent/child links are missing, so the hierarchy exclusion cannot fire | Populate the parent-account field, or add the domain to `ignore_domains` |
| Too many findings to take to a meeting | It is an audit of a four-year-old org | Raise `min_finding_count`; move triaged fields into `known_dead_fields` |
| `No org profile at ~/.leanscale-gtm/profile.json` | Setup has not run | `/crm-hygiene:setup`. The profile is shared across every LeanScale GTM agent, so you only do it once. |
| Deltas look wrong after a smoke test | A truncated slice was banked as a baseline | Delete the offending snapshot from `~/.leanscale-gtm/baselines/crm-hygiene/`; always pass `--no-baseline` on partial runs |

### Verifying the machinery without touching your CRM

```bash
python3 scripts/analyze.py --raw fixtures/raw --out /tmp/x
python3 scripts/report.py  --findings /tmp/x/findings.json --out /tmp/x
```

Then open `/tmp/x/report.html`. Fixture runs print a `FIXTURE MODE` banner and never write a
baseline. A HubSpot-shaped fixture is in `fixtures/raw-hubspot` — pass
`--config fixtures/config.hubspot.json` with it.

### What the plugin will never do

Merge records. Update a field. Delete anything. Deploy metadata. Upload a report. Run on a
schedule without you. There is no flag for any of it, because there is no code for any of it.
