# LeanScale GTM Agents — Build Spec v1

**Read this completely before writing a single file.** Nine plugins are built against this
spec by nine different authors. Anything in here is non-negotiable; anything not in here is
your call. The goal is that a customer who installs three of these cannot tell they were
built separately.

These ship to **paying external customers** who install them into *their own* Claude Code
against *their own* systems. Nothing LeanScale-internal may appear in a plugin: no Teamwork,
no `leanscale3.*`, no `@leanscale.team` accounts, no hardcoded customer names, no LeanScale
Netlify site IDs, no LeanScale Slack channel IDs, no LeanScale MCP server IDs.

---

## 0. The two rules that matter most

**Rule 1 — Discover before you ask.** A setup skill that opens with "what are your deal
stages?" is a bad setup skill. Read their CRM first: describe the objects, pull the picklists,
compute field fill-rates, count records by stage, find the custom fields. *Then* ask questions
that only a human can answer, phrased in terms of what you found:

> ❌ "What are your pipeline stages?"
> ✅ "I found 9 opportunity stages. 4 of them have had no deal enter them in 180 days
>    (`Technical Validation`, `Legal`, `Verbal`, `Pilot`). Are those dead stages I should
>    ignore, or real stages that are just slow?"

Every question you ask that the CRM could have answered is a question that makes the product
feel dumb. Every question you *fail* to ask that changes the analysis makes the output wrong.

**Rule 2 — Baseline on run one, value on run two.** Every run writes a baseline snapshot.
Every run after the first shows deltas. Run one must say so explicitly, in the report:
*"This is your baseline. The comparison starts next run."* We have lost a real renewal
because we delivered a year of work with no kickoff baseline to prove it against. Do not
ship an agent that can't show its own progress.

---

## 1. Runtime architecture — how these actually work

A Claude Code plugin cannot call MCP tools from Python. Only Claude can. So every plugin
uses the same three-layer split, and you must not deviate:

```
LAYER 1  SKILL.md (Claude)     → calls MCP tools (CRM, transcripts, comms)
                                 writes raw results to runs/<date>/raw/*.json
LAYER 2  scripts/analyze.py    → pure stdlib transform of raw/*.json → findings.json
                                 deterministic, testable, no network, no MCP
LAYER 3  scripts/report.py     → findings.json → report.md + report.html (+ baseline diff)
```

Consequences you must respect:

- **Python is offline.** `analyze.py` and `report.py` read and write local files only. No
  `requests`, no API calls, no pip installs — **Python 3.9+ standard library only.**
- **Claude does the fetching**, following explicit, copy-pasteable queries written in the
  SKILL.md. Write the actual SOQL / HubSpot filter payloads into the skill. Do not write
  "query their opportunities" and hope.
- **Judgment lives in the skill**, not in Python. Python computes counts, rates and diffs.
  Claude interprets. Never try to encode qualification judgment in a regex.

### Run directory (identical across all nine)

```
./gtm-agents/<plugin-slug>/<YYYY-MM-DD-HHMM>/
    raw/            # exactly what came back from each source, unmodified
    findings.json   # analyze.py output — the machine-readable result
    report.md       # the human findings doc
    report.html     # self-contained, LeanScale-branded, opens locally
    manifest.json   # provenance + record counts + failures
```

**Reports are local files. Never deploy a customer report to Netlify or any host.** The
catalog site is LeanScale marketing; customer data stays on the customer's machine.

---

## 2. Config — lives in the user's home, never in the plugin

Plugins get replaced on update. Config must survive that.

```
~/.leanscale-gtm/
    profile.json              # SHARED org profile — written once, read by all nine
    <plugin-slug>.json        # per-plugin settings
    baselines/<plugin-slug>/  # dated baseline snapshots
    audit/<plugin-slug>.log   # append-only write log (write-capable plugins only)
```

### `profile.json` — the shared org profile

The **first** setup skill a customer runs creates this. Every subsequent setup skill reads
it, shows the customer what's already known, and only asks for what's missing. Do not
re-interrogate someone about their fiscal year nine times.

```jsonc
{
  "schema_version": 1,
  "org_name": "Acme",
  "crm": {
    "system": "salesforce",           // salesforce | hubspot
    "mcp_probe": "run_soql_query",    // the tool that proved it works
    "instance_label": "Acme Production",
    "secondary": "hubspot_marketing"  // null | hubspot_marketing | marketo | pardot
  },
  "fiscal_year_start_month": 2,        // 1-12. Drives every period calculation.
  "currency": { "corporate": "USD", "multi_currency": true },
  "motion": ["inbound_led", "outbound", "plg", "enterprise", "channel"],
  "quota_carrying_reps": 14,
  "segments": ["SMB", "Mid-Market", "Enterprise"],
  "segment_field": "Account.Segment__c",
  "material_deal_floor": 5000,         // ignore noise below this
  "team_map": { "roll_up_field": "User.ManagerId" },
  "objects": {                         // renamed/custom objects, if any
    "opportunity": "Opportunity",
    "account": "Account",
    "lead": "Lead"
  },
  "competitors": ["Competitor A", "Competitor B"],
  "redact_pii_in_reports": false
}
```

**Interview rules for `profile.json`:**
- `fiscal_year_start_month` — never assume January. Read it from CRM settings if you can
  (Salesforce: `Organization.FiscalYearStartMonth`), then confirm.
- `quota_carrying_reps` — ask directly. It is the single most load-bearing number in the
  suite; ratios computed against total headcount are wrong and embarrassing.
- `material_deal_floor` — propose the 10th percentile of closed-won amount, then confirm.
- `segments` — read the picklist, don't invent.

### Per-plugin config

Same file shape everywhere: a `_comment` header, and every key followed by
`"_<key>_help"` explaining it in one sentence. Copy this convention exactly — customers
edit these files by hand.

---

## 3. Capability probe — how a plugin finds out what it can reach

Customers connect their own MCP servers. You cannot assume tool names. Every setup skill
opens with a probe, mapping real tools to canonical capabilities:

| Capability | Probe query | Known providers |
|---|---|---|
| `crm.query` | `ToolSearch("run_soql_query salesforce")` / `ToolSearch("hubspot crm search")` | Salesforce MCP, HubSpot MCP |
| `crm.describe` | `ToolSearch("describe metadata object schema")` | Salesforce MCP |
| `crm.write` | `ToolSearch("create update record")` | Salesforce, HubSpot |
| `transcripts.list` | `ToolSearch("transcripts meetings recordings")` | Gong, Fireflies, Chorus, Grain, Otter, Zoom, Google Drive |
| `transcripts.get` | `ToolSearch("get transcript")` | same |
| `comms.search` | `ToolSearch("slack search")` / `ToolSearch("gmail search threads")` | Slack, Gmail, Outlook |
| `docs.read` | `ToolSearch("read file content drive")` | Google Drive, Notion |

**Field reality — plan for it.** Across a 43-company panel of exactly this ICP:
Salesforce is CRM-of-record at **63%**, HubSpot-as-CRM at **33%**, and Gong is present at
only **40%**. So:

- **Support Salesforce and HubSpot as first-class.** Not "Salesforce, HubSpot later."
- **Never hard-require a conversation-intelligence tool.** Six in ten prospects don't have
  Gong. Support a Drive/folder of transcript files and a manual-paste path as real,
  documented options — not a footnote.
- Transcript sources are fragmented. Write an adapter section in the skill per source, and
  degrade gracefully: a plugin that needs transcripts must still produce its CRM-only
  findings and clearly mark the transcript sections as unavailable.

**Probe failures are specific.** Never report "Slack not available." Report: *"Slack tools
resolve, but reading `#deal-room-acme` returned 0 messages while the channel exists — the
connected Slack identity is probably a bot that isn't a member of the channel."*

---

## 4. The setup skill — required structure

Every plugin ships `skills/<slug>-setup/SKILL.md`, invoked as `/<slug>-setup`. It runs in
this order and ends with a pass/fail table:

1. **Probe** connectors (§3). Report exactly which capability each resolved tool provides.
2. **Read `~/.leanscale-gtm/profile.json`** if present; show the customer what's already
   known and confirm rather than re-ask.
3. **Automatic CRM discovery** — the part that makes this feel expensive. At minimum:
   object list, record counts, picklist values for every field you'll use, field fill-rates
   over the last 12 months, custom-field inventory, and the org's fiscal settings.
4. **The interview** — your plugin's questions from §7, informed by step 3.
5. **Write config** to `~/.leanscale-gtm/`. Show the customer the file you wrote.
6. **Smoke test** — run the real pipeline against a small slice (e.g. 30 days, one segment)
   and show a genuine finding. A setup that ends without proving output is not done.
7. **Pass/fail table** + a plain-English statement of what will and won't work, and what
   the customer must do to fix each gap.

Setup must be **idempotent and re-runnable** — it doubles as the health check when a run
later fails.

---

## 5. Safety posture — non-negotiable

- **Read-only by default.** Eight of nine plugins never write. Say so in the README, in
  plain words, in the first paragraph. It is a sales feature: 30% of this ICP has an active
  AI-governance problem, and read-only is what gets approved.
- **The one write-capable plugin** (`meeting-to-crm`) must: default to dry-run, render a
  diff table of every proposed change, require explicit per-batch human approval, never
  auto-write on a schedule, append every applied write to `~/.leanscale-gtm/audit/`, and
  write only the fields named in its config allow-list.
- **Nothing leaves the machine.** No telemetry, no phone-home, no uploading reports. The
  only network egress is the MCP servers the customer already connected.
- **Fail loud.** Autonomous agents fail silently — we have watched a customer's enrichment
  job stop authenticating for six weeks with nobody noticing. Therefore: every run writes
  `manifest.json` with per-source record counts, and **if a required source returns zero
  records the run stops with a diagnosis** rather than emitting a clean-looking empty
  report. A report that says "0 issues found" because auth failed is worse than a crash.
- **PII.** Honour `profile.json.redact_pii_in_reports`. When true, replace person names and
  emails with stable pseudonyms (`Rep 3`, `contact-a71@…`) in `report.md` / `report.html`;
  `raw/` and `findings.json` stay unredacted locally.

---

## 6. Findings — one shared shape

Every plugin's `findings.json` uses this envelope so the suite reads as one product.

```jsonc
{
  "plugin": "crm-hygiene",
  "generated_at": "2026-08-10T14:22:00Z",
  "window": { "start": "2026-02-10", "end": "2026-08-10" },
  "is_baseline_run": true,
  "scores": [ { "key": "hygiene_index", "label": "Hygiene Index", "value": 61, "unit": "score_0_100", "delta_vs_last": null } ],
  "findings": [
    {
      "id": "dupe-accounts-by-domain",
      "severity": "high",              // critical | high | medium | low
      "title": "1,204 accounts share 511 email domains",
      "what": "One sentence stating the defect.",
      "evidence": { "count": 1204, "sample_ids": ["001..."], "query": "SELECT ..." },
      "why_it_matters": "Routing sends the same buyer to three reps; ...",
      "recommended_fix": "Concrete, specific, do-this-next.",
      "effort": "medium",              // quick | medium | project
      "owner_hint": "RevOps"
    }
  ],
  "sections": { }                       // plugin-specific structured detail
}
```

**Severity means the same thing in all nine plugins:**
- `critical` — the number an executive is looking at is wrong, or revenue is actively leaking.
- `high` — a decision is being made on bad data; fix this quarter.
- `medium` — real drag on the team; fix when convenient.
- `low` — hygiene, cosmetic, or a watch item.

Every finding must carry **evidence with record IDs or counts and the query that produced
them**. A finding a customer cannot verify in their own CRM in 60 seconds is not shippable.

---

## 7. The nine plugins

Slugs, commands, required capabilities, and the interview each must run. Your plugin's
questions are a floor, not a ceiling — add what the analysis genuinely needs.

Plugin skills are **always** namespaced `/<plugin-name>:<skill-name>` — there is no way to
get a bare `/crm-hygiene`. So every plugin ships the same two skill names, and a customer
with four installed types the same thing every time: `:run` and `:setup`.

| # | Plugin name | Invocations | Requires | Optional |
|---|---|---|---|---|
| 1 | `crm-hygiene` | `/crm-hygiene:run` · `:setup` | `crm.query`, `crm.describe` | — |
| 2 | `pipeline-inspection` | `/pipeline-inspection:run` · `:setup` | `crm.query` | `transcripts.*` |
| 3 | `meeting-to-crm` | `/meeting-to-crm:run` · `:setup` | `crm.query`, `transcripts.*` | `crm.write` |
| 4 | `forecast-agent` | `/forecast-agent:run` · `:setup` | `crm.query` | `transcripts.*` |
| 5 | `sales-coach` | `/sales-coach:run` · `:setup` | `transcripts.*` | `crm.query` |
| 6 | `customer-health` | `/customer-health:run` · `:setup` | `crm.query` | `transcripts.*`, `comms.search` |
| 7 | `stage-architect` | `/stage-architect:run` · `:setup` | `crm.query`, `crm.describe` | — |
| 8 | `lead-source` | `/lead-source:run` · `:setup` | `crm.query`, `crm.describe` | — |
| 9 | `system-map` | `/system-map:run` · `:setup` | `crm.describe` | `crm.query` |

### Interview requirements (minimum) — beyond the shared profile

**1. crm-hygiene** — objects in scope; policy-required fields (vs schema-required); the
dedupe key (domain / name+geo / email); staleness threshold for an open opp; record types
in use; known-dead fields; who owns data quality; whether inactive users still own records.

**2. pipeline-inspection** — expected days-in-stage per stage (discover the actual medians
first, then confirm); where "next step" lives (field vs task vs neither); how many close-date
pushes is too many; the single-threading definition (contact-role count); what "commit"
means to them; deal-size bands; whether they inspect weekly or monthly.

**3. meeting-to-crm** — which meeting types to process; the exact field allow-list it may
propose; the qualification framework's field mapping; whether close-date and amount may be
proposed or are rep-only; who approves; dry-run vs apply default; how to match a meeting to
an opportunity (attendee domain / calendar link / manual).

**4. forecast-agent** — the methodology (category-based commit/best-case/pipeline, weighted-
by-stage, or a hybrid); what they forecast (bookings vs ARR vs revenue; new vs renewal vs
expansion, counted how); the roll-up hierarchy; submission cadence and deadline; quota source
and whether it's in the CRM; what a "commit" costs a rep socially; how far back history goes
for conversion rates; whether they want a call or an audit first (default: audit first).

**5. sales-coach** — the framework (MEDDPICC / MEDDIC / BANT / SPICED / Challenger / Command
of the Message / custom — capture the custom one properly); call types to coach; per-rep
tenure so ramping reps are coached differently from tenured ones; talk-ratio and
question-rate targets, or use defaults and say they're defaults; whether output goes to the
manager (default) or the rep; what "good" looks like — ask for 2–3 exemplar calls; how
often they run it.

**6. customer-health** — what counts as a customer; where contract dates and renewal dates
live; the CSM book and how accounts map to owners; whether product-usage signal exists and
where; support-ticket source; who the champion and economic buyer are per account (or the
field that holds it); **the kickoff baseline** — capture it explicitly; and separate
**sentiment** from **commercial risk**, because they diverge (the happy account with an
unsigned renewal is the one that churns).

**7. stage-architect** — current stage list with their written definitions (ask for the doc);
which stage is sales-accepted; whether a separate lead lifecycle exists; whether stages are
supposed to be buyer-verifiable or rep-asserted; what they believe their conversion rates
are (capture the belief, then show them the measured reality — the gap is the deliverable).

**8. lead-source** — every field that holds source/channel/campaign; first-touch vs
last-touch intent; whether UTMs are captured and where they land; the canonical channel
taxonomy they *think* they have; whether source survives Lead→Contact conversion; self-
reported source field if any.

**9. system-map** — prod vs sandbox; whether to include managed packages, integration users,
connected apps, automation (flows/workflows/APEX triggers), and scheduled jobs; whether to
flag orphaned automation; the tools they *believe* are connected (capture the belief, then
show them the measured reality).

---

## 8. Every plugin's file layout

```
plugins/<name>/
    .claude-plugin/plugin.json
    README.md                       # what it does, what it reads, read-only statement, sample output
    SETUP.md                        # install + connect + first run, with a troubleshooting table
    skills/run/SKILL.md             # the pipeline            -> /<name>:run
    skills/setup/SKILL.md           # probe + discovery + interview + smoke test -> /<name>:setup
    scripts/analyze.py              # raw/*.json -> findings.json      (stdlib only)
    scripts/report.py               # findings.json -> report.md/.html (stdlib only)
    scripts/lib/                    # VENDORED copy of core/lib — do not edit here
    fixtures/                       # sample raw/*.json so the scripts are testable offline
    config.example.json             # documented default, copied to ~/.leanscale-gtm/
```

**No `commands/` directory.** Skills only — the docs recommend it for new plugins, and a
flat command file can't carry supporting scripts.

**Naming:** `plugin.json.name` is the bare slug (`crm-hygiene`), kebab-case. Version
`1.0.0`. Author `{"name":"LeanScale","email":"anthony@leanscale.team","url":"https://leanscale.team"}`,
homepage `https://leanscale.team`. LeanScale branding lives in the marketplace name and the
report, not in a redundant plugin prefix.

**Paths:** reference bundled files as `"${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py"` — always
quoted, never relative, never absolute-from-the-build-machine.

**Two hard constraints from the plugin runtime:**
- A plugin **cannot read files outside its own directory** (`../core/lib` will not resolve —
  installed plugins are copied into a cache). This is why `core/lib` is *vendored* into every
  plugin. Build against `core/lib`; the packaging step copies it in.
- `${CLAUDE_PLUGIN_ROOT}` is **read-only** on a marketplace install. Never write there. All
  output goes to the working directory; all config to `~/.leanscale-gtm/`.

---

## 9. The HTML report

Self-contained single file — CSS **inlined**, no external requests, no CDN, no fonts fetched
(the font stack falls back to system sans if Plus Jakarta Sans isn't installed). Use the
LeanScale tokens: `--ls-off-white #FFFBFF`, `--ls-ink #1a1420`, `--ls-dark-purple #301934`,
`--ls-strong-purple #642585`, `--ls-purple-soft #F3EAF7`, `--ls-lime #E8FFCF`,
`--ls-light-gray #E9E9E7`, radius `18px`, max-width `1120px`. Structure: eyebrow pill →
h1 → score KPI row → severity-sorted findings → evidence tables → method footer. Include the
brand-mark favicon data-URI from the design standard. `core/lib/report_html.py` gives you the
renderer — use it rather than hand-rolling a fourth variant.

---

## 10. Definition of done

A plugin is done when:

- [ ] `python3 -m py_compile` passes on every script, and `analyze.py` + `report.py` run
      end-to-end against the bundled `fixtures/` sample and produce a real report.
- [ ] `plugin.json` and both `SKILL.md` frontmatters validate against the schema in
      `core/PLUGIN-SCHEMA.md`.
- [ ] The setup skill asks every question in §7 for your plugin — and asks nothing the CRM
      could have told it.
- [ ] Zero LeanScale-internal references (`grep -ri "teamwork\|leanscale3\|leanscale\.team\|netlify.*site"`
      returns only the author email and homepage).
- [ ] Read-only statement present in README paragraph one (or, for `meeting-to-crm`, the
      full write-safety contract).
- [ ] Baseline + delta implemented, with the run-one message.
- [ ] `manifest.json` written, and a zero-record required source aborts the run.
- [ ] Both Salesforce and HubSpot paths written out — actual queries, not "adapt as needed."
- [ ] A skeptical RevOps buyer reading `report.html` can verify any finding in their CRM in
      under a minute.
