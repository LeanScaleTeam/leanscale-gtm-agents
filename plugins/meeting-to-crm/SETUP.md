# Setting up Meeting to CRM

Fifteen minutes, most of it spent agreeing which fields this thing may touch. That
conversation is the product — do not skip it.

## 1. Install

```
/plugin marketplace add ./leanscale-gtm-agents      # or the git URL you were given
/plugin install meeting-to-crm@leanscale-gtm
```

Requires Python 3.9+ (already on macOS and Linux). Nothing to pip install — the scripts are
standard library only and never touch the network.

## 2. Connect your systems

You need two things connected as MCP servers in Claude Code, plus one optional:

| Capability | Required | What it is |
|---|---|---|
| `crm.query` | yes | Salesforce or HubSpot. Read access to accounts, contacts and opportunities/deals. |
| `transcripts.*` or a folder | yes | Gong, Fireflies, Chorus, Grain, Otter, Zoom, Google Meet/Drive, or a local directory of transcript files. |
| `crm.describe` | strongly recommended | Field types, picklist values, and which fields are updateable. Without it, invalid values reach the API instead of being caught locally. |
| `crm.write` | only to apply | Not needed to propose. You can run this read-only forever and copy values across by hand. |

**Permissions to ask your admin for.** Read on Account, Contact, Opportunity and the fields
on your allow-list. For applying: update on Opportunity, and create on Contact, Task and
OpportunityContactRole (HubSpot: `crm.objects.deals.write`, `crm.objects.contacts.write`,
`crm.objects.notes.write`, `crm.objects.tasks.write`). Nothing else. If someone offers a
System Administrator integration user, decline — a narrow profile is the point.

**A note on transcript scopes.** Most conversation-intelligence tools default an API key to
*that user's own calls*. If your run comes back with three meetings and you know the team
had thirty, that is why, and it is a scope change on their side, not a bug here.

## 3. Run setup

```
/meeting-to-crm:setup
```

It probes what is connected, reads your CRM schema to find the real API names for next
step, competitor and each of your qualification dimensions, measures how often those fields
are actually filled today, samples a real recent meeting and shows you the match it would
make — and only then starts asking questions.

The questions it will ask, so you can think about them in advance:

1. Which meeting types to process, and whether internal calls are ever in scope (default: never).
2. **The field allow-list.** Field by field, in or out. This is the whole safety model.
3. **The overwrite policy per field.** Fill-blanks-only is the default. Next step usually
   wants `always`; long-text notes usually want `append`.
4. Your qualification framework, mapped onto real field API names.
5. Whether Amount, close date or stage may ever be proposed. Default no, and we recommend
   keeping it that way.
6. Who may approve a batch. Real names — this goes in the audit log.
7. How to match a meeting to an opportunity: calendar link, title convention, attendee
   domain, or manual.
8. Anything the agent must never touch.

It finishes with a real diff on a real meeting, without writing anything, and a pass/fail
table.

## 4. First run

```
/meeting-to-crm:run --window 7d
```

You get a diff table, an approval token, and a report at
`./gtm-agents/meeting-to-crm/<timestamp>/report.html`. Nothing has been written.

Read it. If it is right, on a **later turn**:

```
/meeting-to-crm:run --apply
```

You will be asked to confirm by name. The token from the report must match; if the diff
changed since you read it, the approval is refused and you review again.

## 5. Check the audit log any time

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/diff.py" show-audit --tail 20
cat ~/.leanscale-gtm/audit/meeting-to-crm.log      # one JSON line per applied field
```

Each line carries the timestamp, record id, field, old value, new value, the meeting it
came from, the quote, and who approved it. It is append-only and it is yours.

## Verifying the guards yourself

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/diff.py" selftest
```

38 checks against the bundled fixtures, in a temporary sandbox that does not touch your
config or your audit log. The fixtures deliberately contain proposals that must be refused:
a field off the allow-list, a field that already has a value, Amount and close date, an
invented quote, a meeting matching two open opportunities, and a stakeholder who is already
on the deal.

To watch a whole run without connecting anything:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" \
    --raw "${CLAUDE_PLUGIN_ROOT}/fixtures/salesforce" --out ./demo \
    --config "${CLAUDE_PLUGIN_ROOT}/fixtures/salesforce/config.json"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --run ./demo
open ./demo/report.html
```

Swap `salesforce` for `hubspot` to see the same thing against HubSpot-shaped data.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Run aborted — a required data source returned zero records` | Working as designed. A required source came back empty and a clean-looking empty report would be a lie. | Read the diagnosis in the abort message. Usually the window, the transcript API scope, or CRM object permissions. |
| Every meeting is unmatched | Attendee domains are not on the accounts, or the transcript source strips emails. | Add website/domain to the accounts, or switch matching to the title convention or calendar link. |
| Lots of ambiguous matches, all on a few accounts | Those accounts have more than one open opportunity. | Link the calendar invite to the deal — it is the single change that most improves match quality. Or set `matching.overrides` per meeting. |
| `field_not_on_allowlist` on a field you want | It is not in `field_allowlist`. | Add it, with an explicit overwrite policy. |
| `field_restricted` on Amount or CloseDate | Working as designed. | Add the fully-qualified name to `restricted_fields_opt_in` *and* to `field_allowlist`. Two locks, deliberately. Most teams should leave them off. |
| `field_read_only` on a field you can edit in the UI | Your CRM describe reports it as not updateable — usually a formula, roll-up or calculated property. | Nothing to fix. Pick a writable field. |
| Lots of `quote_not_verified` | The transcript is being truncated before the model sees all of it. | Check the adapter's page size or chunk limit. If it persists, report it. |
| Lots of `field_populated` | The allow-list policies do not match how the team works. | Decide deliberately which fields should be `always` or `append`. Do not loosen everything. |
| `approve` says *token mismatch* | The diff changed after it was rendered. | Re-run `report.py`, read the new diff, use the new token. |
| `approve` says *the diff was rendered Ns ago* | The review window has not elapsed. | Read the diff. That is the point. Adjust `approval.min_review_seconds` if your workflow genuinely differs. |
| `approve` says *unattended run* | CI/cron/scheduler markers in the environment. | Run it interactively. This agent is not meant to be scheduled. |
| `audit` exits non-zero with *writes reported that were never approved* | Something wrote outside the approved plan. | Stop and investigate. The offending lines are in the audit log flagged `approved:false`. |
| No `~/.leanscale-gtm/profile.json` | No LeanScale GTM agent has been set up yet. | Run `/meeting-to-crm:setup`. It creates the shared profile the whole suite reads. |

## Privacy

Everything stays local. Reports are files in your working directory; config and the audit
log live in `~/.leanscale-gtm/`. There is no telemetry and no phone-home. The only network
traffic is to the MCP servers you connected.

Set `redact_pii_in_reports: true` in `profile.json` and the rendered report replaces
attendee names, transcript speakers, CRM contacts, proposed new contacts and every email
address with stable pseudonyms. `raw/` and `findings.json` stay unredacted on your own
machine so the evidence trail is intact. Be aware of the limit: a third party named only in
the middle of a spoken sentence is not reliably detectable, so treat redaction as a strong
reduction rather than a guarantee before forwarding a report outside the company.
