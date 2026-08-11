# Stage Architect

**This plugin is read-only. It issues queries and nothing else — it never creates, updates,
or deletes a record in your CRM, and it has no code path that could.** Everything it produces
is written to files on your own machine: a run directory in your working folder and a config
file in your home directory. Nothing is uploaded, hosted, or sent anywhere. The only network
traffic is to the MCP servers you already connected.

It derives what your deal stages **actually mean** from your own closed history, and shows you
the gap against what your team **believes** they mean.

Setup captures the belief. The run measures the reality. The report is the delta.

---

## The number this exists to fix

Ask a sales leader what converts out of Discovery and you get a number. Pull the same number
from the CRM's stock funnel report and you get a different, higher one. Both are wrong, and
they are wrong for different reasons.

The stock report is wrong because it counts from the stage each deal is **sitting in today**.
A deal that died in Discovery is now sitting in Closed Lost — so it has silently left the
Discovery denominator. The question that report actually answers is *"of the deals still alive
at or past Discovery, how many are past it?"*, which trends towards 100% by construction.

Stage Architect computes conversion cohort-controlled by the stage each deal **entered**, read
from stage-transition history:

```
denominator = deals that ENTERED the stage and have since resolved
              (advanced past it, or closed lost)
numerator   = deals that ever reached a later stage
censored    = deals still sitting at or below the stage — counted in neither,
              and reported separately
```

Both numbers appear in the report. The snapshot one is labelled as wrong every time it is
shown, with the gap in percentage points beside it.

---

## What it measures

- **Conversion per stage**, cohort-controlled by stage entered, with the number of resolved
  deals beside every rate. No rate is ever printed without its n.
- **Time in stage** — median, p75 and p90 per stage. The mean is shown only to demonstrate
  how far a handful of zombie deals drags it.
- **Stage-skip rate** — which stages get jumped, and by what share of the deals that pass
  them. A stage skipped by 60% of deals is not a stage.
- **Backwards movement** — how often deals regress, and across which boundary. The cleanest
  available signal that a stage's exit criteria are guesswork.
- **Stages that do not discriminate** — adjacent stages whose forward conversion is
  statistically indistinguishable. Two stages that predict the same outcome are one stage
  wearing two hats. This is the highest-value finding the plugin produces.
- **Zero-dwell stages** — stages with a median under a day, where deals are dragged through
  for process compliance rather than because anything happened.
- **Terminal integrity** — closed-lost reason fill rate, whether one value swallows the
  picklist, and which options nobody has ever used.
- **Lead lifecycle** — if a separate one exists: MQL → SQL → sales-accepted yield, rejection
  rate, and how much of the funnel dies at or before the sales-accepted step.
- **Exit-criteria proposal** — for every stage, criteria that are **buyer-verifiable** rather
  than **rep-asserted**, each naming the artifact the buyer produced that proves it.

### Buyer-verifiable vs rep-asserted

The core distinction, held throughout the report:

| Rep-asserted (unfalsifiable) | Buyer-verifiable (has an artifact) |
|---|---|
| Rep believes there is budget. | The buyer confirmed in writing who owns the budget. |
| Rep has identified the decision maker. | Someone on the buyer's side named who signs. |
| Demo went well. | The buyer agreed in writing to the evaluation's success criteria. |
| Proposal sent. | The buyer acknowledged receipt and booked a review. |
| Rep has a verbal. | Procurement returned redlines. |

A rep-asserted criterion is always satisfied when the rep needs it to be, which is why it is
always satisfied at quarter end. Every downstream number — stage conversion, forecast
category, weighted pipeline — inherits that. The plugin will not propose a criterion it
cannot attach an artifact to.

---

## Headline scores

| Score | Meaning |
|---|---|
| Measured win rate | Won ÷ (won + lost), over closed deals in the window |
| Belief vs reality gap | Percentage-point gap on the headline conversion number |
| Stages that are really one stage | Count of non-discriminating adjacent pairs |
| Deals skipping a stage | Share of deals that jumped at least one live stage |
| Stages earning their place | Stages that survive every test, of those configured |

## The statistical guard

Two stages are only called non-discriminating when **all three** hold:

1. a pooled two-proportion z-test fails to reject at `significance_alpha` (default 0.05), and
2. the rates are within `equivalence_band_pp` of each other (default 5 points), and
3. **both stages have at least `min_cohort_size` resolved deals** (default 30).

The third condition is the one that matters. On small cohorts every stage looks like every
other stage, and calling that equivalence is ignorance dressed as a finding. Pairs that fail
it are reported as *"not tested — needs 30 resolved deals per stage, has 98 and 32"*, never as
merge candidates. All three thresholds are configurable.

---

## Both CRMs, first class

Salesforce and HubSpot are equally supported, with the actual queries written into the run
skill — real SOQL and real HubSpot request payloads, not "adapt as needed".

- **Salesforce** — `OpportunityStage` for the ladder, `OpportunityHistory` for transitions
  (a standard object, no setup required), with `OpportunityFieldHistory` as fallback. Field
  history is retained 18 months, 24 with Field Audit Trail; the plugin detects partial
  coverage and says so rather than quietly reporting optimistic rates.
- **HubSpot** — the pipelines API for the ladder, and either the `hs_date_entered_<stageId>`
  calculated properties (one request, but only the most recent entry per stage, so regression
  counts are a floor) or true deal property history via batch read (exact, 100 deals per
  call). The report states which route was used and what it costs you.

**If stage history is unavailable entirely**, the run degrades to a snapshot analysis, raises
a `critical` finding saying so, and lists every affected section under `unavailable`. Snapshot
rates are never presented as measured conversion.

---

## Usage

```
/stage-architect:setup     # probe, discover, interview, write config, smoke test
/stage-architect:run       # fetch, analyse, report
```

Output lands in `./gtm-agents/stage-architect/<YYYY-MM-DD-HHMM>/`:

```
raw/            exactly what came back from each source
findings.json   machine-readable result
report.md       the human findings doc
report.html     self-contained, opens locally, survives being forwarded
manifest.json   provenance, per-source record counts, failures
```

Config lives in `~/.leanscale-gtm/` so it survives plugin updates. `profile.json` is shared
with every other agent in the suite; you answer those questions once.

## Baseline and delta

Run one writes a baseline snapshot and says so, plainly, in the report and on the console.
Every run after it shows what moved and by how much. A health score with no baseline is a
vibe — this is the evidence that the stage redesign changed something.

## Fail-loud contract

Every run writes `manifest.json` with per-source record counts and the query behind each one.
**If a required source returns zero records the run stops with a diagnosis** rather than
emitting a clean-looking empty report. A report that says "no issues found" because the
integration user lost a permission is worse than a crash.

## Sample output

```
stage-architect: analysed 518 opportunities on 'New Business' (6 live stages, 2 with no traffic)
  measured win rate 21.0% · 11 findings

  [critical] The team believes overall win rate is 35.0%. It is 21.0%.
  [high    ] 2 adjacent stage pairs cannot be told apart - merge them
  [high    ] Your stock funnel report overstates Discovery conversion by 29.8 points
  [high    ] Negotiation is skipped by 64.1% of the deals that pass it
  [high    ] 7 of 8 stages cannot be exited on evidence the buyer produced
  [medium  ] Qualification hides zombie deals: median 11.6d, p90 73.7d
  [medium  ] Contracting has a median dwell of 0.2 days - nothing happens there
```

The corresponding table in the report:

| Earlier stage | Rate (n) | Later stage | Rate (n) | Diff | p | Verdict |
|---|---|---|---|---|---|---|
| Qualification | 72.5% (n=320) | Solution Validation | 71.5% (n=228) | 1.0 pp | 0.795 | indistinguishable — these are one stage |
| Demo Delivered | 68.4% (n=98) | Champion Confirmed | 81.2% (n=32) | 12.8 pp | 0.161 | not tested — needs 40 resolved deals per stage |

## Try it without a CRM

Bundled fixtures for both CRM shapes let you see the whole pipeline offline:

```bash
python3 scripts/analyze.py --raw fixtures/salesforce/raw \
  --run-dir /tmp/sa --config fixtures/salesforce/config.json --as-of 2026-08-10
python3 scripts/report.py --run-dir /tmp/sa
```

Swap `salesforce` for `hubspot` to see the other shape. Both contain several hundred
opportunities with full stage-transition history.

## Requirements

- Python 3.9+, standard library only. No pip installs, no network access from the scripts.
- A connected CRM MCP server providing `crm.query` and `crm.describe`.

## Licence

`LicenseRef-LeanScale-Customer` — see your agreement.
