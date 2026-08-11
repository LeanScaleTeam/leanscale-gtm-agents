---
name: run
description: >-
  Score recent sales calls against the team's own qualification framework and produce ONE
  team pattern report for the manager — the weakest dimension, the calls that prove it,
  with verbatim quotes and timestamps. Use when the user says "run sales coach", "coach my
  team", "review this month's calls", "how is the team doing on MEDDPICC / MEDDIC / BANT /
  SPICED / Challenger", "what should I coach on Monday", "call review", "which reps are
  missing the economic buyer", or asks what patterns show up across recent calls. Read-only:
  it reads transcripts and CRM records and writes only local files.
argument-hint: "[--window 30d] [--rep \"Dana Whitfield\"] [--call-type discovery] [--per-call]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---

# Sales Coach — run

You are producing the coaching brief a sales manager reads before Monday's team meeting.

**The primary output is one team pattern, not nine call reviews.** Reps ignore per-call
feedback; managers act on patterns. Lead with the single weakest dimension across the team
and the specific calls that prove it. Per-call reviews are a secondary, opt-in section.

**Read-only.** You read transcripts and (optionally) CRM records. You write only into the
run directory. Never modify a CRM record, never rename or delete a recording, never upload
anything anywhere.

---

## 0. Preflight

```bash
cat ~/.leanscale-gtm/profile.json ~/.leanscale-gtm/sales-coach.json 2>/dev/null
```

If `sales-coach.json` is missing, stop and say: *"No coaching config yet — run
`/sales-coach:setup` first. It takes about ten minutes and it is what makes the scoring
match your framework rather than a generic rubric."* Do not invent a framework.

Read from config and state them back to the user in one line before you start:
framework name, call types, window, rep roster size, exemplar calls, whether per-call
reviews are on.

Create the run directory:

```bash
RUN="./gtm-agents/sales-coach/$(date +%Y-%m-%d-%H%M)"
mkdir -p "$RUN/raw/transcripts"
echo "$RUN"
```

Arguments override config: `--window 30d`, `--rep`, `--call-type`, `--per-call`
(force the per-call appendix on for this run only).

---

## 1. Transcript source adapters

`transcript_source.provider` in config tells you which one to use. Probe first — never
assume a tool name, because customers connect their own servers:

```
ToolSearch("transcripts meetings recordings calls")
ToolSearch("get transcript")
ToolSearch("read file content drive folder")
```

Every adapter produces the same two things:

1. `raw/transcripts/<call_id>.<ext>` — the transcript exactly as it came back, unmodified.
2. an entry in `raw/calls.json` (schema in §2).

**Whatever the source, capture participant emails.** Email domain is how a speaker gets
resolved to internal, and it is the difference between a coaching report and a guess.

---

### Gong

Probe: `ToolSearch("gong calls transcript")`. Tools are usually named like `list_calls` /
`retrieve_transcripts`, wrapping the Gong v2 API.

List (`POST /v2/calls` or the tool's equivalent):

```json
{ "filter": { "fromDateTime": "2026-07-11T00:00:00Z",
              "toDateTime":   "2026-08-10T23:59:59Z" },
  "contentSelector": { "exposedFields": { "parties": true, "content": { "structure": true } } } }
```

Transcripts (`POST /v2/calls/transcript`):

```json
{ "filter": { "callIds": ["8891", "8892"] } }
```

Map: `metaData.id` → `call_id`; `metaData.title` → `title`; `metaData.started` →
`started_at`; `metaData.duration` → `duration_sec`; `parties[]` → `participants[]` with
`speaker_id` = `parties[].speakerId`, `email` = `emailAddress`, `affiliation` =
`Internal`/`External`.

**Gong is the easy case for attribution** — `parties[].affiliation` is authoritative, so
pass it straight through and set `transcript_format: "gong_json"`, `time_unit: "ms"`.

Deal linkage: `metaData.crmContext` or the tool's opportunity association gives `deal_id`.

---

### Fireflies

Probe: `ToolSearch("fireflies transcript")`. Tools are usually named like
`fireflies_get_transcripts` (list) and `fireflies_get_transcript` (one), over GraphQL.

```graphql
query { transcripts(fromDate: "2026-07-11", toDate: "2026-08-10", limit: 50) {
  id title date duration
  meeting_attendees { displayName email }
} }

query { transcript(id: "FF-77123") {
  id title date duration
  meeting_attendees { displayName email }
  sentences { index speaker_name raw_text start_time end_time }
} }
```

Map: `id` → `call_id`; `meeting_attendees[]` → `participants[]` (name + email; **no
affiliation flag**, so attribution rides entirely on email domain — make sure every
internal domain is in config). Save the whole transcript object as
`raw/transcripts/<call_id>.json`, `transcript_format: "fireflies_json"`,
`time_unit: "s"`. Note Fireflies `duration` is **minutes** — multiply by 60.

---

### Chorus (ZoomInfo)

Probe: `ToolSearch("chorus engagements transcript")`. Coverage varies; if there is no
Chorus MCP, use the platform export and take the **local directory** path — say so plainly
rather than pretending it is connected.

List engagements over the window, then fetch the transcript per engagement. Map
`engagement.id` → `call_id`, `participants[].is_internal` / `participantType` →
`affiliation` (Chorus does carry this — use it), `participants[].email` → email.
Save as JSON, `transcript_format: "chorus_json"`.

---

### Grain

Probe: `ToolSearch("grain recordings transcript")`. Grain's API lists recordings and
returns a transcript as JSON or VTT:

```
GET /v1/recordings?cursor=...            -> id, title, start_datetime, end_datetime, participants[]
GET /v1/recordings/{id}?transcript_format=json
```

Map `participants[].email` where present. Grain often has no affiliation flag; resolve by
domain. JSON → `transcript_format: "json"`; VTT → `"vtt"`.

---

### Otter

Otter's API access is limited and often absent. Two supported paths:

1. If a tool resolves, list conversations over the window and fetch each transcript.
2. Otherwise — the common case — have the user export (Otter: *Export → Text with
   timestamps and speaker names*) into a folder and use the **local directory** adapter.

Otter exports are diarized text: `Speaker Name  12:04` on its own line, then the text.
`transcript_format: "diarized_text"`. Otter carries **no emails**, so build
`participants[]` from the calendar invite or ask the user; where you cannot, leave the
speaker unresolved rather than assuming.

---

### Zoom

Probe: `ToolSearch("zoom recordings")`. Cloud recordings:

```
GET /v2/users/me/recordings?from=2026-07-11&to=2026-08-10&page_size=100
```

Each meeting's `recording_files[]` contains an entry with `file_type: "TRANSCRIPT"` — that
download is a **VTT** file. Save it as `raw/transcripts/<call_id>.vtt`,
`transcript_format: "vtt"`. Zoom VTT cues look like `Dana Whitfield: text` and the parser
handles that.

Participants: `GET /v2/report/meetings/{meetingId}/participants` gives names and, when the
account allows it, emails. Get them — a Zoom VTT alone has no domain information at all.

---

### Google Meet / a Google Drive folder of transcripts

Probe: `ToolSearch("drive search files read file content")`.

Meet writes transcripts into Drive as Google Docs named `<Meeting> - Transcript`. Search
the folder from config (`transcript_source.folder_id`), or by name:

```
q: "'<FOLDER_ID>' in parents and modifiedTime > '2026-07-11T00:00:00' and trashed = false"
q: "name contains 'Transcript' and modifiedTime > '2026-07-11T00:00:00'"
```

Read each file's text content and save it as `raw/transcripts/<call_id>.txt`,
`transcript_format: "diarized_text"`. Meet transcripts are `Name: text` lines with a
timestamp header; the parser handles them.

Attendees come from the calendar event if a calendar tool is connected, or from the
transcript's own attendee header. **Get emails from the calendar event where you can** —
Drive transcripts carry names only.

---

### Local directory (no conversation-intelligence platform)

This is a first-class path, not a fallback. Roughly six in ten teams do not own a
conversation-intelligence tool.

```bash
ls -la "$(python3 -c 'import os,sys;print(os.path.expanduser(sys.argv[1]))' "<local_directory>")"
find "<dir>" -maxdepth 2 \( -name '*.vtt' -o -name '*.txt' -o -name '*.md' -o -name '*.json' \) -newermt '30 days ago'
```

Copy (do not move) each file into `raw/transcripts/`. Infer what you can from the filename
— `2026-07-14 Fabrik Robotics discovery Dana.vtt` gives you date, account, type and rep —
then **confirm the mapping with the user in one table before scoring.** Getting the rep
wrong is the one error that poisons everything downstream.

Check what the parser actually saw before you trust it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transcripts.py" inspect "<file>" --internal-domain acme.com
```

It prints the detected format, every speaker, how many turns each has, and whether each
resolved to internal, external or UNRESOLVED. If a real conversation comes back as two
turns, the layout is unusual — show the user the first twenty lines and ask.

---

## 2. Write `raw/calls.json`

```jsonc
{
  "source": "gong",                      // or the provider you used; "mixed" is fine
  "fetched_at": "2026-08-10T13:02:00Z",
  "window": { "start": "2026-07-11", "end": "2026-08-10" },
  "internal_domains": ["acme.com"],
  "calls": [
    {
      "call_id": "gong-8891",
      "source": "gong",
      "title": "Fabrik Robotics <> Acme — Discovery",
      "started_at": "2026-07-14T15:02:00Z",
      "duration_sec": 2430,
      "call_type": "discovery",          // must match one of config.call_types
      "account": "Fabrik Robotics",
      "deal_id": "0061",                 // CRM opportunity id, if you can get it
      "deal_amount": 85000,
      "rep": "Dana Whitfield",
      "rep_email": "dana@acme.com",
      "transcript_file": "transcripts/gong-8891.json",
      "transcript_format": "gong_json",  // gong_json | fireflies_json | json | vtt | diarized_text
      "time_unit": "ms",                 // optional; only when you know
      "participants": [
        { "name": "Dana Whitfield", "email": "dana@acme.com", "speaker_id": "1001", "affiliation": "Internal" },
        { "name": "Ines Okafor",  "email": "ines@fabrik.com", "speaker_id": "2001", "affiliation": "External" }
      ]
    }
  ]
}
```

Then normalize and read the attribution report:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transcripts.py" normalize \
  --raw "$RUN/raw" --config ~/.leanscale-gtm/sales-coach.json
```

It prints one line per call with `attribution=high|medium|low`. **Any call at `low` is a
call where you must not attribute a quote to a named rep.** Mechanics for it are dropped
automatically; your job is to keep the framework quotes labelled exactly as the transcript
labels them (`"Speaker 2 (unidentified, customer side)"`), and to set `attribution_caveat`
on that call in the scored file.

---

## 3. Score each call against the framework

**This is your job, not Python's.** Read `raw/normalized_calls.json`. For each call, read
the whole transcript and score every dimension in `config.framework.dimensions`, using that
dimension's own `evidence_rule` and `met_means` — not a generic rubric, not your own idea
of good discovery.

Statuses:

| Status | When | Evidence required |
|---|---|---|
| `met` | clears `met_means` | **verbatim quote + timestamp, mandatory** |
| `partial` | evidence exists but falls short of `met_means` | **verbatim quote + timestamp, mandatory** |
| `missing` | applicable, not established | no quote possible — supply `missed_moment` if the buyer opened the door |
| `not_applicable` | genuinely does not apply at this stage | a one-line `rationale`, mandatory |

Rules that are not negotiable:

- **No evidence, no score.** A `met` or `partial` without a quote and a timestamp is
  demoted to `unscored` by `analyze.py` and reported as a data-quality failure.
- **Quotes must be verbatim.** `analyze.py` searches for every quote in the transcript. If
  it is not there, the score is dropped. Copy and paste; do not tidy up grammar.
- **`missed_moment` is the most valuable thing you produce.** For a `missing` dimension,
  find the moment where the buyer handed the rep the opening and the rep moved on. "At
  22:14 she said she needs Raj comfortable and he owns the budget line, and the rep said
  'Understood' and started the demo" is a coaching conversation. "Economic buyer: missing"
  is not.
- **Never attribute across the internal/external line on a low-attribution call.**
- Score the **exemplar calls first** (`config.exemplar_call_ids`). They calibrate you: if
  a call the manager considers excellent scores badly, do not adjust the score — that gap
  is a finding, and the report reports it.

Write `raw/scored_calls.json`:

```jsonc
{
  "framework": "MEDDPICC",
  "scored_at": "2026-08-10T13:41:00Z",
  "calls": [
    {
      "call_id": "gong-8891",
      "rep": "Dana Whitfield",
      "rep_email": "dana@acme.com",
      "call_type": "discovery",
      "deal_id": "0061",
      "attribution_caveat": null,        // set a sentence here when attribution is low
      "dimensions": [
        { "key": "metrics", "status": "met",
          "evidence": { "quote": "It's about thirty hours a month between me and one analyst.",
                        "timestamp": "03:03", "speaker": "Tom Reddy", "speaker_side": "customer" },
          "rationale": "Quantified by the buyer and pushed one level further by the rep." },
        { "key": "economic_buyer", "status": "missing", "evidence": {},
          "missed_moment": { "quote": "I'd have to get Raj comfortable with it. He owns the budget line",
                             "timestamp": "22:14", "speaker": "Ines Okafor" },
          "rationale": "The buyer named the budget owner; the rep said 'Understood' and moved to the demo." }
      ],
      "next_step": {
        "set": true,
        "quote": "Tuesday the twenty-first at ten, ninety minutes, you, Tom, and one of our solution architects.",
        "timestamp": "39:12",
        "detail": "Named day, time, length and attendees."
      },
      "pricing": {
        "discussed": true, "first_at": "03:02", "raised_by": "customer",
        "speaker": "Ana Petrova", "quote": "can I ask what this costs",
        "rationale": "Asked before any need was established."
      },
      "notable": { "strength": "one sentence", "risk": "one sentence" }
    }
  ]
}
```

`next_step.set` is **true only** when a specific date (or a named day) and the people
involved were agreed on the call. "I'll follow up in a week or so" is `false`.

`pricing.discussed` is a judgment, not a keyword hit — "the pricing model she never got to
build" is not a pricing discussion. Record it and Python will use it; omit the block and
Python falls back to a keyword locator that the report labels as one.

---

## 4. Optional: CRM deal linkage

This is the section that gets budget: which framework gaps precede the deals that slip or
lose. Skip it if no CRM is connected — the report marks it unavailable rather than silently
omitting it.

Probe: `ToolSearch("run_soql_query salesforce")` / `ToolSearch("hubspot crm search")`.

**Salesforce:**

```sql
SELECT Id, Name, Amount, StageName, IsClosed, IsWon, CloseDate,
       Owner.Name, Owner.Email, Account.Name
FROM Opportunity
WHERE Id IN ('0061','0062','0063')
```

If the org tracks pushes, add the fields you found in setup — commonly
`Original_Close_Date__c` and a push counter. Without them a still-open deal reads as
`open` rather than `slipped`, which is the safe direction to be wrong in.

**HubSpot** (CRM search on `deals`):

```json
{ "filterGroups": [ { "filters": [
      { "propertyName": "hs_object_id", "operator": "IN", "values": ["0061","0062"] } ] } ],
  "properties": ["dealname","amount","dealstage","closedate","hs_is_closed","hs_is_closed_won",
                 "hs_date_entered_closedwon","hubspot_owner_id","hs_deal_stage_probability"],
  "limit": 100 }
```

Save as `raw/deals.json`:

```json
{ "source": "salesforce", "query": "<the exact query you ran>", "records": [ ... ] }
```

Either CRM shape works — `normalize_records` flattens both.

---

## 5. Provenance, then run the pipeline

Write `raw/_fetch_log.json` as you go — one entry per source, with a `diagnosis` that says
what is probably wrong if it comes back empty. "Gong returned nothing" is useless; "the
connected Gong token is scoped to one user, and a manager needs workspace scope" is
actionable.

```json
{ "window": { "start": "2026-07-11", "end": "2026-08-10" },
  "sources": [
    { "name": "calls listed (transcript source)", "tool": "list_calls",
      "query": "fromDateTime 2026-07-11 -> 2026-08-10, types discovery+demo",
      "count": 9, "required": true,
      "diagnosis": "No calls came back. The connected identity may only see its own recordings — a manager needs workspace or team scope — or the window predates retention." },
    { "name": "transcripts fetched", "tool": "retrieve_transcripts", "count": 9, "required": true,
      "diagnosis": "Calls listed but no transcript bodies — recordings may exist without transcription enabled." },
    { "name": "framework scoring pass", "tool": "claude", "count": 9, "required": true,
      "diagnosis": "Nothing scored — the scoring step did not run or scored_calls.json is malformed." },
    { "name": "crm deals (optional linkage)", "tool": "run_soql_query", "count": 9, "required": false,
      "diagnosis": "Optional; without it the report cannot correlate gaps against deal outcomes." }
  ],
  "warnings": [] }
```

Then:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" --run-dir "$RUN"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py"  --run-dir "$RUN"
```

`analyze.py` writes `manifest.json` first and **stops the run** if a required source came
back empty. If it does, do not work around it — relay the diagnosis and stop. A report that
says "no issues" because auth failed is worse than a crash.

---

## 6. What to tell the user

Report in this order. Keep it short — the report is the artefact, your message is the
pointer.

1. **The one thing.** The weakest dimension, its coverage, and how many calls show it.
   Name two calls with timestamps they can play in the meeting.
2. **Anything critical.** Especially the deal-outcome correlation, if it fired.
3. **Per rep, one line each** — tenure band and the single thing to coach. Do not list
   every dimension per rep.
4. **What was not covered** — degraded calls, missing CRM, reps with too few calls. Say
   "unavailable, not clean."
5. **On run one only**, say plainly: *"This is your baseline. The comparison starts next
   run."* `report.py` prints the exact wording — use it.
   On later runs, lead with what moved: *"Economic Buyer coverage went from 17% to 34%."*
6. The file paths: `report.html` (open it), `report.md`, `findings.json`.

Do not paste the whole report into chat. Do not promise a per-call review unless
`include_per_call_reviews` is on.
