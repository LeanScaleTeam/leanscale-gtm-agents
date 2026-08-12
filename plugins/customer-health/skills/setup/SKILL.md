---
name: setup
description: >-
  One-time (and re-runnable) setup for the customer-health agent. Probes the connected CRM,
  transcript and comms tools, discovers where contract and renewal dates actually live,
  detects the CSM ownership field, then interviews the user only about what the CRM cannot
  answer — including the kickoff baseline per account. Ends with a live smoke test that scores
  one real account both ways and a pass/fail table. Use when the user says
  "/customer-health:setup", "set up customer health", "configure the health agent",
  "reconnect customer health", or when a run fails and needs diagnosing.
argument-hint: "[--reconfigure] [--accounts-only]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch, WebSearch, WebFetch
---

# Customer Health — setup

Run in this order. Do not reorder, and do not ask a question in step 4 that step 3 could have
answered. Every question you ask that the CRM could have told you makes the product feel
dumb; every question you fail to ask makes the output wrong.

This skill is **idempotent**. It is also the health check — when a run breaks, this is what
diagnoses it.

---

## 0. Locate this plugin

Everything below runs this plugin's scripts through a small shim at
`~/.leanscale-gtm/bin/customer-health`. Create it before anything else — nothing later works without it.

`AGENT_ROOT` is this plugin's own directory: the one containing `scripts/`, `skills/` and
`.claude-plugin/`. Inside Claude Code, `${CLAUDE_PLUGIN_ROOT}` already holds it. On Cursor,
VS Code, Codex CLI or Gemini CLI that variable does not exist — use the directory you loaded
this SKILL.md from, two levels up from `skills/setup/`.

If the agents were installed with `tools/install-skills.py` (the non-plugin path), this is
already done — skip to the confirmation below. Otherwise:

```bash
AGENT_ROOT="${CLAUDE_PLUGIN_ROOT:-<the directory this plugin was loaded from>}"
python3 "$AGENT_ROOT/scripts/lib/config.py" install-shim --plugin customer-health --root "$AGENT_ROOT"
```

It verifies the directory really is a plugin root, records it in
`~/.leanscale-gtm/customer-health.json`, and writes the shim. If it answers *"does not look like a
plugin root"*, the path is wrong — fix it now rather than debugging a later step.

Confirm it works before continuing:

```bash
"$HOME/.leanscale-gtm/bin/customer-health" --root
```

Re-running this is safe, and is the first thing to try if a run later fails with a missing
script — a plugin update moves the install and the recorded path goes stale.

---

## 1. Probe

Required capabilities: `crm.describe`, `crm.query`, `transcripts.*`, `comms.search`, `docs.read`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("run_soql_query salesforce")               -> crm.query
    ToolSearch("describe object metadata schema")         -> crm.describe
    ToolSearch("hubspot crm search companies deals")      -> crm.query
    ToolSearch("transcripts meetings recordings calls")   -> transcripts.*
    ToolSearch("slack search messages channel")           -> comms.search
    ToolSearch("gmail search threads outlook mail")       -> comms.search
    ToolSearch("read file content drive folder")          -> docs.read

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    crm.describe   salesforce  run_soql_query over EntityDefinition / FieldDefinition (useToolingApi where noted)
                   hubspot     hubspot-list-properties
    crm.query      salesforce  run_soql_query
                   hubspot     hubspot-search-objects / hubspot-list-objects / hubspot-batch-read-objects
    transcripts.*  any vendor  gong / fireflies / chorus / grain / otter / zoom list+get transcript tools
                   fallback    docs.read over a folder of exported transcripts — no vendor is required
    comms.search   slack       message/channel search
                   email       gmail or outlook thread search
    docs.read      drive       file search + read file content
                   local       plain filesystem reads

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


Report exactly which resolved tool provides which capability, by name. Then prove each one
works with a one-record read — a tool that resolves is not a tool that has permission.

If a probe half-works, diagnose it specifically. Never write "Slack not available." Write:
*"Slack tools resolve and `#acme-shared` exists, but reading it returned 0 messages — the
connected identity is almost certainly a bot that has not been invited to the channel."*

---

## 2. Read the shared profile

```bash
cat ~/.leanscale-gtm/profile.json 2>/dev/null || echo "no profile yet"
```

If it exists, show the user what is already known and **confirm rather than re-ask** — this
profile is shared across every agent in the suite and they should only answer it once.

If it does not exist, create it. Read what you can before asking:

- Salesforce: `SELECT Name, FiscalYearStartMonth, DefaultCurrencyIsoCode, UsesStartDateAsFiscalYearName FROM Organization`
- HubSpot: `GET /account-info/v3/details` and `GET /crm/v3/owners`

Then confirm the fiscal year start month, ask for `quota_carrying_reps` directly (it is
load-bearing across the suite and cannot be inferred), propose `material_deal_floor` as the
10th percentile of closed-won amount, and read `segments` off the picklist rather than
inventing them. Ask whether `redact_pii_in_reports` should be on — some organisations need
person names pseudonymised in anything that leaves the CS team.

---

## 3. Discover — before you ask anything

This is the step that makes the product feel expensive. Run it all, then summarise.

### 3a. The customer universe

**Salesforce**

```sql
SELECT Type, COUNT(Id) FROM Account GROUP BY Type
SELECT StageName, COUNT(Id) FROM Opportunity WHERE IsClosed = false GROUP BY StageName
SELECT COUNT(Id) FROM Account WHERE Type = 'Customer'
```

**HubSpot**

```json
POST /crm/v3/objects/companies/search
{ "filterGroups": [{ "filters": [
    { "propertyName": "lifecyclestage", "operator": "EQ", "value": "customer" }]}],
  "properties": ["name"], "limit": 1 }
```
plus `GET /crm/v3/properties/companies/lifecyclestage` for the full option list.

### 3b. Where do contract and renewal dates actually live?

Do not ask this — go and look. Contract data lives in one of five places and the org usually
has an opinion that turns out to be half true.

**Salesforce — inventory every candidate:**

```sql
-- every custom Account field whose name smells like a date or money
SELECT QualifiedApiName, Label, DataType
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Account'
  AND (QualifiedApiName LIKE '%Renew%' OR QualifiedApiName LIKE '%Contract%'
       OR QualifiedApiName LIKE '%Term%' OR QualifiedApiName LIKE '%ARR%'
       OR QualifiedApiName LIKE '%MRR%' OR QualifiedApiName LIKE '%End_Date%')

-- is the Contract object in real use, or is it empty?
SELECT COUNT(Id) FROM Contract
SELECT Status, COUNT(Id) FROM Contract GROUP BY Status

-- are renewals modelled as opportunities?
SELECT Type, COUNT(Id) FROM Opportunity GROUP BY Type
SELECT Name, DeveloperName FROM RecordType WHERE SobjectType = 'Opportunity'

-- any custom subscription-shaped object?
SELECT QualifiedApiName, Label FROM EntityDefinition
WHERE QualifiedApiName LIKE '%Subscript%' OR QualifiedApiName LIKE '%Contract%'
   OR QualifiedApiName LIKE '%Entitle%'
```

Then measure fill rate on every candidate — a field that exists and is 11% populated is not
where the renewal date lives, whatever anyone tells you:

```sql
SELECT COUNT(Id) FROM Account WHERE Type='Customer' AND Renewal_Date__c != null
SELECT COUNT(Id) FROM Account WHERE Type='Customer'
```

**HubSpot:**

```
GET /crm/v3/properties/companies      -- scan for renewal / contract / arr / mrr names
GET /crm/v3/properties/deals          -- deal_type options, custom renewal properties
GET /crm/v3/pipelines/deals           -- is there a dedicated renewals pipeline?
```
Then sample 25 customer companies and compute the fill rate of each candidate property
yourself. HubSpot has no `GROUP BY`; count it in the response.

### 3c. Who owns the account?

**Salesforce**

```sql
SELECT QualifiedApiName, Label, DataType FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Account'
  AND (QualifiedApiName LIKE '%CSM%' OR QualifiedApiName LIKE '%Success%'
       OR QualifiedApiName LIKE '%Owner%' OR QualifiedApiName LIKE '%Manager%')

SELECT Owner.Name, COUNT(Id) FROM Account WHERE Type='Customer' GROUP BY Owner.Name
SELECT COUNT(Id) FROM Account WHERE Type='Customer' AND Owner.IsActive = false
```

That last one matters more than it looks: customers owned by deactivated users are customers
nobody is watching.

**HubSpot:** `GET /crm/v3/owners` and count companies per `hubspot_owner_id`, plus any custom
`csm` property you found in 3b.

### 3d. Relationship depth and coverage

```sql
SELECT AccountId, COUNT(Id) FROM Contact WHERE AccountId IN (...) GROUP BY AccountId
SELECT COUNT(Id) FROM Contact WHERE AccountId IN (...) AND EmailBouncedDate != null
SELECT AccountId, MAX(ActivityDate) FROM Task WHERE AccountId IN (...) GROUP BY AccountId
```

And check conversation coverage directly: for a sample of ten customers, can you find any
call or message thread in the last 90 days? Report the coverage rate as a percentage. If it
is under 50%, say so now — it changes what this agent can tell them, and they should hear it
in setup rather than discovering it in a report.

### 3e. Report what you found

Summarise in a table before you ask anything: account counts by type, the renewal-date
candidates with their fill rates, the owner field and book distribution, contacts per account,
bounce count, conversation coverage. Then ask your questions **in terms of what you found.**

> ❌ "Where do you store renewal dates?"
> ✅ "`Renewal_Date__c` exists on Account and is populated on 61% of customers. The Contract
>    object has 0 records. 88 opportunities have Type = 'Renewal' and their CloseDates line up
>    with the Account field where both exist. It looks like renewal opportunities are the
>    source of truth and the Account field is a stale copy — is that right?"

---

## 4. The interview

Ask these. They are the floor, not the ceiling, and every one of them is something the CRM
genuinely cannot tell you.

1. **What counts as a customer?** Lead with the measured answer: *"I found 312 accounts with
   Type = Customer, but 47 of them have ARR of 0 and 11 have a contract that ended over a year
   ago. My proposed filter is `Type = 'Customer' AND ARR__c > 0`, which gives 254. Right?"*
   Ask explicitly about paused/on-hold accounts, partners and resellers, internal test
   records, and multi-entity parents where the paper sits on the parent and the work on a child.

2. **Where do contract and renewal dates live** — confirm the source of truth you inferred in
   3b, and ask which one wins when two disagree. Also ask what happens to the date when a
   contract is signed: does the field move to the new term end, or stay on the old one?

3. **The notice window in days.** Ask for the real number from their paper, per segment if it
   varies. *"What is the contractual notice period? If a customer has to tell you 60 days
   before renewal that they are leaving, then an unsigned renewal 45 days out is already past
   the point where you can react — that is why this is the heaviest signal in the model."*

4. **The CSM book.** Confirm the ownership field and show them the distribution you measured.
   Ask about the accounts owned by inactive users, and whether owner-of-record actually
   matches who runs the relationship.

5. **Does product-usage signal exist, and where?** A warehouse table, a product-analytics
   export, a field on the account, or nothing. If nothing, say plainly that the usage signal
   will be dropped and its weight redistributed — and that usage decline typically leads
   sentiment decline by about a quarter, so it is the most valuable thing they could add next.

6. **Support-ticket source.** Zendesk, Jira, Intercom, Freshdesk, Salesforce Case, HubSpot
   Ticket, or none. Ask what their severity levels are actually called, and map them to
   `critical | high | normal | low`.

7. **Champion and economic buyer.** First ask whether a field already holds them, and check
   its fill rate before you take the answer at face value. If there is no field, you will
   capture them per account in step 5. Agree a definition first and write it into the config:
   *"By champion I mean the person who spends their own political capital to keep you — not
   the day-to-day admin, and not necessarily the signer. Does that match how your team uses
   the word?"*

8. **The kickoff baseline.** This is the most important part of setup. See step 5.

9. **How often will you run this?** Monthly before the business review is the common answer.
   Whatever it is, the answer sets the expectation for when deltas become meaningful.

---

## 5. Capture the kickoff baseline — do not let this be skipped

Say this out loud, in these terms:

> A health score with no baseline is a vibe. At renewal you will be asked what changed since
> you started, and "they're at 64" is not an answer — "they started at 41 and they're at 64"
> is. This is the single most valuable ten minutes in the whole setup, and it is the one thing
> that cannot be reconstructed later from the CRM.

For each account in the book, capture:

| Field | How to get it |
|---|---|
| `kickoff_date` | Contract start date, or the first recorded call. Show them what you found. |
| `kickoff_arr` | ARR at signature, not today's ARR. The CRM usually has the original opportunity amount. |
| `kickoff_sentiment` | 0–100. If you have transcripts from that period, **read the earliest two or three calls and propose a number with the quotes that support it.** Otherwise ask the CSM for their honest recollection and mark it reconstructed in `notes`. |
| `kickoff_engaged_contacts` | Distinct people who attended the first month of calls. |
| `kickoff_open_escalations` | How many problems they arrived with. Accounts that arrive from a failed vendor start low and that is not a failure. |
| `champion` / `economic_buyer` | Name, email, title — as at kickoff, and note if they have already changed. |
| `success_criteria` | **In the customer's own words.** What did they say success looks like? This is what you will be measured against, and it is the thing most often never written down. |

Two rules:

- **An approximate baseline beats none.** A reconstructed number marked as reconstructed is
  worth far more than a blank. Do not let perfect stop this.
- **Never overwrite an existing baseline.** If `accounts[].kickoff_*` is already populated,
  show it and leave it alone unless the user explicitly asks to change it. The whole value is
  that it is fixed. If they do change one, record the old value in `notes`.

For a large book, work top-down by ARR and get the top 20 done properly rather than 200 done
badly. Tell the user which accounts still have no baseline — every report will keep telling
them, by name, until they do.

---

## 6. Write the config

```bash
mkdir -p ~/.leanscale-gtm
```

Write `~/.leanscale-gtm/profile.json` (create or merge — never clobber another agent's keys)
and `~/.leanscale-gtm/customer-health.json`, using the plugin's `config.example.json` as the
template — `"$HOME/.leanscale-gtm/bin/customer-health" --root` prints the directory it lives
in. Keep every `_<key>_help` line; customers edit these files by hand.

Then show them the file you wrote, in full, and tell them where it lives and that it survives
plugin updates.

---

## 7. Smoke test — score one real account, both ways

A setup that ends without proving output is not done.

Pick one real account — ideally a mid-sized one with a renewal inside the next six months and
at least a few calls. Run the whole pipeline against a narrow slice:

```bash
RUN="./gtm-agents/customer-health/setup-smoketest"
mkdir -p "$RUN/raw"
# fetch that one account through steps 2-7 of the run skill, then:
"$HOME/.leanscale-gtm/bin/customer-health" analyze --run-dir "$RUN"
"$HOME/.leanscale-gtm/bin/customer-health" report  --run-dir "$RUN" --no-save-baseline
```

`--no-save-baseline` matters: a smoke test over one account must not become the baseline the
first real run is compared against.

Show them both numbers and the arithmetic behind each:

> **Northwind Logistics — $240,000 ARR**
> **Sentiment 86** — six readings, five positive. Their VP RevOps on 4 August: *"I have
> already recommended you to two other VPs here."*
> **Commercial risk 55 (Elevated)** — the renewal opportunity is 35 days out, inside your
> 60-day notice window, and it has not moved stage since 30 June. That alone floors the risk
> score regardless of how the calls read.
> **Quadrant: happy but exposed.** A single blended health score would have put this account
> at about 70 and nobody would have looked at it again.

That contrast *is* the demonstration. If the smoke-test account happens to be healthy on both
axes, say so and pick a second one — the user needs to see divergence once to understand what
they have bought.

If you cannot run the pipeline offline first, do it against the bundled fixtures to prove the
scripts work before blaming the connection:

```bash
AGENT_ROOT="$("$HOME/.leanscale-gtm/bin/customer-health" --root)"
LEANSCALE_GTM_HOME=/tmp/ch-demo "$HOME/.leanscale-gtm/bin/customer-health" analyze \
  --run-dir /tmp/ch-demo-run --raw "$AGENT_ROOT/fixtures/raw" --as-of 2026-08-10
```
(copy `fixtures/profile.demo.json` and `fixtures/config.demo.json` into `/tmp/ch-demo/` first,
as `profile.json` and `customer-health.json`.)

---

## 8. Pass/fail table

End with this, filled in, plus one plain-English sentence per gap saying what the user must
do to close it.

| Check | Status | What it means |
|---|---|---|
| CRM read (`crm.query`) | ✅ / ❌ | Required. Nothing runs without it. |
| Customer filter returns a sane count | ✅ / ⚠️ | 254 accounts — matches what you expected. |
| Renewal date resolvable per account | ✅ / ⚠️ / ❌ | 61% today. The other 39% are unmeasured, not low risk. |
| Notice window captured | ✅ / ❌ | The heaviest signal in the model depends on this number. |
| CSM ownership mapped | ✅ / ⚠️ | 4 accounts owned by deactivated users. |
| Champion identified per account | ✅ / ⚠️ / ❌ | No field; captured for the top 20 by ARR. |
| Conversation source (sentiment) | ✅ / ⚠️ / ❌ | Covers 26 of 43. The other 17 score on paper only. |
| Support tickets | ✅ / ❌ | Not connected — signal dropped, weight redistributed. |
| Product usage | ✅ / ❌ | Not connected — signal dropped, weight redistributed. |
| Kickoff baseline captured | ✅ / ⚠️ / ❌ | 20 of 43. The rest cannot prove progress at renewal. |
| Smoke test produced a real finding | ✅ / ❌ | Both scores computed on a live account. |

Close with what will and will not work, in the user's words rather than yours. Something like:

> This will tell you, every month, which accounts are commercially exposed and which ones are
> exposed *while looking happy* — with the quote and the renewal date side by side. It will not
> tell you anything about sentiment on the 17 accounts with no calls or shared channel, and it
> will keep listing those 17 by name rather than quietly scoring them as fine. The 23 accounts
> without a kickoff baseline will show a health score you cannot defend at renewal; that is the
> first thing worth fixing, and it is ten minutes each.
