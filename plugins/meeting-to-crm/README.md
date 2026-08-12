# Meeting to CRM

**This is the one agent in the suite that can write to your CRM, so here is exactly what it
can and cannot do, in plain words. It never writes anything on its own. Every run is a dry
run: it reads your call transcripts, works out which record each call belongs to, and
produces a diff table — object, record, field, the value that is there now, the value it
proposes, the verbatim quote and timestamp that justifies it, and a confidence. Then it
stops. Nothing is written on the same turn a change is proposed. To apply a batch, a named
human passes an explicit `--apply` flag along with a token that only matches the exact diff
they just read; change one value and the token stops matching. It can only ever propose
fields you listed in your own config — a field that is not on that list is dropped before
it reaches the diff, however confident the model is, and Amount, close date, stage,
probability and forecast category are off that list by default and need a second, separate
opt-in because they are the rep's call. It never overwrites a field that already has a
value unless you turned that on for that specific field; the default is fill-blanks-only,
so what your reps wrote survives. It refuses to run unattended: no cron, no scheduler, no
CI. And every applied write appends one line to `~/.leanscale-gtm/audit/meeting-to-crm.log`
with the record, the field, the old value, the new value, the call it came from and who
approved it. Nothing leaves your machine — the only network traffic is to the MCP servers
you already connected.**

---

## What it does

After a sales call, a rep is supposed to open the CRM and type up what happened. Mostly
they do not, and the deal review two weeks later runs on notes from the first meeting.

This reads the transcript and proposes what they would have typed:

- **Next step** and **next-step date** — the commitment, its owner, its date
- **Qualification fields** — your framework (MEDDPICC, MEDDIC, BANT, SPICED or your own)
  mapped onto your actual field API names during setup
- **Competitor mentioned**, validated against your picklist
- **Pain / use case**, in the buyer's words
- **Decision process and timeline** — the steps they described and what gates what
- **New stakeholders** heard on the call, as contact and contact-role records
- **A call summary** to the activity or note object
- **A list of what it could not determine** — the qualification dimensions the call never
  covered. A blank is a correct answer, and the list of blanks is the list of questions
  your reps are not asking.

It is deliberately a bounded, checkable task rather than a judgment call. Across a
43-company study of exactly this kind of company, the AI workflows that reached production
were the ones that did something a human already did by hand and could verify at a glance.
The ones that asked a human to trust a judgment stalled at single-digit adoption.

## How the safety works, mechanically

Every guard below is enforced in Python, in `scripts/diff.py`, not in a prompt. A guard
that lives only in an instruction file is a suggestion.

| Guard | What it does |
|---|---|
| **Allow-list** | Only fields in `field_allowlist` may be proposed. Anything else is dropped and reported. |
| **Restricted fields** | Amount, CloseDate, StageName, Probability and ForecastCategory need a second opt-in in `restricted_fields_opt_in`, on top of the allow-list. |
| **Read-only** | System, formula, roll-up and HubSpot calculated properties are never written, even if you allow-list them by mistake. |
| **Overwrite policy** | Per field: `if_blank` (default — a populated field is preserved), `always`, or `append` (adds a dated block underneath and never deletes anything). |
| **Evidence** | Every proposal needs a verbatim quote, and Python checks that quote against the actual transcript text. A paraphrase presented as a quote is dropped and reported as an invention, not tidied up. |
| **Matching** | A meeting that matches more than one plausible record, or none, produces zero proposals. It is surfaced for a human instead. |
| **Value sanity** | Picklist membership, date parsing, field length, and a next-step date that lands before the call itself. |
| **Dry-run** | No config key can enable apply mode. If one is present it is ignored and the run says so. |
| **Approval** | `--apply`, a named human, and a token fingerprinting the exact rendered diff. Refused if the report was never rendered, if the token does not match, or if the review window has not elapsed. |
| **Unattended** | Refuses to apply when CI, cron or scheduler environment markers are present. |
| **Audit** | One JSON line per applied field. A write reported but never approved is still logged, flagged `approved:false`, and the command exits non-zero. |

Run them yourself against the bundled fixtures:

```bash
"$HOME/.leanscale-gtm/bin/meeting-to-crm" diff selftest
```

38 checks, in a sandbox that does not touch your config or your audit log.

## Do not put this on a schedule

There is no scheduled mode and there will not be one. A batch that nobody is watching
cannot be approved by anyone, and an agent that writes to a CRM every morning at 7am with
no human in the loop is how a whole quarter of pipeline data quietly becomes untrustworthy.
`diff.py approve` checks for scheduler environment markers and refuses. Run it after a call,
or at the end of the day, with a person reading the diff.

## Transcript sources

First-class, all of them: **Gong, Fireflies, Chorus, Grain, Otter, Zoom, Google Meet or a
Drive folder, a plain local directory of transcript files, and manual paste.** Only about
four in ten companies this size have a conversation-intelligence tool, so the folder-of-files
path is a real supported option, not a footnote. Each source has its own adapter section in
`skills/run/SKILL.md`, and sample payloads for four of them are in `fixtures/transcripts/`.

One thing worth knowing before you pick: **Zoom, Google Meet and Otter transcripts usually
carry no attendee email addresses.** Without emails you lose attendee-domain matching, and
matching falls back to the meeting-title convention or the calendar event. Setup will tell
you which situation you are in rather than letting you find out from a run full of
unmatched meetings.

## CRMs

Salesforce and HubSpot are both first class, with real queries written into the run skill —
SOQL for one, search-endpoint payloads for the other. There are complete fixture sets for
both in `fixtures/salesforce/` and `fixtures/hubspot/`.

## What it reads

- Call transcripts from your configured source, for the window you choose (default 7 days)
- Accounts, contacts, opportunities/deals — to find the right record and read its current
  field values
- Your CRM field schema — types, picklist values, lengths and which fields are updateable

Reports are written to `./gtm-agents/meeting-to-crm/<timestamp>/` and stay there. They are
never uploaded anywhere.

## Sample output

```
PROPOSED CHANGES — NOTHING HAS BEEN WRITTEN
==============================================================================

  [p-101] Opportunity 0065f00000NW1AAA · Northwind Analytics — Platform Expansion
      field     NextStep  (Next step)
      current   Follow up after discovery
      proposed  Send the security questionnaire to Priya today; she routes it to legal
                by Aug 14. Joint review Aug 18 with Marcus Vela in the room.
      policy    always   confidence 0.94
      quote     "Send me the security questionnaire and I will get it to legal by the fourteenth."
                — Priya Raman at 00:31:34 · Northwind Analytics <> Acme — Discovery

DROPPED BY THE GUARDS — 15
------------------------------------------------------------------------------
  [p-202] Opportunity.Identified_Pain__c — field_populated: already reads "Reps rebuild
          the same forecast spreadsheet every Monday." and its policy is 'if_blank'
  [p-203] Opportunity.Amount — field_restricted: forecast-bearing and off by default
  [p-401] Opportunity.Metrics__c — quote_not_verified: that quote does not appear in the
          transcript
  [p-301] Opportunity.Identified_Pain__c — match_ambiguous: two candidates within 0.00 of
          each other — one account with more than one open opportunity

proposed 31 · ready 16 · dropped 15 · records 5 · blanks filled 14 · existing values preserved 1
approval token: ffce3d198e1a
```

The HTML report carries the same diff as a table you can read in thirty seconds, plus the
severity-sorted findings and the method footer.

## The headline numbers

Five, and no more:

| Score | Means |
|---|---|
| **Fields proposed** | changes that survived every guard and are waiting on a human |
| **Fields applied** | what actually landed, from the audit trail — not what was hoped for |
| **Records touched** | how many records the batch changes |
| **Blanks filled** | empty fields the call could answer |
| **Existing values preserved** | proposals dropped rather than overwrite what a rep already wrote |

That last one is the number to watch. If it climbs, either your allow-list policies do not
match how the team actually works, or the agent is trying to relitigate fields a human
curates. Both are worth a conversation; neither is fixed by loosening the policy quietly.

The first run writes a baseline and says so. Every run after it shows what moved.

## Usage

```
/meeting-to-crm:setup                # once — probe, discover, agree the allow-list, smoke test
/meeting-to-crm:run                  # after a call, or at the end of the day
/meeting-to-crm:run --window 1d
/meeting-to-crm:run --meeting <id>   # one specific call
```

Then, after reading the diff, on a later turn:

```
/meeting-to-crm:run --apply          # proposes, stops, and applies once you approve by name
```

Config lives in `~/.leanscale-gtm/meeting-to-crm.json` and survives plugin updates. See
`config.example.json` — every key is documented inline, and it is meant to be edited by
hand.

## Files

```
.claude-plugin/plugin.json
README.md · SETUP.md
skills/run/SKILL.md        the pipeline, with an adapter section per transcript source
skills/setup/SKILL.md      probe · discovery · interview · smoke test
scripts/diff.py            the guard engine, the diff builder, the audit-log writer
scripts/analyze.py         raw/*.json -> diff.json + findings.json   (stdlib, offline)
scripts/report.py          findings.json -> report.md + report.html  (stdlib, offline)
fixtures/salesforce/       full sample run, including a populated field and an ambiguous match
fixtures/hubspot/          the same, HubSpot-shaped, with a flat non-diarized transcript
fixtures/transcripts/      what each adapter consumes: Gong, Fireflies, Zoom VTT, plain text
config.example.json
```
