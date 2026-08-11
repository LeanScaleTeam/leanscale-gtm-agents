# Pipeline Inspection

**This plugin is read-only.** It issues queries against your CRM and writes files to your own
machine. It never creates, updates, deletes or merges a record, it has no write path in its
code, and nothing it produces leaves your laptop — no telemetry, no uploads, no hosted
reports. The only network traffic is to the MCP servers you already connected.

It answers one question: **which open deals are violating the rules you set for yourself?**

That is deliberately not "what will we close" — no probability weighting, no roll-up, no
number to defend in a board meeting. Those belong to a forecast tool. This produces a ranked
call list: deal by deal, the rule it broke, the evidence, and the query you can paste into
your own CRM to check it yourself in under a minute.

---

## What it detects

Sixteen rules. Every threshold is either measured from your own history or confirmed by you
during setup — there is not a single industry benchmark anywhere in the codebase.

| Rule | Default threshold | Severity |
|---|---|---|
| `past-due-open-deals` | Close date is before today and the deal is still open | critical |
| `close-date-serial-pushes` | Close date moved later **3+** times | critical |
| `stage-stagnation-severe` | Days in stage > **4×** the measured median for that stage | critical |
| `no-contact-roles` | **Zero** contacts attached to an open deal | critical |
| `stage-stagnation` | Days in stage > **2×** the measured median for that stage | high |
| `no-next-step` | No next step in the field / an open task / either (you choose) | high |
| `single-threaded-deals` | Fewer contacts than your size bar: **1** under $25k, **2** over $25k, **3** over $100k, **5** over $250k | high |
| `activity-silence` | No logged activity in **21** days (per-stage overrides supported) | high |
| `post-commit-amount-change` | Amount moved **>10%** after entering a commit stage | high |
| `close-date-faster-than-history` | Close date nearer than **0.5×** the median time your own won deals took from that stage | high |
| `missing-or-zero-amount` | Open deal with a null or zero amount | high |
| `stage-regression` | Deal moved backwards through your stage order | medium |
| `stale-next-step` | Next step exists but is past due, or untouched for **14** days | medium |
| `quarter-end-clustering` | **≥30%** of dated open deals close on the last day of a month | medium |
| `same-day-create-and-close` | Closed deal created and closed within **1** day — a backfill that corrupts cycle time | medium |
| `possible-double-counted-pipeline` | Two open deals on one account with identical amount and close date | medium |
| `close-date-pushes-emerging` | 1–2 pushes — the watch list before they become serial | low |

Materiality can promote a finding one level when it touches ≥20% of open pipeline, but never
into critical. Critical means what it says: revenue is leaking now.

### The measured medians

The plugin reads your closed history, reconstructs every stage transition, and computes the
median, 75th and 90th percentile days spent in each stage. Most teams have never seen these
numbers. They are printed in the report next to the threshold derived from them, so
"this deal is stuck" stops being an opinion:

| Stage | Closed samples | Median | 75th | 90th | You expected | Flag above |
|---|---|---|---|---|---|---|
| Discovery | 70 | 9.9d | 16.0d | 20.6d | 14d | 19.7d |
| Qualification | 63 | 16.9d | 27.0d | 35.3d | 21d | 33.7d |
| Negotiation | 49 | 26.7d | 36.3d | 41.6d | 30d | 53.3d |

Where a stage has too few completed intervals to measure, the threshold falls back to the
number you confirmed in setup, then to the all-stage median — and the report names which
basis it used, per stage.

### Headline scores

`Inspection Score` (share of open pipeline dollars breaking no rule) · `At-Risk Pipeline` ·
`Pipeline Flagged` · `Past-Due Pipeline` · `Coverage Ratio` (when you supply a quota).

---

## Both CRMs, first-class

Salesforce and HubSpot are equal citizens; the actual SOQL and the actual HubSpot search
payloads are written out in `skills/run/SKILL.md`, not left as "adapt as needed".

Where a platform genuinely cannot answer a check, the report says so in a **"not covered"**
banner rather than showing a clean pass:

| Check | Salesforce | HubSpot |
|---|---|---|
| Close-date pushes | `OpportunityFieldHistory` — needs history tracking on, retained 18 months (24 with Field Audit Trail) | Property history — very old versions can be truncated, so counts are a floor |
| Stage dwell time | `OpportunityHistory`, present in every org | `hs_v2_time_in_<stage>` plus property history |
| Contact count | `OpportunityContactRole`, with roles and a primary flag | v4 associations — **no role semantics**, so "who is the economic buyer" is not answerable |
| Stage order | `OpportunityStage.SortOrder` | `pipelines[].stages[].displayOrder` |

---

## What it reads

Open opportunities and 24 months of closed ones (required); stage history, close-date and
amount field history, contact roles, activities, open tasks, stage metadata and quota
(optional, each degrading loudly rather than silently).

## What it writes

Everything lands in `./gtm-agents/pipeline-inspection/<timestamp>/`:

```
raw/            exactly what your CRM returned, unmodified — your audit trail
findings.json   machine-readable findings, full row tables, the query behind each one
manifest.json   per-source record counts and provenance
report.md       the findings doc
report.html     self-contained, no external requests, opens with the wifi off
call-list.csv   every flagged deal, ranked — the file a manager actually works from
```

Config lives in `~/.leanscale-gtm/` so it survives plugin updates. Baselines are kept
forever in `~/.leanscale-gtm/baselines/pipeline-inspection/` — they are your evidence trail.

---

## Baseline and delta

Run one writes a baseline and says so, in the report, in plain words: *"This is your baseline
run. The comparison starts next run."* Every run after it shows movement on every score and
every finding count:

```
Inspection Score:  32.8   (+2.3 vs last run)
At-Risk Pipeline:  4002000 (-480000 vs last run)
Past-Due Pipeline: 32000   (-443000 vs last run)
```

That delta is the whole point. A pipeline health score with nothing to compare it to is a
vibe; the snapshot is what proves the work changed something.

---

## Usage

```
/pipeline-inspection:setup     # once — probes, measures, interviews, writes config, smoke-tests
/pipeline-inspection:run       # weekly — produces the call list
```

Re-run `:setup` any time. It is idempotent and doubles as the health check when a run fails.

## Try it offline first

Bundled fixtures let you exercise the whole pipeline with no CRM connected. They are a frozen
snapshot dated 2026-08-10 — that date is baked into `raw/meta.json`, so results stay stable
whenever you run them.

```bash
# Salesforce shapes
python3 scripts/analyze.py --raw-dir ./fixtures/raw --out-dir /tmp/demo \
                           --config ./config.example.json
python3 scripts/report.py  --run-dir /tmp/demo

# HubSpot shapes
python3 scripts/analyze.py --raw-dir ./fixtures/hubspot/raw --out-dir /tmp/demo-hs \
                           --config ./fixtures/hubspot/config.json
python3 scripts/report.py  --run-dir /tmp/demo-hs
```

Both produce all sixteen findings on the same underlying business, expressed in each
platform's native response shapes — Salesforce nested relationships and `attributes` bags,
HubSpot `properties` objects, `propertiesWithHistory` batch reads and v4 association results.

## Requirements

Python 3.9+, standard library only. No pip installs. `analyze.py` and `report.py` never touch
the network — Claude fetches through MCP and writes JSON; the scripts only transform local
files.
