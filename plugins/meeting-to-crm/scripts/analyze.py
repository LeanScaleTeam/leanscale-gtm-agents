#!/usr/bin/env python3
"""
meeting-to-crm — raw/*.json -> diff.json + findings.json

Layer 2 of the three-layer split. Claude fetched the meetings and the CRM state
through MCP and wrote them to raw/. This file is offline, deterministic and
stdlib-only: it decides which record each meeting belongs to, runs every
proposal through the guards in diff.py, and turns what is left into the shared
findings envelope.

It never writes to a CRM. It cannot: there is no network here.

    python3 scripts/analyze.py --raw fixtures/salesforce --out ./gtm-agents/meeting-to-crm/2026-08-10-1400
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    ConfigError,
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    SourceEmptyError,
    load_profile,
)
from lib.crmutil import parse_dt  # noqa: E402

import diff as diffmod  # noqa: E402

PLUGIN = "meeting-to-crm"


def _short(value: Any, limit: int = 70) -> str:
    return diffmod._short(value, limit)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")


def _copy_raw(src: Path, dest: Path) -> None:
    if src.resolve() == dest.resolve():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("*.json")):
        shutil.copy2(path, dest / path.name)


def _resolve_raw(raw_arg: Optional[str], out: Path) -> Path:
    """--raw may point at a run dir or straight at a raw/ dir. Accept both."""
    if raw_arg:
        candidate = Path(raw_arg)
        return candidate / "raw" if (candidate / "raw").is_dir() else candidate
    return out / "raw"


# ------------------------------------------------------------------ the manifest


def build_manifest(run_dir: Path, raw: Path, window: Dict[str, str]) -> RunManifest:
    """
    Counts come from the raw files themselves, not from what the skill claimed —
    a skill that says it fetched 12 meetings and wrote an empty file must still
    trip the fail-loud check.
    """
    declared = {s.get("name"): s for s in (diffmod.read_json(raw / "_sources.json", default=[]) or [])}

    def count_of(filename: str, key: str) -> int:
        doc = diffmod.read_json(raw / filename, default=None)
        if doc is None:
            return 0
        rows = doc.get(key) if isinstance(doc, dict) else doc
        return len(rows or [])

    manifest = RunManifest(PLUGIN, run_dir, window=window)
    spec = [
        ("meetings", "meetings.json", "meetings", True,
         "the transcript connector returned nothing for this window. Either no calls were recorded, "
         "the window is wrong, or the connected identity cannot see other reps' calls — "
         "conversation-intelligence tools default to private recordings in several plans."),
        ("crm_records", "crm_records.json", "records", True,
         "no CRM records came back for any meeting. Usually the integration user lacks read access to "
         "Opportunity/Deal, or every attendee domain is unknown to the CRM. Re-run :setup to diagnose."),
        ("match_candidates", "match_candidates.json", "matches", False,
         "no match candidates were gathered, so every meeting will be reported as unmatched."),
        ("proposals", "proposals.json", "proposals", False,
         "Claude drafted no proposed values. That is a legitimate outcome for a short or purely social call."),
        ("crm_schema", "crm_schema.json", "objects", False,
         "no field schema was captured, so picklist and length validation is skipped."),
    ]
    for name, filename, key, required, diagnosis in spec:
        meta = declared.get(name, {})
        count = count_of(filename, key)
        if name == "crm_schema":
            doc = diffmod.read_json(raw / filename, default={}) or {}
            count = len(doc.get("objects") or {})
        manifest.record(
            name,
            tool=str(meta.get("tool") or "(not declared)"),
            query=str(meta.get("query") or ""),
            count=count,
            required=required,
            note=str(meta.get("note") or ""),
            diagnosis=str(meta.get("diagnosis") or diagnosis),
        )
    return manifest


# ------------------------------------------------------------------- the findings


def _diff_rows_for_evidence(rows: List[Dict[str, Any]], limit: int = 200) -> List[Dict[str, Any]]:
    out = []
    for row in rows[:limit]:
        out.append({
            "Object": row["object"],
            "Record": f"{row['record_name'] or ''} {row['record_id'] or ''}".strip() or "(unresolved)",
            "Field": row["field"],
            "Current": _short(row["current_value"]) or "(blank)",
            "Proposed": _short(row["final_value"]),
            "Quote": f"“{_short(row['quote'], 90)}”",
            "At": f"{row['quote_ts'] or '??'} · {row['quote_speaker'] or 'unknown'}",
            "Confidence": row["confidence"],
        })
    return out


def build_findings(diff: Dict[str, Any], config: Dict[str, Any], profile: Dict[str, Any],
                   window: Dict[str, str], applied: Optional[Dict[str, Any]]) -> FindingsDoc:
    rows: List[Dict[str, Any]] = diff.get("rows") or []
    matches: List[Dict[str, Any]] = diff.get("matches") or []
    stats: Dict[str, Any] = diff.get("stats") or {}
    ready = [r for r in rows if r["status"] == "ready"]

    def dropped(code: str) -> List[Dict[str, Any]]:
        return [r for r in rows if r["status"] == "dropped"
                and any(d["code"] == code for d in r["drop_reasons"])]

    doc = FindingsDoc(
        plugin=PLUGIN,
        window=window,
        org_name=config.get("org_name") or profile.get("org_name") or "",
    )

    applied = applied or {}
    doc.add_score(Score(key="fields_proposed", label="Fields proposed", value=len(ready),
                        unit="count", direction_good="up",
                        context=f"{stats.get('proposed', 0)} drafted · {stats.get('dropped', 0)} dropped by the guards"))
    doc.add_score(Score(key="fields_applied", label="Fields applied", value=int(applied.get("fields_applied", 0)),
                        unit="count", direction_good="up",
                        context=("approved by " + str(applied.get("approved_by")))
                        if applied else "Dry run — nothing has been written"))
    doc.add_score(Score(key="records_touched", label="Records touched",
                        value=int(applied.get("records_touched", 0)) if applied else stats.get("records_touched", 0),
                        unit="count", direction_good="up",
                        context="records the approved batch would change" if not applied else "records changed"))
    doc.add_score(Score(key="blanks_filled", label="Blanks filled", value=stats.get("blanks_filled", 0),
                        unit="count", direction_good="up",
                        context="empty fields the call can answer"))
    doc.add_score(Score(key="existing_preserved", label="Existing values preserved",
                        value=stats.get("existing_preserved", 0), unit="count", direction_good="up",
                        context="proposals dropped rather than overwrite what a rep already wrote"))

    # ---------------------------------------------------------------- matching
    ambiguous = [m for m in matches if m["status"] == "ambiguous"]
    if ambiguous:
        doc.add(Finding(
            id="ambiguous-meeting-match",
            severity="critical",
            title=f"{len(ambiguous)} meeting(s) could not be tied to one record with confidence",
            what=("These calls matched more than one plausible record — most often an account carrying "
                  "two open opportunities. No value was proposed for any of them."),
            why_it_matters=("Matching is the highest-risk step in this agent. Guessing here writes one "
                            "customer's words onto another customer's deal, and that is the mistake a rep "
                            "never forgives. Silence is the correct behaviour; your decision is the fix."),
            recommended_fix=("Pick the right record for each call, then add it to "
                             "config.matching.overrides as {\"<meeting id>\": {\"object\": \"Opportunity\", "
                             "\"id\": \"<record id>\"}} and re-run. Longer term, link the calendar invite to "
                             "the opportunity — that single signal resolves nearly all of these."),
            evidence={
                "count": len(ambiguous),
                "sample_ids": [m["meeting_id"] for m in ambiguous[:10]],
                "rows": [{
                    "Meeting": m["meeting_id"],
                    "Why": _short(m["reason"], 110),
                    "Candidates": " | ".join(
                        f"{a.get('name') or a.get('id')} ({a.get('stage', '?')}, score {a.get('score')})"
                        for a in (m.get("alternatives") or [])[:4]),
                } for m in ambiguous[:25]],
            },
            effort="quick",
            owner_hint="Deal owner",
        ))

    unmatched = [m for m in matches if m["status"] == "unmatched"]
    if unmatched:
        doc.add(Finding(
            id="unmatched-meetings",
            severity="high",
            title=f"{len(unmatched)} recorded call(s) have no record in the CRM at all",
            what=("No account, contact or opportunity shares a domain, a calendar link or a title with "
                  "these meetings."),
            why_it_matters=("A sales call with no CRM record is pipeline that exists only in someone's "
                            "head. It is also the single most common cause of 'the forecast missed a deal "
                            "nobody knew about'."),
            recommended_fix=("Create the account/opportunity, or add the attendee's domain to the existing "
                             "account, then re-run. If these are genuinely not sales calls, add their "
                             "meeting type to meeting_types.exclude so they stop appearing here."),
            evidence={
                "count": len(unmatched),
                "sample_ids": [m["meeting_id"] for m in unmatched[:10]],
                "rows": [{"Meeting": m["meeting_id"], "Why": _short(m["reason"], 120)} for m in unmatched[:25]],
            },
            effort="quick",
            owner_hint="Deal owner",
        ))

    # ---------------------------------------------------------------- the batch
    if ready:
        doc.add(Finding(
            id="proposals-awaiting-approval",
            severity="high",
            title=f"{len(ready)} field update(s) are drafted and waiting on a human",
            what=(f"Across {stats.get('records_touched', 0)} record(s), every one of them justified by a "
                  f"verbatim quote from the call. Nothing has been written."),
            why_it_matters=("This is the CRM hygiene the rep would have typed after the call and usually "
                            "does not. Approving it is a two-minute read; skipping it means the next "
                            "pipeline review runs on last month's notes."),
            recommended_fix=(f"Read the diff table below. Approve the batch with the token "
                             f"{diff.get('token', '')} — the approval step refuses any token that does not "
                             f"match exactly what you just read."),
            evidence={
                "count": len(ready),
                "rows": _diff_rows_for_evidence(ready),
                "query": f"approval token {diff.get('token', '')}",
            },
            effort="quick",
            owner_hint="Deal owner",
        ))

    # ---------------------------------------------------------------- the guards
    unverified = dropped("quote_not_verified") + dropped("quote_missing") + dropped("quote_too_short")
    if unverified:
        doc.add(Finding(
            id="unverifiable-evidence",
            severity="critical",
            title=f"{len(unverified)} proposed value(s) had no quote that survives checking",
            what=("The quote offered as justification does not appear in the transcript, or there was no "
                  "quote at all. Python checks every quote against the actual call text, so these were "
                  "dropped rather than cleaned up."),
            why_it_matters=("This is the invention check. A paraphrase presented as a quote is exactly "
                            "the failure mode that gets an AI tool banned from a CRM, and it is the one "
                            "thing a rep cannot audit by eye at volume."),
            recommended_fix=("No action needed — nothing was proposed. If this happens often on one "
                             "transcript source, the transcript is probably being truncated before the "
                             "model sees the whole call; check the adapter's page/chunk limit."),
            evidence={
                "count": len(unverified),
                "rows": [{"Field": f"{r['object']}.{r['field']}", "Meeting": r["meeting_id"],
                          "Claimed quote": _short(r["quote"], 100)} for r in unverified[:25]],
            },
            effort="quick",
            owner_hint="RevOps",
        ))

    restricted = dropped("field_restricted")
    if restricted:
        doc.add(Finding(
            id="restricted-fields-blocked",
            severity="high",
            title=f"{len(restricted)} proposal(s) targeted forecast-bearing fields and were blocked",
            what=("Amount, close date, stage, probability and forecast category are off by default. "
                  "The call may well have implied a change to one of them; the agent will not make it."),
            why_it_matters=("These five fields are what the forecast is built from. An agent that moves "
                            "them from a sentence on a call is an agent that quietly re-forecasts the "
                            "quarter. That is the rep's call, and the manager's conversation."),
            recommended_fix=("Read the rows below and make the change yourself if it is right. If you "
                             "genuinely want the agent proposing one of these, add its fully-qualified "
                             "name to restricted_fields_opt_in AND to field_allowlist — two locks, "
                             "deliberately."),
            evidence={
                "count": len(restricted),
                "rows": [{"Record": r["record_name"] or r["record_id"] or "(unresolved)",
                          "Field": r["field"], "Current": _short(r["current_value"], 40) or "(blank)",
                          "Model suggested": _short(r["proposed_value"], 60),
                          "Quote": _short(r["quote"], 80)} for r in restricted[:25]],
            },
            effort="quick",
            owner_hint="Deal owner",
        ))

    populated = dropped("field_populated")
    if populated:
        doc.add(Finding(
            id="existing-values-preserved",
            severity="medium",
            title=f"{len(populated)} field(s) already had a value and were left alone",
            what=("The call implied a different value, but these fields are fill-blanks-only, so what the "
                  "rep already wrote survives untouched."),
            why_it_matters=("Silently clobbering a rep's own note is how this kind of tool gets "
                            "uninstalled in week two. Preserving it is the default for a reason."),
            recommended_fix=("If a field on this list should track the latest call rather than the first "
                             "one, set its overwrite policy to 'always' — or 'append' for long-text "
                             "fields, which adds a dated block underneath and never deletes anything."),
            evidence={
                "count": len(populated),
                "rows": [{"Record": r["record_name"] or r["record_id"] or "", "Field": r["field"],
                          "Kept": _short(r["current_value"], 60),
                          "Not written": _short(r["proposed_value"], 60)} for r in populated[:25]],
            },
            effort="quick",
            owner_hint="RevOps",
        ))

    not_allowed = dropped("field_not_on_allowlist") + dropped("field_read_only")
    if not_allowed:
        doc.add(Finding(
            id="fields-outside-the-allowlist",
            severity="medium",
            title=f"{len(not_allowed)} proposal(s) named a field this agent is not allowed to touch",
            what=("The field is absent from config.field_allowlist, or your CRM reports it as not "
                  "updateable (formula, roll-up, or a calculated property)."),
            why_it_matters=("The allow-list is the contract. If it drifts out of date the agent gets "
                            "quieter, not more dangerous — but you also stop capturing things you care "
                            "about."),
            recommended_fix=("Review the rows. Add the ones you want to config.field_allowlist with an "
                             "explicit overwrite policy; ignore the rest."),
            evidence={
                "count": len(not_allowed),
                "rows": [{"Object": r["object"], "Field": r["field"],
                          "Would have set": _short(r["proposed_value"], 70)} for r in not_allowed[:25]],
            },
            effort="quick",
            owner_hint="RevOps",
        ))

    invalid = dropped("value_invalid_picklist") + dropped("value_invalid_date") + dropped("value_date_in_past")
    if invalid:
        doc.add(Finding(
            id="values-your-crm-would-reject",
            severity="medium",
            title=f"{len(invalid)} proposed value(s) would not have been accepted by the CRM",
            what="A value outside the picklist, an unparseable date, or a next step dated before the call.",
            why_it_matters=("Catching these here is the difference between a clean batch and a batch that "
                            "half-fails in the API and leaves records in a mixed state."),
            recommended_fix=("Add the missing picklist value in your CRM if it is a real one, or leave it "
                             "— a blank is a correct answer."),
            evidence={
                "count": len(invalid),
                "rows": [{"Field": f"{r['object']}.{r['field']}", "Value": _short(r["proposed_value"], 50),
                          "Why": _short(r["drop_reasons"][0]["message"], 100)} for r in invalid[:25]],
            },
            effort="quick",
            owner_hint="RevOps",
        ))

    # ---------------------------------------------------------------- stakeholders
    new_people = [r for r in ready if r["action"] == "create"]
    if new_people:
        doc.add(Finding(
            id="new-stakeholders-heard",
            severity="medium",
            title=f"{len(new_people)} new stakeholder or follow-up record to create",
            what="People named on the call who are not yet on the record, plus the follow-up tasks agreed.",
            why_it_matters=("Single-threading is the best predictor of a deal slipping, and the cheapest "
                            "fix is writing down the second name the moment it is said. Creating a record "
                            "adds information without changing any existing value — the lowest-risk write "
                            "this agent makes."),
            recommended_fix="Approve them with the rest of the batch.",
            evidence={
                "count": len(new_people),
                "rows": [{"Object": r["object"], "On record": r["record_name"] or r["record_id"] or "",
                          "Details": _short(r["proposed_value"], 90),
                          "Quote": _short(r["quote"], 70)} for r in new_people[:25]],
            },
            effort="quick",
            owner_hint="Deal owner",
        ))

    # ---------------------------------------------------------------- the blanks
    undetermined = diff.get("undetermined") or []
    if undetermined:
        framework = (config.get("framework") or {}).get("name") or "your qualification framework"
        doc.add(Finding(
            id="undetermined-on-the-call",
            severity="medium",
            title=f"{len(undetermined)} qualification field(s) the call simply never answered",
            what=(f"These {framework} dimensions were not discussed, so nothing was proposed for them. "
                  f"A blank is a correct answer."),
            why_it_matters=("This is the more useful half of the output: it is a list of the questions "
                            "your reps are not asking. An agent that invented plausible values here would "
                            "hide exactly the gap you want to see."),
            recommended_fix=("Look for the dimensions that are blank on every call — that is a coaching "
                             "topic, not a data-entry problem."),
            evidence={
                "count": len(undetermined),
                "rows": [{"Meeting": u.get("meeting_id", ""), "Field": u.get("field", ""),
                          "Dimension": u.get("dimension", ""),
                          "Why blank": _short(u.get("why", ""), 110)} for u in undetermined[:40]],
            },
            effort="medium",
            owner_hint="Sales management",
        ))

    low_conf = dropped("confidence_below_floor")
    if low_conf:
        doc.add(Finding(
            id="low-confidence-readings",
            severity="low",
            title=f"{len(low_conf)} reading(s) fell below the confidence floor",
            what=f"Below the {config.get('min_confidence')} floor set for this workspace, so not proposed.",
            why_it_matters="Worth a glance — a cluster here usually means one call had bad audio.",
            recommended_fix="Ignore, or lower min_confidence if you would rather see them and decide yourself.",
            evidence={
                "count": len(low_conf),
                "rows": [{"Field": f"{r['object']}.{r['field']}", "Confidence": r["confidence"],
                          "Suggested": _short(r["proposed_value"], 60)} for r in low_conf[:25]],
            },
            effort="quick",
            owner_hint="RevOps",
        ))

    doc.sections = {
        "mode": diff.get("mode"),
        "approval_token": diff.get("token"),
        "diff_stats": stats,
        "matches": matches,
        "plan": diff.get("plan"),
        "write_safety": {
            "default_mode": "dry-run",
            "apply_requires": ["--apply flag", "named approver", "matching token", "a later turn"],
            "audit_log": str(diffmod.audit_path()),
            "allowlisted_fields": (diff.get("config_summary") or {}).get("allowlisted_fields", 0),
            "restricted_opt_in": (diff.get("config_summary") or {}).get("restricted_opt_in", []),
        },
    }
    return doc


# -------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="meeting-to-crm: build the proposal diff and findings.")
    parser.add_argument("--raw", default=None, help="directory holding raw/*.json (or a run dir containing raw/)")
    parser.add_argument("--out", default=None, help="run directory to write into")
    parser.add_argument("--config", default=None, help="explicit config file (default ~/.leanscale-gtm/meeting-to-crm.json)")
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else Path("gtm-agents") / PLUGIN / _stamp()
    raw_src = _resolve_raw(args.raw, out)
    if not raw_src.is_dir():
        print(f"No raw directory at {raw_src}. The run skill writes raw/*.json before analyze.py runs.",
              file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    _copy_raw(raw_src, out / "raw")
    raw = out / "raw"

    try:
        config = diffmod.load_config(args.config)
    except (ConfigError, diffmod.GuardError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    profile: Dict[str, Any] = {}
    profile_warning = ""
    try:
        profile = load_profile(required=False) or {}
    except ConfigError as exc:
        profile_warning = str(exc)
    if not profile:
        profile_warning = profile_warning or (
            "No ~/.leanscale-gtm/profile.json — running on this plugin's own config only. "
            "Run any LeanScale GTM agent's :setup skill to create the shared profile."
        )

    meetings_doc = diffmod.read_json(raw / "meetings.json", default={}) or {}
    meetings = (meetings_doc.get("meetings") if isinstance(meetings_doc, dict) else meetings_doc) or []
    dates = sorted(d for d in (str(m.get("started_at") or "")[:10] for m in meetings) if d)
    window = {
        "start": args.window_start or (dates[0] if dates else ""),
        "end": args.window_end or (dates[-1] if dates else ""),
    }

    # build_manifest reads every raw extract, so a truncated or hand-edited file
    # surfaces here. It sat outside any handler, so a malformed extract ended the
    # run in a bare JSONDecodeError from inside the standard library, naming no file.
    try:
        manifest = build_manifest(out, raw, window)
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1                      # matches this plugin's ConfigError convention
    if profile_warning:
        manifest.warn(profile_warning)
    mode, mode_warnings = diffmod.resolve_mode(False, config)
    for warning in mode_warnings:
        manifest.warn(warning)
    manifest.warn("analyze.py is read-only. Proposals are drafts until a human approves them by name.")

    try:
        manifest.finalize()
    except SourceEmptyError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    result = diffmod.build_from_run(out, config, mode=mode)
    result.warnings.extend(mode_warnings)
    diff_path = diffmod.write_diff(result, out)
    diff_doc = diffmod.read_json(diff_path)

    applied = diffmod.read_json(out / "applied.json", default=None)
    doc = build_findings(diff_doc, config, profile, window, applied)
    unavailable: List[str] = []
    for source in manifest.unavailable_optional():
        unavailable.append({
            "match_candidates": "CRM match candidates (every meeting will read as unmatched)",
            "proposals": "drafted proposals (Claude produced none for this window)",
            "crm_schema": "CRM field schema (picklist and length validation skipped)",
        }.get(source, source))
    doc.unavailable = unavailable
    findings_path = doc.write(out)

    print(diffmod.render_diff_table(diff_doc))
    print(f"\nfindings  {findings_path}")
    print(f"diff      {diff_path}")
    print(f"manifest  {out / 'manifest.json'}")
    print("\nDRY RUN. Nothing in your CRM was modified. Render the report next:")
    print(f"  python3 scripts/report.py --run {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
