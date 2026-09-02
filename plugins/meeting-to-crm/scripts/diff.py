#!/usr/bin/env python3
"""
meeting-to-crm — the proposal/diff builder, the guard engine, and the audit-log writer.

This is the only file in the LeanScale GTM suite that stands between a language
model's opinion and a customer's CRM. Everything it does is deliberately boring.

WHY THE GUARDS LIVE HERE AND NOT IN THE PROMPT
    A guard that only exists in a SKILL.md is a suggestion. The model can be
    talked out of a suggestion; it cannot be talked out of an `if` statement.
    So every safety rule in the README is enforced in this file:

      allow-list        _guard_allowlist        a field that is not in
                                                config.field_allowlist is dropped,
                                                whatever the confidence
      restricted        _guard_restricted       Amount / CloseDate / StageName /
                                                Probability / ForecastCategory need
                                                a SECOND explicit opt-in
      read-only         _guard_read_only        formula, roll-up and calculated
                                                properties are never written
      overwrite         _guard_overwrite        default is fill-blanks-only; a
                                                populated field is preserved unless
                                                that field says 'always' or 'append'
      evidence          _guard_quote            no verbatim quote found in the actual
                                                transcript => no proposal
      match             _guard_match            an ambiguous or missing meeting->record
                                                match produces zero proposals
      value sanity      _guard_value            picklist membership, date parsing,
                                                length caps, no-op detection
      dry-run           resolve_mode            no config key can enable apply; only
                                                the explicit --apply flag can
      approval          cmd_approve             named human + the token printed on the
                                                rendered diff + a later invocation
      audit             cmd_audit               one JSON line per applied field, and a
                                                loud failure for any write that was
                                                reported but never approved

Python 3.9+, standard library only. Never touches the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field as dc_field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import GTM_HOME, ConfigError, load_plugin_config  # noqa: E402
from lib.crmutil import email_domain, is_blank, normalize_company, parse_dt  # noqa: E402

PLUGIN = "meeting-to-crm"

# --------------------------------------------------------------------------- defaults

DEFAULT_CONFIG: Dict[str, Any] = {
    "schema_version": 1,
    "org_name": "",
    "crm": "salesforce",
    "internal_email_domains": [],
    "transcript_source": {"kind": "local_dir", "tool": "", "folder_id": None, "local_dir": None},
    "meeting_types": {
        "include": ["discovery", "demo", "technical_validation", "negotiation", "renewal", "qbr"],
        "exclude": ["internal", "interview", "one_on_one", "all_hands"],
        "title_exclude_patterns": [],
        "require_external_attendee": True,
        "min_duration_minutes": 0,
    },
    "matching": {
        "strategies": ["crm_link", "calendar_event", "title_convention", "contact_email_exact", "attendee_domain"],
        "title_convention": r"^\s*(?P<account>.+?)\s*(?:<>|\||—|-)\s*",
        "min_score": 0.5,
        "margin": 0.25,
        "require_open_opportunity": True,
        "allow_account_only_match": True,
        "overrides": {},
    },
    "field_allowlist": {},
    "restricted_fields_opt_in": [],
    "read_only_fields": [],
    "framework": {"name": "", "dimensions": {}},
    "child_records": {
        "contact_roles": {"enabled": False},
        "tasks": {"enabled": False},
        "call_summary": {"enabled": False},
    },
    "min_confidence": 0.6,
    "allow_past_dates": False,
    "max_value_length": 32000,
    "approval": {"approvers": [], "require_named_approver": True, "min_review_seconds": 10},
    "audit": {"enabled": True},
}

# Forecast-bearing fields. Off by default even when someone puts them on the
# allow-list; they need a second, explicit opt-in. These are the rep's call.
RESTRICTED_FIELDS = {
    "salesforce": {"amount", "closedate", "stagename", "probability", "forecastcategoryname", "forecastcategory"},
    "hubspot": {"amount", "closedate", "dealstage", "hs_forecast_amount", "hs_forecast_probability",
                "hs_deal_stage_probability", "hs_manual_forecast_category"},
}

# Never writable, whatever the config says.
BUILTIN_READ_ONLY = {
    "salesforce": {"id", "createddate", "createdbyid", "lastmodifieddate", "lastmodifiedbyid",
                   "systemmodstamp", "isdeleted", "isclosed", "iswon", "lastactivitydate",
                   "fiscalquarter", "fiscalyear", "fiscal", "expectedrevenue", "age",
                   "hasopportunitylineitem", "lastvieweddate", "lastreferenceddate"},
    "hubspot": {"hs_object_id", "createdate", "hs_createdate", "hs_lastmodifieddate",
                "notes_next_activity_date", "notes_last_updated", "notes_last_contacted",
                "hs_deal_stage_probability", "days_to_close", "hubspot_owner_assigneddate",
                "hs_time_in_dealstage", "num_associated_contacts"},
}

SIGNAL_WEIGHTS = {
    "crm_link": 1.00,            # the transcript source itself carried a CRM record id
    "calendar_event": 0.90,      # the calendar invite is linked to the record
    "title_convention": 0.70,    # meeting title follows the agreed "<Account> <> Us" shape
    "contact_email_exact": 0.60, # an attendee email matches a contact on the record
    "attendee_domain": 0.50,     # an attendee's email domain matches the account
    "account_name_fuzzy": 0.35,
    "single_open_opp": 0.30,     # the account has exactly one open opportunity
    "owner_is_organizer": 0.20,
    "recent_activity": 0.15,
}
DECISIVE_SIGNALS = ("crm_link", "calendar_event")

MIN_QUOTE_CHARS = 15

# Drop codes, ordered. The first one a row trips is its headline reason.
DROP_ORDER = [
    "meeting_out_of_scope",
    "match_unmatched",
    "match_ambiguous",
    "record_id_conflict",
    "field_restricted",
    "field_not_on_allowlist",
    "field_read_only",
    "quote_missing",
    "quote_too_short",
    "quote_not_verified",
    "confidence_below_floor",
    "value_empty",
    "value_invalid_picklist",
    "value_invalid_date",
    "value_date_in_past",
    "value_too_long",
    "field_populated",
    "child_already_exists",
    "no_change",
]

DROP_SEVERITY = {
    "match_ambiguous": "critical",
    "quote_not_verified": "critical",
    "match_unmatched": "high",
    "field_restricted": "high",
    "record_id_conflict": "high",
    "field_not_on_allowlist": "medium",
    "field_read_only": "medium",
    "field_populated": "medium",
    "value_invalid_picklist": "medium",
    "value_invalid_date": "medium",
    "value_date_in_past": "medium",
    "value_too_long": "low",
    "quote_missing": "high",
    "quote_too_short": "medium",
    "confidence_below_floor": "low",
    "value_empty": "low",
    "child_already_exists": "low",
    "no_change": "low",
    "meeting_out_of_scope": "low",
}


class GuardError(RuntimeError):
    """Raised when a safety precondition fails. The message must say how to fix it."""


# ------------------------------------------------------------------------- utilities


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SMART = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-", 0x2026: "..."}


def norm_text(value: Any) -> str:
    """Whitespace/quote-insensitive form used for verbatim quote matching."""
    text = str(value or "").translate(_SMART)
    return re.sub(r"\s+", " ", text).strip().lower()


def read_json(path: Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        # A truncated fetch leaves valid-looking JSON that stops mid-object. Raising
        # the bare decoder error named no file, so the customer saw a stack trace
        # ending in the standard library with no idea which extract was bad.
        raise ConfigError(
            f"{p.name} is not valid JSON — {exc.msg} at line {exc.lineno}, column "
            f"{exc.colno}.\nThis usually means the fetch was interrupted and the file "
            f"was written half-complete.\nDelete {p} and re-run the run skill's fetch "
            f"step for that source."
        ) from exc


def write_json(path: Path, payload: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.write("\n")
    return p


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (overlay or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(explicit: Optional[str] = None) -> Dict[str, Any]:
    """
    Per-plugin settings, deep-merged over DEFAULT_CONFIG.

    `explicit` points at a config file directly (the fixtures use this so the
    bundled demo runs without touching a real ~/.leanscale-gtm).
    """
    if explicit:
        overlay = read_json(Path(explicit), default=None)
        if overlay is None:
            raise GuardError(f"No config file at {explicit}.")
    else:
        overlay = load_plugin_config(PLUGIN)  # strips _ keys, raises a fixable ConfigError
    return _deep_merge(DEFAULT_CONFIG, overlay or {})


def resolve_mode(apply_flag: bool, config: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    Dry-run is the default and CONFIG CANNOT CHANGE THAT.

    Only the explicit --apply flag selects apply mode. If someone has hopefully
    added `"auto_apply": true` to their config, we ignore it and say so out loud.
    """
    warnings: List[str] = []
    for key in ("apply", "auto_apply", "apply_by_default", "default_mode", "mode", "dry_run", "write"):
        if key in config:
            warnings.append(
                f"config key '{key}' is ignored: dry-run is the default and only the explicit "
                f"--apply flag, plus a named approval on a later turn, can change it."
            )
    return ("apply" if apply_flag else "dry-run"), warnings


def refuse_if_unattended() -> None:
    """
    This plugin must never write on a schedule. If it looks like a CI/cron
    context, stop — a batch that no human is watching cannot be approved by one.
    """
    markers = [
        "CI", "CONTINUOUS_INTEGRATION", "CRON", "GITHUB_ACTIONS", "GITLAB_CI",
        "JENKINS_URL", "BUILDKITE", "TEAMCITY_VERSION",
        "CLAUDE_SCHEDULED_TASK", "LEANSCALE_GTM_SCHEDULED",
    ]
    hit = [m for m in markers if str(os.environ.get(m, "")).strip().lower() not in ("", "0", "false")]
    if hit:
        raise GuardError(
            "Refusing to apply: this looks like an unattended run "
            f"({', '.join(hit)} set in the environment).\n"
            "meeting-to-crm is never wired into a scheduler. A batch is proposed for a human, "
            "reviewed by that human, and approved by name. Run it interactively."
        )


def canonical_field(name: Any) -> str:
    return str(name or "").strip().lower()


# ----------------------------------------------------------------------- data shapes


@dataclass
class MatchResult:
    meeting_id: str
    status: str                       # confident | ambiguous | unmatched | out_of_scope | override
    object: Optional[str] = None
    record_id: Optional[str] = None
    record_name: str = ""
    account_id: Optional[str] = None
    account_name: str = ""
    score: float = 0.0
    signals: List[str] = dc_field(default_factory=list)
    reason: str = ""
    alternatives: List[Dict[str, Any]] = dc_field(default_factory=list)

    @property
    def is_writable(self) -> bool:
        return self.status in ("confident", "override") and bool(self.record_id)


@dataclass
class Row:
    """One proposed field change, after the guards have had their say."""

    id: str
    meeting_id: str
    meeting_title: str = ""
    meeting_date: str = ""
    meeting_source: str = ""
    action: str = "update"            # update | create
    object: str = ""
    record_id: Optional[str] = None
    record_name: str = ""
    field: str = ""
    field_label: str = ""
    current_value: Any = None
    proposed_value: Any = None
    final_value: Any = None           # what would actually be sent (append policy differs)
    overwrite_policy: str = "if_blank"
    quote: str = ""
    quote_ts: str = ""
    quote_speaker: str = ""
    confidence: float = 0.0
    rationale: str = ""
    status: str = "ready"             # ready | dropped
    drop_reasons: List[Dict[str, str]] = dc_field(default_factory=list)
    fills_blank: bool = False
    child_payload: Optional[Dict[str, Any]] = None

    @property
    def primary_drop(self) -> str:
        return self.drop_reasons[0]["code"] if self.drop_reasons else ""


@dataclass
class DiffResult:
    rows: List[Row] = dc_field(default_factory=list)
    matches: List[MatchResult] = dc_field(default_factory=list)
    undetermined: List[Dict[str, Any]] = dc_field(default_factory=list)
    warnings: List[str] = dc_field(default_factory=list)
    plan: List[Dict[str, Any]] = dc_field(default_factory=list)
    token: str = ""
    mode: str = "dry-run"
    config_summary: Dict[str, Any] = dc_field(default_factory=dict)

    # ---- counts used for the headline scores
    @property
    def ready(self) -> List[Row]:
        return [r for r in self.rows if r.status == "ready"]

    @property
    def dropped(self) -> List[Row]:
        return [r for r in self.rows if r.status == "dropped"]

    def dropped_by(self, code: str) -> List[Row]:
        return [r for r in self.rows if r.status == "dropped" and any(d["code"] == code for d in r.drop_reasons)]

    @property
    def records_touched(self) -> int:
        return len({(r.object, r.record_id or r.id) for r in self.ready})

    @property
    def blanks_filled(self) -> int:
        return sum(1 for r in self.ready if r.fills_blank)

    @property
    def existing_preserved(self) -> int:
        return len(self.dropped_by("field_populated"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin": PLUGIN,
            "generated_at": utcnow(),
            "mode": self.mode,
            "token": self.token,
            "config_summary": self.config_summary,
            "stats": {
                "proposed": len(self.rows),
                "ready": len(self.ready),
                "dropped": len(self.dropped),
                "records_touched": self.records_touched,
                "blanks_filled": self.blanks_filled,
                "existing_preserved": self.existing_preserved,
                "by_drop_reason": self.drop_counts(),
            },
            "matches": [asdict(m) for m in self.matches],
            "rows": [asdict(r) for r in self.rows],
            "undetermined": self.undetermined,
            "warnings": self.warnings,
            "plan": self.plan,
        }

    def drop_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.dropped:
            for reason in row.drop_reasons:
                counts[reason["code"]] = counts.get(reason["code"], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# ------------------------------------------------------------------------- matching


def _attendee_domains(meeting: Dict[str, Any], internal: List[str]) -> List[str]:
    internal_set = {d.lower().lstrip("@") for d in internal or []}
    out: List[str] = []
    for att in meeting.get("attendees") or []:
        dom = email_domain(att.get("email"))
        if dom and dom not in internal_set and dom not in out:
            out.append(dom)
    return out


def meeting_in_scope(meeting: Dict[str, Any], config: Dict[str, Any]) -> Tuple[bool, str]:
    rules = config.get("meeting_types") or {}
    mtype = str(meeting.get("meeting_type") or "").strip().lower()
    include = [str(x).lower() for x in rules.get("include") or []]
    exclude = [str(x).lower() for x in rules.get("exclude") or []]
    title = str(meeting.get("title") or "")

    if mtype and mtype in exclude:
        return False, f"meeting_type '{mtype}' is on the exclude list"
    if include and mtype and mtype not in include:
        return False, f"meeting_type '{mtype}' is not in the include list"
    if include and not mtype:
        return False, "meeting has no meeting_type and the include list is set"
    for pattern in rules.get("title_exclude_patterns") or []:
        try:
            if re.search(pattern, title):
                return False, f"title matches exclude pattern {pattern!r}"
        except re.error:
            continue
    if rules.get("require_external_attendee"):
        if not _attendee_domains(meeting, config.get("internal_email_domains") or []):
            return False, "no external attendee — internal-only calls are never written to the CRM"
    floor = rules.get("min_duration_minutes") or 0
    dur = meeting.get("duration_minutes")
    if floor and isinstance(dur, (int, float)) and dur < floor:
        return False, f"{dur:g} minutes is under the {floor}-minute floor"
    return True, ""


def match_meeting(meeting: Dict[str, Any], candidates: List[Dict[str, Any]], config: Dict[str, Any]) -> MatchResult:
    """
    Tie one meeting to one CRM record.

    THE HIGHEST-RISK STEP IN THE PLUGIN. A wrong match writes one customer's
    words onto another customer's deal, and the rep who finds it never trusts
    the agent again. So the bar is deliberately high: a single decisive signal,
    or a clear winner that beats the runner-up by a margin. Everything else is
    reported as ambiguous and produces ZERO proposals until a human picks.
    """
    mid = str(meeting.get("id"))
    rules = config.get("matching") or {}
    overrides = rules.get("overrides") or {}

    in_scope, why = meeting_in_scope(meeting, config)
    if not in_scope:
        return MatchResult(meeting_id=mid, status="out_of_scope", reason=why)

    if mid in overrides:
        ov = overrides[mid] or {}
        return MatchResult(
            meeting_id=mid, status="override", object=ov.get("object", "Opportunity"),
            record_id=ov.get("id"), record_name=ov.get("name", ""),
            account_id=ov.get("account_id"), account_name=ov.get("account_name", ""),
            score=1.0, signals=["manual_override"],
            reason="matched by a manual override in config.matching.overrides",
        )

    if not candidates:
        return MatchResult(
            meeting_id=mid, status="unmatched",
            reason="no CRM record shares a domain, a calendar link, or a title with this meeting",
        )

    strategies = set(rules.get("strategies") or list(SIGNAL_WEIGHTS))
    scored: List[Tuple[float, List[str], Dict[str, Any]]] = []
    for cand in candidates:
        signals = [s.split(":", 1)[0] for s in (cand.get("signals") or [])]
        # Corroborating signals always count; the config only chooses which
        # PRIMARY strategies (domain, title, calendar, CRM link) are in play.
        corroborating = ("single_open_opp", "recent_activity", "owner_is_organizer", "account_name_fuzzy")
        usable = [
            s for s in signals
            if s in SIGNAL_WEIGHTS and (s in strategies or s in corroborating)
        ]
        score = min(1.0, sum(SIGNAL_WEIGHTS[s] for s in dict.fromkeys(usable)))
        scored.append((score, list(dict.fromkeys(usable)), cand))

    scored.sort(key=lambda t: -t[0])
    best_score, best_signals, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    def alt_rows() -> List[Dict[str, Any]]:
        return [
            {
                "object": c.get("object"), "id": c.get("id"), "name": c.get("name", ""),
                "account_name": c.get("account_name", ""), "stage": c.get("stage", ""),
                "is_open": c.get("is_open"), "score": round(s, 2), "signals": sig,
            }
            for s, sig, c in scored[:6]
        ]

    decisive = [(s, sig, c) for s, sig, c in scored if any(d in sig for d in DECISIVE_SIGNALS)]
    if len(decisive) == 1:
        s, sig, c = decisive[0]
        return _confident(mid, c, s, sig, "a decisive link (calendar event or CRM link) points at exactly one record", alt_rows())
    if len(decisive) > 1:
        return MatchResult(
            meeting_id=mid, status="ambiguous", score=round(best_score, 2), signals=best_signals,
            reason=f"{len(decisive)} records carry a decisive link to this meeting — a human must pick",
            alternatives=alt_rows(),
        )

    if best_score < float(rules.get("min_score", 0.5)):
        return MatchResult(
            meeting_id=mid, status="ambiguous", score=round(best_score, 2), signals=best_signals,
            reason=(f"best candidate scores {best_score:.2f}, under the {rules.get('min_score', 0.5)} floor — "
                    f"the evidence tying this call to a record is too thin to write on"),
            alternatives=alt_rows(),
        )
    if (best_score - runner_up) < float(rules.get("margin", 0.25)):
        return MatchResult(
            meeting_id=mid, status="ambiguous", score=round(best_score, 2), signals=best_signals,
            reason=(f"two candidates are within {best_score - runner_up:.2f} of each other "
                    f"(needs {rules.get('margin', 0.25)}) — most likely one account with more than one "
                    f"open opportunity. Pick one with config.matching.overrides."),
            alternatives=alt_rows(),
        )
    if rules.get("require_open_opportunity") and best.get("object") == "Opportunity" and best.get("is_open") is False:
        return MatchResult(
            meeting_id=mid, status="ambiguous", score=round(best_score, 2), signals=best_signals,
            reason="the best match is a CLOSED opportunity — confirm the deal before writing to it",
            alternatives=alt_rows(),
        )
    return _confident(mid, best, best_score, best_signals,
                      f"clear winner: {best_score:.2f} vs {runner_up:.2f} runner-up", alt_rows())


def _confident(mid: str, cand: Dict[str, Any], score: float, signals: List[str],
               reason: str, alts: List[Dict[str, Any]]) -> MatchResult:
    return MatchResult(
        meeting_id=mid, status="confident", object=cand.get("object"), record_id=cand.get("id"),
        record_name=cand.get("name", ""), account_id=cand.get("account_id"),
        account_name=cand.get("account_name", ""), score=round(score, 2), signals=signals,
        reason=reason, alternatives=[a for a in alts if a.get("id") != cand.get("id")],
    )


# --------------------------------------------------------------------------- guards


class DiffBuilder:
    def __init__(self, config: Dict[str, Any], meetings: List[Dict[str, Any]],
                 records: List[Dict[str, Any]], schema: Dict[str, Any],
                 match_candidates: Dict[str, List[Dict[str, Any]]]):
        self.config = config
        self.crm = str(config.get("crm", "salesforce")).lower()
        self.meetings = {str(m.get("id")): m for m in meetings}
        self.records: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for rec in records:
            self.records[(str(rec.get("object")), str(rec.get("id")))] = rec
        self.schema = schema or {}
        self.candidates = match_candidates or {}
        self.transcripts = {mid: self._transcript_text(m) for mid, m in self.meetings.items()}
        self.matches: Dict[str, MatchResult] = {}
        self.warnings: List[str] = []

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _transcript_text(meeting: Dict[str, Any]) -> str:
        if meeting.get("transcript_text"):
            return norm_text(meeting["transcript_text"])
        parts = [seg.get("text", "") for seg in meeting.get("transcript") or []]
        return norm_text(" ".join(parts))

    def _allowlist_entry(self, obj: str, field: str) -> Optional[Dict[str, Any]]:
        table = (self.config.get("field_allowlist") or {}).get(obj)
        if not isinstance(table, dict):
            return None
        for key, entry in table.items():
            if canonical_field(key) == canonical_field(field):
                return dict(entry or {}, _api_name=key)
        return None

    def _schema_field(self, obj: str, field: str) -> Dict[str, Any]:
        fields = ((self.schema.get("objects") or {}).get(obj) or {}).get("fields") or {}
        for key, meta in fields.items():
            if canonical_field(key) == canonical_field(field):
                return meta or {}
        return {}

    def _record(self, obj: str, rid: Optional[str]) -> Dict[str, Any]:
        return self.records.get((str(obj), str(rid)), {})

    def _current_value(self, obj: str, rid: Optional[str], field: str) -> Any:
        rec = self._record(obj, rid)
        values = rec.get("fields") or {}
        for key, value in values.items():
            if canonical_field(key) == canonical_field(field):
                return value
        return None

    # ---------------------------------------------------------------- the guards

    def _guard_match(self, row: Row, match: Optional[MatchResult]) -> None:
        if match is None:
            row.drop_reasons.append({"code": "match_unmatched",
                                     "message": "no meeting found for this proposal"})
            return
        if match.status == "out_of_scope":
            row.drop_reasons.append({"code": "meeting_out_of_scope", "message": match.reason})
        elif match.status == "unmatched":
            row.drop_reasons.append({"code": "match_unmatched", "message": match.reason})
        elif match.status == "ambiguous":
            row.drop_reasons.append({
                "code": "match_ambiguous",
                "message": (f"{match.reason} Writing on a guess here would put this call's notes on the "
                            f"wrong deal, so nothing is proposed."),
            })

    def _guard_record_id(self, row: Row, match: Optional[MatchResult], claimed: Optional[str]) -> None:
        if not claimed or not match or not match.is_writable:
            return
        if str(claimed) != str(match.record_id) and row.action == "update":
            row.drop_reasons.append({
                "code": "record_id_conflict",
                "message": (f"the proposal names record {claimed} but the meeting matched "
                            f"{match.record_id}. Never resolve that disagreement automatically."),
            })

    def _guard_allowlist(self, row: Row) -> Optional[Dict[str, Any]]:
        entry = self._allowlist_entry(row.object, row.field)
        if entry is None:
            row.drop_reasons.append({
                "code": "field_not_on_allowlist",
                "message": (f"{row.object}.{row.field} is not in config.field_allowlist. "
                            f"Add it there if you want this agent touching it."),
            })
        return entry

    def _guard_restricted(self, row: Row) -> bool:
        """True when the field was blocked as forecast-bearing."""
        restricted = RESTRICTED_FIELDS.get(self.crm, set())
        if canonical_field(row.field) not in restricted:
            return False
        opt_in = {canonical_field(x) for x in self.config.get("restricted_fields_opt_in") or []}
        qualified = canonical_field(f"{row.object}.{row.field}")
        if qualified in opt_in or canonical_field(row.field) in opt_in:
            return False
        row.drop_reasons.append({
            "code": "field_restricted",
            "message": (f"{row.field} is forecast-bearing and off by default — amount, close date, stage and "
                        f"probability are the rep's call. Add \"{row.object}.{row.field}\" to "
                        f"restricted_fields_opt_in AND to field_allowlist to change that."),
        })
        return True

    def _guard_read_only(self, row: Row) -> None:
        extra = {canonical_field(x) for x in self.config.get("read_only_fields") or []}
        extra |= {canonical_field(x.split(".")[-1]) for x in self.config.get("read_only_fields") or []}
        meta = self._schema_field(row.object, row.field)
        updateable = meta.get("updateable")
        if canonical_field(row.field) in BUILTIN_READ_ONLY.get(self.crm, set()) or canonical_field(row.field) in extra:
            row.drop_reasons.append({"code": "field_read_only",
                                     "message": f"{row.field} is not writable (system, formula or calculated field)."})
        elif updateable is False:
            row.drop_reasons.append({"code": "field_read_only",
                                     "message": f"your CRM describe reports {row.field} as not updateable."})

    def _guard_quote(self, row: Row) -> None:
        quote = str(row.quote or "").strip()
        if not quote:
            row.drop_reasons.append({
                "code": "quote_missing",
                "message": "no verbatim quote. Nothing is proposed without something the customer actually said.",
            })
            return
        if len(quote) < MIN_QUOTE_CHARS:
            row.drop_reasons.append({
                "code": "quote_too_short",
                "message": f"the quote is {len(quote)} characters — too short to be evidence of anything.",
            })
        transcript = self.transcripts.get(row.meeting_id, "")
        if not transcript:
            row.drop_reasons.append({"code": "quote_not_verified",
                                     "message": "the transcript for this meeting is empty, so the quote cannot be checked."})
            return
        fragments = [f.strip() for f in norm_text(quote).split("...") if len(f.strip()) >= 12] or [norm_text(quote)]
        cursor, ok = 0, True
        for frag in fragments:
            idx = transcript.find(frag, cursor)
            if idx < 0:
                ok = False
                break
            cursor = idx + len(frag)
        if not ok:
            row.drop_reasons.append({
                "code": "quote_not_verified",
                "message": ("that quote does not appear in the transcript. This is the anti-invention guard: "
                            "a paraphrase presented as a quote is dropped, not cleaned up."),
            })

    def _guard_confidence(self, row: Row) -> None:
        floor = float(self.config.get("min_confidence", 0.6))
        if float(row.confidence or 0) < floor:
            row.drop_reasons.append({
                "code": "confidence_below_floor",
                "message": f"confidence {row.confidence} is below the {floor} floor for this workspace.",
            })

    def _guard_value(self, row: Row, entry: Dict[str, Any], meeting: Dict[str, Any]) -> None:
        value = row.proposed_value
        if is_blank(value):
            row.drop_reasons.append({"code": "value_empty",
                                     "message": "empty proposed value — a blank is a correct answer, not a write."})
            return

        meta = self._schema_field(row.object, row.field)
        ftype = str(entry.get("type") or meta.get("type") or "text").lower()

        picklist = meta.get("picklist_values")
        if picklist:
            allowed = {norm_text(v) for v in picklist}
            if norm_text(value) not in allowed:
                row.drop_reasons.append({
                    "code": "value_invalid_picklist",
                    "message": (f"'{value}' is not one of the {len(picklist)} values {row.field} accepts "
                                f"({', '.join(list(picklist)[:6])}...). Add the value in your CRM first."),
                })

        if ftype in ("date", "datetime"):
            parsed = parse_dt(value)
            if parsed is None:
                row.drop_reasons.append({"code": "value_invalid_date",
                                         "message": f"'{value}' is not a date this CRM will accept."})
            else:
                row.final_value = parsed.strftime("%Y-%m-%d") if ftype == "date" else parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
                if not self.config.get("allow_past_dates"):
                    meeting_dt = parse_dt(meeting.get("started_at"))
                    if meeting_dt and parsed.date() < meeting_dt.date():
                        row.drop_reasons.append({
                            "code": "value_date_in_past",
                            "message": (f"the proposed date {parsed.date()} is before the call itself "
                                        f"({meeting_dt.date()}) — that is a misread, not a plan."),
                        })

        limit = meta.get("length") or self.config.get("max_value_length") or 32000
        if isinstance(value, str) and len(value) > int(limit):
            row.drop_reasons.append({"code": "value_too_long",
                                     "message": f"{len(value)} characters exceeds the {limit}-character limit on {row.field}."})

    def _guard_overwrite(self, row: Row, entry: Dict[str, Any]) -> None:
        """
        Default is fill-blanks-only. Silently clobbering a rep's own note is how
        this plugin gets uninstalled, so a populated field is preserved unless
        that specific field opted into 'always' or 'append'.
        """
        policy = str(entry.get("overwrite") or "if_blank").lower()
        row.overwrite_policy = policy
        current = row.current_value
        blank_now = is_blank(current)
        row.fills_blank = blank_now

        if blank_now:
            return
        if policy == "always":
            return
        if policy == "append":
            stamp = row.meeting_date[:10] or utcnow()[:10]
            header = f"--- {stamp} · {row.meeting_title or 'call'} (meeting-to-crm) ---"
            row.final_value = f"{current}\n\n{header}\n{row.final_value if row.final_value is not None else row.proposed_value}"
            row.fills_blank = False
            return
        row.drop_reasons.append({
            "code": "field_populated",
            "message": (f"{row.field} already reads \"{_short(current)}\" and its policy is '{policy}'. "
                        f"Existing value preserved. Set overwrite to 'always' or 'append' on that field "
                        f"if you want it replaced."),
        })

    def _guard_no_change(self, row: Row) -> None:
        if row.status == "dropped":
            return
        if row.action == "update" and norm_text(row.final_value) == norm_text(row.current_value):
            row.drop_reasons.append({"code": "no_change",
                                     "message": "the proposed value is what the field already says."})

    # ---------------------------------------------------------------- build

    def build(self, proposals: List[Dict[str, Any]], child_proposals: List[Dict[str, Any]],
              undetermined: List[Dict[str, Any]], mode: str = "dry-run") -> DiffResult:
        result = DiffResult(mode=mode, undetermined=list(undetermined or []))

        for mid, meeting in self.meetings.items():
            cands = self.candidates.get(mid, [])
            self.matches[mid] = match_meeting(meeting, cands, self.config)
        result.matches = [self.matches[mid] for mid in self.meetings]

        for idx, prop in enumerate(proposals or [], 1):
            result.rows.append(self._build_row(prop, idx))
        for idx, child in enumerate(child_proposals or [], 1):
            result.rows.append(self._build_child_row(child, idx))

        result.warnings = list(self.warnings)
        result.plan = build_plan(result.ready)
        result.token = approval_token(result.plan)
        result.config_summary = {
            "crm": self.crm,
            "allowlisted_fields": sum(len(v) for v in (self.config.get("field_allowlist") or {}).values()),
            "restricted_opt_in": list(self.config.get("restricted_fields_opt_in") or []),
            "min_confidence": self.config.get("min_confidence"),
            "framework": (self.config.get("framework") or {}).get("name", ""),
            "transcript_source": (self.config.get("transcript_source") or {}).get("kind", ""),
        }
        return result

    def _base_row(self, prop: Dict[str, Any], row_id: str) -> Tuple[Row, Optional[MatchResult], Dict[str, Any]]:
        mid = str(prop.get("meeting_id"))
        meeting = self.meetings.get(mid, {})
        match = self.matches.get(mid)
        row = Row(
            id=row_id,
            meeting_id=mid,
            meeting_title=str(meeting.get("title") or ""),
            meeting_date=str(meeting.get("started_at") or ""),
            meeting_source=str(meeting.get("source") or (self.config.get("transcript_source") or {}).get("kind", "")),
            quote=str(prop.get("quote") or ""),
            quote_ts=str(prop.get("quote_ts") or ""),
            quote_speaker=str(prop.get("quote_speaker") or ""),
            confidence=float(prop.get("confidence") or 0),
            rationale=str(prop.get("rationale") or ""),
        )
        return row, match, meeting

    def _build_row(self, prop: Dict[str, Any], idx: int) -> Row:
        row, match, meeting = self._base_row(prop, str(prop.get("id") or f"p-{idx:03d}"))
        row.action = "update"
        row.object = str(prop.get("object") or "")
        row.field = str(prop.get("field") or "")
        row.proposed_value = prop.get("proposed_value")
        row.final_value = prop.get("proposed_value")

        self._guard_match(row, match)
        self._guard_record_id(row, match, prop.get("record_id"))
        if match and match.is_writable:
            row.record_id = match.record_id
            row.record_name = match.record_name or match.account_name
            if row.object and match.object and row.object != match.object:
                # A proposal on a different object than the matched record (e.g. Account
                # while the match is an Opportunity) is fine as long as we can resolve it.
                if row.object in ("Account",) and match.account_id:
                    row.record_id = match.account_id
                    row.record_name = match.account_name
        row.current_value = self._current_value(row.object, row.record_id, row.field)
        row.field_label = str(prop.get("field_label") or "")

        # Structural guards are mutually exclusive and short-circuit: if the field may
        # not be written at all, the value it would have taken is beside the point, and
        # reporting it as "an existing value we preserved" would flatter the numbers.
        entry: Optional[Dict[str, Any]] = None
        if not self._guard_restricted(row):
            entry = self._guard_allowlist(row)
            if entry is not None:
                self._guard_read_only(row)
                if not any(d["code"] == "field_read_only" for d in row.drop_reasons):
                    row.field_label = row.field_label or str(entry.get("label") or row.field)
                    self._guard_value(row, entry, meeting)
                    self._guard_overwrite(row, entry)
        self._guard_quote(row)
        self._guard_confidence(row)
        row.field_label = row.field_label or row.field
        self._finish(row)
        self._guard_no_change(row)
        self._finish(row)
        return row

    def _build_child_row(self, child: Dict[str, Any], idx: int) -> Row:
        row, match, meeting = self._base_row(child, str(child.get("id") or f"c-{idx:03d}"))
        row.action = "create"
        row.object = str(child.get("object") or "")
        values = dict(child.get("values") or {})
        row.field = "(new record)"
        row.field_label = str(child.get("label") or f"New {row.object}")
        row.proposed_value = "; ".join(f"{k}={v}" for k, v in values.items())
        row.final_value = row.proposed_value
        row.child_payload = values
        row.fills_blank = True

        self._guard_match(row, match)
        if match and match.is_writable:
            row.record_id = match.record_id
            row.record_name = match.record_name or match.account_name

        # Every field of a created record must itself be on the allow-list.
        table = (self.config.get("field_allowlist") or {}).get(row.object)
        if not isinstance(table, dict):
            row.drop_reasons.append({
                "code": "field_not_on_allowlist",
                "message": f"{row.object} has no entry in config.field_allowlist, so no {row.object} record is created.",
            })
        else:
            allowed = {canonical_field(k) for k in table}
            stray = [k for k in values if canonical_field(k) not in allowed]
            if stray:
                row.drop_reasons.append({
                    "code": "field_not_on_allowlist",
                    "message": f"{row.object} fields not on the allow-list: {', '.join(sorted(stray))}.",
                })

        enabled = self._child_enabled(row.object)
        if not enabled:
            row.drop_reasons.append({
                "code": "field_not_on_allowlist",
                "message": f"child-record creation for {row.object} is switched off in config.child_records.",
            })

        self._guard_quote(row)
        self._guard_confidence(row)
        self._guard_child_values(row, values)
        self._guard_child_duplicate(row, values)
        self._finish(row)
        return row

    def _guard_child_values(self, row: Row, values: Dict[str, Any]) -> None:
        """Same picklist/date sanity a field update gets — a create that the API
        rejects leaves the batch half-written, which is the worst outcome of all."""
        for field, value in values.items():
            if isinstance(value, str) and value.startswith("@ref:"):
                continue  # resolved from an earlier create in the same plan
            meta = self._schema_field(row.object, field)
            picklist = meta.get("picklist_values")
            if picklist and not is_blank(value) and norm_text(value) not in {norm_text(v) for v in picklist}:
                row.drop_reasons.append({
                    "code": "value_invalid_picklist",
                    "message": f"'{value}' is not a value {row.object}.{field} accepts.",
                })
            if str(meta.get("type") or "").lower() in ("date", "datetime") and not is_blank(value):
                if parse_dt(value) is None:
                    row.drop_reasons.append({
                        "code": "value_invalid_date",
                        "message": f"'{value}' is not a date {row.object}.{field} will accept.",
                    })

    def _child_enabled(self, obj: str) -> bool:
        for cfg in (self.config.get("child_records") or {}).values():
            if not isinstance(cfg, dict) or not cfg.get("enabled"):
                continue
            if canonical_field(cfg.get("object")) == canonical_field(obj):
                return True
            # A contact role for someone who is not in the CRM yet needs the person
            # created first, which is what create_missing_contacts authorises.
            if cfg.get("create_missing_contacts") and canonical_field(obj) in ("contact", "contacts"):
                return True
        return False

    def _parent_record(self, record_id: Optional[str]) -> Dict[str, Any]:
        """The matched record, whatever object it is — children hang off it."""
        for (_obj, rid), rec in self.records.items():
            if rid == str(record_id):
                return rec
        return {}

    def _guard_child_duplicate(self, row: Row, values: Dict[str, Any]) -> None:
        """Never add a stakeholder who is already on the deal."""
        existing = self._parent_record(row.record_id).get("children") or {}
        pool = existing.get(row.object) or []
        id_keys = ("Email", "email", "ContactEmail", "contact_email", "ContactId", "contact_id")
        name_keys = ("ContactName", "Name", "name", "LastName", "lastname")
        keys = [canonical_field(values.get(k)) for k in id_keys if values.get(k)]
        names = [canonical_field(values.get(k)) for k in name_keys if values.get(k)]
        for item in pool:
            item_keys = [canonical_field(item.get(k)) for k in id_keys if item.get(k)]
            item_names = [canonical_field(item.get(k)) for k in name_keys if item.get(k)]
            if (keys and set(keys) & set(item_keys)) or (names and set(names) & set(item_names)):
                row.drop_reasons.append({
                    "code": "child_already_exists",
                    "message": f"{item.get('ContactName') or item.get('Name') or 'that person'} is already on this record.",
                })
                return

    @staticmethod
    def _finish(row: Row) -> None:
        if row.drop_reasons:
            row.drop_reasons.sort(key=lambda d: DROP_ORDER.index(d["code"]) if d["code"] in DROP_ORDER else 99)
            # de-duplicate, keep order
            seen, ordered = set(), []
            for reason in row.drop_reasons:
                if reason["code"] in seen:
                    continue
                seen.add(reason["code"])
                ordered.append(reason)
            row.drop_reasons = ordered
            row.status = "dropped"
        else:
            row.status = "ready"


def _short(value: Any, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ----------------------------------------------------------------- plan + token


def build_plan(rows: List[Row]) -> List[Dict[str, Any]]:
    """
    Group ready rows into the exact writes to make. One entry per record for
    updates, one per new record for creates. This is what a human approves and
    the ONLY thing the audit log will accept afterwards.
    """
    updates: Dict[Tuple[str, str], Dict[str, Any]] = {}
    plan: List[Dict[str, Any]] = []
    for row in rows:
        if row.action == "create":
            plan.append({
                "kind": "create",
                "object": row.object,
                "parent_id": row.record_id,
                "values": row.child_payload or {},
                "row_ids": [row.id],
                "meeting_id": row.meeting_id,
            })
            continue
        key = (row.object, str(row.record_id))
        entry = updates.get(key)
        if entry is None:
            entry = {
                "kind": "update",
                "object": row.object,
                "record_id": row.record_id,
                "record_name": row.record_name,
                "values": {},
                "old_values": {},
                "row_map": {},   # field -> row id, so a partial approval stays exact
                "row_ids": [],
                "meeting_id": row.meeting_id,
            }
            updates[key] = entry
            plan.append(entry)
        entry["values"][row.field] = row.final_value
        entry["old_values"][row.field] = row.current_value
        entry["row_map"][row.field] = row.id
        entry["row_ids"].append(row.id)
    return plan


def filter_plan(plan: List[Dict[str, Any]], wanted: set) -> List[Dict[str, Any]]:
    """
    Narrow an approved plan to the rows a human actually picked.

    Field-level, not record-level: approving three rows on a deal must authorise
    three fields, not the other seven that happened to be grouped with them.
    """
    out: List[Dict[str, Any]] = []
    for item in plan:
        if item.get("kind") == "create":
            if set(item.get("row_ids") or []) & wanted:
                out.append(item)
            continue
        row_map = item.get("row_map") or {}
        keep = {field: rid for field, rid in row_map.items() if rid in wanted}
        if not keep:
            continue
        out.append(dict(
            item,
            values={f: v for f, v in (item.get("values") or {}).items() if f in keep},
            old_values={f: v for f, v in (item.get("old_values") or {}).items() if f in keep},
            row_map=keep,
            row_ids=sorted(keep.values()),
        ))
    return out


def approval_token(plan: List[Dict[str, Any]]) -> str:
    """
    Fingerprint of the exact writes. Printed on the rendered diff and required
    at approval time, so a human can only approve the batch they actually read —
    if anything about the plan changes, the token changes and approval fails.
    """
    payload = json.dumps(plan, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ run plumbing


def build_from_run(run_dir: Path, config: Dict[str, Any], mode: str = "dry-run") -> DiffResult:
    """Read raw/*.json out of a run directory and build the diff."""
    raw = Path(run_dir) / "raw"
    meetings_doc = read_json(raw / "meetings.json", default={}) or {}
    meetings = meetings_doc.get("meetings") if isinstance(meetings_doc, dict) else meetings_doc
    records_doc = read_json(raw / "crm_records.json", default={}) or {}
    records = records_doc.get("records") if isinstance(records_doc, dict) else records_doc
    schema = read_json(raw / "crm_schema.json", default={}) or {}
    cands_doc = read_json(raw / "match_candidates.json", default={}) or {}
    proposals_doc = read_json(raw / "proposals.json", default={}) or {}

    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for item in (cands_doc.get("matches") if isinstance(cands_doc, dict) else cands_doc) or []:
        candidates[str(item.get("meeting_id"))] = item.get("candidates") or []

    builder = DiffBuilder(config, meetings or [], records or [], schema, candidates)
    return builder.build(
        proposals_doc.get("proposals") or [],
        proposals_doc.get("child_records") or [],
        proposals_doc.get("undetermined") or [],
        mode=mode,
    )


def write_diff(result: DiffResult, run_dir: Path) -> Path:
    return write_json(Path(run_dir) / "diff.json", result.to_dict())


def render_diff_table(diff: Dict[str, Any], width: int = 0) -> str:
    """Plain-text diff table for the terminal. The HTML version lives in report.py."""
    lines: List[str] = []
    rows = diff.get("rows") or []
    ready = [r for r in rows if r["status"] == "ready"]
    dropped = [r for r in rows if r["status"] == "dropped"]

    lines.append("")
    lines.append("PROPOSED CHANGES — NOTHING HAS BEEN WRITTEN")
    lines.append("=" * 78)
    if not ready:
        lines.append("  (no change passed the guards)")
    for row in ready:
        target = f"{row['object']} {row['record_id']}" + (f" · {row['record_name']}" if row["record_name"] else "")
        lines.append("")
        lines.append(f"  [{row['id']}] {target}")
        lines.append(f"      field     {row['field']}  ({row['field_label']})")
        lines.append(f"      current   {_short(row['current_value'], 90) or '(blank)'}")
        lines.append(f"      proposed  {_short(row['final_value'], 90)}")
        lines.append(f"      policy    {row['overwrite_policy']}   confidence {row['confidence']}")
        lines.append(f"      quote     \"{_short(row['quote'], 110)}\"")
        lines.append(f"                — {row['quote_speaker'] or 'speaker unknown'} at {row['quote_ts'] or '??:??'}"
                     f" · {row['meeting_title']}")
    lines.append("")
    lines.append(f"DROPPED BY THE GUARDS — {len(dropped)}")
    lines.append("-" * 78)
    for row in dropped:
        reason = row["drop_reasons"][0] if row["drop_reasons"] else {"code": "?", "message": ""}
        lines.append(f"  [{row['id']}] {row['object']}.{row['field']} — {reason['code']}: {_short(reason['message'], 120)}")
    lines.append("")
    stats = diff.get("stats", {})
    lines.append(f"proposed {stats.get('proposed', 0)} · ready {stats.get('ready', 0)} · "
                 f"dropped {stats.get('dropped', 0)} · records {stats.get('records_touched', 0)} · "
                 f"blanks filled {stats.get('blanks_filled', 0)} · existing values preserved "
                 f"{stats.get('existing_preserved', 0)}")
    lines.append(f"approval token: {diff.get('token', '')}")
    return "\n".join(lines)


# ----------------------------------------------------------------------- audit log


def audit_path() -> Path:
    return GTM_HOME / "audit" / f"{PLUGIN}.log"


def append_audit(entries: List[Dict[str, Any]]) -> Path:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return path


# -------------------------------------------------------------------------- CLI


def cmd_build(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    mode, warnings = resolve_mode(False, config)
    result = build_from_run(Path(args.run), config, mode=mode)
    result.warnings.extend(warnings)
    path = write_diff(result, Path(args.run))
    print(render_diff_table(result.to_dict()))
    print(f"\ndiff written to {path}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """
    The approval gate. Everything here is a hard precondition:

      --apply            must be present. Dry-run is the default; no config key changes that.
      not unattended     no CI/cron/scheduler environment.
      report rendered    you cannot approve a diff that was never shown to a human.
      review window      the rendered diff must have existed for min_review_seconds,
                         which is what stops a batch being proposed and applied in one breath.
      --approved-by      a named human, checked against config.approval.approvers.
      --token            the fingerprint printed on that diff. If the plan changed, it fails.
    """
    run = Path(args.run)
    config = load_config(args.config)
    approval_cfg = config.get("approval") or {}

    if not args.apply:
        raise GuardError(
            "Refusing: --apply was not passed.\n"
            "meeting-to-crm is dry-run by default and there is no setting that changes that. "
            "Re-run with --apply once a human has read the diff."
        )
    refuse_if_unattended()
    if not (config.get("audit") or {}).get("enabled", True):
        raise GuardError("Refusing: audit.enabled is false. The audit log is the price of writing — turn it back on.")

    diff = read_json(run / "diff.json")
    if diff is None:
        raise GuardError(f"No diff.json in {run}. Run analyze.py first — you cannot approve a diff that does not exist.")
    report = run / "report.html"
    if not report.exists():
        raise GuardError(
            f"No report.html in {run}. Run report.py and put the diff in front of a human before approving it."
        )

    min_wait = float(approval_cfg.get("min_review_seconds", 10) or 0)
    age = datetime.now(timezone.utc).timestamp() - report.stat().st_mtime
    if age < min_wait:
        raise GuardError(
            f"Refusing: the diff was rendered {age:.0f}s ago and the review window is {min_wait:.0f}s.\n"
            "Propose, stop, let a human read it, approve on a later turn. That ordering is the product."
        )

    approver = (args.approved_by or "").strip()
    if approval_cfg.get("require_named_approver", True) and len(approver) < 2:
        raise GuardError("Refusing: --approved-by must name the human who reviewed this batch.")
    allowed = [str(a).strip().lower() for a in approval_cfg.get("approvers") or []]
    if allowed and approver.lower() not in allowed:
        raise GuardError(
            f"Refusing: '{approver}' is not in config.approval.approvers ({', '.join(allowed)})."
        )

    expected = diff.get("token") or approval_token(diff.get("plan") or [])
    if (args.token or "").strip() != expected:
        raise GuardError(
            f"Refusing: token mismatch. The diff you are approving fingerprints as {expected}, "
            f"you passed {args.token!r}.\nEither you are approving a batch you did not read, or the "
            f"proposal changed after it was rendered. Re-render and review again."
        )

    existing = run / "approval.json"
    if existing.exists() and not args.reapprove:
        raise GuardError(f"Refusing: {existing} already exists — this batch was approved once already.")

    plan = diff.get("plan") or []
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        plan = filter_plan(plan, wanted)
        if not plan:
            raise GuardError(f"Refusing: --only {args.only} selected no rows from the approved diff.")

    approval = {
        "plugin": PLUGIN,
        "run_dir": str(run),
        "approved_by": approver,
        "approved_at": utcnow(),
        "diff_built_at": diff.get("generated_at"),
        "diff_rendered_at": datetime.fromtimestamp(report.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token": expected,
        "selected_rows": args.only or "all",
        "mode": "apply",
        "plan": plan,
    }
    write_json(run / "approval.json", approval)
    write_json(run / "write_plan.json", {"plan": plan, "token": expected, "approved_by": approver})
    print(f"Approved by {approver} at {approval['approved_at']}.")
    print(f"{len(plan)} write operation(s) authorised. Execute ONLY what is in {run / 'write_plan.json'},")
    print(f"then record the outcome with:  diff.py audit --run {run} --results <results.json>")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """
    Append the applied writes to ~/.leanscale-gtm/audit/meeting-to-crm.log.

    Anything reported as applied that is NOT in the approved plan is still
    logged — it happened, so it belongs on the record — but it is flagged
    approved:false and the command exits non-zero. Silence there would defeat
    the whole point of an audit log.
    """
    run = Path(args.run)
    approval = read_json(run / "approval.json")
    if approval is None:
        raise GuardError(f"No approval.json in {run}. Nothing was authorised, so nothing can be audited.")
    diff = read_json(run / "diff.json") or {}
    rows = {r["id"]: r for r in diff.get("rows") or []}
    results = read_json(Path(args.results))
    if results is None:
        raise GuardError(f"No results file at {args.results}.")
    if isinstance(results, dict):
        results = results.get("results") or []

    approved_pairs: Dict[Tuple[str, str, str], Any] = {}
    approved_creates: Dict[str, Dict[str, Any]] = {}
    for item in approval.get("plan") or []:
        if item.get("kind") == "update":
            for field, value in (item.get("values") or {}).items():
                approved_pairs[(str(item.get("object")), str(item.get("record_id")), canonical_field(field))] = value
        else:
            for rid in item.get("row_ids") or []:
                approved_creates[str(rid)] = item

    entries: List[Dict[str, Any]] = []
    unapproved: List[str] = []
    applied_fields = 0
    touched: set = set()

    for res in results:
        row_id = str(res.get("row_id") or "")
        row = rows.get(row_id, {})
        status = str(res.get("status") or "applied").lower()
        obj = str(res.get("object") or row.get("object") or "")
        rid = str(res.get("record_id") or row.get("record_id") or "")
        field = str(res.get("field") or row.get("field") or "")
        new_value = res.get("new_value", row.get("final_value"))
        old_value = res.get("old_value", row.get("current_value"))

        key = (obj, rid, canonical_field(field))
        is_create = row.get("action") == "create" or str(res.get("action") or "") == "create"
        approved_ok = (row_id in approved_creates) if is_create else (key in approved_pairs)
        if not approved_ok:
            unapproved.append(f"{obj}.{field} on {rid} (row {row_id or '?'})")

        entry = {
            "ts": utcnow(),
            "plugin": PLUGIN,
            "run_dir": str(run),
            "approved": bool(approved_ok),
            "approved_by": approval.get("approved_by"),
            "approved_at": approval.get("approved_at"),
            "token": approval.get("token"),
            "row_id": row_id,
            "action": "create" if is_create else "update",
            "object": obj,
            "record_id": rid,
            "record_name": row.get("record_name", ""),
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "status": status,
            "error": res.get("error", ""),
            "crm_tool": res.get("tool", ""),
            "source_meeting": {
                "id": row.get("meeting_id", res.get("meeting_id", "")),
                "title": row.get("meeting_title", ""),
                "date": row.get("meeting_date", ""),
                "source": row.get("meeting_source", ""),
                "quote": row.get("quote", ""),
                "quote_ts": row.get("quote_ts", ""),
            },
        }
        entries.append(entry)
        if status == "applied":
            applied_fields += 1
            touched.add((obj, rid or row_id))

    path = append_audit(entries)
    write_json(run / "applied.json", {
        "applied_at": utcnow(),
        "approved_by": approval.get("approved_by"),
        "fields_applied": applied_fields,
        "records_touched": len(touched),
        "failed": sum(1 for e in entries if e["status"] == "failed"),
        "unapproved": unapproved,
        "audit_log": str(path),
    })
    print(f"{len(entries)} write(s) recorded in {path}")
    print(f"applied {applied_fields} field(s) across {len(touched)} record(s)")
    if unapproved:
        print("\nWRITES REPORTED THAT WERE NEVER APPROVED:")
        for item in unapproved:
            print(f"  · {item}")
        print("They are in the audit log flagged approved:false. Investigate before running this again.")
        return 2
    return 0


def cmd_show_audit(args: argparse.Namespace) -> int:
    path = audit_path()
    if not path.exists():
        print(f"No audit log yet at {path} — nothing has ever been written by this plugin.")
        return 0
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    print(f"{len(lines)} write(s) logged in {path}\n")
    for line in lines[-int(args.tail):]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        flag = "" if entry.get("approved") else "  ** NOT APPROVED **"
        print(f"{entry.get('ts')}  {entry.get('object')}.{entry.get('field')} on {entry.get('record_id')}"
              f"  [{entry.get('status')}] by {entry.get('approved_by')}{flag}")
        print(f"    {_short(entry.get('old_value'), 50) or '(blank)'}  ->  {_short(entry.get('new_value'), 50)}")
        print(f"    source: {entry.get('source_meeting', {}).get('title', '')}")
    return 0


# ------------------------------------------------------------------------ selftest

def cmd_selftest(args: argparse.Namespace) -> int:
    """Prove the guards actually guard. Ships with the plugin as a health check."""
    import tempfile

    # GTM_HOME is resolved at import time, so the only way to keep the selftest's
    # fake writes out of a real audit log is to re-exec inside a sandbox.
    if not os.environ.get("LEANSCALE_GTM_SELFTEST"):
        sandbox = tempfile.mkdtemp(prefix="m2c-selftest-home-")
        env = dict(os.environ, LEANSCALE_GTM_HOME=sandbox, LEANSCALE_GTM_SELFTEST="1")
        env.pop("CI", None)
        print(f"(selftest sandbox: {sandbox} — your real config and audit log are not touched)", flush=True)
        os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), "selftest"], env)

    fails: List[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        if ok:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label} {detail}")
            fails.append(label)

    here = Path(__file__).resolve().parent.parent
    fixtures = here / "fixtures" / "salesforce"
    config = load_config(str(fixtures / "config.json"))

    tmp = Path(tempfile.mkdtemp(prefix="m2c-selftest-"))

    print("\nguards — allow-list and overwrite policy")
    result = build_from_run(fixtures, config)
    by_id = {r.id: r for r in result.rows}

    def dropped_for(row_id: str, code: str) -> bool:
        row = by_id.get(row_id)
        return bool(row and row.status == "dropped" and any(d["code"] == code for d in row.drop_reasons))

    check("field NOT on the allow-list is dropped", dropped_for("p-201", "field_not_on_allowlist"),
          str(by_id.get("p-201")))
    check("already-populated field is dropped under the default policy",
          dropped_for("p-202", "field_populated"), str(by_id.get("p-202")))
    check("populated field is PRESERVED, not overwritten",
          by_id["p-202"].current_value == "Reps rebuild the same forecast spreadsheet every Monday.",
          repr(by_id["p-202"].current_value))
    check("Amount is dropped as restricted", dropped_for("p-203", "field_restricted"))
    check("CloseDate is dropped as restricted", dropped_for("p-204", "field_restricted"))
    check("read-only field is dropped", dropped_for("p-205", "field_read_only"))
    check("unverifiable quote is dropped", dropped_for("p-401", "quote_not_verified"))
    check("value outside the picklist is dropped", dropped_for("p-206", "value_invalid_picklist"))
    check("low confidence is dropped", dropped_for("p-207", "confidence_below_floor"))
    check("no-op proposal is dropped", dropped_for("p-208", "no_change"))
    check("'always' policy may replace a populated field", by_id["p-101"].status == "ready")
    check("'append' policy keeps the old text",
          str(by_id["p-108"].final_value).startswith("Inbound from the Q2 webinar"))
    check("ambiguous match produces zero proposals",
          all(r.status == "dropped" for r in result.rows if r.meeting_id == "mtg-003"))
    check("ambiguous match is reported for a human",
          any(m.status == "ambiguous" for m in result.matches if m.meeting_id == "mtg-003"))
    check("unmatched meeting produces zero proposals",
          all(r.status == "dropped" for r in result.rows if r.meeting_id == "mtg-005"))
    check("duplicate stakeholder is not created", dropped_for("c-002", "child_already_exists"))
    check("new stakeholder IS proposed", by_id["c-001"].status == "ready")
    check("blank fill counted", result.blanks_filled > 0)
    check("existing values preserved counted", result.existing_preserved >= 1)

    print("\nguards — dry-run and approval")
    mode, warns = resolve_mode(False, dict(config, auto_apply=True, dry_run=False))
    check("config cannot enable apply", mode == "dry-run")
    check("config attempt is reported", any("auto_apply" in w for w in warns))
    check("--apply is the only switch", resolve_mode(True, config)[0] == "apply")

    token_a = result.token
    mutated = [dict(p) for p in result.plan]
    if mutated:
        first_field = list(mutated[0]["values"])[0]
        mutated[0] = dict(mutated[0], values=dict(mutated[0]["values"], **{first_field: "something else"}))
    check("token changes when the plan changes", approval_token(mutated) != token_a)

    run = tmp / "run"
    (run / "raw").mkdir(parents=True, exist_ok=True)
    write_diff(result, run)

    def approve(**kw: Any) -> Tuple[bool, str]:
        ns = argparse.Namespace(run=str(run), config=str(fixtures / "config.json"), apply=True,
                                approved_by="Dana Ruiz", token=result.token, only=None, reapprove=False)
        for key, value in kw.items():
            setattr(ns, key, value)
        try:
            cmd_approve(ns)
            return True, ""
        except GuardError as exc:
            return False, str(exc)

    ok, msg = approve(apply=False)
    check("approve without --apply is refused", not ok and "--apply" in msg)
    ok, msg = approve()
    check("approve without a rendered report is refused", not ok and "report.html" in msg)

    (run / "report.html").write_text("<html>diff</html>", encoding="utf-8")
    old = datetime.now(timezone.utc).timestamp() - 3600
    os.utime(run / "report.html", (old, old))

    ok, msg = approve(token="deadbeef")
    check("approve with a stale/wrong token is refused", not ok and "token mismatch" in msg)
    ok, msg = approve(approved_by="")
    check("approve without a named human is refused", not ok and "approved-by" in msg)

    os.environ["CI"] = "true"
    ok, msg = approve()
    check("approve refuses in an unattended/scheduled environment", not ok and "unattended" in msg)
    os.environ.pop("CI", None)

    subset = [r.id for r in result.ready if r.action == "update"][:2]
    ok, msg = approve(only=",".join(subset))
    partial = read_json(run / "write_plan.json") or {}
    approved_fields = sum(len(p.get("values") or {}) for p in partial.get("plan") or [])
    check("partial approval authorises ONLY the rows picked", ok and approved_fields == len(subset),
          f"{approved_fields} fields authorised for {len(subset)} rows")
    (run / "approval.json").unlink()

    ok, msg = approve()
    check("a reviewed, named, correctly-tokened batch IS approved", ok, msg)
    check("write_plan.json emitted", (run / "write_plan.json").exists())
    ok, msg = approve()
    check("double approval is refused", not ok and "already" in msg)

    print("\nguards — audit log")
    good_row = next(r for r in result.ready if r.action == "update")
    results_file = run / "results.json"
    write_json(results_file, [{"row_id": good_row.id, "status": "applied", "tool": "test"}])
    rc = cmd_audit(argparse.Namespace(run=str(run), results=str(results_file)))
    check("approved write is logged", rc == 0 and audit_path().exists())
    log_lines = audit_path().read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(log_lines[-1])
    check("audit line carries record, field, old and new value",
          entry["record_id"] and entry["field"] and "old_value" in entry and "new_value" in entry)
    check("audit line carries the source meeting", bool(entry["source_meeting"]["id"]))
    check("audit line carries the approver", entry["approved_by"] == "Dana Ruiz")

    write_json(results_file, [{"row_id": "made-up", "status": "applied", "object": "Opportunity",
                               "record_id": "006ROGUE", "field": "Amount", "new_value": 999}])
    rc = cmd_audit(argparse.Namespace(run=str(run), results=str(results_file)))
    check("an unapproved write is logged AND fails loudly", rc == 2)
    rogue = json.loads(audit_path().read_text(encoding="utf-8").strip().splitlines()[-1])
    check("rogue write flagged approved:false", rogue["approved"] is False)

    print()
    if fails:
        print(f"FAILED {len(fails)}: {', '.join(fails)}")
        return 1
    print("meeting-to-crm guards: all checks passed")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diff.py",
        description="meeting-to-crm proposal/diff builder, guard engine and audit-log writer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="rebuild diff.json from a run directory (dry-run, always)")
    p_build.add_argument("--run", required=True)
    p_build.add_argument("--config", default=None)
    p_build.set_defaults(func=cmd_build)

    p_ok = sub.add_parser("approve", help="record a named human's approval of a rendered diff")
    p_ok.add_argument("--run", required=True)
    p_ok.add_argument("--config", default=None)
    p_ok.add_argument("--approved-by", dest="approved_by", default="")
    p_ok.add_argument("--token", default="")
    p_ok.add_argument("--only", default=None, help="comma-separated row ids to approve (default: all)")
    p_ok.add_argument("--apply", action="store_true",
                      help="REQUIRED. Without it nothing is authorised — dry-run is the default.")
    p_ok.add_argument("--reapprove", action="store_true")
    p_ok.set_defaults(func=cmd_approve)

    p_audit = sub.add_parser("audit", help="append applied writes to the audit log")
    p_audit.add_argument("--run", required=True)
    p_audit.add_argument("--results", required=True)
    p_audit.set_defaults(func=cmd_audit)

    p_show = sub.add_parser("show-audit", help="print the tail of the audit log")
    p_show.add_argument("--tail", default=20)
    p_show.set_defaults(func=cmd_show_audit)

    p_test = sub.add_parser("selftest", help="prove the guards work, against the bundled fixtures")
    p_test.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except GuardError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
