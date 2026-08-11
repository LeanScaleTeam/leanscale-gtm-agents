# Customer Health — setup

Read-only throughout. Nothing here writes to your CRM.

---

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents
/plugin install customer-health@leanscale-gtm
```

Or from a downloaded zip: unzip it, then `/plugin marketplace add <path-to-folder>`.

## 2. Connect what you have

| Capability | Needed? | Typical providers |
|---|---|---|
| `crm.query` | **required** | Salesforce MCP, HubSpot MCP |
| `crm.describe` | recommended | Salesforce MCP — makes discovery much better |
| `transcripts.*` | optional | Gong, Chorus, Fireflies, Grain, Otter, Zoom, a Drive folder, a local folder |
| `comms.search` | optional | Slack, Gmail, Outlook |
| `docs.read` | optional | Google Drive, Notion — for a transcript folder |

The connected identity needs **read** on Account, Opportunity, Contact, Task/Event, and
Contract or your subscription object (Salesforce); or the `crm.objects.*.read` scopes
(HubSpot). It needs nothing else. If your security team asks, this plugin has no write path
at all — there is no `crm.write` capability in its manifest and no code that could use one.

## 3. Run setup

```
/customer-health:setup
```

It will, in order: probe every connector and prove each with a one-record read; read or create
`~/.leanscale-gtm/profile.json`; **discover** your customer universe, where renewal and
contract dates actually live (with fill rates, because a field that exists and is 11%
populated is not the source of truth), your ownership field and your conversation coverage;
then interview you only on what the CRM could not answer.

Have these ready — they are the questions no system can answer for you:

1. **What counts as a customer.** Including paused accounts, partners, resellers, internal
   test records, and multi-entity parents where the paper and the work sit on different records.
2. **Your contractual notice period, in days.** The real number from your paper, per segment if
   it varies. This is the heaviest signal in the model; a wrong number here silently
   mis-scores every account.
3. **Which renewal date wins** when two systems disagree.
4. **Who owns each account**, and what to do about accounts owned by deactivated users.
5. **Whether product-usage signal exists**, and where.
6. **Your support-ticket source** and what your severity levels are actually called.
7. **Champion and economic buyer** per account — or the field that holds them, if its fill
   rate holds up.
8. **The kickoff baseline.** See below. Do not skip it.

## 4. The kickoff baseline — the ten minutes that matter

Setup captures, per account: kickoff date, ARR at signature, sentiment at kickoff, engaged
contacts, open escalations on arrival, champion and economic buyer, and **success criteria in
the customer's own words**.

A health score with no baseline is a vibe. At renewal you will be asked what changed since you
started, and *"they're at 64"* is not an answer — *"they started at 41 and they're at 64"* is.
This is also the one thing that cannot be reconstructed from the CRM later.

Two rules the setup skill enforces:

- **An approximate baseline beats none.** If you have transcripts from that period, Claude will
  read the earliest calls and propose a starting sentiment with the quotes behind it. If you
  don't, give your honest recollection and it gets marked as reconstructed. Do not let perfect
  stop this.
- **Baselines are never overwritten.** Once captured, a kickoff reading is fixed. That is the
  entire point. If you deliberately change one, the old value is preserved in the notes.

Large book? Work top-down by ARR. Twenty done properly beats two hundred done badly, and every
report will keep naming the accounts that still have none.

## 5. First run

```
/customer-health:run
```

Run one is your baseline; the report says so. Deltas start on run two.

```
open ./gtm-agents/customer-health/<stamp>/report.html
```

## 6. Try it offline first

You can prove the whole pipeline works before connecting anything, using the bundled fixtures
(two complete demo books — one Salesforce-shaped, one HubSpot-shaped):

```bash
mkdir -p /tmp/ch-demo
cp "$PLUGIN/fixtures/profile.demo.json" /tmp/ch-demo/profile.json
cp "$PLUGIN/fixtures/config.demo.json"  /tmp/ch-demo/customer-health.json

LEANSCALE_GTM_HOME=/tmp/ch-demo python3 "$PLUGIN/scripts/analyze.py" \
  --run-dir /tmp/ch-demo-run --raw "$PLUGIN/fixtures/raw" --as-of 2026-08-10
LEANSCALE_GTM_HOME=/tmp/ch-demo python3 "$PLUGIN/scripts/report.py" \
  --run-dir /tmp/ch-demo-run
open /tmp/ch-demo-run/report.html
```

(`$PLUGIN` is the plugin directory — inside Claude Code it is `${CLAUDE_PLUGIN_ROOT}`.)
Swap `config.demo.json` for `config.demo-hubspot.json` and `fixtures/raw` for
`fixtures/raw-hubspot` to see the same model run against a HubSpot portal. Nothing in the
scoring changes between them.

## 7. Where things live

```
~/.leanscale-gtm/
    profile.json                       shared across every agent in the suite
    customer-health.json               this plugin's settings + your kickoff baselines
    baselines/customer-health/*.json   one snapshot per run — your evidence trail

./gtm-agents/customer-health/<stamp>/  run output, in your working directory
```

Config lives in your home directory, so a plugin update never wipes it. Baselines are never
pruned automatically.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Run aborted — a required data source returned zero records` | The customer filter matches nothing, or the connected identity cannot read Account | Re-run `:setup`. This abort is deliberate — an empty report that looks clean is worse than a crash |
| `No org profile at ~/.leanscale-gtm/profile.json` | Setup has never completed | Run `/customer-health:setup` |
| Mean sentiment shows `n/a` | No conversation source is connected | Expected, not broken. The commercial half still ran; see the "unavailable" list in the report |
| Every account is "outside the grid" | Transcripts resolved but matched no account | Check that transcript filenames or attendee domains map to account records; ask Claude to print the match attempt for one account |
| Many accounts show renewal "unknown" | Renewal dates are on a field or object the config does not point at | Re-run `:setup` — it inventories every candidate field and reports fill rates |
| A shared channel returns zero messages | The connected identity is a bot that is not a member of the channel | Invite it, or fall back to the mailbox or transcript-folder adapter |
| Risk scores all look low | An optional signal is missing everywhere, or the notice window is too short | Check `signals_with_no_data_anywhere` in `findings.json` and confirm `notice_window_days` against your actual paper |
| An account is at risk and you disagree | Open `sections.accounts[]` in `findings.json` | Every sub-score, its weight, its effective weight after redistribution, and any tripwire floor that fired is recorded there |
| Names appear that shouldn't leave the CS team | PII redaction is off | Set `redact_pii_in_reports: true` in `~/.leanscale-gtm/profile.json`. `raw/` and `findings.json` stay unredacted locally |
| Report looks stale after a plugin update | You are looking at an old run directory | Runs are timestamped; open the newest one |
