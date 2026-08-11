# Customer Health

**This plugin is read-only.** It reads your CRM, your call transcripts and your shared
message threads, and it writes nothing back — not a field, not a note, not a task. Every
output is a local file in your working directory. Nothing is uploaded, nothing is deployed,
and there is no telemetry. The only network calls it makes are to the MCP servers you already
connected, plus public web search when it researches your customers' companies.

---

## What it does

It scores every customer account **twice**, and keeps the two scores apart.

**Sentiment (0–100, higher is better)** — how the relationship feels. Composed from Claude's
reading of your actual calls and threads, plus behavioural facts: who still shows up, whether
the champion is still in the room, how fast they reply. Trended across the window rather than
read as a point in time.

**Commercial risk (0–100, higher is worse)** — what the paper says. Computed deterministically
from your CRM: renewal proximity against renewal status, champion employment, contract value
movement, single-threading, silence weighted by ARR, executive touch, expansion pipeline, and
support and usage where those exist.

### Why two scores and not one

The account that churns is very often the *happy* one with an unsigned renewal forty days out.
A single blended health score averages that account into the middle of the pack — 86 sentiment
and 55 risk becomes "about 70, fine" — and nobody looks at it again until the notice window
has closed.

So the report leads with the quadrant:

|  | **Commercially at risk** | **Commercially safe** |
|---|---|---|
| **Happy** | **Happy but exposed** — the quadrant that churns | Healthy |
| **Unhappy** | Burning — everyone already knows | Grumbling but safe |

Accounts with no conversation source sit **outside** the grid, labelled unmeasured. They are
never quietly counted as healthy.

### The kickoff baseline

Setup captures a starting reading per account — sentiment, ARR, engaged contacts, the
customer's own words for what success looks like. Every report then shows movement against it.

This is the most important thing the plugin does. A health score with no baseline is a vibe.
At renewal you will be asked what changed since you started, and *"they're at 64"* is not an
answer — *"they started at 41 and they're at 64"* is. Accounts without a baseline are listed
in every report, by name, as unprovable rather than fine.

### Deep company research

On every run it researches each customer's own business: funding stage, months since the last
raise, layoffs, restructuring, exec changes, M&A. A champion's departure or a down round will
end a contract that has flawless sentiment and a flawless delivery record, and no amount of
analysis of your own call transcripts will ever surface it.

---

## What it reads

| Source | Required | Used for |
|---|---|---|
| CRM (Salesforce or HubSpot) | **yes** | accounts, renewals, contracts, contacts, activity, expansion |
| Call transcripts | no | sentiment, attendance decay, champion engagement |
| Shared channels / mailbox | no | sentiment, response latency, silence |
| Support desk | no | ticket volume and severity |
| Product usage | no | usage decline |
| Public web | no | funding, layoffs, exec changes, M&A |

**Salesforce and HubSpot are both first-class.** Actual SOQL and actual HubSpot search
payloads are written into the run skill for every shape contract data takes: renewal
opportunity, Contract object, custom subscription object, or dates sitting on the account.

**No conversation-intelligence tool is required.** Roughly six in ten organisations this size
do not have one, so a transcript folder, a mailbox, a shared channel and a manual paste are all
supported paths. When no conversation source exists, the plugin produces the full
commercial-risk half and marks sentiment as **unavailable, not clean** — in the headline, in
the quadrant, and in a named list of the accounts it could not hear.

---

## Install and run

```
/plugin marketplace add ./leanscale-gtm-agents
/plugin install customer-health@leanscale-gtm

/customer-health:setup      # probe, discover, interview, capture kickoff baselines, smoke test
/customer-health:run        # score the book and write the report
```

Output lands in `./gtm-agents/customer-health/<YYYY-MM-DD-HHMM>/`:

```
raw/            exactly what came back from each source
findings.json   machine-readable result
report.md       the findings doc
report.html     self-contained, opens locally, survives being forwarded to a CFO
manifest.json   provenance, per-source record counts, failures
```

Config lives in `~/.leanscale-gtm/` and survives plugin updates. See `SETUP.md`.

---

## Sample output

From a run against the bundled fixtures:

```
Accounts at commercial risk:   3      of 5 customers · risk score ≥ 50
ARR at risk:                   $486,000   50% of book ARR
Mean sentiment:                57.6   across 4 of 5 accounts with a conversation source
Unsigned renewals in window:   2      inside the 60-day notice window
Happy but exposed:             1      high sentiment, high commercial risk

13 findings: 3 critical · 7 high · 3 medium

| Quadrant                                  | Accounts | ARR      |
|-------------------------------------------|----------|----------|
| Happy but exposed                         | 1        | $240,000 |
| Healthy                                   | 1        | $420,000 |
| Burning                                   | 1        | $96,000  |
| Grumbling but safe                        | 1        | $64,000  |
| Outside the grid — sentiment unavailable  | 1        | $150,000 |
```

And the finding that justifies the whole design:

> **[CRITICAL] 1 account is happy and still at commercial risk ($240,000 ARR)**
>
> High sentiment, high commercial risk. Every one of these would look fine on a blended health
> score, because the good relationship averages out the bad paper.
>
> | Account | ARR | Sentiment | Risk | Renewal | Days out | What they said |
> |---|---|---|---|---|---|---|
> | Northwind Logistics | $240,000 | 86 | 55 | 2026-09-14 | 35 | *"I have already recommended you to two other VPs here. You have made me look good this year."* — Dana Whitfield, 2026-08-04 |
>
> **Why it matters.** This is the quadrant that churns. Nobody escalates a happy account, so
> nothing gets done until the renewal date arrives and the answer is a procurement process
> nobody started. The praise above is real — it is also not a renewal.

Every finding carries record IDs, a per-account evidence table and the exact query that
produced it, so anything here can be checked in your own CRM in under a minute.

---

## The two models

### Commercial risk — weighted, then floored

| Signal | Weight | What it measures |
|---|---|---|
| Unsigned renewal | **30** | Days to renewal against renewal status. Inside the notice window with no renewal record at all scores worse than an open one in negotiation. |
| Champion departure | 14 | Contact inactive, email bouncing, publicly departed, or absent from every conversation |
| External company risk | 10 | Researched funding, layoff, restructuring, M&A and exec-change events, decayed over a year |
| Silence | 9 | Days since any touch, tolerance scaled by ARR tier |
| Contract value trend | 8 | Current ARR against the kickoff baseline |
| Single-threading | 8 | Distinct engaged contacts: 1 → 100, 2 → 55, 3 → 25, 4+ → 0 |
| Executive touch gap | 7 | Days since any interaction with an executive on either side |
| Support burden | 6 | Severity-weighted tickets per $100k ARR, ranked across the book |
| Usage decline | 6 | Tracked usage metric against its baseline |
| No open expansion | 2 | A mild negative — nobody is buying more |

Two signals also act as **tripwires**, because a 30% weight cannot on its own carry an account
over the risk line and these two should: an unsigned renewal inside the notice window floors
the composite at 55 (at 80 inside half the window, or past the date), and a departed champion
floors it at 62. Weights rank the book; floors stop the ones that matter being averaged into
the middle.

### Sentiment — weighted, from judgment plus behaviour

| Signal | Weight | Source |
|---|---|---|
| Interaction tone | 40 | Claude's −2…+2 reading per moment, recency-weighted with a 30-day half-life |
| Escalation load | 20 | Share of recent interactions Claude flagged as a formal escalation |
| Champion engagement | 15 | Is the champion still in the room, and how recently |
| Senior attendance | 15 | Share of recent meetings with a senior or exec attendee, compared to the earlier half |
| Responsiveness | 10 | Median reply latency |

**There is no keyword list anywhere in this plugin.** *"That's fine"* after three failed
attempts is not neutral, and a regex cannot tell the difference — so tone and escalation are
decided by Claude while reading the actual transcript, and Python only weights, trends and
renders them.

### Missing data never lowers a score

If a signal has no data anywhere in your book, its weight is redistributed proportionally
across the signals that do, and the report names what was dropped. An organisation with no
support desk does not score safer than one that has one. And sentiment requires at least one
conversation-derived component: an account known only through a CRM bounce flag is reported as
unmeasured, not as unhappy.

---

## Safety

- Read-only. No writes to any connected system, ever.
- Nothing leaves the machine. No telemetry, no uploads, no hosted reports.
- A required source returning zero records **aborts the run** with a diagnosis rather than
  emitting a clean-looking empty report. "0 issues found" because auth failed is worse than a
  crash.
- `redact_pii_in_reports` in the shared profile pseudonymises names in `report.md` and
  `report.html`; `raw/` and `findings.json` stay unredacted on your disk.

---

Built by LeanScale. Author contact and homepage are in `.claude-plugin/plugin.json`.
