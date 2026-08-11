---
name: run
description: >-
  Score every customer account twice — sentiment and commercial risk, kept separate — and
  produce a local HTML report with the sentiment × risk quadrant, per-account evidence and
  movement since kickoff. Use this when the user asks about customer health, churn risk,
  renewal risk, at-risk accounts, which accounts are quiet, whether an account is happy,
  what the book looks like before a QBR or board meeting, or says "run customer health",
  "score my accounts", "who is going to churn", "check my renewals", "/customer-health:run".
  Read-only: it never writes to the CRM.
argument-hint: "[--window 180d] [--account \"Acme\"] [--owner \"Dana\"] [--skip-research]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch, WebSearch, WebFetch
---

# Customer Health — run

You fetch and you judge. Python scores, trends and renders. Never invert that.

**Read-only.** Nothing in this skill writes to a CRM, a ticketing system or a comms tool.
If a customer asks you to update a record, decline and point them at the finding.

---

## 0. Preflight

```bash
test -f ~/.leanscale-gtm/customer-health.json && echo "config ok" || echo "MISSING"
test -f ~/.leanscale-gtm/profile.json && echo "profile ok" || echo "MISSING"
```

If either is missing, stop and run `/customer-health:setup`. Do not guess the customer
definition or the notice window — a wrong notice window silently mis-scores the heaviest
signal in the model.

Read both files. Announce the assumptions you are running under in one line: org name, CRM,
customer count expected, notice window, sentiment floor and risk threshold.

Create the run directory:

```bash
RUN="./gtm-agents/customer-health/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw"
echo "$RUN"
```

Resolve the window: `window_days` back from today unless the user overrode it. Keep it at or
above 120 days — trending compares the recent half against the earlier half, and two 45-day
halves are noise.

---

## 1. Probe what you can reach

```
ToolSearch("run_soql_query salesforce")            -> crm.query (Salesforce)
ToolSearch("hubspot crm search companies deals")   -> crm.query (HubSpot)
ToolSearch("transcripts meetings recordings calls")-> transcripts.list / transcripts.get
ToolSearch("slack search messages channel")        -> comms.search
ToolSearch("gmail search threads")                 -> comms.search
ToolSearch("read file content drive folder")       -> docs.read (transcript folder path)
```

`crm.query` is **required**. Everything else is optional and the run must complete without it.

State plainly which of the three signal families you have:

| Family | Have it? | Consequence if not |
|---|---|---|
| CRM | required | run aborts |
| Conversations (transcripts and/or comms) | optional | **sentiment is unavailable, not clean** — accounts land outside the quadrant |
| Support / usage | optional | those signals drop and their weight redistributes |

Roughly six in ten organisations of this size have no conversation-intelligence tool. That is
a supported configuration, not a degraded one — say so, and use the folder or mailbox path in
step 4.

---

## 2. Pull the CRM — Salesforce

Substitute the customer filter from `customer_definition.account_filter_soql` and the field
names from `crm_fields`. These are complete, runnable queries; do not paraphrase them.

**Customer accounts** → `raw/accounts.json`

```sql
SELECT Id, Name, Type, ARR__c, Segment__c, CSM__c,
       Renewal_Date__c, Contract_Start_Date__c, Auto_Renew__c,
       Champion__c, Economic_Buyer__c, Owner.Name, LastActivityDate
FROM Account
WHERE Type = 'Customer' AND ARR__c > 0
ORDER BY ARR__c DESC
```

**Renewal instrument** → `raw/renewals.json`. Which of these you run is decided by
`renewal_source` in config. Run more than one and concatenate if the org uses more than one.

```sql
-- renewal_source = "renewal_opportunity"
SELECT Id, Name, AccountId, Amount, CloseDate, StageName, IsClosed, IsWon, Type,
       LastModifiedDate
FROM Opportunity
WHERE AccountId IN ('001...','001...')
  AND Type IN ('Renewal','Existing Business')
  AND CloseDate >= LAST_N_DAYS:90
ORDER BY CloseDate ASC

-- renewal_source = "contract_object"
SELECT Id, ContractNumber, AccountId, StartDate, EndDate, Status, ContractTerm,
       ContractValue__c, SpecialTerms
FROM Contract
WHERE AccountId IN ('001...','001...') AND Status != 'Draft'

-- renewal_source = "custom_object"  (swap in the object and field names setup discovered)
SELECT Id, Account__c, Term_End_Date__c, Renewal_Status__c, ACV__c, Auto_Renew__c
FROM Subscription__c
WHERE Account__c IN ('001...','001...')

-- renewal_source = "account_fields"  -- already covered by the Account query above.
--   Note in raw/_sources.json that no renewal record exists, so the run can tell
--   "no renewal opportunity created" apart from "renewal signed".
```

Add `"record_kind"` to every record you write: `"opportunity"`, `"contract"`, or
`"subscription"`. That is how the scorer knows what it is looking at.

**Open expansion pipeline** → `raw/expansion.json`

```sql
SELECT Id, Name, AccountId, Amount, CloseDate, StageName, IsClosed, Type
FROM Opportunity
WHERE AccountId IN ('001...','001...')
  AND Type IN ('Upsell','Expansion','Add-On')
  AND IsClosed = false
```

**Contacts** (champion status, single-threading, bounce detection) → `raw/contacts.json`

```sql
SELECT Id, AccountId, FirstName, LastName, Name, Email, Title,
       Is_Active__c, EmailBouncedDate, EmailBouncedReason, LastActivityDate
FROM Contact
WHERE AccountId IN ('001...','001...')
```

**Activity history** (last touch, executive touch) → `raw/activities.json`

```sql
SELECT Id, AccountId, Subject, ActivityDate, Status, Owner.Name, Owner.Title
FROM Task
WHERE AccountId IN ('001...','001...') AND ActivityDate = LAST_N_DAYS:180

SELECT Id, AccountId, Subject, ActivityDateTime, Owner.Name, Owner.Title
FROM Event
WHERE AccountId IN ('001...','001...') AND ActivityDateTime = LAST_N_DAYS:180
```

Tag each activity with `"seniority": "exec" | "senior" | "ic"` from the owner's title before
you write it. That title-to-seniority mapping is a judgment; make it, don't regex it.

**Support cases** (only if `support_source` = `salesforce_case`) → `raw/tickets.json`

```sql
SELECT Id, AccountId, Subject, Priority, Status, CreatedDate, ClosedDate, IsEscalated
FROM Case
WHERE AccountId IN ('001...','001...') AND CreatedDate = LAST_N_DAYS:180
```

Write it out as `{"id","account_id","subject","severity","status","opened_at","closed_at"}`
with `severity` in `critical|high|normal|low`.

---

## 3. Pull the CRM — HubSpot

Same data, HubSpot's shapes. The scorer flattens `properties` for you, so write the API
response through unmodified — but **always request `associatedcompanyid`** on child objects,
because that is the only link back to the account.

**Customer companies** → `raw/accounts.json`

```json
POST /crm/v3/objects/companies/search
{
  "filterGroups": [{ "filters": [
    { "propertyName": "lifecyclestage", "operator": "EQ", "value": "customer" },
    { "propertyName": "arr", "operator": "GT", "value": "0" }
  ]}],
  "properties": ["name","arr","lifecyclestage","segment","renewal_date","contract_start_date",
                 "auto_renew","champion_contact","economic_buyer","hubspot_owner_id",
                 "notes_last_contacted","hs_lastmodifieddate"],
  "sorts": [{ "propertyName": "arr", "direction": "DESCENDING" }],
  "limit": 100
}
```

Paginate on `paging.next.after` until it is absent. A truncated customer list is a silently
wrong report.

**Renewal deals** → `raw/renewals.json` (add `"record_kind": "deal"` to each)

```json
POST /crm/v3/objects/deals/search
{
  "filterGroups": [{ "filters": [
    { "propertyName": "associatedcompanyid", "operator": "IN", "values": ["14778211903","..."] },
    { "propertyName": "deal_type", "operator": "EQ", "value": "renewal" }
  ]}],
  "properties": ["dealname","amount","closedate","dealstage","pipeline","deal_type",
                 "associatedcompanyid","hs_is_closed","hs_is_closed_won","hs_lastmodifieddate"],
  "limit": 200
}
```

If this portal has no `deal_type`, filter on `pipeline` equal to the renewals pipeline id that
setup discovered instead. If renewal dates live on the company record only, skip this query
and note in `raw/_sources.json` that no renewal record exists — the scorer treats "inside the
notice window with no renewal record at all" as worse than "open renewal deal in negotiation",
and it can only do that if you tell it which situation you are in.

**Expansion deals** → `raw/expansion.json`

```json
POST /crm/v3/objects/deals/search
{
  "filterGroups": [{ "filters": [
    { "propertyName": "associatedcompanyid", "operator": "IN", "values": ["..."] },
    { "propertyName": "deal_type", "operator": "IN", "values": ["upsell","expansion"] },
    { "propertyName": "hs_is_closed", "operator": "EQ", "value": "false" }
  ]}],
  "properties": ["dealname","amount","closedate","dealstage","associatedcompanyid","hs_is_closed"],
  "limit": 200
}
```

**Contacts** → `raw/contacts.json`

```json
POST /crm/v3/objects/contacts/search
{
  "filterGroups": [{ "filters": [
    { "propertyName": "associatedcompanyid", "operator": "IN", "values": ["..."] }
  ]}],
  "properties": ["firstname","lastname","email","jobtitle","hs_email_hard_bounce_reason",
                 "is_active","notes_last_contacted","hs_email_last_reply_date",
                 "associatedcompanyid"],
  "limit": 500
}
```

`hs_email_hard_bounce_reason` carrying any value is a hard bounce. That plus the champion
field is how champion departure gets detected on a HubSpot portal.

**Engagements** → `raw/activities.json`. Search `calls`, `emails` and `meetings` with
`hs_timestamp` at or after the window start, request `associatedcompanyid`, and tag each with
`"seniority"` as above.

**Tickets** (only if `support_source` = `hubspot_ticket`) → `raw/tickets.json`

```json
POST /crm/v3/objects/tickets/search
{
  "filterGroups": [{ "filters": [
    { "propertyName": "associatedcompanyid", "operator": "IN", "values": ["..."] },
    { "propertyName": "createdate", "operator": "GTE", "value": "<window start epoch ms>" }
  ]}],
  "properties": ["subject","hs_ticket_priority","hs_pipeline_stage","createdate",
                 "closed_date","associatedcompanyid"],
  "limit": 500
}
```

---

## 4. Pull conversations — whichever source exists

Write **metadata only** to `raw/interactions.json`. Transcript bodies stay where they are;
they do not belong in the run directory. One record per meeting and per message thread:

```json
{
  "id": "unique-string",
  "account_id": "CRM id of the account",
  "source": "transcripts | comms",
  "provider": "conversation_intelligence | transcript_folder | shared_channel | email",
  "type": "meeting | message",
  "occurred_at": "2026-07-22T15:00:00Z",
  "title": "Q3 business review",
  "direction": "inbound | outbound",
  "response_to_id": "id of the message this replies to",
  "response_latency_hours": 4.5,
  "our_participants":      [{ "name": "...", "seniority": "exec|senior|ic" }],
  "customer_participants": [{ "name": "...", "email": "...", "title": "...",
                              "seniority": "exec|senior|ic", "is_champion": true }]
}
```

`seniority` and `is_champion` are your judgments. C-level and anyone with "Chief", "President"
or "founder" in the title is `exec`; VP and Director are `senior`; everyone else is `ic`.
`response_latency_hours` may be stated directly or left to the scorer to compute from
`response_to_id` — do whichever is cheaper for the source you are on.

**Adapters, in order of preference:**

1. **Conversation-intelligence tool** (Gong, Chorus, Fireflies, Grain, Otter, Zoom).
   List calls in the window, filter to those with an attendee on the account's email domain,
   fetch each transcript, extract attendees and timestamps.
2. **Transcript folder** (Drive, Notion, or a local directory in `transcript_folder_path`).
   Glob the folder, match files to accounts by the account name in the filename or the
   attendee list in the header, read each file. This is the path most organisations are on.
   ```bash
   ls -1 "<transcript_folder_path>" | head -50
   ```
3. **Shared channels.** Search the channel per account. Message counts, timestamps, who spoke
   and reply latency — do **not** copy whole threads into the run directory.
4. **Mailbox threads.** Search per account domain, over the window. Use thread timestamps for
   latency; read bodies only for the sentiment pass.
5. **Manual paste.** Ask the user to paste transcripts for the accounts they care most about.
   This is a real, supported path — say so rather than reporting sentiment as unavailable when
   the user has the material sitting in a folder.

If none of these resolve for an account, write nothing for it. Do not invent a neutral
reading. An account with no conversation source must land outside the quadrant.

---

## 5. Do the sentiment judgment — this is the part only you can do

Read the transcripts and threads. For each account write one object into
`raw/sentiment.json`:

```json
{
  "account_id": "001...",
  "account_name": "Acme",
  "champion_signal": "engaged | fading | departed | unknown",
  "summary": "One honest paragraph. Say what changed and when.",
  "readings": [
    {
      "interaction_id": "matches an id in interactions.json",
      "occurred_at": "2026-07-22",
      "source": "Recorded call — Q3 business review",
      "tone": 2,
      "is_escalation": false,
      "kind": "praise | complaint | neutral | escalation",
      "speaker": "Dana Whitfield",
      "speaker_role": "VP Revenue Operations",
      "quote": "verbatim, exactly as said, no cleanup",
      "claude_note": "why you read it that way"
    }
  ]
}
```

**The tone scale.** One reading per meaningful moment, not one per sentence.

| tone | Means |
|---|---|
| **+2** | Specific, outcome-based praise, or the customer advocating for you internally |
| **+1** | Warm, general, or praise for effort and approach rather than results |
| **0** | Routine status. Nothing either way. Most readings are this |
| **−1** | A concrete complaint, frustration, or a commitment missed |
| **−2** | Trust stated as lost, an ultimatum, spend questioned, or the relationship itself in doubt |

**`is_escalation` is true** when someone formally raises severity: asks for a manager or an
executive, says "this needs to be treated as urgent", references a contract or SLA, or is
explicitly speaking on behalf of an absent senior stakeholder. Frustration alone is not an
escalation; frustration plus a demand for a different process is.

**Rules that keep this honest:**

- **Never keyword-match.** "That's fine" after three failed attempts is not neutral. "This is
  a disaster" from a customer who says it weekly is not a −2. You are reading a room; a regex
  cannot, which is why this step is yours and not Python's.
- **Quote verbatim.** No cleanup, no paraphrase, no ellipsis-hiding of the awkward half. The
  quote is the evidence a CS leader takes into the account review.
- **Politeness is not health.** Customers are pleasant right up until they leave. Weight
  what they *do* — who attends, how fast they reply, whether they bring their exec.
- **Attribute honestly.** If the transcript does not identify the speaker, say `"speaker":
  "unattributed"` rather than guessing. An unattributed quote in a report is a credibility
  problem the first time someone checks it.
- **Say when you cannot tell.** `champion_signal: "unknown"` is a legitimate answer and is
  handled correctly downstream.

---

## 6. Research the companies — refresh every run

For each account, search for what is happening at the customer's own business. This exists
because a champion's departure or a down round will end a contract that has perfect sentiment
and a perfect delivery record, and nothing in your own call transcripts will ever tell you.

Search per account: `"<company> layoffs"`, `"<company> funding round"`,
`"<company> acquires OR acquired"`, `"<company> CEO OR CRO OR CFO appointed"`,
`"<champion name> <company>"` (has your champion changed jobs?). Fetch anything credible.

Write `raw/company_research.json`:

```json
{
  "account_id": "001...",
  "company": "Acme",
  "researched_at": "2026-08-10",
  "funding_stage": "Series C",
  "last_raise": { "date": "2024-03-12", "round": "Series C", "amount_usd": 62000000 },
  "months_since_last_raise": 29,
  "headcount_trend": "growing | flat | declining",
  "external_risk_note": "Plain-English read, including what it means for the renewal.",
  "events": [
    { "type": "layoff", "date": "2026-06-15", "severity": "high",
      "headline": "...", "source_url": "https://...", "note": "why this matters to us" }
  ]
}
```

Event types the scorer knows, and their direction:

- **Adds risk:** `down_round`, `acquired`, `runway_risk`, `layoff`, `restructuring`,
  `exec_change`, `champion_departure_public`, `hiring_freeze`, `acquisition`, `public_incident`
- **Removes risk:** `funding_raise`, `ipo`, `champion_promotion`, `expansion_announcement`

Cite a URL for every event. An uncited event is a rumour, and it will be the thing the
customer challenges first. Write `"events": []` with an honest note when you find nothing —
that is a finding too. If the user passed `--skip-research`, write the file with empty events
and record it in `_sources.json` so the report says the signal was skipped, not clean.

---

## 7. Write the provenance file

`raw/_sources.json` — one entry per source you attempted, including the ones that failed.

```json
{
  "crm": "salesforce",
  "window": { "start": "2026-02-11", "end": "2026-08-10" },
  "sources": [
    { "name": "customer_accounts", "tool": "run_soql_query", "required": true,
      "count": 43, "query": "SELECT ...",
      "diagnosis": "What is probably wrong if this comes back empty." }
  ],
  "warnings": ["Anything the reader needs to know about coverage."]
}
```

Source names the scorer recognises: `customer_accounts` (the only required one), `renewals`,
`expansion_pipeline`, `contacts`, `crm_activities`, `interactions`, `sentiment_readings`,
`company_research`, `support_tickets`, `product_usage`.

**Diagnoses must be specific.** Not "Slack unavailable" — *"Slack tools resolve and the
channel exists, but reading it returned 0 messages; the connected identity is probably a bot
that is not a member of the channel."*

---

## 8. Score and render

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" --run-dir "$RUN"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py"  --run-dir "$RUN"
```

`analyze.py` aborts if `customer_accounts` came back empty. That abort is correct behaviour —
a report that says "no issues found" because auth failed is worse than a crash. Fix the
connection and re-run; do not work around it.

`report.py` prints the baseline message, the kickoff-baseline status and the quadrant counts.

---

## 9. Present it

Open with the quadrant, because that is the product:

> 43 customers. **4 are happy and still at commercial risk** — $1.2M of ARR where sentiment is
> above 60 and the paper is not signed. That is the quadrant that churns, and it is the one a
> blended health score hides.

Then, in order:

1. **Happy but exposed** — name every account, its ARR, its renewal date and the verbatim
   quote showing they are happy. The gap between the quote and the renewal status is the
   entire argument.
2. **Unsigned renewals inside the notice window** — ARR, days out, whether a renewal record
   even exists.
3. **Champion departures** — with the evidence: the bounce date, the inactive flag, or the
   public signal.
4. **Everything else**, severity order.
5. **What this run could not see.** Read the `unavailable` list out loud. Never let a missing
   transcript source read as "no sentiment problems".
6. **Baseline.** On run one say plainly that this is the baseline and the comparison starts
   next run. On later runs lead with what moved.
7. **Kickoff baselines.** Name the accounts with none. Those are the accounts whose progress
   you cannot prove at renewal, which is the single most expensive gap in this whole system.

Give them the file path:

```
open ./gtm-agents/customer-health/<stamp>/report.html
```

Never deploy the report anywhere. It is the customer's data and it stays on their machine.
