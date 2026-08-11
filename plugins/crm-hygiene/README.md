# CRM Hygiene

**This plugin is read-only. It never creates, updates, merges or deletes a single record in
your CRM, and it has no write capability to turn on.** Every query it issues is a `SELECT` or
a `GET`. Every duplicate it finds is presented as a *candidate for human review*, never as a
merge. Reports are written to a folder in your working directory and stay on your machine —
nothing is uploaded, hosted, or phoned home. If your organisation has an AI-governance
review, this is the paragraph to send them.

It audits Salesforce or HubSpot and answers the question a VP of RevOps cannot answer from
inside the CRM UI: *how much of what we are looking at is real?* Duplicate accounts and
contacts, custom fields nobody has filled in since 2023, "required" fields the schema does
not require, open deals owned by people who left, pipeline that closed three months ago and
is still in the forecast, contacts with no account, picklist values that mean the same thing,
and duplicate rules that have been switched off the whole time.

It produces a **Hygiene Index from 0 to 100** and a severity-ranked findings report. Run one
sets the baseline; every run after it shows what moved.

---

## What you get

```
./gtm-agents/crm-hygiene/2026-08-10-1402/
    raw/            what came back from your CRM, unmodified
    manifest.json   every source, the tool that read it, and the record count
    findings.json   machine-readable findings, scores and full detail sections
    report.md       the findings, in markdown
    report.html     one self-contained file — opens offline, forwards to a CFO
```

A finding looks like this — count, sample record ids, and the exact query that produced them,
so it can be verified in the CRM in under a minute:

> **[CRITICAL] 21 open opportunities have a close date in the past**
>
> 21 opportunities are still open with a close date that has already passed, carrying
> $1,540,000. The median is 134 days overdue; the worst is 234 days.
>
> **Why it matters.** This is the number an executive spots in thirty seconds, and it
> invalidates every period-based report at once: pipeline created, coverage, conversion by
> close month, quarterly forecast.
>
> **Fix.** Give every owner their list this week with three options: push the date with a
> reason, close it lost, or explain why it is still live. Then add a validation rule blocking
> a past close date on save.
>
> *21 records affected · Effort: quick · Owner: RevOps*
> `0068e0000000167AAE` `0068e0000000168AAE` `0068e0000000169AAE` …
> ▸ Verify this yourself — the exact query

Five headline scores sit above the findings: the Hygiene Index, records sitting in duplicate
clusters, dead custom fields, pipeline dollars past their close date, and policy-field
compliance. From run two onward each carries a delta.

---

## The Hygiene Index, in full

A score whose derivation is hidden is a score nobody trusts, so here is all of it. Six
pillars, each a measured **clean rate** between 0 and 1, combined as a weighted average:

```
Hygiene Index = 100 × Σ(weight × clean_rate) / Σ(weight)      over measurable pillars only
```

| Pillar | Weight | Clean rate = 1 − (defects ÷ denominator) |
|---|---:|---|
| **Duplicates** | 20 | accounts + contacts sitting in any duplicate cluster ÷ all accounts + contacts read |
| **Field discipline** | 15 | custom fields that are never-populated or under the fill threshold ÷ all measurable custom fields |
| **Policy compliance** | 20 | *not* a defect ratio — the mean fill rate of the fields you declared policy-required, across in-scope records |
| **Ownership** | 15 | records owned by a deactivated user or by nobody ÷ all owned records (open opps, accounts, contacts, open leads) |
| **Pipeline freshness** | 20 | open opportunities that are past close, stale, older than two win cycles, missing an amount, or dated implausibly ÷ all open opportunities |
| **Structural integrity** | 10 | orphans — contacts with no account, accounts with no contacts, opps with no account, open opps with no contact roles ÷ records checked |

Rules that keep it honest:

- **Every clean rate is clamped to 0–1** and rounded once, at the end.
- **A pillar with no denominator is dropped**, and the remaining weights renormalize. If your
  connector cannot read field metadata, field discipline is excluded rather than scored zero
  — and the report names every excluded pillar under the score, so a partial run is never
  mistaken for a good one.
- **A record is counted once per pillar**, not once per finding. An account that is both a
  domain duplicate and a name duplicate is one defect in the duplicates pillar.
- **Governance is deliberately not a pillar.** Duplicate rules, validation rules and record
  types are binary switches, not rates; letting one toggle swing the score ten points would
  make the trend line useless. Those findings are reported at full severity — they are just
  not in the arithmetic.
- The weights live in `~/.leanscale-gtm/crm-hygiene.json` under `hygiene_index_weights` and
  are yours to change. Change them once, then leave them alone: a score computed under
  shifting weights is not a trend.

The report renders the full derivation table — pillar, weight, clean rate, points earned, and
the counts behind it — so the number can be rebuilt by hand.

---

## Every check it runs

**Duplicates** (surfaced as review candidates, never merged)
- accounts sharing a corporate email domain — free-mail domains excluded, and clusters linked
  by parent/child are excluded as legitimate hierarchies and reported separately
- accounts colliding on a normalized company name (`Acme, Inc.` = `ACME LLC`), with a count of
  how many the domain check missed because the website field is empty
- contacts sharing an exact email address, including HubSpot's `hs_additional_emails`
- the same person name twice on one account — the duplicates an email merge leaves behind
- two open opportunities with the same name on the same account, with the pipeline dollars at risk
- an open **lead whose email is already a contact** — the same human, two objects, two owners

**Fields**
- custom fields **never populated** in the window, with API names, types and the fill basis
- custom fields **under the fill threshold** (default 5%) — worse than empty, because they
  produce charts that look real
- **policy-required fields that are blank**, per object, scoped to open records plus anything
  created in the window
- **policy-required fields the schema does not enforce** — the gap that causes everything else
- policy-required fields configured in your config that **do not exist in the CRM**

**Ownership**
- open opportunities, accounts, contacts and open leads owned by **deactivated users**, with
  the pipeline dollars stranded and a per-departed-user breakdown
- records with **no owner at all**
- **owner concentration** — one user holding an outsized share of the account book, which is
  almost always an integration user or a departed rep's undistributed territory

**Freshness**
- open opportunities with a **close date in the past**, with median and worst days overdue
- open opportunities with **no activity** past your threshold, split by whether the deal is
  above your material floor, and how many carry no activity date at all
- open opportunities **older than twice this org's own median closed-won cycle** — measured
  from your history, not a benchmark
- open opportunities with an **implausible close date** (placeholder dates far in the future)
- **closed** opportunities dated **in the future**, which land bookings in the wrong period
- open opportunities with **no amount**
- **stale open leads** that were never disqualified, by owner

**Structure**
- contacts with **no account**, and how many are recoverable by email domain
- accounts with **open pipeline and zero contacts** — a deal with no known buyer
- accounts with no contacts at all
- **closed-won accounts with no contacts** — customers you cannot name a single person at
- open opportunities with **no account**
- open opportunities with **zero contact roles** (HubSpot: zero associated contacts), plus the
  single-threaded count
- contacts with **no email**, with a **malformed email**, or on an account whose **domain they
  do not share** (the signature of a merge that went to the wrong parent)
- accounts with **no usable website domain** — the join key for the entire GTM stack

**Picklists**
- values defined but **unused** in the window
- values that **differ only in case or punctuation** (`Enterprise` / `enterprise`)
- values present **on records but not in the active value set** — retired stages that quietly
  drop records out of every filtered report

**Governance**
- duplicate rules **inactive**, or absent entirely
- validation rules **inactive** — someone built the fix and it was switched off
- record types with **no records** in the window
- HubSpot: **archived pipelines and stages that still hold deals**

---

## Both CRMs, for real

| | Salesforce | HubSpot |
|---|---|---|
| Records | SOQL via `run_soql_query` | `GET /crm/v3/objects/{object}` with associations |
| Field metadata | `FieldDefinition` per object | `GET /crm/v3/properties/{object}` |
| Exact fill rates | aggregate SOQL, `COUNT(field)` batched ~25 at a time | `HAS_PROPERTY` search, read `total` |
| Picklist values | describe, `StandardValueSet`, Tooling `CustomField`, or `retrieve_metadata` | `results[].options[]` + the pipelines API |
| Picklist usage | `GROUP BY` | `EQ` search, read `total` |
| Schema-required | `IsNillable = false` | **does not exist** — required is a form/workflow property |
| Duplicate rules | `DuplicateRule` + `MatchingRule` | **no public API** |
| Validation rules | Tooling `ValidationRule` | **no equivalent object** |
| Contact roles | `OpportunityContactRole` | deal↔contact associations; labels are the closest analogue |
| Record types | `RecordType` + usage | deal pipelines and stages |

The exact queries and payloads are written out in `skills/run/SKILL.md` — copy-pasteable, not
"adapt as needed". Where a check cannot run on your CRM, it appears in the report's
**unavailable** list with the reason, because a missing connector must never read as a pass.

---

## Usage

```
/crm-hygiene:setup     once — probes, reads your org, asks ~10 questions, proves the pipeline
/crm-hygiene:run       the audit
```

Arguments to `run`: `--window 365d`, `--objects Account,Contact,Opportunity`, and `--quick`
to skip the exact fill-rate and picklist-usage sweeps (seconds instead of minutes, at the cost
of sampled rather than exact field counts).

Monthly is the usual cadence. Run one is a baseline — the report says so — and run two is
where the tool starts earning its slot.

### Trimming the report

A first run against a four-year-old org routinely produces forty findings. That is the point
of an audit, but if you want a shorter list for a meeting, raise `min_finding_count` in
`~/.leanscale-gtm/crm-hygiene.json`; anything below that count is suppressed. Fields you have
already triaged go in `known_dead_fields` — still counted in the index, no longer reported.

### Reading the counts correctly

Record-level findings (duplicates, ownership, staleness, orphans) count the records that were
fetched. Field fill rates and picklist usage come from aggregate queries over the whole
window, so those counts can be larger than the sample. Every finding carries a `basis` in its
evidence saying which it is, and the report's method table shows exactly what was read.

---

## Requirements

- Claude Code with a connected **Salesforce or HubSpot MCP server**
- Read access to Account/Company, Contact, Opportunity/Deal, User/Owner, and field metadata
  (Salesforce: `FieldDefinition`; HubSpot: the `crm.schemas.*.read` scopes)
- Python 3.9+ — standard library only, no packages to install, no network access

Optional but worth granting: Salesforce Tooling API read for validation rules, and
`OpportunityContactRole` read for the contact-role checks.

## Where things live

| | |
|---|---|
| Config | `~/.leanscale-gtm/crm-hygiene.json` — survives plugin updates, edit by hand |
| Shared org profile | `~/.leanscale-gtm/profile.json` — written once, read by every LeanScale GTM agent |
| Baselines | `~/.leanscale-gtm/baselines/crm-hygiene/` — never pruned; this is your evidence trail |
| Reports | `./gtm-agents/crm-hygiene/<timestamp>/` in your working directory |

## Testing it without touching your CRM

Two complete offline fixtures ship with the plugin — one Salesforce-shaped, one HubSpot-shaped
— with synthetic data that fires every check:

```bash
python3 scripts/analyze.py --raw fixtures/raw --out /tmp/x
python3 scripts/report.py  --findings /tmp/x/findings.json --out /tmp/x

python3 scripts/analyze.py --raw fixtures/raw-hubspot --out /tmp/h \
        --config fixtures/config.hubspot.json
python3 scripts/report.py  --findings /tmp/h/findings.json --out /tmp/h
```

Fixture runs print a `FIXTURE MODE` banner and never write a baseline, so they cannot
contaminate a real trend line.
