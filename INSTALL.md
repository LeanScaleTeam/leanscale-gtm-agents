# Installing the LeanScale GTM Agents

Ten agents that read your Salesforce or HubSpot and tell you what your revenue systems are
actually doing. Nine are strictly read-only.

## Before you start

You need three things:

1. **A client that reads `SKILL.md` and can run local commands** — Claude Code (desktop app,
   CLI or IDE extension), Cursor, VS Code with Copilot, OpenAI Codex CLI, or Gemini CLI.
   See [which clients work](#which-clients-work) below — two popular ones cannot, and it is
   better to know now.
2. **Python 3.9 or newer** — check with `python3 --version`. There is nothing to `pip install`;
   the analysis scripts use only the standard library.
3. **A CRM connector already authorised in that client** — the Salesforce or HubSpot MCP server,
   connected as a user who can read the objects you care about. The agents run under *your*
   permissions: if you can't see a record, neither can the agent.

Optional, and only for three of the ten: a source of call transcripts. Gong, Fireflies, Chorus,
Grain, Otter, Zoom, a Google Drive folder, or just a directory of exported transcript files all
work. **No agent requires a particular vendor.**

## Which clients work

The agents read your CRM over MCP and then run **local Python** over what came back. A client
therefore needs all three: it reads `SKILL.md`, it speaks MCP, and it can execute local
scripts.

| Client | Runs the agents? |
|---|---|
| Claude Code | Yes — `/plugin install`, the best experience |
| Cursor | Yes — via `tools/install-skills.py` |
| VS Code / Copilot | Yes — via `tools/install-skills.py` |
| OpenAI Codex CLI | Yes — via `tools/install-skills.py` |
| Gemini CLI | Yes — via `tools/install-skills.py` |
| ChatGPT (web) | **No** — cannot run local scripts against your CRM |
| Claude.ai / Claude Desktop | **No** — same reason |

The last two are not a temporary gap. Those products can connect the LeanScale MCP perfectly
well; they just cannot execute the analysis that turns CRM records into a report.

## Install — Claude Code

```bash
# 1. Point Claude Code at the marketplace (use the folder you unzipped)
/plugin marketplace add ./leanscale-gtm-agents

# 2. Install the agents you want
/plugin install crm-hygiene@leanscale-gtm
/plugin install system-map@leanscale-gtm
```

Then restart Claude Code and confirm the commands appear.

## Install — Cursor, VS Code, Codex CLI, Gemini CLI

Clone the repo somewhere permanent, then copy the skills into your client's skills directory:

```bash
git clone https://github.com/LeanScaleTeam/leanscale-gtm-agents.git
cd leanscale-gtm-agents

python3 tools/install-skills.py --target ~/.cursor/skills
```

Use `--target .github/skills` for VS Code / Copilot, and `--plugin <name>` (repeatable) to
install only some of them. `--dry-run` shows what it would do first.

Two things it handles that copying by hand does not:

- **It renames the skills.** Every plugin ships skills called `run` and `setup`. Claude Code
  namespaces them (`/crm-hygiene:run`); a flat skills directory does not, so ten plugins would
  give you ten skills called `run`. They install as `crm-hygiene-run`, `crm-hygiene-setup`, and
  so on.
- **It records where the clone lives**, so the agents can find their own analysis scripts.
  Keep the clone where it is, or re-run the script after moving it.

There are no slash commands outside Claude Code. Ask for the agent in plain language instead —
*"set up crm hygiene"*, *"audit my CRM"*, *"how dirty is our Salesforce"*. The skills are
written to be found that way.

## Set each one up

```bash
/crm-hygiene:setup
```

Setup does five things, in this order:

1. **Probes your connectors** and tells you exactly which capability each resolved tool provides.
2. **Reads your CRM** — objects, record counts, picklists, field fill-rates, custom fields, active
   vs. inactive users, fiscal settings.
3. **Asks you what the data couldn't answer**, phrased in terms of what it found.
4. **Writes your config** to `~/.leanscale-gtm/` and shows you the file.
5. **Runs a smoke test** against a small slice and shows you a real finding.

It ends with a pass/fail table and a plain statement of what will and won't work. Setup is
idempotent — re-run it any time a run fails and it doubles as the health check.

**You only describe your business once.** The first agent you set up writes a shared
`~/.leanscale-gtm/profile.json` — CRM, fiscal calendar, quota-carrying rep count, segments, deal
floor. Every other agent reads it and asks only for what's missing.

## Run

```bash
/crm-hygiene:run
```

Output lands in `./gtm-agents/<agent>/<timestamp>/`:

| File | What it is |
|---|---|
| `report.html` | Self-contained report. Opens with the wifi off. This is the one you forward. |
| `report.md` | Same findings in markdown. |
| `findings.json` | Machine-readable, for your own pipelines. |
| `raw/` | Exactly what came back from each source, unmodified. |
| `manifest.json` | Provenance: every source, tool, query, and record count. |

## Where to start

If you're evaluating the suite, run these two first — they need nothing but the CRM connector and
between them take about fifteen minutes:

- **`crm-hygiene`** — is the data under your reports trustworthy?
- **`system-map`** — what is actually wired into your CRM, versus what you think is?

Then add `pipeline-inspection` for your weekly deal review and `forecast-agent` (in its default
audit mode) before your next forecast call.

## What these will never do

- **Send your CRM data anywhere.** No telemetry, no uploads of record data — not a sample, not
  an aggregate, not ever. Your records go from your connector to your machine and stop there,
  and reports are files on your disk.

  There is exactly one optional exception, and it involves no CRM data at all: three of the
  agents (`crm-hygiene`, `forecast-agent`, `stage-architect`) can cite LeanScale playbooks and
  benchmarks alongside a finding, which needs a free key. If you don't have one, setup offers
  to fetch it and sends **only the work email you type**, only after you say yes. Decline and
  the agent runs exactly as it otherwise would — it just won't cite the playbook. You can also
  get a key yourself at https://mcp.leanscale.team/ and never let setup near it.
- **Write to your CRM** — with exactly one exception, `meeting-to-crm`, which is dry-run by default,
  proposes a diff and stops, only touches fields you've allow-listed, won't overwrite a non-empty
  value unless you tell it to, refuses to run on a schedule, and logs every applied write to
  `~/.leanscale-gtm/audit/`.
- **Pretend an outage is a clean bill of health.** If a required source returns zero records, the
  run aborts and tells you what's probably wrong. A report claiming "no issues found" because
  authentication silently failed is worse than a crash.

## Run one vs. run two

Every run snapshots itself to `~/.leanscale-gtm/baselines/`. Run one is a baseline and says so.
From run two on, every score and every finding carries its delta, so you can prove what moved.
Don't delete the baselines — they're the evidence trail.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Command doesn't appear after install | Claude Code not restarted, or marketplace path wrong | Re-run `/plugin marketplace add` with the folder that contains `.claude-plugin/` |
| Run aborts: "required source returned zero records" | Connector auth, or the connected identity lacks object access | Re-run `:setup` — it diagnoses which source and why |
| Setup says a capability is missing | The MCP server isn't connected in Claude Code | Connect it in Settings → Connectors, then re-run `:setup` |
| A whole section says "unavailable, not clean" | That connector or permission is missing | Section is skipped, not passed — grant the access named in the report |
| Numbers differ from your dashboard | Usually the dashboard's filter, not the agent | Open the "verify this yourself" toggle on the finding and run the query |
| `python3: command not found` | Python not installed or not on PATH | Install Python 3.9+ |
| `cannot locate the plugin … re-run setup` | A plugin update moved the install, so the recorded path is stale | Re-run that agent's `:setup` — step 0 re-records it |
| `no such file … /bin/<agent>` | Setup has never completed for that agent | Run its `:setup`. Outside Claude Code, re-run `tools/install-skills.py` |
| `claude mcp list` shows LeanScale connected but playbooks never cite | The endpoint answers unauthenticated, so green there proves nothing | Run `python3 <plugin>/scripts/lib/config.py mcp-key-status`; add the `export` line and restart |

## Questions

anthony@leanscale.team
