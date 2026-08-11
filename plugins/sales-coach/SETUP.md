# Setting up Sales Coach

Read-only throughout. Nothing in your CRM, your recordings or your calendar is modified at
any point, and no data leaves your machine.

---

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents      # or the git URL you were given
/plugin install sales-coach@leanscale-gtm
```

Then restart Claude Code so the skills register. You should see `/sales-coach:setup` and
`/sales-coach:run`.

Requires **Python 3.9 or newer**. Nothing else — the analysis is standard library only, with
no pip install and no network access.

---

## 2. Connect a transcript source

You need **one** of these. A conversation-intelligence platform is not required; a folder of
exported files is a fully supported path.

| Source | What to connect | What to check first |
|---|---|---|
| **Gong** | Gong MCP server with an API key | The key must have **workspace** scope. A personal key sees only that user's calls, which looks like "we barely record anything". |
| **Fireflies** | Fireflies MCP server / API key | A member key returns only that member's meetings; you want an admin or team key. |
| **Chorus** | ZoomInfo/Chorus API access | Coverage varies. If nothing resolves, export and use the folder path. |
| **Grain** | Grain API token | Confirm transcripts are enabled on the workspace, not just recordings. |
| **Otter** | Usually no API — export instead | Otter → Export → **Text with timestamps and speaker names**, into a folder. |
| **Zoom** | Zoom MCP / API with `recording:read` | Cloud recording **and** audio transcript must both be on, or there is no transcript file. |
| **Google Meet** | Google Drive MCP | Meet transcripts land in Drive as `<Meeting> - Transcript` Docs. Have the folder ID ready. |
| **A folder** | Nothing to connect | `.vtt`, `.txt`, `.md`, `.json`. Name files so the date, account and rep are recoverable. |

**Emails matter more than you would expect.** Whether a speaker is one of yours is resolved
first from the provider's own internal/external flag, then from email domain, then from the
rep roster. Sources that carry only display names (Zoom VTT, Otter, Meet) work, but a
speaker the roster does not recognise stays unresolved — and unresolved calls are dropped
from every talk-time and question number rather than guessed at. Setup tells you which
bucket you are in before you rely on it.

## 3. Optional: connect your CRM

Salesforce or HubSpot, read-only. Without it everything still works; you lose the deal
linkage section — which framework gaps precede the deals that slip or lose. That is the
section that usually justifies the coaching time, so connect it if you can.

---

## 4. Run setup

```
/sales-coach:setup
```

It will, in this order:

1. **Probe** what is connected and tell you exactly what each tool provides.
2. **Read or create** `~/.leanscale-gtm/profile.json`, the org profile shared by every
   LeanScale GTM agent. If another agent already wrote it, you confirm rather than re-answer.
3. **Inventory your calls before asking you anything** — how many, over what date range, per
   rep, clustered by type, with the duration distribution and the internal email domains it
   found. It also runs a speaker-attribution dry run on three real calls, so you learn about
   an unusual export layout during setup rather than three weeks later.
4. **Interview you** on the things only you can answer:
   - **Your framework** — MEDDPICC, MEDDIC, BANT, SPICED, Challenger, Command of the
     Message, or your own. If it is your own you will be asked for each dimension, what
     evidence counts for it, and where the line between "met" and "partial" sits. That takes
     ten minutes and it is the difference between coaching against your standard and
     coaching against a generic rubric. Bring your enablement one-pager if you have one.
   - Which **call types** to coach.
   - **Rep start dates** — pulled from your CRM if a start-date field exists, otherwise
     asked. A rep ten weeks in is coached differently from one at three years.
   - **Talk-ratio and question-rate targets**, or accept the industry defaults, which the
     report labels as defaults until you replace them.
   - Whether output goes to the **manager** (default: one team report) or also to reps.
   - **Two or three exemplar calls** you consider genuinely good.
   - How often you intend to run it.
5. **Write** `~/.leanscale-gtm/sales-coach.json` and show you the file.
6. **Score one real call end to end** and show you the scorecard — every dimension, its
   status, the quote and the timestamp. If you disagree with any of it, that is a config
   edit, and setup will make it and re-score the same call in front of you.
7. Print a **pass/fail table** and a plain statement of what will and will not work.

Setup is re-runnable. Run it again after any change, and run it when a run fails — it
doubles as the health check.

---

## 5. First run

```
/sales-coach:run
```

Output lands in `./gtm-agents/sales-coach/<date-time>/`. Open `report.html`.

**Run one is your baseline** and says so on the page. The comparison starts on run two,
which shows what moved on every headline number. Keep the snapshots — they live in
`~/.leanscale-gtm/baselines/sales-coach/` and they are the evidence that the coaching
changed something.

---

## 6. See the output before connecting anything

Six fictional calls across four export shapes, with no credentials and no network:

```bash
cd <plugin directory>
export LEANSCALE_GTM_HOME=/tmp/sc-demo-home && mkdir -p $LEANSCALE_GTM_HOME
cp fixtures/profile.json $LEANSCALE_GTM_HOME/profile.json
cp fixtures/config.json  $LEANSCALE_GTM_HOME/sales-coach.json
python3 scripts/transcripts.py selftest
python3 scripts/analyze.py --run-dir /tmp/sc-demo --raw ./fixtures/raw
python3 scripts/report.py  --run-dir /tmp/sc-demo
open /tmp/sc-demo/report.html
```

`LEANSCALE_GTM_HOME` is overridden so the sample never touches your real config or your
baselines.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Run aborted — a required data source returned zero records` | Working as designed. A required source came back empty and the run refused to publish a clean-looking empty report. | Read the diagnosis in the message — it names the probable cause. Then re-run `/sales-coach:setup`. |
| Only a handful of calls for a whole team | The connected token is scoped to one user. | Get a workspace/team-scoped key. This is the single most common setup problem. |
| "Calls were listed but no transcript bodies came back" | Recording is on, transcription is not; or the API scope covers metadata but not transcript content. | Enable transcription on the platform, or widen the scope. Historic calls will not backfill. |
| A call parsed as 1–2 turns | Unrecognised export layout. | `python3 scripts/transcripts.py inspect <file>` shows what the parser saw. Send the first twenty lines and the format can be added. |
| Lots of calls marked "attribution degraded" | Speakers carry no email and no affiliation flag — often two people on one room mic, or an export that labels people "Speaker 2". | Add every internal domain to `transcript_source.internal_domains` and every rep to `reps`. Structurally: avoid shared room mics, or move to a source with a per-speaker internal flag. |
| "Scored dimensions dropped for missing or unverifiable evidence" | A quote could not be found verbatim in the transcript. | Usually a transcript problem — a paraphrase, a truncated recording, a merged speaker. If one dimension keeps failing, its `evidence_rule` in config is too vague to score consistently. |
| Every rep flagged "not enough calls to coach fairly" | The window is too short for your call volume. | Raise `window_days` to 60 or 90, or lower `min_calls_per_rep`. Below three calls a single bad conversation moves a rep's score more than their habits do. |
| Scores do not match your judgment | The framework definitions are not yours yet. | Edit `framework.dimensions[].met_means` and `evidence_rule` in `~/.leanscale-gtm/sales-coach.json` and re-run. This is expected on the first pass and is the intended way to tune it. |
| Deal linkage section says "not available" | No CRM connected, or no call could be matched to an opportunity. | Connect Salesforce/HubSpot; make sure `deal_id` is being captured for each call in `raw/calls.json`. |
| Everything reads `open`, nothing reads `slipped` | No close-date push tracking in the CRM. | Add `Original_Close_Date__c` and a push counter, or enable field history on `CloseDate`. |
| Report shows names you would rather not circulate | | Set `redact_pii_in_reports: true` in `~/.leanscale-gtm/profile.json`. Names and emails become stable pseudonyms in the reports; `raw/` and `findings.json` stay unredacted locally. |
| `ModuleNotFoundError: No module named 'lib'` | Running the scripts from a source checkout where the shared library has not been vendored in. | Use the installed plugin, or set `PYTHONPATH` to the directory containing `lib/`. |

---

## What this plugin never does

Writes to your CRM. Modifies, renames or deletes a recording. Sends email or Slack. Uploads
a transcript, a quote or a report anywhere. Phones home with telemetry. Deploys a report to
a website. Runs itself on a schedule.

Recording calls and using those recordings to evaluate employees is governed by your own
consent notices, recording laws and employment obligations. This plugin reports what is
already in your systems; whether you may record, retain or coach from a given conversation
is your decision, not its.
