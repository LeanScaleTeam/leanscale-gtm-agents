---
name: setup
description: >-
  One-time (re-runnable) setup for the sales-coach plugin. Probes which transcript source is
  connected, inventories the calls actually available and over what date range, detects the rep
  roster, then asks only what a human can answer — the qualification framework (MEDDPICC,
  MEDDIC, BANT, SPICED, Challenger, Command of the Message, or a custom one captured in full),
  call types, rep start dates, mechanics targets, and two or three exemplar "good" calls — and
  finishes by scoring one real call end to end. Use when the user says "set up sales coach",
  "configure call coaching", "connect Gong/Fireflies/Zoom/my transcripts", or when
  /sales-coach:run reports missing config. Doubles as the health check when a run later fails.
argument-hint: "[--reconfigure] [--source gong|fireflies|chorus|grain|otter|zoom|google_drive|local_directory]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# Sales Coach — setup

Run in this order. **Discover before you ask.** Every question you ask that the connected
systems could have answered makes the product feel dumb; every question you fail to ask
that changes the analysis makes the output wrong.

Setup is idempotent — re-running it is also how you diagnose a failed run.

---

## 0. Locate this plugin

Everything below runs this plugin's scripts through a small shim at
`~/.leanscale-gtm/bin/sales-coach`. Create it before anything else — nothing later works without it.

`AGENT_ROOT` is this plugin's own directory: the one containing `scripts/`, `skills/` and
`.claude-plugin/`. Inside Claude Code, `${CLAUDE_PLUGIN_ROOT}` already holds it. On Cursor,
VS Code, Codex CLI or Gemini CLI that variable does not exist — use the directory you loaded
this SKILL.md from, two levels up from `skills/setup/`.

If the agents were installed with `tools/install-skills.py` (the non-plugin path), this is
already done — skip to the confirmation below. Otherwise:

```bash
AGENT_ROOT="${CLAUDE_PLUGIN_ROOT:-<the directory this plugin was loaded from>}"
python3 "$AGENT_ROOT/scripts/lib/config.py" install-shim --plugin sales-coach --root "$AGENT_ROOT"
```

It verifies the directory really is a plugin root, records it in
`~/.leanscale-gtm/sales-coach.json`, and writes the shim. If it answers *"does not look like a
plugin root"*, the path is wrong — fix it now rather than debugging a later step.

Confirm it works before continuing:

```bash
"$HOME/.leanscale-gtm/bin/sales-coach" --root
```

Re-running this is safe, and is the first thing to try if a run later fails with a missing
script — a plugin update moves the install and the recorded path goes stale.

---

## 1. Probe what is actually connected

Required capabilities: `transcripts.*`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("transcripts meetings recordings calls")
    ToolSearch("get transcript")
    ToolSearch("gong calls")           ToolSearch("fireflies transcript")
    ToolSearch("chorus engagement")    ToolSearch("grain recordings")
    ToolSearch("zoom recordings")      ToolSearch("drive search files read file content")
    ToolSearch("run_soql_query salesforce")
    ToolSearch("hubspot crm search")

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    transcripts.*  any vendor  gong / fireflies / chorus / grain / otter / zoom list+get transcript tools
                   fallback    docs.read over a folder of exported transcripts — no vendor is required

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


Report what each resolved tool provides, mapped to a capability:

| Capability | Required? | Resolved tool | Verdict |
|---|---|---|---|
| `transcripts.list` | **required** | … | … |
| `transcripts.get` | **required** | … | … |
| `crm.query` | optional | … | … |

**A conversation-intelligence platform is not required.** If nothing resolves, the local
directory path is a genuine first-class option — offer it in the same breath, not as a
consolation: *"Nothing is connected, and that is fine. Export your calls to a folder —
`.vtt`, `.txt`, `.md` or `.json` all work — and point me at it."*

Probe failures must be specific. Not "Gong not available" but *"Gong tools resolve and
`list_calls` returns 200, but only 3 calls came back for a 90-day window across a
14-person team — the connected token is almost certainly scoped to one user. A manager
needs workspace-level API scope."*

---

## 2. The shared org profile

```bash
cat ~/.leanscale-gtm/profile.json 2>/dev/null
```

**If it exists:** show it back in one paragraph and ask only whether anything changed. Do
not re-interrogate someone about their fiscal year for the fifth time.

**If it does not exist,** you are the first agent in the suite this customer has run, so
create it. Discover first:

- Salesforce: `SELECT Name, FiscalYearStartMonth, DefaultCurrencyIsoCode FROM Organization`
- Salesforce segment picklist: describe `Account`, read the values of the segment field
- Closed-won amount distribution, for the material deal floor:
  ```sql
  SELECT Amount FROM Opportunity
  WHERE IsWon = true AND CloseDate = LAST_N_DAYS:365 AND Amount != null
  ORDER BY Amount ASC
  ```
  Propose the 10th percentile as `material_deal_floor` and confirm it.
- HubSpot: the fiscal year is an account setting the API does not expose reliably — ask,
  and say why you are asking.

Then ask only these: org name (confirm from CRM), **quota-carrying reps** (ask directly —
never infer it from headcount; it is the most load-bearing number in the suite),
competitors to track, and whether reports should redact person names
(`redact_pii_in_reports`). Write it with the schema in SPEC §2 and show the customer the
file you wrote.

---

## 3. Automatic discovery — do this before asking anything in §4

### 3a. What calls actually exist

Pull a 90-day window from whichever source resolved and report:

- **How many calls, and over what date range.** If the earliest is 40 days old on a
  90-day request, say so — that is a retention limit or a scope limit, and it changes the
  window they should pick.
- **Calls per person**, so the roster comes out of the data:

  ```
  Dana Whitfield   dana@acme.com     23 calls
  Priya Raman      priya@acme.com    19 calls
  Marcus Oyelaran  marcus@acme.com    6 calls   ← joined recently?
  ```
- **Call titles clustered into types.** Count how many contain "discovery"/"intro",
  "demo", "technical"/"security", "QBR", "renewal", "negotiation". Show the distribution
  and how many match nothing — that last number decides whether call type has to be
  inferred rather than read.
- **Duration distribution.** Median, and how many are under five minutes (those are
  reschedules and get filtered out).
- **Which internal domains appear** in participant emails. Show them and confirm:
  *"I see `acme.com` and `acme-eu.com` on your side — is the second one yours?"*

### 3b. Attribution dry run — the one that saves the deployment

Take three calls, normalize them, and report what happened:

```bash
mkdir -p /tmp/sc-probe/raw/transcripts   # write calls.json + 3 transcripts here
"$HOME/.leanscale-gtm/bin/sales-coach" transcripts normalize --raw /tmp/sc-probe/raw \
  --internal-domain acme.com
```

You are looking for `attribution=high` on all three. If any come back `medium` or `low`,
tell the customer now, in plain words, and what it costs: *"Your Zoom exports carry
speaker names but no emails, so I can only tell who is internal by matching names against
the roster. Anyone who joins from a shared room mic will come back unidentified, and I
will exclude those calls from talk-time rather than guess."*

Also run the parser tests, so an unusual export layout surfaces during setup rather than
three weeks later:

```bash
"$HOME/.leanscale-gtm/bin/sales-coach" transcripts selftest
```

### 3c. CRM discovery, if a CRM is connected

- Opportunity owners with recent activity — cross-check against the roster from 3a:
  ```sql
  SELECT Owner.Name, Owner.Email, COUNT(Id) opps
  FROM Opportunity WHERE CreatedDate = LAST_N_DAYS:180
  GROUP BY Owner.Name, Owner.Email ORDER BY COUNT(Id) DESC
  ```
- **Rep start dates — look before you ask.** Describe `User` and look for a date field
  named like `Start_Date__c`, `Hire_Date__c`, `Employment_Start__c`:
  ```sql
  SELECT Name, Email, Start_Date__c FROM User WHERE IsActive = true AND UserType = 'Standard'
  ```
  If one exists, show the roster with tenure already filled in and just ask for
  confirmation. If not, ask for start dates — but only for the reps who actually appear in
  the calls.
- **Close-date push tracking**, for deal linkage: describe `Opportunity` and look for
  `Original_Close_Date__c`, `Close_Date_Push_Count__c`, or a history-tracked `CloseDate`.
  Report what you found; without it a still-open deal reads as `open` rather than
  `slipped`, which is the safe direction to be wrong in.
- HubSpot equivalents: `owners` API for the roster, `deals` search for volumes,
  `hs_date_entered_*` stage-entry properties for slippage.

---

## 4. The interview

Ask these, phrased against what you found in §3. Ask them in batches, not one at a time.

### 4a. The framework — the question that decides whether this is worth paying for

> "Which qualification framework does your team actually work to?
> MEDDPICC · MEDDIC · BANT · SPICED · Challenger · Command of the Message · your own."

If they name a built-in, load its dimensions from `config.example.json`, **show them the
list with each dimension's `evidence_rule` and `met_means`,** and ask what they would
change. Most teams have one house rule ("a job title with no name is not an economic
buyer") — capture it in `met_means`. That one edit is what makes the scores theirs.

**If they say custom, capture it properly.** A one-line name is useless; you need enough
that a scorer can be consistent without the manager in the room:

1. "What are the stages or dimensions, in your own words and your own order?"
2. Ask for the enablement doc, one-pager or scorecard if one exists — read it rather than
   re-deriving it. `ToolSearch("read file content drive")`, or have them paste it.
3. For **each** dimension, get two things and write them down verbatim:
   - **evidence_rule** — what counts as proof it happened on a call?
   - **met_means** — where is the line between "met" and "partial"?
   Push for a real answer with a worked example: *"Give me a sentence a buyer might say
   that you would count, and one that you would not."*
4. For each dimension, ask: **fundamental or advanced?** Frame it as: *"If a rep is ten
   weeks in, is this one of the things you would coach first, or one you would leave until
   they have the basics?"* This is what makes the coaching tenure-aware.
5. Read the whole thing back and get an explicit yes before writing it.

Whatever they choose, say plainly what it buys them: *"Every score in the report will cite
your definition, not a generic rubric — that is the difference between this and the
conversation-intelligence tool you may already own."*

### 4b. Call types to coach

> "You have 71 discovery calls, 44 demos, 12 that look technical and 31 I could not
> classify. Which of these should be coached? Coaching a demo against discovery criteria
> produces scores nobody believes."

Also confirm how to infer type when the title does not say — position in the deal, the
calendar invite, or ask them to accept "unknown" being skipped.

### 4c. Tenure

> "Here is the roster from your calls, with start dates from Salesforce where I found them.
> Fill in the blanks — a rep ten weeks in gets coached on fundamentals, one at three years
> gets coached on decision process and paper process, and I want to get that split right."

Confirm the bands too: ramping under 90 days, developing under a year, tenured beyond —
and ask whether their own ramp definition differs.

### 4d. Mechanics targets — state the defaults as defaults

> "For talk ratio and question rate I use industry defaults, and the report labels them
> that way: your side under **55%** of speaking time on a discovery call, longest
> uninterrupted stretch under **150 seconds**, at least **8 questions per 30 minutes**,
> **90%** of calls ending with a dated next step, and pricing flagged if it comes up in the
> first **25%** of a call. Do you have your own numbers, or shall I use those and label
> them as defaults?"

If they give their own, set `mechanics_targets.source` to the name of their methodology so
the report stops calling them industry defaults.

### 4e. Audience

> "Who is this for? Default is **manager**: one team pattern report, no per-call reviews.
> The alternative also writes a one-page card per rep. I default to manager because reps
> ignore per-call feedback and managers act on patterns — but if you want to hand
> something to the team, I will write both."

### 4f. Exemplars — what "good" looks like

> "Give me two or three calls you consider genuinely good — the ones you would play for a
> new hire. I score those first and show you how the framework rated them. If they do not
> score better than everything else, that is worth knowing on run one: it means the
> framework is not measuring what you actually value."

Get the call IDs (or titles you can resolve to IDs) and write them to `exemplar_call_ids`.
Do not skip this. It is the cheapest credibility you will ever buy.

### 4g. Cadence and window

> "How often will you run this — weekly, fortnightly, monthly? And how far back should
> each run look?" Monthly with a 30-day window suits most teams; a rep doing four calls a
> month needs 90 days before the numbers mean anything. Check the per-rep counts from §3a
> and recommend accordingly.

---

## 5. Write the config

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/sales-coach" --root)"
mkdir -p ~/.leanscale-gtm
cp "$AGENT_ROOT/config.example.json" ~/.leanscale-gtm/sales-coach.json
```

Then edit it with the answers. Keep every `_help` key — customers edit this file by hand.
**Show them the file you wrote** and point at the three keys they are most likely to want
to change later: `framework.dimensions`, `mechanics_targets`, `exemplar_call_ids`.

---

## 6. Smoke test — score one real call, end to end

A setup that ends without proving output is not done.

1. Fetch **one real call** (an exemplar if you have one) using the §1 adapter from the run
   skill, into `/tmp/sc-smoke/raw/`.
2. Normalize it and report the attribution verdict.
3. Score it against the configured framework, with quotes and timestamps.
4. Run the pipeline:

   ```bash
   "$HOME/.leanscale-gtm/bin/sales-coach" analyze --run-dir /tmp/sc-smoke --no-baseline
   "$HOME/.leanscale-gtm/bin/sales-coach" report  --run-dir /tmp/sc-smoke
   ```

   `--no-baseline` so the smoke test does not become the baseline the customer is measured
   against.
5. **Show them the scorecard for that call in chat** — every dimension, its status, and
   the quote with its timestamp. Then ask the only question that matters:

   > "Does this match how you would have scored this call? If not, tell me which dimension
   > I got wrong and why — that is a config change, not an argument."

   If they disagree, edit the relevant `evidence_rule` / `met_means` and re-score the same
   call in front of them. Do this until they agree. A framework the manager does not
   recognise produces a report the team will not accept.

If no live source is connected yet, run the bundled offline sample instead so they can see
the shape of the output — and say clearly that it is fictional sample data:

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/sales-coach" --root)"
export LEANSCALE_GTM_HOME=/tmp/sc-demo-home && mkdir -p $LEANSCALE_GTM_HOME
cp "$AGENT_ROOT/fixtures/profile.json" $LEANSCALE_GTM_HOME/profile.json
cp "$AGENT_ROOT/fixtures/config.json"  $LEANSCALE_GTM_HOME/sales-coach.json
"$HOME/.leanscale-gtm/bin/sales-coach" analyze --run-dir /tmp/sc-demo \
        --raw "$AGENT_ROOT/fixtures/raw"
"$HOME/.leanscale-gtm/bin/sales-coach" report  --run-dir /tmp/sc-demo
open /tmp/sc-demo/report.html
```

---

## 7. Pass/fail table and the honest summary

End with this table and a plain-English statement of what will and will not work.

| Check | Status | What it means | To fix |
|---|---|---|---|
| Transcript source reachable | ✅ / ❌ | … | … |
| Calls found in a 90-day window | ✅ 137 | … | … |
| Speaker attribution | ⚠️ medium | Zoom exports carry no emails; internal speakers matched by roster name | Add every internal domain and rep to config; or move to a source with an affiliation flag |
| Rep roster complete | ✅ 8 of 8 | … | … |
| Start dates captured | ⚠️ 6 of 8 | Two reps will be coached without tenure weighting | Add `start_date` for the two named reps |
| Framework captured | ✅ MEDDPICC + 2 house rules | … | … |
| Exemplar calls nominated | ✅ 3 | … | … |
| CRM connected (deal linkage) | ❌ | The report cannot correlate gaps against deals that slip or lose | Connect Salesforce or HubSpot and re-run setup |
| Close-date push tracking | ⚠️ | Open deals will read as `open`, never `slipped` | Add `Original_Close_Date__c` or enable field history on `CloseDate` |
| Smoke test scored a real call | ✅ | … | … |

Then, in prose, the two or three sentences that matter: what this will tell them on run
one, what it cannot tell them yet, and the single highest-value fix. Finish with:

> "Run `/sales-coach:run` when you want the first real pass. Run one is your baseline —
> the comparison starts on run two."
