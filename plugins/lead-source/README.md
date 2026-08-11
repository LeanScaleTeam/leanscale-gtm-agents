# Lead Source of Truth

**This plugin is READ-ONLY. It never creates, updates, deletes or merges anything in your CRM —
it issues read queries, writes report files to your own machine, and stops.** Its scope is
deliberately narrow: it audits the **integrity of your lead source data**, not multi-touch
attribution. It tells you whether the channel report you present is measuring what you think it
is measuring. It does not model influence, distribute credit across touchpoints, or connect to
an ad platform.

Everything stays local. No telemetry, no phone-home, no uploaded reports. The only network
traffic is the CRM connector you already had.

---

## Where the line is

Attribution is the highest-volume, lowest-confidence diagnostic in revenue operations. It gets
rebuilt from scratch on nearly every engagement, and it almost always gets rebuilt on top of
source data nobody checked first. This plugin is the check.

**What it does**

- Measures the null / "Other" / "Unknown" rate on every source-bearing field, trended over time
  and broken down by how the record was created — web form, import, API, or a human typing.
- Finds duplicate and near-duplicate source values (`Webinar` / `webinar` / `Webinars` /
  `Web Inar`; `Paid Search` / `PPC` / `SEM` / `Google Ads`) and proposes a canonical mapping.
- Measures whether the source value **survives conversion**, hop by hop: Lead → Contact →
  Opportunity, or Contact → Deal on HubSpot.
- Checks first-touch versus last-touch: which field holds which, whether a field declared
  first-touch is being overwritten, and whether the two fields are really one field twice.
- Checks UTM capture and whether UTMs agree with the source field.
- Measures self-reported source ("How did you hear about us?") against tracked source.
- Flags source values that carry volume and have never produced a win.
- Flags records with no source at all, and values that are not in the field's own picklist.

**What it does not do, and will not pretend to do**

- No multi-touch attribution. No first-touch/last-touch/linear/U-shaped/time-decay modelling, no
  credit splitting, no influenced-pipeline number.
- No ad-platform, MAP or web-analytics integration. It reads your CRM. That is the whole point:
  it installs in about twenty minutes because it needs one connector, not five.
- No campaign-influence or campaign-member analysis.
- No writes, no bulk updates, no auto-merging of source values. Every merge in the output is a
  **proposal with the record counts attached**, for a human to accept or reject.

If someone needs full multi-touch attribution, they need this first anyway. A multi-touch model
built on a field that is 27% "Other" produces a number that is precise, defensible-looking, and
wrong — and everyone quietly stops believing it about six weeks in.

---

## What it reads

| Source | Salesforce | HubSpot |
|---|---|---|
| Primary records | `Lead` | `Contact` |
| Middle hop | `Contact` (via `ConvertedContactId`) | — no Lead object, so this hop does not exist |
| Deal records | `Opportunity` (via `ConvertedOpportunityId`) | `Deal` (via the associations API) |
| Source fields | `LeadSource` on all three, plus your custom source/channel/UTM fields | `hs_analytics_source`, `hs_latest_source`, `hs_analytics_source_data_1/2`, plus the custom property you actually report on |
| Capture route | `CreatedBy.Name` | `hs_object_source_label` (`FORM` / `IMPORT` / `INTEGRATION` / `CRM_UI`) |
| Field history | `LeadHistory` | `propertiesWithHistory` via batch read |
| Picklists | `PicklistValueInfo` | `GET /crm/v3/properties/contacts` |

Both CRMs are first-class. The exact queries and payloads are written out in
`skills/run/SKILL.md` — copy-pasteable, not "adapt as needed".

---

## Install and run

```
/plugin marketplace add ./leanscale-gtm-agents
/plugin install lead-source@leanscale-gtm

/lead-source:setup     # once — discovers your fields, then asks what only you can answer
/lead-source:run       # the audit
```

Full install and troubleshooting detail is in `SETUP.md`.

Output lands in `./gtm-agents/lead-source/<date>/`:

```
raw/            exactly what came back from the CRM, unmodified
findings.json   machine-readable findings, plus the full taxonomy mapping proposal
report.md       for pasting into a doc or a ticket
report.html     self-contained, opens offline, safe to forward
manifest.json   what was read, from where, and how many records came back
```

Config lives in `~/.leanscale-gtm/`, not in the plugin, so it survives plugin updates.

---

## The four headline numbers

| Score | Meaning |
|---|---|
| **Source Integrity Score** | 0–100. The formula is below and is printed in every report. |
| **Unattributed Source Rate** | Share of records whose source field is blank or holds a placeholder. |
| **Source Survival Rate** | Share of converted records that arrive at the opportunity carrying the source they started with. |
| **Distinct Source Values** | How many values the field actually holds, against the size of the taxonomy you believe you have. |

### The Source Integrity Score, in full

A weighted mean of up to five components, each on a 0–100 scale:

| Component | Weight | Definition |
|---|---|---|
| `coverage` | 0.30 | `100 − unattributed_rate` on the reported source field. Unattributed = blank **or** a placeholder (`Other`, `Unknown`, `N/A`, `TBD`, `Legacy`, …). |
| `survival` | 0.25 | The Lead → Opportunity source survival rate. |
| `taxonomy` | 0.20 | `100 −` the share of attributed records sitting on a value that is either off your intended taxonomy or a non-canonical member of a duplicate cluster. |
| `agreement` | 0.15 | Mean of every agreement rate measurable (`100 − disagreement`): UTM-vs-source, and each configured field pair such as self-reported vs tracked. |
| `stability` | 0.10 | Share of records whose declared first-touch source was never overwritten after creation. Needs field history. |

```
score = round( Σ(weight_i × component_i) / Σ(weight_i) )   over AVAILABLE components only
```

Components that cannot be measured are **dropped**, and the remaining weights are rescaled to
sum to 1. The report always names which components went in, because a score built from three of
five components is a different number and hiding that is how a dashboard starts lying.

| Band | Reading |
|---|---|
| 85–100 | Trustworthy — the channel mix you present is defensible. |
| 70–84 | Usable with caveats — directionally right, with known holes. |
| 50–69 | Directional at best — do not make budget decisions on this. |
| 0–49 | The channel report is fiction. Fix the capture layer before the next board deck. |

### How survival is defined

Everyone measures this differently, so precisely:

- **Denominator** — records that converted, carried an *attributed* source at the lead level,
  and actually produced a record at the target hop. Converted leads that produced no opportunity
  are excluded and reported separately: a contact-only conversion is not a leak.
- **Numerator** — the target record carries the same source on the normalised key, so a pure
  casing difference counts as survival. Those are counted separately too and called out, because
  they are a duplicate-value problem rather than a survival problem.
- **Failures** split into **lost** (target blank or placeholder) and **changed** (target carries
  a different value). When the changed values pile onto one target value, or onto one converting
  user, the report says so — that is a default, not a rule.

---

## How the duplicate clustering works, and why it is conservative

Every cluster is a **candidate for human confirmation**. Nothing is merged, nothing is rewritten,
and each cluster shows the record count behind every member so a human can judge the blast radius
before agreeing to anything.

Values are normalised (lowercased, punctuation stripped, de-pluralised) into a collision key —
`Web Inar`, `Webinars` and `webinar` all key to `webinar`. Then four link tiers, weakest first:

| Tier | Evidence | Confidence |
|---|---|---|
| `subset` | One value is the other minus exactly one word (`Referral` ⊂ `Customer Referral`) | low, always |
| `synonym` | Both map to the same entry in the built-in channel lexicon (`PPC`, `SEM`, `Google Ads` → Paid Search) | medium |
| `similar` | Normalised strings match at ≥ 0.88 similarity (`difflib`, plus token-set overlap for word-order differences) | high at ≥ 0.93, else medium |
| `exact` | Same collision key | high |

A cluster is labelled with its **weakest** link, never its strongest.

Two guards keep it honest:

1. **A semantic link (synonym or subset) may never put two values that are both already in your
   intended taxonomy into one cluster, not even transitively.** If you deliberately blessed both
   `Referral` and `Partner`, the existence of `Partner Referral` does not entitle this tool to
   decide they are the same thing.
2. **Typographic links (exact, similar) are exempt from that guard on purpose.** When two
   spellings of the same string are both sitting in your picklist, that is the most damning
   thing this plugin can find, and it says so loudly.

Thresholds are configurable. Raising `similarity_threshold` towards 0.95 yields fewer, safer
proposals; below 0.85 it starts pairing genuinely different channels.

---

## Sample output

```
lead-source · Northwind Analytics · CRM salesforce
  900 leads · 275 opportunities · 159 history entries
  Source Integrity Score 60/100 (5 of 5 components measured)
  16 findings

  [critical] 29.4% of leads carry no usable source on LeadSource
  [critical] Only 46.1% of source values survive the trip to Opportunity
  [high    ] The missing source concentrates in one capture route: import
  [high    ] 5 groups of source values look like the same channel spelled differently
  [high    ] Converted source is being overwritten, 57.8% of it to 'Website'
  [high    ] LeadSource is declared first-touch but behaves like last-touch
  [high    ] The source field and the UTM disagree on 33.2% of records
  [high    ] Tracked source vs self-reported: 56.9% disagree on the channel
  [high    ] 3 source values carry real volume and have never produced a win
  [medium  ] Your first-touch and last-touch fields agree 99.1% of the time — one is a copy
```

Every finding carries the record count, sample record IDs, and the exact query that produced it,
so anyone can verify it in their own CRM in under a minute.

---

## Baseline and delta

Run one takes a baseline snapshot and says so, in the report and on the console: *this is your
baseline, the comparison starts next run.* Every run after that shows what moved and by how much,
on every score and every finding count. Snapshots live in
`~/.leanscale-gtm/baselines/lead-source/` and are never pruned — they are the evidence trail that
the work changed something.

Re-render a report without consuming a baseline slot with `report.py --no-baseline`.

---

## Failing loud

If a required source comes back with zero records, the run **stops** and prints a diagnosis
instead of emitting a clean-looking empty report. A report that says "0 issues found" because
authentication silently expired is worse than a crash.

Optional sources degrade instead. If there is no field history, the stability component is
dropped from the score and the report states that overwrite detection was **unavailable, not
clean** — an absent section is never presented as a pass.

---

## Offline test

The plugin ships fixtures for both CRM shapes, including deliberate near-duplicate values,
converted records whose source did not survive, and UTM/source disagreement, so the whole
pipeline can be exercised without a CRM connection:

```bash
python3 scripts/analyze.py --run-dir /tmp/ls-sf \
  --raw fixtures/salesforce/raw \
  --config fixtures/salesforce/config.json \
  --profile fixtures/salesforce/profile.json
python3 scripts/report.py --run-dir /tmp/ls-sf

python3 scripts/analyze.py --run-dir /tmp/ls-hs \
  --raw fixtures/hubspot/raw \
  --config fixtures/hubspot/config.json \
  --profile fixtures/hubspot/profile.json
python3 scripts/report.py --run-dir /tmp/ls-hs
```

The clustering engine also runs standalone against any value-count JSON:

```bash
echo '{"Webinar": 118, "webinar": 41, "Webinars": 30, "PPC": 44, "Paid Search": 96}' > /tmp/c.json
python3 scripts/taxonomy.py --counts-file /tmp/c.json --taxonomy "Paid Search,Webinar,Email"
```

Python 3.9+, standard library only. No pip install, no network access from any script.
