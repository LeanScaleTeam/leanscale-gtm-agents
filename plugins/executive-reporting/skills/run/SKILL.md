---
name: run
description: >-
  Build the executive GTM reporting pack from Salesforce or HubSpot — bookings, created
  pipeline, coverage, cohort conversion, retention and concentration, each against a goal and
  each backed by the rows underneath. Audits reporting readiness first and withholds any rate
  it cannot defend. Read-only. Trigger on "/executive-reporting:run", "build the board pack",
  "executive reporting pack", "what do I show the board", "monthly reporting pack", "our
  dashboards disagree", "why is our conversion rate different every time", "build exec
  dashboards", "QBR numbers", or any request for the numbers leadership runs the business on.
argument-hint: "[--window 13] [--force] [--section all|readiness|spine|channels]"
allowed-tools: Read, Write, Bash, Glob, Grep, ToolSearch
---

# Executive Reporting — run

**Read-only.** This skill queries the CRM and writes files on this machine. It never creates,
updates or deletes a record, and it never uploads anything anywhere. The pack is a local file.

**Audit first, then publish.** If the underlying data cannot support the numbers, this run says
so and withholds the headlines rather than printing them prettily. Publishing on top of bad
data does not hide the mess — it just puts your name on it.

---

## 0. Before anything

1. Read `~/.leanscale-gtm/profile.json` and `~/.leanscale-gtm/executive-reporting.json`.
   **If either is missing, stop and tell the user to run `/executive-reporting:setup` first.**
   Do not guess a stage map, a fiscal calendar or a target — each one changes a headline.
2. Echo the assumptions in one block: org, CRM, fiscal year start month, window, conversion
   basis, ripeness, pipeline stage, amount field, recurring cadence, who owns expansion, goal
   level, and whether targets are configured at all.
3. Create the run directory:

```bash
RUN="./gtm-agents/executive-reporting/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw"
```

## 1. Probe

```
ToolSearch("run_soql_query salesforce")   → Salesforce path
ToolSearch("hubspot crm search deals")    → HubSpot path
```

Use whichever matches `profile.crm.system`. If the expected tool does not resolve, say exactly
that and stop — never silently fall back to the other CRM.

## 2. Fetch — write each result to `$RUN/raw/<name>.json`

Write raw results **unmodified**, as `{"records": [...]}`. Do not reshape, filter or fix data
here; `analyze.py` needs to see the defects in order to report them.

### `opportunities.json` — required

Every deal created **or** closed inside the window. Both, not either — a deal created 14 months
ago and won last month belongs in the bookings series, and a deal created last month that is
still open belongs in the cohort.

**Salesforce**

```sql
SELECT Id, Name, AccountId, Account.Name, StageName, Amount, CloseDate, CreatedDate,
       IsClosed, IsWon, Type, LeadSource, OwnerId, Owner.Name,
       Account.Industry, Account.Segment__c
FROM Opportunity
WHERE CreatedDate = LAST_N_MONTHS:13 OR CloseDate = LAST_N_MONTHS:13
```

**HubSpot** — `POST /crm/v3/objects/deals/search` with a `createdate` OR `closedate` range
filter, requesting `dealname, dealstage, amount, closedate, createdate, hs_is_closed_won,
hs_analytics_source, hubspot_owner_id, pipeline`, paging until exhausted.

Normalise field names into the shape `analyze.py` reads — it accepts either CRM's naming, but
be explicit where you can: `stage`, `amount`, `created_date`, `close_date`, `owner_name`,
`channel`, `account_name`, `segment`, `type`.

### `stage_metadata.json` — optional but recommended

The stage picklist with labels and sort order. Used for provenance in the report; the maths
routes through the confirmed `stage_map` in config, never through these labels.

### `accounts.json` — optional

Customer accounts with recurring revenue, for the concentration view. Include accounts with a
**negative** balance if any exist — excluding them makes the account list stop reconciling to
the book total, which is worse than showing them.

### `goals.json` — optional

Targets by period, if they live in the CRM rather than in config.

**Record counts as you go.** If a required source returns zero records, stop and diagnose —
do not proceed to produce a clean-looking empty pack.

## 3. Analyze and render

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" --run "$RUN"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --run "$RUN"
```

`analyze.py` exits 3 with `ABORT:` if a required source is empty or the stage map is missing.
Surface that message verbatim — it names the fix.

## 4. Read the output before you summarise it

Open `findings.json` and lead your summary with these, in this order:

1. **Reporting Readiness score and band.** If it is below 55 the headlines were withheld.
   Say that first, plainly, and do not paraphrase it into something softer. The customer can
   override with `--force`, and you should tell them that — and tell them what it costs.
2. **Any withheld conversion rate.** A rate above 100% means a stage is not being stamped.
   Explain that it is a data-capture defect, not performance, and resist the urge to "fix" it
   by picking a different denominator.
3. **The reconciliation table.** If their quoted number and the measured number differ, that
   gap is the most important thing on the page. It should be walked through privately with the
   sponsor before the pack circulates — never discovered live in the review.
4. **Critical and high findings**, with the evidence attached.

## 5. Hand it over

Point them at the three files, and say what each is for:

- `report.html` — the pack. Self-contained, opens locally, nothing is uploaded.
- `report.md` — the same content as text, for pasting into a doc or a ticket.
- `findings.json` — the machine-readable version, including every row the tables truncated.

The report ends with a **drafted executive email**. Tell them it is there, and tell them to
keep the reconciliation paragraph even if they rewrite the rest — that is the paragraph that
stops a definition change from reading as a performance change.

**Baseline.** The first run is the baseline and says so. From run two onward every score
carries a delta. If the customer wants to prove the reporting improved, they need run one to
exist — so run it before the fixes, not after.
