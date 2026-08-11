# Executive Reporting

**Read-only.** This plugin queries your CRM and writes files on your machine. It never creates,
updates or deletes a record, and nothing is uploaded anywhere. The pack is a local HTML file.

Builds the executive GTM reporting pack leadership actually runs the business on — bookings,
created pipeline, coverage, conversion, retention and concentration — with two rules applied
throughout: **every number lands against a goal**, and **every number opens into the rows
underneath it**.

---

## The part most reporting projects skip

It audits whether your CRM can support the reporting *before* it publishes a single headline,
and scores that 0–100 as a **Reporting Readiness** score. Below the band, the headline numbers
are withheld and the report tells you exactly which component failed and what to fix.

That is not caution for its own sake. A pack built on data that cannot support it does not hide
the mess — it publishes it faster and more credibly, and whoever's name is on the chart owns it.

Three defects it is specifically built to catch, because all three look like performance and
none of them are:

| What it finds | Why it matters |
|---|---|
| **An under-stamped stage** — a later stage holding more records than an earlier one | Conversion into it computes above 100%. That is a missing stamp, not a funnel. The rate is withheld and the counts are shown. |
| **A swapped stage label** | We have seen an org whose displayed "SAL" is the canonical SQL stage. Nothing here trusts display names; everything routes through a stage map you confirm during setup. |
| **An unripe cohort** | A created-date cohort still mostly open produces a conversion rate that flatters or damns at random. Suppressed until it ripens, and the report says which and why. |

---

## The conversion-rate definition, stated once

Group deals by the date they were **created**. Divide **won** by everything that reached a
decision — **won + lost**. Deals still open are excluded until they resolve.

> Ten deals created in Q1. Four won, four lost, two still open → **50%**.

This will usually differ from a number calculated on deals that *closed* in the period, which
is why the plugin reconciles against whatever you currently quote and reports the gap. Finding
that gap before the board meeting rather than during it is the highest-value thing here.

---

## What it produces

```
./gtm-agents/executive-reporting/<timestamp>/
    raw/            exactly what came back from the CRM
    findings.json   machine-readable, including every row the tables truncate
    report.md       the pack as text
    report.html     the pack — self-contained, opens locally
    manifest.json   provenance and per-source record counts
```

The report carries:

- **Reporting Readiness** — every component, weight and subscore, with the sentence that
  produced it. Auditable, not a vibe.
- **The metric spine** — plan attainment, a rolling 13-month window (13, not 12, so this month
  sits beside the same month last year), and the funnel with any impossible rate flagged.
- **Conversion** — the definition in full, every cohort, and which were suppressed.
- **Channel and owner views** — with quadrant dividers set to *this portfolio's* blended
  conversion and mean bookings, never a fixed 50%. A fixed split routinely puts the best
  channel in the "low quality" quadrant.
- **Concentration** — how much of the book sits in how few accounts.
- **Reconciliation** — measured against what leadership quotes today, with the delta.
- **A drafted executive email** to send with the pack.

---

## Install

```
/plugin marketplace add ./leanscale-gtm-agents
/plugin install executive-reporting@leanscale-gtm
/executive-reporting:setup     # reads your CRM first, then asks what it couldn't work out
/executive-reporting:run
```

Setup is idempotent — re-run it any time as a health check.

## What it needs

| Capability | Required | Used for |
|---|---|---|
| `crm.query` | yes | Deals, accounts, owners |
| `crm.describe` | recommended | The stage picklist and field metadata |

Salesforce and HubSpot are both first-class. No conversation-intelligence tool is required.

## Configuration

Config lives in `~/.leanscale-gtm/` and survives plugin updates. `profile.json` holds the org
facts shared across every LeanScale agent; `executive-reporting.json` holds this plugin's
settings. Every key has a `_help` line — see `config.example.json`.

The two settings worth reading before your first run:

- **`stage_map`** — maps *your* stage values onto canonical keys. Setup proposes it from your
  real picklist; confirm it carefully, because everything downstream depends on it.
- **`believed_conversion`** / **`believed_metrics`** — what leadership quotes today. Leave them
  empty and the pack cannot tell you where it disagrees with the last board deck.

## No filters, on purpose

The pack ships with a date filter and nothing else. Every filter beyond that hands one
executive a number nobody else in the room is looking at. If they want it by rep and by
territory, those are cards, not controls — set them in `segments`.
