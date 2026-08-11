# Forecast Agent

**This plugin is read-only. It never creates, updates or deletes a record in your CRM, and
nothing leaves your machine — the reports are local HTML files.** It runs in two modes, and the
default is not the forecast. It is an **audit of whether your CRM can support one at all**,
scored 0–100. That order is deliberate: forecasting is rarely a maths problem. Close dates are
fiction, stages aren't exit-criteria-based, and a tool that re-adds your pipeline and prints a
confident number is a tool your team stops opening by week two. Run the audit, fix the two or
three things it names, and then the call is worth making.

When you do run the call, it produces **three numbers — worst, likely and best — with the
method fully shown**, and the thing that actually earns its keep: **the delta between what your
reps called and what your own closed history says.** That delta is the deliverable. There is no
point estimate anywhere in this plugin.

Works first-class against **Salesforce** and **HubSpot-as-CRM**.

```
/forecast-agent:setup      once, per org
/forecast-agent:run        the audit (default)
/forecast-agent:run --mode forecast    the call, once the audit clears
```

---

## The two modes

### Mode 1 — Forecast integrity audit (default)

Answers one question: *can this CRM support a forecast?* It produces a **Forecast Integrity
Score out of 100** and up to fifteen findings, each with a record count, sample record IDs, and
the exact query that produced it — so a sceptical RevOps lead can verify any of them in their
own CRM in under a minute.

### Mode 2 — The call

Only worth running once the audit clears the threshold (default 60). Below it the plugin
**refuses to publish a number** and tells you why. That refusal is a feature; `--force`
overrides it, and if you use it, put the score on the slide next to the number.

---

## The Forecast Integrity Score

Six weighted components, each scored 0–100 from your own data. The weights sum to 100.

| Component | Weight | What it measures |
|---|---:|---|
| **Date integrity** | 22 | The share of forecast deals whose close date has already been pushed, and the share pushed three or more times. `100 × (1 − 0.7·pushed_once − 0.3·min(1, 2·pushed_3x))` |
| **Deal evidence** | 18 | Whether a committed deal carries a next step, a *specific* next step, and recent activity. `100 × (0.45·has_next_step + 0.20·specific + 0.35·active_within_window)` |
| **Buying-group coverage** | 12 | Share of commit deals with more than one contact role. `100 × multi_threaded_share` |
| **History depth** | 15 | Whether a conversion rate can mean anything here. `100 × (0.45·min(1, closed/100) + 0.30·min(1, quarters/6) + 0.25·has_stage_history)` |
| **Calibration** | 20 | What Commit has actually delivered. `100 × (0.5·accuracy + 0.3·stability + 0.2·no_repeat)` where accuracy = `1 − min(1, |1 − attainment| ÷ 0.4)`, stability = `1 − min(1, swing ÷ 0.25)`, no_repeat = `1 − min(1, repeat_commit_share ÷ 0.2)` |
| **Date realism** | 13 | Quarter-end clustering above what history justifies, plus the share of forecast dollars on deals younger than the p25 sales cycle. `100 × (1 − 0.5·min(1, excess_cluster ÷ 0.35) − 0.5·min(1, young_dollar_share ÷ 0.4))` |

**The final score:**

```
score = ( Σ subscore × weight  ÷  Σ weight of MEASURABLE components )
        × ( 0.6 + 0.4 × measurable share of weight )
```

A component you cannot measure — no close-date history, no contact associations — is dropped
from the average, and then the coverage multiplier claws the score back down. **Missing
measurement can never raise your score.** A CRM where only 60% of the weight is measurable is
capped at 84 no matter how clean the rest looks, and the report says which components went dark
and why.

| Band | Meaning |
|---|---|
| 0–39 | **Not forecastable.** Do not run the call. |
| 40–59 | **Directional only.** Ranges, never a number. |
| 60–79 | **Forecastable with named caveats.** The default threshold to publish the call. |
| 80–100 | **Board-grade.** |

## What the audit checks

Every one of these becomes a finding with evidence, or is silent because it found nothing.

1. Share of the forecast sitting on close dates that have **already been pushed**, and the total
   days pushed.
2. **Serial pushers** — deals moved three or more times and still in Commit.
3. Commit deals with **no next step**.
4. Commit deals that have gone **quiet** past the staleness window.
5. Commit deals that are **single-threaded** (contact roles at or below the threshold).
6. Share of the forecast on deals **created this quarter**, measured against the p25 of the
   real sales cycle — deals being asked to close faster than 75% of everything ever won here.
7. **Quarter-end clustering** of close dates, compared with where won deals actually landed.
8. **Measured stage conversion vs the probability the CRM assigns**, cohort-controlled by stage
   *entered*, alongside the survivorship version so you can see how much that choice flatters
   you.
9. **History depth** — whether there are enough closed deals and comparable quarters for a rate
   to be quotable, with per-stage cohort sizes and 95% bands.
10. **Commit attainment by quarter** — of everything ever called Commit for a quarter, how much
    landed in it, and how much that swings.
11. **Repeat commits** — the same deal called Commit in two different quarters.
12. Open deals with a **close date in the past**.
13. Forecast deals owned by **deactivated users** — nobody is calling them.
14. Forecast deals with **no amount**.
15. **Unmapped forecast categories** and **unmapped deal types** — values falling outside the
    counting rules, so deals vanish from the roll-up without anyone deciding they should.
16. **Multi-currency without conversion** — summing euros into dollars.
17. **The commit book by rep and manager**, with at-risk dollars, so you know who to sit with.

## How the three numbers are derived

Nothing here is a rule of thumb. Every input is measured from the customer's own closed history
and stated in the report.

**Win probability, cohort-controlled by entered.** For each open deal, `p_win` is the measured
win rate of the cohort that **entered** that deal's stage (or forecast category, depending on
your methodology) — not the deals sitting in it today. Measuring by current stage is
survivorship: the deals that fell out have left the denominator, and every rate comes out high.
The report prints both so you can see the gap.

**Confidence, not assertion.** Worst case uses the **lower bound of the 95% Wilson interval** on
each measured rate; best case uses the upper bound. Wilson is n-aware, so a thin cohort widens
the range on its own rather than us bolting on a caveat.

**Timing, from your own slip distribution.** For each deal we take the days of room between its
stated close date and the period end, then read off the share of historical closes that landed
within that many days of *their* stated date. Slip is reconstructed from the close-date change
log as *"how wrong was the date 30 days before the deal actually closed"* — the question a
forecast is actually asking. Worst case shifts that lookup one inter-quartile step pessimistic
(p75 − p50, from your data); best case shifts it the same distance optimistic.

**A measured penalty for pushed deals.** Deals pushed twice or more are multiplied by the ratio
of the win rate of pushed deals to un-pushed deals *in this company's history*. Not an assumed
haircut — a number the report shows you.

```
worst  = banked + Σ amount × wilson_low(p_win)  × push_penalty × slip_cdf(room − step)
likely = banked + Σ amount × p_win              × push_penalty × slip_cdf(room)
best   = banked + Σ amount × wilson_high(p_win)               × slip_cdf(room + step)

delta  = rep_called − likely          ← this is the deliverable
```

`banked` is closed-won already landed in the period and is added to all three unchanged.

## What it handles that most forecast tools don't

- **Three methodologies, because all three are real.** Category-based (Commit / Best Case /
  Pipeline / Omitted), weighted-by-stage, and hybrid. Setup asks; nothing is assumed.
- **What you actually forecast.** Bookings vs ARR vs revenue, and whether new, expansion and
  renewal each count. Getting this wrong makes every number wrong, so setup insists on it.
- **Your fiscal calendar.** Never assumes January. Reads `FiscalYearStartMonth` and both naming
  conventions (a February start can be FY2027 or FY2026 depending on the house style).
- **Roll-up, rep → manager → org**, from `profile.team_map.roll_up_field`.
- **Quota that isn't in the CRM.** Enter it by hand in the config, or don't — with no quota
  there is simply no coverage ratio, rather than one computed against an invented denominator.
- **Multi-currency.** Uses the converted-amount field and names which one it used. If there
  isn't one, that's a critical finding, not a silent sum.
- **Slip analysis.** The honest basis for the worst case is how long this company's deals
  actually slip, in days, at the quantiles.

## Sample output

Verbatim from the bundled Salesforce fixture (`/forecast-agent:run`, audit mode):

```
Forecast Integrity Score  57.3 / 100 — Directional only
Commit at risk            $1,186,000  of $1,544,200 total commit
Measured win rate         25.7%       61 won of 237 closed, 8 quarters
Commit actually lands at  54.3%       ±22 pts quarter to quarter
Pipeline coverage         2.18×       against $2,003,500 still to find

4 critical · 7 high · 3 medium

[CRITICAL] 4 deals have been pushed 3+ times and are still in the forecast
[CRITICAL] 56% of the forecast number sits on close dates that have already moved
[CRITICAL] Commit has landed at 54% of what was called, not 100%
[CRITICAL] Stage probabilities run up to 32 points above what your history measures
[HIGH]     39% of the commit number rests on one contact
[HIGH]     The commit book by rep: Dana Whitfield carries the most at-risk dollars
```

And in forecast mode (`--mode forecast --force`, since this org scores below the threshold):

```
Worst   $701,163      Likely  $1,379,560      Best  $2,312,408
Called by the team    $2,140,700
Delta                 $761,140  (35.6%)

Roll-up            Deals   Called      Likely      Called − evidence
Rob Tanaka           21   $1,018,700    $333,987      $684,713
Alicia Chen          15     $293,900    $233,429       $60,471
Fenella Boyd         22     $231,600    $215,644       $15,956
ORG TOTAL            58   $2,140,700  $1,379,560      $761,140
```

Nine tenths of the gap sits under one manager. That is the conversation the report is for.

## Files it writes

```
./gtm-agents/forecast-agent/<YYYY-MM-DD-HHMM>/
    raw/            exactly what came back from the CRM
    findings.json   machine-readable result
    report.md       the findings doc
    report.html     self-contained, opens locally, no external requests
    manifest.json   provenance, per-source record counts, failures
```

Config lives in `~/.leanscale-gtm/` so it survives plugin updates:
`profile.json` (shared with every LeanScale GTM agent) and `forecast-agent.json`. Baseline
snapshots go to `~/.leanscale-gtm/baselines/forecast-agent/`.

## Baselines

Run one writes a baseline and says so in the report: *this is the starting point, the comparison
begins next run.* Every run after it shows what moved — per score and per finding. Keep the
snapshots; they are the evidence that the work changed something.

## Fails loud

Every run writes `manifest.json` with per-source record counts. **If a required source returns
zero records the run stops with a diagnosis** rather than emitting a clean-looking empty report.
A report saying "0 issues found" because authentication expired is worse than a crash.

## Requirements

- `crm.query` — Salesforce (`run_soql_query`) or HubSpot (deal search). Required.
- `crm.describe` — recommended; drives the custom-field inventory during setup.
- Python 3.9+. Standard library only, no network access, no packages to install.
- Optional but high-value: **field history tracking on `CloseDate` and `ForecastCategoryName`**
  in Salesforce. Without it, a third of the score cannot be measured. It starts collecting the
  day you switch it on and cannot be backfilled — so switch it on today even if you run this
  next quarter.

## Licence

LicenseRef-LeanScale-Customer. Author: LeanScale · anthony@leanscale.team · https://leanscale.team
