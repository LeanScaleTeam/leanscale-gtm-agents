# Sales Coach

**Read-only, and built for the manager.** This plugin reads your call transcripts and
(optionally) your CRM, and writes nothing back to either — every output is a local file on
your machine. It is deliberately manager-first: the primary deliverable is **one team
pattern report** — the weakest dimension across the team, the specific calls that prove it,
each with a verbatim quote and a timestamp — because reps ignore per-call feedback and
managers act on patterns. Per-call reviews exist, but they are a secondary section you have
to switch on.

It coaches against **your** qualification framework, not a generic rubric. That is the
whole point: MEDDPICC, MEDDIC, BANT, SPICED, Challenger, Command of the Message, or a custom
framework captured properly during setup — its dimensions, what evidence counts for each,
and where the line between "met" and "partial" sits. If you already own a
conversation-intelligence tool, this is the part it does not do.

```
/sales-coach:setup     probe · inventory the calls you actually have · interview · score one real call
/sales-coach:run       fetch · score · analyse · report
```

---

## What you get

A single `report.html` you can open with the wifi off, opening with four numbers:

| | |
|---|---|
| **MEDDPICC coverage** | 46.7% — 45 scored dimension checks across 6 calls |
| **Weakest dimension** | 16.7% — Economic Buyer, missing on 5 of 6 calls |
| **Next step set** | 66.7% — target 90% (industry default) |
| **Calls analysed** | 6 — 5 with reliable speaker attribution |

then the findings, most severe first. The headline one reads like this:

> **[CRITICAL] Economic Buyer is the team's weakest dimension — 5 of 6 scored calls have it
> missing entirely.**
> On deals at or above $25,000 it is unresolved on 4 calls carrying $242,000 of pipeline.
> The table lists every one, with the moment on the call where the buyer opened the door
> and nobody walked through it.
>
> | Call | Rep | Deal | Status | Moment it was there to take |
> |---|---|---|---|---|
> | Fabrik Robotics — Discovery | Dana Whitfield | $85,000 | missing | 22:14 — "I'd have to get Raj comfortable with it. He owns the budget line…" |
> | Orbit Payments — Discovery | Dana Whitfield | $60,000 | missing | 02:43 — "our CFO gets the flash report on day nine…" |

and then, in order: **the Monday coaching agenda** (one topic for the room, one line per
rep), the **team scorecard** (every dimension × every rep, colour-coded), the **rep table**
with tenure alongside score, the **mechanics**, **exemplar calibration**, and **deal
linkage**.

---

## The scoring model

Each dimension on each call is scored `met` (2 points), `partial` (1), `missing` (0), or
`not_applicable` (excluded from both sides, so an early discovery call is not penalised for
having no paper process). Coverage is points over points available.

**No evidence, no score.** A `met` or `partial` must carry a verbatim quote and a timestamp.
The analysis then searches the transcript for that quote, and anything it cannot find is
demoted to `unscored` — counted as neither a pass nor a fail — and listed as a data-quality
finding. A coaching conversation that opens with a quote the rep cannot find in the
recording is over before it starts.

For a `missing` dimension there is nothing to quote, so the scorer supplies the
**missed moment** instead: where the buyer handed the rep the opening and the rep moved on.
That is the artefact you play in the team meeting.

**Judgment and arithmetic are separated on purpose.** Claude reads the transcripts and makes
every qualification judgment; Python parses, verifies the quotes, computes the mechanics,
rolls the scores up, and renders. Nothing in this plugin tries to score a framework
dimension with a regular expression.

**Tenure changes the coaching.** Each dimension is tagged fundamental or advanced. A rep
under 90 days is coached on fundamentals only — telling someone ten weeks in that their
paper process is weak spends the attention they have on the wrong thing. A rep past a year
is coached on the advanced dimensions, and the report flags the expensive pattern where a
tenured rep runs excellent discovery and no decision process, because those calls sound
good and the pipeline slips quietly.

---

## Transcript sources

There is no privileged source here. Roughly six in ten teams do not own a
conversation-intelligence platform, so a folder of exported files is a first-class path,
not a footnote.

| Source | How it is read | Speaker attribution |
|---|---|---|
| **Gong** | `list_calls` + `retrieve_transcripts` | `parties[].affiliation` — authoritative |
| **Fireflies** | `transcripts` / `transcript` GraphQL | email domain from `meeting_attendees` |
| **Chorus** | engagements + transcript | participant type flag |
| **Grain** | `/v1/recordings` (JSON or VTT) | email domain where present |
| **Otter** | export → folder | names only — roster match, or unresolved |
| **Zoom** | cloud recordings, `file_type: TRANSCRIPT` (VTT) | participants report, else roster |
| **Google Meet / Drive folder** | Drive search + read file content | calendar invite, else roster |
| **A local directory** | `.vtt` · `.txt` · `.md` · `.json` | roster + configured domains |

All of them normalize into one internal shape — speaker, timestamp, text, is_internal — and
everything downstream reads only that.

**Speaker attribution is the trap, and it is handled explicitly.** Internal speakers are
resolved in a fixed order of trust: the provider's own affiliation flag, then email domain,
then the call's participant list, then the rep roster. Where a speaker cannot be resolved —
two people on one room mic, an export that says only "Speaker 2" — the call is marked
degraded, **every talk-time, monologue and question number for it is dropped rather than
estimated**, and it is listed by name in the report with what to do about it. A report that
credits a customer's discovery question to the rep is worse than no report, because the team
can disprove it and then stops believing the rest of the page.

---

## Mechanics, with defaults labelled as defaults

Talk-to-listen ratio, longest uninterrupted monologue with the timestamp it starts at,
question count and question rate per 30 minutes, next-step-set rate, competitor mentions,
and the first pricing moment.

Every threshold ships as an **industry default** and the report says so on the page until
you change it: your side under 55% of speaking time, longest stretch under 150 seconds, at
least 8 questions per 30 minutes, 90% of calls ending with a dated next step, pricing
flagged inside the first 25% of a call. Override them in `mechanics_targets` and set
`source` to the name of your own methodology; the labels change with it.

Speaking time comes from the transcript's own timestamps where the export provides them and
is otherwise estimated from word count at 150 wpm — whichever was used is recorded per call
and stated in the report, because mixing the two skews a talk ratio by more than the thing
you are measuring.

---

## Deal linkage and calibration

**Deal linkage** (optional, when a CRM is connected) correlates framework gaps against what
happened to the deal — coverage on won deals versus deals that slipped or lost. This is the
finding that gets budget, and it states its sample size and calls itself directional when
the numbers are thin, because a coach who over-reads six deals stops trusting the tool on
the seventh.

**Exemplar calibration** scores the two or three calls you nominated as good during setup
and shows how the framework rated them against everything else. If your exemplars do not
score better, the report says so plainly — that means the framework is not measuring what
you value, and it is much better to learn that on run one.

---

## Baseline and delta

Run one writes a baseline snapshot to `~/.leanscale-gtm/baselines/sales-coach/` and says so
in the report: *this is your baseline, the comparison starts next run.* Every run after it
shows movement on every headline number and every finding count. An agent that cannot show
its own progress is not worth running twice.

---

## Privacy

Transcripts are read through **your** connectors, with your credentials, into a directory on
your machine. The analysis runs locally in Python with no network access at all, and nothing
— no transcript, no quote, no report, no telemetry — is uploaded anywhere. The only network
traffic in a run is your own MCP servers, which you connected. Reports are local files;
nothing is deployed or hosted.

If `redact_pii_in_reports` is set in `~/.leanscale-gtm/profile.json`, person names and email
addresses are replaced with stable pseudonyms in `report.md` and `report.html` (and in the
per-rep coaching cards), while `raw/` and `findings.json` stay unredacted on your disk so
you can still work the detail.

Recording calls, and using those recordings to evaluate employees, is governed by your own
consent notices, recording laws and employment obligations. This plugin surfaces what is
already in your systems; it does not decide whether you are permitted to record, retain or
coach from a given conversation. That call is yours.

---

## Run output

```
./gtm-agents/sales-coach/<YYYY-MM-DD-HHMM>/
    raw/                 exactly what came back from each source, plus the framework scoring pass
    manifest.json        provenance and per-source record counts
    findings.json        the machine-readable result, including every per-call scorecard
    report.md
    report.html          self-contained, opens offline
    coaching/<rep>.md    only when output_audience is "reps"
```

If a required source returns zero records the run **stops** with a diagnosis instead of
emitting a clean-looking empty report. "0 issues found" because authentication failed is
worse than a crash.

---

## Try it offline

The bundled fixtures are six fictional calls across four export shapes — Zoom VTT,
Teams/Meet VTT, a diarized text export, a diarized export where one speaker could not be
identified, the Gong JSON shape and the Fireflies JSON shape — with a scored-calls file and
a CRM pull. No credentials, no network:

```bash
export LEANSCALE_GTM_HOME=/tmp/sc-demo-home && mkdir -p $LEANSCALE_GTM_HOME
cp fixtures/profile.json $LEANSCALE_GTM_HOME/profile.json
cp fixtures/config.json  $LEANSCALE_GTM_HOME/sales-coach.json
python3 scripts/transcripts.py selftest
python3 scripts/analyze.py --run-dir /tmp/sc-demo --raw ./fixtures/raw
python3 scripts/report.py  --run-dir /tmp/sc-demo
open /tmp/sc-demo/report.html
```

Requires Python 3.9+ and nothing else — no pip install, standard library only.

---

## Configuration

`~/.leanscale-gtm/profile.json` — shared across every LeanScale GTM agent; written once.
`~/.leanscale-gtm/sales-coach.json` — this plugin's settings, documented key by key in
`config.example.json`. Both live in your home directory, not in the plugin, so they survive
plugin updates. Edit them by hand whenever you like; `/sales-coach:setup` is re-runnable and
only fills in what is missing.
