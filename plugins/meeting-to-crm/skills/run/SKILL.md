---
name: run
description: >-
  Read recent sales-call transcripts and propose the CRM updates the rep would have
  typed — next step and next-step date, qualification fields, competitor, pain, decision
  process and timeline, new stakeholders, and a call summary — as a reviewable diff with
  a verbatim quote behind every value. Dry-run by default; writes only after a named human
  approves the exact diff. Trigger on "/meeting-to-crm:run", "update the CRM from my calls",
  "log my calls", "what should I update after that meeting", "write up yesterday's calls",
  "meeting notes to Salesforce/HubSpot", "sync Gong/Fireflies/Gong calls to the CRM", or any
  request to turn call transcripts into CRM field updates.
argument-hint: "[--window 7d] [--meeting <id>] [--rep <email>] [--apply]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# meeting-to-crm — run

Turn recorded calls into proposed CRM updates. Boring on purpose: this does the bounded,
verifiable job a rep already does by hand, and it stops at the point where judgment starts.

## The contract you are operating under

**You do not write to the CRM in this skill.** You read transcripts, gather CRM state,
draft proposed values, and hand a diff to a human. Applying is a separate, explicitly
requested step that happens on a **later turn**, after a person has read the diff.

Even when the user passes `--apply`, you still propose first and stop. `--apply` is the
user saying "I intend to apply this batch after I read it", not "skip the reading".

Python enforces the rest. `scripts/diff.py` drops any proposal that names a field outside
the allow-list, targets a populated field under a fill-blanks-only policy, touches a
forecast-bearing field without a double opt-in, or carries a quote that is not in the
transcript. You cannot talk it out of those, and you should not try — if the guard fires,
report it, do not work around it.

---

## Step 0 — preflight

```bash
cat ~/.leanscale-gtm/meeting-to-crm.json   # settings: allow-list, matching, framework map
cat ~/.leanscale-gtm/profile.json          # shared org profile
```

If `meeting-to-crm.json` is missing, stop and tell the user to run `/meeting-to-crm:setup`.
Do not invent an allow-list; an allow-list the customer did not agree to is not a guard.

Create the run directory and use it for everything:

```bash
RUN="./gtm-agents/meeting-to-crm/$(date -u +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw"
```

Default window is **7 days**. `--window 1d` after a single call is the common case.

---

## Step 1 — resolve the tools you actually have

Required capabilities: `crm.describe`, `crm.query`, `crm.write`, `transcripts.*`, `docs.read`.

**If `ToolSearch` is available** (Claude Code), that is the fastest route:

    ToolSearch("run_soql_query salesforce")        -> crm.query   (Salesforce)
    ToolSearch("hubspot crm search objects")       -> crm.query   (HubSpot)
    ToolSearch("describe object metadata fields")  -> crm.describe
    ToolSearch("create update record")             -> crm.write   (only used in the apply step)
    ToolSearch("transcripts meetings recordings")  -> transcripts.list
    ToolSearch("get transcript")                   -> transcripts.get
    ToolSearch("read file content drive")          -> docs.read   (Drive/folder transcript path)

**Otherwise** — Cursor, VS Code, Codex CLI, Gemini CLI — match against the tools
already connected in this client. Commonly:

    crm.describe   salesforce  run_soql_query over EntityDefinition / FieldDefinition (useToolingApi where noted)
                   hubspot     hubspot-list-properties
    crm.query      salesforce  run_soql_query
                   hubspot     hubspot-search-objects / hubspot-list-objects / hubspot-batch-read-objects
    crm.write      salesforce  the server's record create/update tool
                   hubspot     hubspot-batch-update-objects / hubspot-create-object
    transcripts.*  any vendor  gong / fireflies / chorus / grain / otter / zoom list+get transcript tools
                   fallback    docs.read over a folder of exported transcripts — no vendor is required
    docs.read      drive       file search + read file content
                   local       plain filesystem reads

These names are the common cases, not the contract; the capability is the contract.
Report which tool resolved for each capability before proceeding.


Record what resolved. If `crm.query` does not resolve, stop — there is nothing to diff
against. If the transcript capability does not resolve but `config.transcript_source.kind`
is `local_dir`, that is fine: read the files with Glob/Read.

---

## Step 2 — fetch the meetings (adapter per source)

Write the normalised result to `$RUN/raw/meetings.json`:

```jsonc
{ "source": "<kind>", "meetings": [ {
  "id": "<stable source id>", "source": "<kind>", "title": "...",
  "meeting_type": "discovery|demo|technical_validation|negotiation|renewal|qbr|internal|...",
  "started_at": "2026-08-04T15:00:00Z", "duration_minutes": 38,
  "url": "...", "organizer_email": "...", "calendar_event_id": "...",
  "crm_links": { "opportunity_id": null, "account_id": null },
  "attendees": [ { "name": "...", "email": "...", "internal": true, "title": "..." } ],
  "transcript": [ { "ts": "00:04:12", "speaker": "...", "text": "..." } ]
  // or, when the source has no speaker labels:
  // "transcript_text": "one long string"
} ] }
```

`meeting_type` is yours to infer from the title, the agenda and the shape of the call —
but never invent one to get a meeting into scope. If you cannot tell, leave it empty and
let it be reported as skipped.

`internal: true` for anyone whose email domain is in `config.internal_email_domains`.

### The adapters

Sample payloads for four of these are in `fixtures/transcripts/` — read one if the shape
is unfamiliar.

**Gong** (`transcripts.list` + `transcripts.get`)
`GET /v2/calls?fromDateTime=&toDateTime=` for metadata, then
`POST /v2/calls/transcript {"filter":{"callIds":[...]}}`.
Sentence times are **milliseconds**; convert to `HH:MM:SS`. Speakers come back as
`speakerId` — resolve through `parties[]` to get names and emails. Gong's `context[]`
often carries the CRM object id: lift it into `crm_links` and you get a decisive match
signal for free. `parties[].affiliation` gives you internal/external without guessing.

**Fireflies**
`transcripts(fromDate:, toDate:)` then `transcript(id:) { sentences { speaker_name start_time text } }`.
`start_time` is **seconds as a float**. `meeting_attendees[]` carries the invite list.
There is no CRM link on the object, so matching leans on attendee domain.

**Chorus**
`GET /v1/engagement/calls?start_date=&end_date=` then the transcript endpoint per call.
Speaker labels are often `Speaker 1`/`Speaker 2` when diarisation is unconfident — keep
them as-is. A quote attributed to "Speaker 2" is still a real quote; a quote attributed to
a name you guessed is not.

**Grain**
`GET /_/public-api/recordings?cursor=` with `include_highlights=true`.
Grain's highlights are human-curated and are the best source of quotes in the suite — but
still verify every quote against the full transcript, not the highlight summary.

**Otter**
Export or API gives you speaker-labelled text with timestamps but frequently **no attendee
emails**. Without emails you lose attendee-domain matching, so fall back to the title
convention and warn in the manifest.

**Zoom**
`GET /v2/meetings/{meetingId}/recordings`, then fetch the `TRANSCRIPT` file — WebVTT with
`Speaker Name: text` on the cue. **No emails anywhere in the file.** Zoom-only shops must
match on the title convention or the calendar event; say so plainly rather than emitting
unmatched meetings. Pull the invite list from the calendar event if you have a calendar
tool.

**Google Meet / Drive folder** (`docs.read`)
Meet drops transcripts as Docs into a Drive folder. List `config.transcript_source.folder_id`,
read each file created in the window, parse `Speaker: text` lines. Meet transcripts name
attendees by display name only — same email problem as Zoom.

**Local directory** (`config.transcript_source.local_dir`)
Glob the directory for files modified in the window and Read them. Format notes are in
`fixtures/transcripts/local-dir-2026-08-06-corvus.txt`. This is a real path, not a
fallback — plenty of teams have no conversation-intelligence tool at all.

**Manual paste**
The user pastes a transcript into the conversation. Write it to
`$RUN/raw/meetings.json` with `transcript_text` and an id like `manual-001`, and ask for
the meeting date and the attendee list — do not infer them.

**Degrade honestly.** If one source errors, process what you have and record the failure in
`raw/_sources.json` so the manifest reports it. Never let a partial fetch look like a quiet
week.

---

## Step 3 — match each meeting to a record (highest-risk step)

Getting this wrong writes one customer's words onto another customer's deal. That is the
mistake that ends the pilot. So your job here is to **gather signals, not to decide** —
`diff.py` makes the call deterministically and reports anything short of clear as ambiguous.

Write `$RUN/raw/match_candidates.json`:

```jsonc
{ "matches": [ { "meeting_id": "mtg-001", "candidates": [
  { "object": "Opportunity", "id": "006...", "name": "...", "account_id": "001...",
    "account_name": "...", "stage": "Discovery", "is_open": true,
    "signals": ["attendee_domain:acme.io", "contact_email_exact:priya@acme.io",
                "single_open_opp", "recent_activity"] } ] } ] }
```

Signals, strongest first — attach every one that is genuinely true:

| Signal | How you establish it |
|---|---|
| `crm_link:<id>` | the transcript source itself carried the record id (Gong context, calendar attachment) |
| `calendar_event:<id>` | the calendar invite is linked to the record |
| `title_convention:<account>` | the title matches `config.matching.title_convention` and the captured account resolves |
| `contact_email_exact:<email>` | an attendee's email matches a Contact on the record |
| `attendee_domain:<domain>` | an external attendee's domain matches the account website/domain |
| `single_open_opp` | that account has exactly one open opportunity |
| `recent_activity` | the record has activity in the last 30 days |
| `owner_is_organizer` | the record owner organised the meeting |

### Salesforce

```sql
-- contacts by attendee email (the strongest cheap signal)
SELECT Id, Name, Email, AccountId, Account.Name
FROM Contact WHERE Email IN ('priya@northwindanalytics.com','erin@heliogrid.io')

-- accounts by attendee domain
SELECT Id, Name, Website FROM Account
WHERE Website LIKE '%northwindanalytics.com%' OR Name LIKE 'Northwind%'

-- open opportunities on those accounts
SELECT Id, Name, StageName, CloseDate, Amount, AccountId, Account.Name, OwnerId, Owner.Name,
       NextStep, LastActivityDate, IsClosed
FROM Opportunity
WHERE AccountId IN ('0015f00000NWACCT') AND IsClosed = false
ORDER BY LastActivityDate DESC

-- calendar-linked events (only if the org logs events against the opportunity)
SELECT Id, Subject, WhatId, ActivityDateTime FROM Event
WHERE ActivityDateTime = LAST_N_DAYS:7 AND WhatId != null
```

### HubSpot

```jsonc
// company by domain
POST /crm/v3/objects/companies/search
{"filterGroups":[{"filters":[{"propertyName":"domain","operator":"EQ","value":"ridgelinesoftware.com"}]}],
 "properties":["name","domain"]}

// contact by attendee email
POST /crm/v3/objects/contacts/search
{"filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":"lena@ridgelinesoftware.com"}]}],
 "properties":["firstname","lastname","email","associatedcompanyid"]}

// that company's deals
GET /crm/v4/objects/companies/550011/associations/deals
POST /crm/v3/objects/deals/search
{"filterGroups":[{"filters":[
   {"propertyName":"hs_object_id","operator":"IN","values":["8801234567"]},
   {"propertyName":"hs_is_closed","operator":"EQ","value":"false"}]}],
 "properties":["dealname","dealstage","amount","closedate","hs_next_step","hs_lastmodifieddate"],
 "limit":100}
```

**The manual override.** When `diff.py` reports an ambiguous match, do not guess and do not
retry with a different query hoping for a different answer. Show the user the candidates
and offer to write the override:

```jsonc
"matching": { "overrides": { "mtg-003": { "object": "Opportunity", "id": "0065f00000BP1AAA" } } }
```

---

## Step 4 — capture the current state of every candidate record

This is the *current value* column. Without it the plugin cannot tell a blank from
something it is about to destroy, so it is not optional.

Write `$RUN/raw/crm_records.json` — every allow-listed field on every candidate, plus the
existing children so a stakeholder is never added twice:

```jsonc
{ "crm": "salesforce", "records": [ {
  "object": "Opportunity", "id": "006...", "name": "...", "account_id": "001...",
  "account_name": "...", "stage": "...", "is_open": true,
  "fields": { "NextStep": "Follow up after discovery", "Metrics__c": null, "...": "..." },
  "children": { "OpportunityContactRole": [ { "ContactId": "003...", "ContactName": "...",
                                              "Email": "...", "Role": "Economic Buyer" } ] } } ] }
```

Salesforce: select exactly the allow-listed API names.
HubSpot: request them in `properties[]` — a property you do not request comes back absent,
which is **not** the same as blank, and treating it as blank is how you overwrite something.

Also refresh `$RUN/raw/crm_schema.json` (types, `updateable`, picklist values, lengths):

```sql
SELECT QualifiedApiName, Label, DataType, IsUpdatable FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName IN ('Opportunity','Task','Contact','OpportunityContactRole')
```
```
GET /crm/v3/properties/deals      GET /crm/v3/properties/contacts
```

---

## Step 5 — read the calls and draft the proposals

Write `$RUN/raw/proposals.json` (`proposals[]`, `child_records[]`, `undetermined[]` — the
exact shape is in `fixtures/salesforce/raw/proposals.json`).

**The rules, in order of how much trouble breaking them causes:**

1. **Every proposal carries a verbatim quote** — copied character-for-character from the
   transcript, with its timestamp and speaker. Python checks it. A paraphrase is dropped,
   and a dropped proposal is reported as a hallucination, not a near miss.
2. **Only allow-listed fields.** Read `config.field_allowlist` and propose nothing else.
   You may note in your summary that a field would be useful; do not propose it.
3. **A blank is a correct answer.** If the call did not cover a qualification dimension,
   put it in `undetermined[]` with one honest sentence about what was missing. Never
   produce a plausible value for something nobody said. The list of blanks is the most
   valuable half of this output — it is the list of questions the reps are not asking.
4. **Never propose Amount, CloseDate or stage** unless they appear in
   `restricted_fields_opt_in`. They are the rep's call.
5. **Confidence is a real number.** 0.9+ for an explicit commitment with an owner and a
   date. 0.6–0.8 for a clear statement that needs interpretation. Below 0.6 for anything
   you are pattern-matching. Do not round everything to 0.9.
6. **Write the value a rep would write** — specific, dated, with names. "Follow up next
   week" is worse than the blank it replaces.

### What to propose

| Target | What good looks like |
|---|---|
| Next step | The commitment, its owner, and its date. Both sides' actions if both committed. |
| Next-step date | An explicit date only. Never a date you computed from "in a couple of weeks". |
| Qualification fields | Map through `config.framework.dimensions` to the real field API names. One dimension per field, in the buyer's language. |
| Competitor | Only a named competitor, and only a value the picklist accepts. |
| Pain / use case | The problem in the buyer's words, plus the consequence they attached to it. |
| Decision process | The sequence of steps they described, including what gates what. |
| Decision timeline | The date and the event driving it. A timeline with no event is a wish. |
| Contact roles | Anyone named on the call who is not already on the record. |
| Call summary | To the activity/note object, never into a field a human curates. |

### Child records

New stakeholders are the highest-value, lowest-risk write in this plugin — creating a
record adds information without changing any existing value. Propose them freely, with a
quote.

When a person is not in the CRM at all, propose the `Contact` create **and** the role, and
reference the first from the second:

```jsonc
{ "id": "c-001", "object": "Contact", "action": "create",
  "values": { "FirstName": "Sofia", "LastName": "Bhatt", "Email": "...", "AccountId": "001..." } }
{ "id": "c-002", "object": "OpportunityContactRole", "action": "create",
  "values": { "ContactId": "@ref:c-001", "Role": "Influencer", "IsPrimary": false } }
```

`@ref:<row id>` means "the id returned when that row was created". Resolve it during the
apply step, in plan order. Never invent an email address that was not said or written down.

Finally, write `$RUN/raw/_sources.json` — one entry per source with `name`, `tool`,
`query`, `note` and a plain-English `diagnosis` for what it means if it comes back empty.
The `diagnosis` is what the user sees when a run aborts, so make it useful.

---

## Step 6 — build the diff and render it

```bash
"$HOME/.leanscale-gtm/bin/meeting-to-crm" analyze --raw "$RUN" --out "$RUN"
"$HOME/.leanscale-gtm/bin/meeting-to-crm" report --run "$RUN"
```

`analyze.py` aborts if a required source came back empty — that abort is correct behaviour,
not a bug to route around. Read the diagnosis to the user and stop.

## Step 7 — present the diff and STOP

Show, in the chat:

- the headline counts: proposed, dropped, records touched, blanks filled, existing values preserved
- **every ambiguous or unmatched meeting first** — these are decisions only the user can make
- the diff table, or its highlights with a pointer to `report.html` if it is long
- what the guards dropped and why, in one line each
- the fields the calls never answered
- the approval token and the exact approve command

Then **stop and wait**. Do not run the approve command in the same turn as the proposal,
even if the user pre-authorised it — `diff.py` will refuse anyway, and trying looks worse
than asking.

---

## The apply path — a later turn, after the user says yes

Only when the user has read the diff and says to apply it:

```bash
"$HOME/.leanscale-gtm/bin/meeting-to-crm" diff approve \
    --run "$RUN" --approved-by "<the user's real name>" --token <token from the report> --apply
# subset:  --only p-101,p-104,c-001
```

Use the name of the human who approved. Never put your own name, "Claude", "agent" or
"auto" in `--approved-by` — the audit log is a record of who decided, and a false name in
it is worse than no log at all.

Then execute **only** what is in `$RUN/write_plan.json`, one operation at a time:

```
Salesforce  update: sobject Opportunity, Id + the values map from the plan
            create: sobject Contact / Task / OpportunityContactRole
HubSpot     update: PATCH /crm/v3/objects/deals/{id} {"properties": {...}}
            create: POST /crm/v3/objects/contacts | /tasks | /notes,
                    then POST /crm/v4/objects/notes/{id}/associations/deals/{dealId}/...
```

Rules while writing:
- Follow plan order so `@ref:` placeholders resolve.
- One record at a time. If a write fails, record it and keep going — then report every failure.
- Never write a field that is not in the plan, even if you notice something else worth fixing.
  Propose it on the next run instead.

Record the outcome and log it:

```bash
cat > "$RUN/results.json" <<'JSON'
[ {"row_id":"p-101","status":"applied","tool":"salesforce_update_record"},
  {"row_id":"c-001","status":"applied","tool":"salesforce_create_record","crm_response_id":"003..."},
  {"row_id":"p-103","status":"failed","error":"INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST"} ]
JSON

"$HOME/.leanscale-gtm/bin/meeting-to-crm" diff audit --run "$RUN" --results "$RUN/results.json"
"$HOME/.leanscale-gtm/bin/meeting-to-crm" report --run "$RUN" --no-baseline
```

`audit` exits non-zero if you report a write that was not approved. If that happens, say so
loudly and stop — something went wrong that the user needs to see.

---

## Failure modes worth naming out loud

| What you see | What it means |
|---|---|
| Every meeting unmatched | attendee domains are not on the accounts, or the transcript source strips emails (Zoom, Meet, Otter). Fix the match strategy, not the data. |
| One account, many ambiguous meetings | that account has several open opportunities. Link calendar invites to the deal, or set overrides. |
| Many `quote_not_verified` | the transcript is being truncated before you see all of it. Check the adapter's page size. |
| Many `field_populated` | the allow-list policies do not match how the team works. Discuss policy, do not loosen it quietly. |
| `approve` refuses on the token | the diff changed since it was rendered. Re-render and let the human read it again. |
