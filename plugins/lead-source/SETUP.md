# Setting up Lead Source of Truth

Twenty minutes, once. Read-only throughout — nothing in this process writes to your CRM.

---

## 1. What you need before you start

| Requirement | Detail |
|---|---|
| Claude Code | With this plugin's marketplace added |
| A CRM connector | A Salesforce or HubSpot MCP server, already connected and authenticated |
| Read access | To Lead/Contact/Opportunity (Salesforce) or Contacts/Deals (HubSpot), plus field metadata |
| Python 3.9+ | Almost certainly already present; `python3 --version` to check |
| Your channel list | The one on your board deck, not the one in the picklist. Have it to hand. |

You do **not** need Gong, a marketing automation platform, an ad account, or a data warehouse.
That is deliberate: this plugin installs against one connector so it can be running the same
afternoon you decide to try it.

### Permissions, precisely

**Salesforce** — the connected identity needs:

- Read on `Lead`, `Contact`, `Opportunity`
- Field-level read on the source, UTM and campaign fields (a field the integration user cannot
  see reads as blank, which would look exactly like a finding and would not be one)
- **View Setup and Configuration** — required for `FieldDefinition` and `PicklistValueInfo`.
  Without it, free-text pollution cannot be separated from legitimate values.
- Read on `LeadHistory`, and **field-history tracking switched on** for the source field. If it
  is off, no history exists to read, and overwrite detection is skipped rather than faked.

**HubSpot** — the private app token needs:

- `crm.objects.contacts.read`
- `crm.objects.deals.read`
- `crm.schemas.contacts.read`, `crm.schemas.deals.read`
- `crm.associations.read` (for the contact → deal hop)

---

## 2. Install

```
/plugin marketplace add ./leanscale-gtm-agents
/plugin install lead-source@leanscale-gtm
```

Or from a downloaded zip: unzip it, then
`/plugin marketplace add /path/to/leanscale-gtm-agents` and install as above.

Verify the two skills are registered:

```
/lead-source:setup
/lead-source:run
```

---

## 3. Run setup

```
/lead-source:setup
```

It runs in this order:

1. **Probes** your connectors and reports which real tool provides each capability.
2. **Reads `~/.leanscale-gtm/profile.json`** if another agent in this suite already created it,
   and confirms what is known rather than re-asking. If it does not exist, it creates it — this
   is the shared org profile, so you only answer these questions once across the whole suite.
3. **Discovers, before asking anything**: every field whose name or label smells like
   source/channel/campaign/medium/UTM, their picklist values, their fill rates over the last 540
   days, how many records were changed after creation, and who or what creates records.
4. **Asks the handful of things your CRM cannot answer** — see below.
5. **Writes `~/.leanscale-gtm/lead-source.json`** and shows it to you.
6. **Smoke tests** against a 90-day slice and shows you a real number from your own data.
7. **Prints a pass/fail table** with a plain-English fix for every gap.

### The questions it will ask, and why each one matters

| Question | Why it cannot be discovered |
|---|---|
| Which source field is the channel report actually built on? | Several fields will be populated. Only you know which one is on the slide. |
| Is each field meant to be first touch or last touch? | Intent lives in people's heads. The run then measures whether the field behaves that way, and the gap is the finding. |
| Are UTMs captured, where do they land, and are they write-once? | The fields are discoverable; whether they are overwritten on a second form fill is not, without history. |
| What channel taxonomy do you *think* you have? | This is a belief, and the report's value is the distance between the belief and the data. |
| Is source supposed to survive Lead → Opportunity conversion? | Whether it does is measurable. Whether it was meant to is not. |
| Is there a self-reported source field, and do you report on it? | Discoverable as a field; whether anyone trusts it is not. |
| Which values mean "we do not know"? | `Other` and `Unknown` are built in. `Not Provided`, `Legacy`, `House Account` are yours. |
| Which creating accounts are forms vs imports vs integrations vs people? | The account names are discoverable. What they *are* is institutional knowledge. |

Answer the taxonomy question honestly. If you cannot produce the list in two minutes, say so —
that is a genuine finding and the run handles the empty case cleanly rather than inventing one.

---

## 4. Run the audit

```
/lead-source:run
```

Output lands in `./gtm-agents/lead-source/<YYYY-MM-DD-HHMM>/`. Open `report.html` first.

**Run one is your baseline.** It says so. The comparison starts on run two, and every run after
the first shows what moved on each score and each finding. Monthly is a sensible cadence; weekly
is noise unless you are mid-remediation.

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/lead-source:run` says config is missing | Setup has not run, or `~/.leanscale-gtm/` was cleared | Run `/lead-source:setup`. It is idempotent. |
| `analyze.py` exits 3, "a required data source returned zero records" | Auth expired, object permission missing, or the window excludes everything | Read the diagnosis it printed. Re-run `/lead-source:setup` — it doubles as the health check. |
| `FieldDefinition` query rejected | Integration user lacks **View Setup and Configuration** | A permission-set change, not a licence change. |
| `PicklistValueInfo` rejected | The query is missing its required `EntityParticleId` filter, or the org does not expose it | Use `crm.describe` on Lead instead, or omit `field_definitions_file` — the run degrades cleanly. |
| `LeadHistory` returns 0 rows | Field-history tracking was never enabled on the source field | Setup → Object Manager → Lead → Fields → Set History Tracking. It starts collecting today; it cannot recover the past. Meanwhile the stability component drops out of the score and the report says so. |
| HubSpot search returns 403 | Private-app scope missing | Add the scopes listed in section 1 and re-issue the token. |
| HubSpot history is empty | The Search API does not return property history | History comes from `POST /crm/v3/objects/contacts/batch/read` with `propertiesWithHistory`. The run skill does this; leave `history_file` empty in the config. |
| Survival rate reads 100% | The downstream source field is a formula or rollup from the lead | Check the field definition before celebrating. Inherited is not the same as preserved. |
| Every record has the same UTM | One hardcoded UTM on a site-wide template | A web fix, not a CRM fix. |
| Findings feel too aggressive | Thresholds are tuned for "the board deck must be right" | Edit `thresholds` in `~/.leanscale-gtm/lead-source.json`. The underlying numbers do not change — only where a severity band starts. |
| Duplicate proposals look wrong | Clustering is deliberately conservative but not omniscient | Raise `similarity_threshold` towards 0.95, or add your vocabulary to `extra_synonym_groups`. Every proposal needs human confirmation by design. |
| Report is enormous | Large orgs generate long evidence tables | Lower `max_evidence_rows`. The complete data always stays in `findings.json`. |

---

## 6. Where things live

```
~/.leanscale-gtm/
    profile.json                     shared across every agent in this suite
    lead-source.json                 this plugin's settings — edit by hand freely
    baselines/lead-source/*.json     one snapshot per run, never pruned

./gtm-agents/lead-source/<date>/
    raw/                             unmodified CRM responses
    findings.json                    findings + full taxonomy mapping proposal
    report.md · report.html          the readable outputs
    manifest.json                    provenance and record counts
```

Config deliberately lives in your home directory rather than inside the plugin, because the
plugin directory is replaced on every update.

---

## 7. Verifying the plugin without a CRM

Every script runs offline against the bundled fixtures — two complete synthetic orgs, one
Salesforce-shaped and one HubSpot-shaped, containing deliberate near-duplicate source values,
converted records whose source did not survive, and UTM/source disagreement:

```bash
python3 scripts/analyze.py --run-dir /tmp/ls-sf \
  --raw fixtures/salesforce/raw \
  --config fixtures/salesforce/config.json \
  --profile fixtures/salesforce/profile.json
python3 scripts/report.py --run-dir /tmp/ls-sf
open /tmp/ls-sf/report.html
```

Swap `salesforce` for `hubspot` to exercise the other shape. Useful for a security review, for
seeing the output before connecting anything, or for confirming an environment is sane after an
upgrade.
