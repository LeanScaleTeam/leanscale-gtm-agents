#!/usr/bin/env python3
"""
stage-architect / analyze.py

Layer 2 of the plugin. Reads raw/*.json that Claude fetched through MCP and turns
it into findings.json. Pure standard library, no network, no MCP, deterministic.

The one idea this file exists to implement correctly:

    Conversion must be cohort-controlled by the stage a deal ENTERED, taken from
    stage-transition history. Conversion computed from the stage a deal is SITTING
    IN today is survivorship bias, and it inflates every rate, because a deal that
    died in Discovery is now sitting in Closed Lost and has silently left the
    Discovery denominator.

Both numbers are computed. The snapshot one is labelled as wrong wherever it appears.

Usage
    python3 analyze.py --run-dir ./gtm-agents/stage-architect/2026-08-10-1430
    python3 analyze.py --raw ./fixtures/salesforce/raw --out /tmp/findings.json \
                       --config ./fixtures/salesforce/config.json --as-of 2026-08-10
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    SourceEmptyError,
    load_plugin_config,
    load_profile,
    median,
    normalize_records,
    parse_dt,
    pct,
    percentile,
)

PLUGIN = "stage-architect"

DEFAULTS: Dict[str, Any] = {
    "crm": "salesforce",
    "pipelines_in_scope": [],
    "history_lookback_days": 540,
    "min_cohort_size": 30,
    "equivalence_band_pp": 5.0,
    "significance_alpha": 0.05,
    "zero_dwell_hours": 24,
    "skip_rate_flag_pct": 25.0,
    "regression_flag_pct": 8.0,
    "zombie_p90_multiple": 6.0,
    "loss_reason_fill_floor_pct": 80.0,
    "loss_reason_dominance_pct": 60.0,
    "history_coverage_floor_pct": 80.0,
    "sales_accepted_stage": "",
    "lead_lifecycle_exists": False,
    "stages_are_meant_to_be": "buyer_verifiable",
    "stage_definitions": {},
    "believed_conversion_rates": {},
    "fields": {},
    "material_deal_floor": None,
    "org_name": None,
}

# Field-name fallbacks, tried in order, so a missing `fields` block still works
# against either CRM.
FIELD_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "id": ("Id", "id", "hs_object_id"),
    "name": ("Name", "dealname", "name"),
    "stage": ("StageName", "dealstage", "stage"),
    "amount": ("Amount", "amount", "hs_tcv"),
    "created": ("CreatedDate", "createdate", "created_at"),
    "close_date": ("CloseDate", "closedate", "close_date"),
    "last_modified": ("LastModifiedDate", "hs_lastmodifieddate", "SystemModstamp", "updatedAt"),
    "loss_reason": (
        "Loss_Reason__c", "closed_lost_reason", "Closed_Lost_Reason__c",
        "Loss_Reason", "hs_closed_lost_reason", "LossReason__c",
    ),
    "pipeline": ("Pipeline", "pipeline", "Pipeline__c"),
    "owner": ("Owner.Name", "hubspot_owner_id", "OwnerId", "owner"),
    "record_type": ("RecordType.Name", "RecordTypeId", "dealtype"),
}


# --------------------------------------------------------------------------- io


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _as_list(payload: Any, *keys: str) -> List[Dict[str, Any]]:
    """
    Unwrap the shapes MCP CRM tools actually return: a bare list, {"records": [...]}
    (Salesforce), {"results": [...]} (HubSpot), or {"data": [...]}.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in list(keys) + ["records", "results", "data", "rows", "items"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _layer_config(path: Optional[Path]) -> Dict[str, Any]:
    merged = dict(DEFAULTS)
    if path is not None:
        payload = _read_json(path) or {}
        for key, value in payload.items():
            if not key.startswith("_"):
                merged[key] = value
        return merged
    return load_plugin_config(PLUGIN, defaults=DEFAULTS)


# ------------------------------------------------------------------ stage model


class StageModel:
    """
    The ladder. Open stages get positions 0..n-1 in pipeline order; the won
    terminal sits at position n; lost terminals sit OFF the ladder (position
    None), which is the whole point - a lost deal did not advance, it stopped.
    """

    def __init__(self, meta: Dict[str, Any], pipeline_id: str):
        self.pipeline_id = pipeline_id
        self.pipeline_label = pipeline_id
        self.stages: List[Dict[str, Any]] = []
        for pipeline in meta.get("pipelines") or []:
            if str(pipeline.get("id")) != str(pipeline_id):
                continue
            self.pipeline_label = pipeline.get("label") or pipeline_id
            self.stages = sorted(
                (dict(s) for s in pipeline.get("stages") or []),
                key=lambda s: (int(s.get("order", 0)), str(s.get("label", ""))),
            )

        self.by_id: Dict[str, Dict[str, Any]] = {}
        for stage in self.stages:
            self.by_id[str(stage.get("id"))] = stage
            label = str(stage.get("label") or "")
            # HubSpot history stores stage ids; a hand-built export may store labels.
            if label and label not in self.by_id:
                self.by_id[label] = stage

        self.open_ids: List[str] = [
            str(s["id"]) for s in self.stages if not s.get("is_closed")
        ]
        self.won_ids: List[str] = [
            str(s["id"]) for s in self.stages if s.get("is_closed") and s.get("is_won")
        ]
        self.lost_ids: List[str] = [
            str(s["id"]) for s in self.stages if s.get("is_closed") and not s.get("is_won")
        ]

        self.position: Dict[str, Optional[int]] = {}
        for i, sid in enumerate(self.open_ids):
            self.position[sid] = i
        self.won_position = len(self.open_ids)
        for sid in self.won_ids:
            self.position[sid] = self.won_position
        for sid in self.lost_ids:
            self.position[sid] = None

    def resolve(self, raw_stage: Any) -> Optional[str]:
        """Map whatever the record holds (id or label) onto a canonical stage id."""
        if raw_stage in (None, ""):
            return None
        key = str(raw_stage)
        stage = self.by_id.get(key)
        return str(stage["id"]) if stage else None

    def label(self, stage_id: str) -> str:
        stage = self.by_id.get(stage_id)
        return str(stage.get("label") or stage_id) if stage else stage_id

    def pos(self, stage_id: Optional[str]) -> Optional[int]:
        if stage_id is None:
            return None
        return self.position.get(stage_id)

    def is_lost(self, stage_id: Optional[str]) -> bool:
        return stage_id in self.lost_ids

    def is_won(self, stage_id: Optional[str]) -> bool:
        return stage_id in self.won_ids

    def is_closed(self, stage_id: Optional[str]) -> bool:
        return self.is_won(stage_id) or self.is_lost(stage_id)


# ---------------------------------------------------------------- history shapes


def normalize_history(rows: List[Dict[str, Any]]) -> Dict[str, List[Tuple[str, datetime]]]:
    """
    Fold the three real-world stage-history shapes into {opp_id: [(stage, when)]}.

      1. Salesforce OpportunityHistory
         {"OpportunityId": "006..", "StageName": "Discovery", "CreatedDate": ".."}
         NOTE: a row is written on any Amount / CloseDate / Probability change too,
         so consecutive rows repeat the stage. Collapsed below.

      2. Salesforce OpportunityFieldHistory
         {"OpportunityId": "006..", "Field": "StageName",
          "OldValue": "Discovery", "NewValue": "Qualification", "CreatedDate": ".."}

      3. HubSpot deal property history, nested or flat
         {"dealId": "123", "propertyName": "dealstage",
          "history": [{"value": "qualifiedtobuy", "timestamp": ".."}, ...]}
         HubSpot returns history NEWEST FIRST; we sort, so order in the file is
         irrelevant.
    """
    raw: Dict[str, List[Tuple[str, datetime, int]]] = {}

    def push(opp_id: Any, stage: Any, when: Any, tiebreak: int = 0) -> None:
        if opp_id in (None, "") or stage in (None, ""):
            return
        stamp = parse_dt(when)
        if stamp is None:
            return
        raw.setdefault(str(opp_id), []).append((str(stage), stamp, tiebreak))

    for row in rows:
        if not isinstance(row, dict):
            continue

        nested = row.get("history") or row.get("propertyHistory") or row.get("versions")
        if isinstance(nested, list):
            opp_id = row.get("dealId") or row.get("id") or row.get("objectId") or row.get("Id")
            for entry in nested:
                if isinstance(entry, dict):
                    push(opp_id, entry.get("value") or entry.get("stage"),
                         entry.get("timestamp") or entry.get("occurredAt") or entry.get("date"))
            continue

        if str(row.get("Field") or row.get("field") or "").lower() == "stagename":
            opp_id = row.get("OpportunityId") or row.get("ParentId") or row.get("Id")
            # OldValue on the earliest row is the stage the deal started in; it is
            # pushed one second earlier so it always sorts ahead of its own change.
            push(opp_id, row.get("OldValue"), row.get("CreatedDate"), tiebreak=-1)
            push(opp_id, row.get("NewValue"), row.get("CreatedDate"), tiebreak=1)
            continue

        opp_id = (
            row.get("OpportunityId") or row.get("dealId") or row.get("objectId")
            or row.get("ParentId") or row.get("Id") or row.get("id")
        )
        stage = row.get("StageName") or row.get("value") or row.get("dealstage") or row.get("stage")
        when = (
            row.get("CreatedDate") or row.get("timestamp") or row.get("SystemModstamp")
            or row.get("occurredAt") or row.get("date")
        )
        push(opp_id, stage, when)

    out: Dict[str, List[Tuple[str, datetime]]] = {}
    for opp_id, entries in raw.items():
        entries.sort(key=lambda e: (e[1], e[2]))
        collapsed: List[Tuple[str, datetime]] = []
        for stage, when, _ in entries:
            if collapsed and collapsed[-1][0] == stage:
                continue  # same stage again = an Amount/CloseDate edit, not a move
            collapsed.append((stage, when))
        out[opp_id] = collapsed
    return out


# -------------------------------------------------------------------- journeys


class Journey:
    """One opportunity's path through the ladder."""

    __slots__ = (
        "opp_id", "amount", "created", "closed_at", "current_stage", "entries",
        "history_missing", "positions", "max_pos", "entered", "skipped",
        "regressions", "dwells", "open_dwell", "terminal",
    )

    def __init__(
        self,
        opp_id: str,
        current_stage: str,
        created: Optional[datetime],
        closed_at: Optional[datetime],
        amount: Optional[float],
        entries: List[Tuple[str, datetime]],
        model: StageModel,
        as_of: datetime,
    ):
        self.opp_id = opp_id
        self.amount = amount
        self.created = created
        self.closed_at = closed_at
        self.current_stage = current_stage
        self.history_missing = not entries
        self.terminal: Optional[str] = (
            "won" if model.is_won(current_stage)
            else ("lost" if model.is_lost(current_stage) else None)
        )

        if not entries:
            # No transition rows for this deal. Seed a single-point timeline so the
            # deal still counts in the denominators it belongs to, and flag it.
            entries = [(current_stage, created or as_of)]
        else:
            # History can lag the record. If the last transition disagrees with the
            # stage the record is in now, append the current stage.
            if entries[-1][0] != current_stage:
                tail = closed_at or as_of
                if tail < entries[-1][1]:
                    tail = entries[-1][1]
                entries = entries + [(current_stage, tail)]

        self.entries = entries
        self.positions = [model.pos(s) for s, _ in entries]
        ladder_positions = [p for p in self.positions if p is not None]
        self.max_pos: Optional[int] = max(ladder_positions) if ladder_positions else None
        self.entered: Set[str] = {s for s, _ in entries}

        # Stages jumped over: on any forward move, everything strictly between.
        self.skipped: Set[str] = set()
        self.regressions: List[Tuple[str, str]] = []
        for i in range(len(entries) - 1):
            a, b = self.positions[i], self.positions[i + 1]
            if a is None or b is None:
                continue
            if b > a + 1:
                for sid, p in model.position.items():
                    if p is not None and a < p < b and sid in model.open_ids:
                        self.skipped.add(sid)
            elif b < a:
                self.regressions.append((entries[i][0], entries[i + 1][0]))

        # Time in stage. Every hop closes the previous dwell. The final stage is
        # closed by the close timestamp when the deal is closed, and is censored
        # (still running) when it is open.
        self.dwells: List[Tuple[str, float]] = []
        self.open_dwell: Optional[Tuple[str, float]] = None
        for i in range(len(entries) - 1):
            days = (entries[i + 1][1] - entries[i][1]).total_seconds() / 86400.0
            self.dwells.append((entries[i][0], max(0.0, days)))
        last_stage, last_when = entries[-1]
        if model.is_closed(last_stage):
            pass  # a terminal stage has no dwell worth measuring
        elif closed_at is not None:
            self.dwells.append((last_stage, max(0.0, (closed_at - last_when).total_seconds() / 86400.0)))
        else:
            self.open_dwell = (last_stage, max(0.0, (as_of - last_when).total_seconds() / 86400.0))

    @property
    def is_won(self) -> bool:
        return self.terminal == "won"

    @property
    def is_lost(self) -> bool:
        return self.terminal == "lost"

    @property
    def is_open(self) -> bool:
        return self.terminal is None


# ------------------------------------------------------------------- statistics


def two_proportion_test(x1: int, n1: int, x2: int, n2: int) -> Tuple[Optional[float], Optional[float]]:
    """
    Pooled two-proportion z-test. Returns (z, two-sided p).

    Used ONLY to fail to reject - i.e. to say 'we cannot tell these two stages
    apart'. That inference is only honest with enough deals behind it, which is
    why min_cohort_size gates every call.
    """
    if n1 <= 0 or n2 <= 0:
        return None, None
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    denom = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    if denom <= 0:
        # Both rates are 0% or both 100%: identical by construction.
        return (0.0, 1.0) if abs(p1 - p2) < 1e-12 else (None, None)
    z = (p1 - p2) / math.sqrt(denom)
    p_value = math.erfc(abs(z) / math.sqrt(2.0))
    return round(z, 4), round(p_value, 4)


def _r(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


# -------------------------------------------------------------- exit criteria


ARCHETYPE_PATTERNS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("discovery", "connect", "intro", "first meeting", "appointment", "prospect", "engaged"), "discovery"),
    (("qualif", "sql", "sales accepted", "needs analysis", "scoping"), "qualification"),
    (("demo", "presentation", "solution", "technical", "evaluation", "validation", "poc",
      "proof of concept", "pilot", "trial", "bake"), "evaluation"),
    (("champion", "decision maker", "business case", "value", "econ", "consensus", "boughtin"), "champion"),
    (("proposal", "quote", "pricing", "propos", "sow", "estimate"), "proposal"),
    (("negotiat", "legal", "redline", "procurement", "security review", "paper",
      "verbal", "commit", "msa"), "negotiation"),
    (("contract", "closing", "signature", "signed", "docusign", "countersign", "po "), "closing"),
)

EXIT_CRITERIA: Dict[str, List[Dict[str, str]]] = {
    "discovery": [
        {
            "buyer_verifiable": "The buyer has stated, in their own words and in writing, the problem and what it currently costs them.",
            "artifact": "Recap email the buyer replied to, or a CRM note quoting them directly.",
            "rep_asserted": "Rep believes there is a real pain.",
        },
        {
            "buyer_verifiable": "A second, specific meeting is on both calendars with an accepted invite.",
            "artifact": "Accepted calendar invite with a date.",
            "rep_asserted": "Rep will follow up next week.",
        },
        {
            "buyer_verifiable": "The buyer has named who else on their side has to be involved.",
            "artifact": "Named individuals with roles, sourced from the buyer, on the opportunity contact roles.",
            "rep_asserted": "Rep has mapped the account.",
        },
    ],
    "qualification": [
        {
            "buyer_verifiable": "The buyer has confirmed the metric this must move, and its current value.",
            "artifact": "A number the buyer supplied, in an email or a shared doc.",
            "rep_asserted": "Rep confirms there is a compelling business need.",
        },
        {
            "buyer_verifiable": "Someone on the buyer's side has named who signs, and that name is recorded.",
            "artifact": "Buyer-sourced statement identifying the approver, on the record.",
            "rep_asserted": "Rep has identified the decision maker.",
        },
        {
            "buyer_verifiable": "The buyer has confirmed a budget exists or described how funding would be found.",
            "artifact": "Written buyer statement on funding source, not a rep estimate.",
            "rep_asserted": "Rep believes budget is available.",
        },
        {
            "buyer_verifiable": "The buyer has confirmed a date by which the problem must be solved, and why that date.",
            "artifact": "Buyer-stated compelling event with a reason attached.",
            "rep_asserted": "Close date set to end of quarter.",
        },
    ],
    "evaluation": [
        {
            "buyer_verifiable": "The buyer has agreed in writing to the success criteria the evaluation will be judged on.",
            "artifact": "Signed-off or replied-to success-criteria doc.",
            "rep_asserted": "Demo went well.",
        },
        {
            "buyer_verifiable": "A technical owner on the buyer's side has been assigned and has attended a working session.",
            "artifact": "Attendance record naming the buyer's technical owner.",
            "rep_asserted": "Rep has technical alignment.",
        },
        {
            "buyer_verifiable": "The buyer has told you what happens if the evaluation passes - the next step and who takes it.",
            "artifact": "Buyer-described next step recorded on the opportunity.",
            "rep_asserted": "Rep expects to move to proposal.",
        },
    ],
    "champion": [
        {
            "buyer_verifiable": "A named person inside the account has advocated for you in a meeting you were not in, and told you so.",
            "artifact": "Buyer message referencing an internal conversation.",
            "rep_asserted": "Rep has a champion.",
        },
        {
            "buyer_verifiable": "The buyer has shared their internal approval steps and the dates attached to them.",
            "artifact": "Buyer-supplied approval path or procurement calendar.",
            "rep_asserted": "Rep understands the process.",
        },
        {
            "buyer_verifiable": "The economic buyer has personally engaged - a call, an email reply, or a meeting attendance.",
            "artifact": "Direct interaction with the economic buyer, timestamped.",
            "rep_asserted": "Champion says the exec is on board.",
        },
    ],
    "proposal": [
        {
            "buyer_verifiable": "The buyer has received the proposal AND acknowledged receipt with a response.",
            "artifact": "Buyer reply, or a document-open event plus a scheduled review.",
            "rep_asserted": "Proposal sent.",
        },
        {
            "buyer_verifiable": "The buyer has confirmed the scope and the commercial shape are what they asked for.",
            "artifact": "Written buyer confirmation of scope, or their redlines on it.",
            "rep_asserted": "Rep believes pricing is aligned.",
        },
        {
            "buyer_verifiable": "A decision date has been supplied by the buyer, not chosen by the rep.",
            "artifact": "Buyer-stated decision date on the record.",
            "rep_asserted": "Close date pushed to next month.",
        },
    ],
    "negotiation": [
        {
            "buyer_verifiable": "Legal or procurement on the buyer's side has been formally engaged and a contact is named.",
            "artifact": "Named procurement/legal contact with a first interaction on record.",
            "rep_asserted": "Deal is in legal.",
        },
        {
            "buyer_verifiable": "Redlines or a security questionnaire have been received from the buyer.",
            "artifact": "The document the buyer sent back.",
            "rep_asserted": "Rep expects redlines soon.",
        },
        {
            "buyer_verifiable": "The buyer has confirmed the remaining open items and who owns each one.",
            "artifact": "A mutual close plan the buyer has responded to.",
            "rep_asserted": "Rep has a verbal.",
        },
    ],
    "closing": [
        {
            "buyer_verifiable": "The final paper is out for signature with the signer named and the request opened.",
            "artifact": "E-signature envelope status showing the signer.",
            "rep_asserted": "Contract sent.",
        },
        {
            "buyer_verifiable": "The buyer has supplied the PO or the billing details required to invoice.",
            "artifact": "PO number or billing contact captured from the buyer.",
            "rep_asserted": "Rep expects a PO.",
        },
    ],
    "generic": [
        {
            "buyer_verifiable": "The buyer has done something observable that only happens if this stage is genuinely complete.",
            "artifact": "An artifact the buyer produced - a reply, a document, an accepted invite, a signature.",
            "rep_asserted": "Rep judges the stage complete.",
        },
        {
            "buyer_verifiable": "The next step has a date and an owner that the buyer has agreed to.",
            "artifact": "Buyer-confirmed next step recorded on the opportunity.",
            "rep_asserted": "Next step is 'follow up'.",
        },
    ],
}


def archetype_for(label: str) -> str:
    text = str(label).lower()
    for needles, name in ARCHETYPE_PATTERNS:
        if any(n in text for n in needles):
            return name
    return "generic"


# ------------------------------------------------------------------- analysis


def analyse(raw_dir: Path, config: Dict[str, Any], as_of: datetime, run_dir: Path) -> FindingsDoc:
    profile = load_profile(required=False)
    fields = {**{k: v[0] for k, v in FIELD_CANDIDATES.items()}, **(config.get("fields") or {})}

    def get(record: Dict[str, Any], key: str) -> Any:
        primary = fields.get(key)
        if primary and record.get(primary) not in (None, ""):
            return record.get(primary)
        for candidate in FIELD_CANDIDATES.get(key, ()):
            if record.get(candidate) not in (None, ""):
                return record.get(candidate)
        return None

    meta = _read_json(raw_dir / "stage_metadata.json") or {}
    opps_raw = normalize_records(_as_list(_read_json(raw_dir / "opportunities.json")))
    history_raw = _as_list(_read_json(raw_dir / "stage_history.json"))
    lifecycle = _read_json(raw_dir / "lead_lifecycle.json") or {}
    sources_meta = _read_json(raw_dir / "_sources.json") or {}

    crm = str(meta.get("crm") or config.get("crm") or "salesforce").lower()
    default_tool = "run_soql_query" if crm == "salesforce" else "hubspot_crm_search"

    def src(key: str) -> Dict[str, str]:
        entry = sources_meta.get(key) or {}
        return {
            "tool": entry.get("tool") or default_tool,
            "query": entry.get("query") or "",
            "note": entry.get("note") or "",
        }

    # ---- manifest first: a required source that came back empty aborts the run,
    # ---- before any analysis runs and before a clean-looking empty report exists.
    lookback = int(config.get("history_lookback_days", 540) or 540)
    requested_window = {
        "start": (as_of - timedelta(days=lookback)).strftime("%Y-%m-%d"),
        "end": as_of.strftime("%Y-%m-%d"),
    }
    manifest = RunManifest(PLUGIN, run_dir, window=requested_window)
    s = src("stage_metadata")
    manifest.record(
        "stage_metadata", tool=s["tool"], count=sum(len(p.get("stages") or []) for p in meta.get("pipelines") or []),
        query=s["query"], required=True, note=s["note"],
        diagnosis=(
            "The stage picklist came back empty. On Salesforce the connected user may lack "
            "read access to OpportunityStage; on HubSpot the token is probably missing the "
            "crm.objects.deals.read / pipelines scope."
        ),
    )
    s = src("opportunities")
    manifest.record(
        "opportunities", tool=s["tool"], count=len(opps_raw), query=s["query"],
        required=True, note=s["note"],
        diagnosis=(
            "Zero opportunities in the window. Either the connected identity cannot see the "
            "Opportunity/Deal object, or the date filter excluded everything - check that the "
            "lookback and the created-date field name are right before believing the CRM is empty."
        ),
    )
    s = src("stage_history")
    manifest.record(
        "stage_history", tool=s["tool"], count=len(history_raw), query=s["query"],
        required=False, note=s["note"],
        diagnosis=(
            "No stage-transition rows. On Salesforce, OpportunityHistory may be restricted, or "
            "field history tracking was never switched on for StageName (and field history is "
            "only retained 18 months, 24 with Field Audit Trail). On HubSpot, the deal read did "
            "not request propertiesWithHistory=dealstage. Without this, conversion rates are "
            "guesses, not measurements."
        ),
    )
    s = src("lead_lifecycle")
    manifest.record(
        "lead_lifecycle", tool=s["tool"], count=len(_as_list(lifecycle, "records")),
        query=s["query"], required=False, note=s["note"],
        diagnosis="No lead/contact lifecycle records returned; the pre-opportunity funnel is not covered.",
    )
    if not history_raw:
        manifest.warn(
            "No stage-transition history. Conversion rates degrade to a snapshot analysis and are "
            "NOT measurements. See the stage-history-unavailable finding."
        )
    manifest.finalize()  # raises SourceEmptyError if stage_metadata or opportunities came back empty

    # ---- pipeline selection -------------------------------------------------
    all_pipelines = [str(p.get("id")) for p in meta.get("pipelines") or []]
    in_scope = [str(p) for p in (config.get("pipelines_in_scope") or [])] or all_pipelines
    in_scope = [p for p in in_scope if p in all_pipelines] or all_pipelines

    floor = config.get("material_deal_floor")
    if floor is None:
        floor = profile.get("material_deal_floor") or 0

    def amount_of(record: Dict[str, Any]) -> Optional[float]:
        value = get(record, "amount")
        try:
            return float(str(value).replace(",", "").replace("$", "")) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def pipeline_of(record: Dict[str, Any]) -> str:
        value = get(record, "pipeline")
        return str(value) if value not in (None, "") else (all_pipelines[0] if all_pipelines else "default")

    by_pipeline: Dict[str, List[Dict[str, Any]]] = {}
    below_floor = 0
    for record in opps_raw:
        amount = amount_of(record)
        if floor and amount is not None and amount < float(floor):
            below_floor += 1
            continue
        by_pipeline.setdefault(pipeline_of(record), []).append(record)

    scoped = {p: rows for p, rows in by_pipeline.items() if p in in_scope}
    if not scoped:
        scoped = by_pipeline
    primary = max(scoped, key=lambda p: len(scoped[p])) if scoped else (all_pipelines[0] if all_pipelines else "default")

    model = StageModel(meta, primary)
    opps = scoped.get(primary, [])

    timelines = normalize_history(history_raw)
    has_history = bool(timelines)

    # ---- journeys -----------------------------------------------------------
    journeys: List[Journey] = []
    unmapped_stages: Dict[str, int] = {}
    for record in opps:
        opp_id = str(get(record, "id") or "")
        if not opp_id:
            continue
        current = model.resolve(get(record, "stage"))
        if current is None:
            unmapped_stages[str(get(record, "stage"))] = unmapped_stages.get(str(get(record, "stage")), 0) + 1
            continue
        created = parse_dt(get(record, "created"))
        closed_at = None
        if model.is_closed(current):
            closed_at = parse_dt(get(record, "last_modified")) or parse_dt(get(record, "close_date"))
        entries: List[Tuple[str, datetime]] = []
        for stage_raw, when in timelines.get(opp_id, []):
            resolved = model.resolve(stage_raw)
            if resolved is not None:
                entries.append((resolved, when))
        collapsed: List[Tuple[str, datetime]] = []
        for stage, when in entries:
            if collapsed and collapsed[-1][0] == stage:
                continue
            collapsed.append((stage, when))

        journeys.append(
            Journey(opp_id, current, created, closed_at, amount_of(record), collapsed, model, as_of)
        )

    total = len(journeys)
    won = [j for j in journeys if j.is_won]
    lost = [j for j in journeys if j.is_lost]
    open_deals = [j for j in journeys if j.is_open]
    closed_n = len(won) + len(lost)
    measured_win_rate = pct(len(won), closed_n)

    history_covered = sum(1 for j in journeys if not j.history_missing)
    history_coverage_pct = pct(history_covered, total)

    dates = [j.created for j in journeys if j.created]
    window = {
        "start": min(dates).strftime("%Y-%m-%d") if dates else requested_window["start"],
        "end": as_of.strftime("%Y-%m-%d"),
    }

    # ---- per-stage cohort analysis -----------------------------------------
    # A stage nobody ever entered is DEAD, not skipped. Establish which stages
    # have traffic before measuring anything, so dead stages never pollute the
    # skip numbers - otherwise every deal "skips" a stage that does not exist in
    # practice and the headline skip rate becomes meaningless.
    entered_counts: Dict[str, int] = {
        sid: sum(1 for j in journeys if sid in j.entered) for sid in model.open_ids
    }
    live_ids: Set[str] = {sid for sid, count in entered_counts.items() if count > 0}

    stage_rows: List[Dict[str, Any]] = []
    stage_stats: Dict[str, Dict[str, Any]] = {}
    for sid in model.open_ids:
        position = model.pos(sid)
        entered = [j for j in journeys if sid in j.entered]
        advanced = [j for j in entered if j.max_pos is not None and position is not None and j.max_pos > position]
        advanced_ids = {j.opp_id for j in advanced}
        died = [j for j in entered if j.is_lost and j.opp_id not in advanced_ids]
        still = [j for j in entered if j.opp_id not in advanced_ids and not j.is_lost]
        resolved = len(advanced) + len(died)
        won_from = [j for j in entered if j.is_won]

        # The snapshot (survivorship-biased) rate: current stage only, no history.
        snap_denom = [
            j for j in journeys
            if model.pos(j.current_stage) is not None and position is not None
            and model.pos(j.current_stage) >= position
        ]
        snap_num = [
            j for j in snap_denom
            if model.pos(j.current_stage) is not None and position is not None
            and model.pos(j.current_stage) > position
        ]

        is_live = sid in live_ids
        # Only journeys with real stage history can evidence a skip. A journey without
        # it has `entered` = {current stage} alone, so every earlier stage trivially
        # looks un-entered and the rate comes out at 100% for the whole funnel — a
        # confident claim manufactured from the absence of data. Excluded from both
        # sides of the ratio, so a fully degraded run reports None, not a catastrophe.
        got_past = [j for j in journeys
                    if not j.history_missing and j.max_pos is not None
                    and position is not None and j.max_pos > position]
        skipped_by = [j for j in got_past if sid not in j.entered] if is_live else []

        dwells = [d for j in journeys for stage, d in j.dwells if stage == sid]
        open_ages = [d for j in journeys if j.open_dwell and j.open_dwell[0] == sid for d in [j.open_dwell[1]]]

        stats = {
            "stage": model.label(sid),
            "stage_id": sid,
            "position": position,
            "n_entered": len(entered),
            "n_resolved": resolved,
            "n_advanced": len(advanced),
            "n_died_here": len(died),
            "n_still_open_here": len(still),
            "forward_rate_pct": pct(len(advanced), resolved) if resolved else None,
            "win_rate_from_pct": pct(len(won_from), resolved) if resolved else None,
            "naive_rate_pct": pct(len(snap_num), len(snap_denom)) if snap_denom else None,
            "naive_n": len(snap_denom),
            "n_got_past": len(got_past) if is_live else 0,
            "n_skipped_stage": len(skipped_by),
            "skip_rate_pct": pct(len(skipped_by), len(got_past)) if (is_live and got_past) else None,
            "has_traffic": is_live,
            "dwell_n": len(dwells),
            "dwell_median_days": _r(median(dwells)),
            "dwell_mean_days": _r(sum(dwells) / len(dwells)) if dwells else None,
            "dwell_p75_days": _r(percentile(dwells, 75)),
            "dwell_p90_days": _r(percentile(dwells, 90)),
            "open_here_median_age_days": _r(median(open_ages)),
            "currently_here": sum(1 for j in journeys if j.current_stage == sid),
        }
        rate = stats["forward_rate_pct"]
        naive = stats["naive_rate_pct"]
        stats["naive_inflation_pp"] = _r(naive - rate) if (rate is not None and naive is not None) else None
        stage_stats[sid] = stats
        stage_rows.append(stats)

    live_stages = [sid for sid in model.open_ids if sid in live_ids]
    dead_stages = [sid for sid in model.open_ids if sid not in live_ids]

    # ---- adjacent-pair discrimination --------------------------------------
    min_n = int(config.get("min_cohort_size", 30))
    band = float(config.get("equivalence_band_pp", 5.0))
    alpha = float(config.get("significance_alpha", 0.05))

    pair_rows: List[Dict[str, Any]] = []
    non_discriminating: List[Dict[str, Any]] = []
    for a, b in zip(live_stages, live_stages[1:]):
        sa, sb = stage_stats[a], stage_stats[b]
        n1, x1 = sa["n_resolved"], sa["n_advanced"]
        n2, x2 = sb["n_resolved"], sb["n_advanced"]
        z, p_value = two_proportion_test(x1, n1, x2, n2)
        diff = (
            abs(sa["forward_rate_pct"] - sb["forward_rate_pct"])
            if sa["forward_rate_pct"] is not None and sb["forward_rate_pct"] is not None
            else None
        )
        underpowered = n1 < min_n or n2 < min_n
        flagged = bool(
            not underpowered and p_value is not None and diff is not None
            and p_value > alpha and diff <= band
        )
        if underpowered:
            verdict = f"not tested - needs {min_n} resolved deals per stage, has {n1} and {n2}"
        elif flagged:
            verdict = "indistinguishable - these are one stage"
        else:
            verdict = "discriminates"
        row = {
            "stage_a": sa["stage"], "stage_b": sb["stage"],
            "rate_a_pct": sa["forward_rate_pct"], "n_a": n1,
            "rate_b_pct": sb["forward_rate_pct"], "n_b": n2,
            "difference_pp": _r(diff), "z": z, "p_value": p_value,
            "verdict": verdict, "non_discriminating": flagged,
        }
        pair_rows.append(row)
        if flagged:
            non_discriminating.append(row)

    # ---- regressions --------------------------------------------------------
    regressed = [j for j in journeys if j.regressions]
    regression_pairs: Dict[str, int] = {}
    for journey in regressed:
        for a, b in journey.regressions:
            key = f"{model.label(a)} -> {model.label(b)}"
            regression_pairs[key] = regression_pairs.get(key, 0) + 1
    regression_rows = [
        {"movement": k, "deals": v}
        for k, v in sorted(regression_pairs.items(), key=lambda kv: -kv[1])
    ]
    regression_rate = pct(len(regressed), total)

    skipped_any = [j for j in journeys if j.skipped & live_ids]
    overall_skip_rate = pct(len(skipped_any), total)

    # ---- terminal integrity -------------------------------------------------
    loss_field = fields.get("loss_reason")
    loss_values: Dict[str, int] = {}
    loss_filled = 0
    lost_records = []
    for record in opps:
        current = model.resolve(get(record, "stage"))
        if not model.is_lost(current):
            continue
        lost_records.append(record)
        reason = get(record, "loss_reason")
        if reason in (None, "") or str(reason).strip().lower() in ("null", "none", "n/a", "-"):
            continue
        loss_filled += 1
        key = str(reason).strip()
        loss_values[key] = loss_values.get(key, 0) + 1
    loss_fill_pct = pct(loss_filled, len(lost_records))
    loss_rows = [
        {"reason": k, "deals": v, "share_of_populated_pct": pct(v, loss_filled)}
        for k, v in sorted(loss_values.items(), key=lambda kv: -kv[1])
    ]
    declared_reasons = [str(v) for v in (meta.get("loss_reason_values") or [])]
    unused_reasons = [v for v in declared_reasons if v not in loss_values]
    top_reason = loss_rows[0] if loss_rows else None

    # ---- lead lifecycle -----------------------------------------------------
    lifecycle_rows: List[Dict[str, Any]] = []
    lifecycle_summary: Dict[str, Any] = {}
    lead_records = _as_list(lifecycle, "records")
    if lead_records:
        stage_field = lifecycle.get("stage_field") or "Status"
        ordered = [str(s) for s in (lifecycle.get("ordered_stages") or [])]
        accepted = str(lifecycle.get("accepted_stage") or "")
        rejected = {str(s) for s in (lifecycle.get("rejected_stages") or [])}
        counts: Dict[str, int] = {}
        converted = 0
        # HubSpot contacts have no lead-conversion object at all, so "0 converted"
        # would be a false negative. Only report conversion when the field exists.
        has_conversion_field = False
        for record in normalize_records(lead_records):
            value = str(record.get(stage_field) or "unset")
            counts[value] = counts.get(value, 0) + 1
            if "ConvertedOpportunityId" in record or "IsConverted" in record:
                has_conversion_field = True
            if record.get("ConvertedOpportunityId") or record.get("IsConverted") in (True, "true"):
                converted += 1
        total_leads = len(lead_records)
        accepted_pos = ordered.index(accepted) if accepted in ordered else None
        reached_accepted = 0
        if accepted_pos is not None:
            for value, count in counts.items():
                if value in ordered and ordered.index(value) >= accepted_pos and value not in rejected:
                    reached_accepted += count
        rejected_n = sum(count for value, count in counts.items() if value in rejected)
        lifecycle_rows = [
            {"lifecycle_stage": k, "records": v, "share_pct": pct(v, total_leads)}
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
        lifecycle_summary = {
            "stage_field": stage_field,
            "total_records": total_leads,
            "accepted_stage": accepted,
            "reached_sales_accepted": reached_accepted,
            "sales_acceptance_rate_pct": pct(reached_accepted, total_leads),
            "rejected": rejected_n,
            "rejection_rate_pct": pct(rejected_n, total_leads),
            "died_at_or_before_acceptance": total_leads - reached_accepted,
            "died_at_or_before_acceptance_pct": pct(total_leads - reached_accepted, total_leads),
            "converted_to_opportunity": converted if has_conversion_field else None,
            "conversion_rate_pct": pct(converted, total_leads) if has_conversion_field else None,
        }

    # ---- belief vs reality --------------------------------------------------
    beliefs = config.get("believed_conversion_rates") or {}
    headline_metric = str(beliefs.get("headline_metric") or "overall_win_rate")
    believed_headline = beliefs.get("believed_headline_pct")
    accepted_stage_name = str(config.get("sales_accepted_stage") or "")
    accepted_sid = model.resolve(accepted_stage_name)
    if headline_metric == "win_rate_from_sales_accepted" and accepted_sid in stage_stats:
        measured_headline = stage_stats[accepted_sid]["win_rate_from_pct"]
        headline_label = f"Win rate from {model.label(accepted_sid)}"
        headline_n = stage_stats[accepted_sid]["n_resolved"]
    else:
        measured_headline = measured_win_rate
        headline_label = "Overall win rate"
        headline_n = closed_n

    belief_rows: List[Dict[str, Any]] = []
    if believed_headline is not None and measured_headline is not None:
        belief_rows.append({
            "metric": headline_label,
            "believed_pct": believed_headline,
            "measured_pct": measured_headline,
            "gap_pp": _r(measured_headline - float(believed_headline)),
            "n": headline_n,
        })
    for stage_name, believed in (beliefs.get("believed_by_stage_pct") or {}).items():
        sid = model.resolve(stage_name)
        if sid in stage_stats and stage_stats[sid]["forward_rate_pct"] is not None:
            belief_rows.append({
                "metric": f"{model.label(sid)} -> next stage",
                "believed_pct": believed,
                "measured_pct": stage_stats[sid]["forward_rate_pct"],
                "gap_pp": _r(stage_stats[sid]["forward_rate_pct"] - float(believed)),
                "n": stage_stats[sid]["n_resolved"],
            })
    headline_gap = abs(belief_rows[0]["gap_pp"]) if belief_rows and belief_rows[0]["gap_pp"] is not None else None

    # ---- exit-criteria proposal --------------------------------------------
    written = config.get("stage_definitions") or {}
    criteria_rows: List[Dict[str, Any]] = []
    undefined_stages: List[str] = []
    rep_asserted_stages: List[str] = []
    for sid in model.open_ids:
        label = model.label(sid)
        declared = written.get(label) or written.get(sid) or {}
        definition = str(declared.get("written_definition") or "").strip()
        verifiability = str(declared.get("verifiability") or "").strip().lower()
        if not definition:
            undefined_stages.append(label)
        elif verifiability != "buyer_verifiable":
            rep_asserted_stages.append(label)
        archetype = archetype_for(label)
        for item in EXIT_CRITERIA.get(archetype, EXIT_CRITERIA["generic"]):
            criteria_rows.append({
                "stage": label,
                "today_the_definition_is": definition or "(no written definition)",
                "proposed_exit_criterion_buyer_verifiable": item["buyer_verifiable"],
                "proof_artifact": item["artifact"],
                "replaces_this_rep_asserted_version": item["rep_asserted"],
            })

    # ---- other pipelines ----------------------------------------------------
    other_pipeline_rows: List[Dict[str, Any]] = []
    for pipeline_id, rows in sorted(scoped.items(), key=lambda kv: -len(kv[1])):
        if pipeline_id == primary:
            continue
        other = StageModel(meta, pipeline_id)
        won_n = sum(1 for r in rows if other.is_won(other.resolve(get(r, "stage"))))
        lost_n = sum(1 for r in rows if other.is_lost(other.resolve(get(r, "stage"))))
        other_pipeline_rows.append({
            "pipeline": other.pipeline_label,
            "deals": len(rows),
            "share_of_deals_pct": pct(len(rows), sum(len(v) for v in scoped.values())),
            "stages": len(other.open_ids),
            "win_rate_pct": pct(won_n, won_n + lost_n),
            "closed_deals": won_n + lost_n,
        })

    # ======================================================================
    # Findings
    # ======================================================================
    org_name = profile.get("org_name") or config.get("org_name") or "Your organization"
    doc = FindingsDoc(plugin=PLUGIN, window=window, org_name=org_name)

    working = [
        sid for sid in live_stages
        if not any(r["non_discriminating"] and r["stage_b"] == model.label(sid) for r in pair_rows)
        and (stage_stats[sid]["skip_rate_pct"] or 0) < float(config.get("skip_rate_flag_pct", 25.0))
        and not (
            stage_stats[sid]["dwell_median_days"] is not None
            and stage_stats[sid]["dwell_median_days"] * 24.0 < float(config.get("zero_dwell_hours", 24))
        )
    ]

    doc.add_score(Score(
        key="measured_win_rate", label="Measured win rate",
        value=measured_win_rate, unit="percent", direction_good="up",
        context=f"{len(won):,} won of {closed_n:,} closed deals in window",
    ))
    doc.add_score(Score(
        key="belief_gap_pp", label="Belief vs reality gap",
        value=headline_gap if headline_gap is not None else "not captured",
        unit="percent" if headline_gap is not None else "",
        direction_good="down",
        context=(
            f"team believes {believed_headline}%, measured {measured_headline}% "
            f"({headline_label.lower()}, n={headline_n:,})"
            if headline_gap is not None else
            "no believed rate captured at setup - re-run :setup and answer the belief question"
        ),
    ))
    doc.add_score(Score(
        key="non_discriminating_pairs", label="Stages that are really one stage",
        value=len(non_discriminating), unit="count", direction_good="down",
        context=(
            "adjacent pairs whose forward conversion is statistically indistinguishable"
            if has_history else "not testable without stage history"
        ),
    ))
    doc.add_score(Score(
        key="stage_skip_rate", label="Deals skipping a stage",
        value=overall_skip_rate if has_history else "unknown",
        unit="percent" if has_history else "", direction_good="down",
        context=(
            f"{len(skipped_any):,} of {total:,} deals jumped at least one stage"
            if has_history else "requires stage history"
        ),
    ))
    doc.add_score(Score(
        key="working_stages", label="Stages earning their place",
        value=len(working) if has_history else "unknown",
        unit="count" if has_history else "", direction_good="up",
        context=(
            f"of {len(model.open_ids)} open stages configured on {model.pipeline_label}"
            if has_history else
            f"{len(model.open_ids)} stages configured, but without history none can be judged"
        ),
    ))

    if not has_history:
        doc.unavailable.append("Stage-transition history (cohort conversion, dwell time, skips, regressions)")
        doc.add(Finding(
            id="stage-history-unavailable", severity="critical",
            title="No stage history: every conversion rate below is a guess, not a measurement",
            what=(
                f"Not one stage-transition row came back for {total:,} opportunities, so this run "
                "could only read the stage each deal is sitting in today. Conversion rate, "
                "time-in-stage, stage skips and backwards movement are all uncomputable from a snapshot."
            ),
            why_it_matters=(
                "A funnel built from current stage counts deals that died in Discovery as though "
                "they converted out of it, because they are now sitting in Closed Lost. Rates built "
                "that way are inflated by construction, and they are the rates in your CRM's stock "
                "funnel report."
            ),
            recommended_fix=(
                "Salesforce: query OpportunityHistory (always populated, no setup needed) rather than "
                "OpportunityFieldHistory, and confirm the integration user can read it. HubSpot: read "
                "deals with propertiesWithHistory=dealstage, or pull the hs_date_entered_<stageId> "
                "properties. Then re-run. Until then treat every rate in this report as directional."
            ),
            evidence={
                "count": total,
                "query": src("stage_history")["query"] or "no stage-history query was recorded for this run",
            },
            effort="quick", owner_hint="RevOps",
        ))
    else:
        inflated = [
            r for r in stage_rows
            if r["has_traffic"] and r["naive_inflation_pp"] is not None and r["naive_inflation_pp"] > 5
        ]
        if inflated:
            worst = max(inflated, key=lambda r: r["naive_inflation_pp"])
            doc.add(Finding(
                id="survivorship-bias-in-reported-rates", severity="high",
                title=(
                    f"Your stock funnel report overstates {worst['stage']} conversion by "
                    f"{worst['naive_inflation_pp']} points"
                ),
                what=(
                    f"Measured cohort-controlled conversion out of {worst['stage']} is "
                    f"{worst['forward_rate_pct']}% (n={worst['n_resolved']} resolved deals that entered it). "
                    f"The same number computed the way a CRM funnel report computes it - from the stage "
                    f"each deal is sitting in right now - is {worst['naive_rate_pct']}% "
                    f"(n={worst['naive_n']}). {len(inflated)} stages are overstated by more than 5 points."
                ),
                why_it_matters=(
                    "A deal that died in this stage is now sitting in Closed Lost, so it has left this "
                    "stage's denominator entirely. The snapshot number therefore answers 'of the deals "
                    "still alive at or past this stage, how many are past it' - which trends towards 100% "
                    "by construction and is not a conversion rate. Capacity plans and pipeline coverage "
                    "targets built on it ask for too little pipeline."
                ),
                recommended_fix=(
                    "Rebuild funnel reporting on stage-entry cohorts from history, not on current stage. "
                    "The denominator is 'deals that ENTERED this stage and have since resolved'; the "
                    "numerator is 'deals that ever reached a later stage'. Deals still sitting in the "
                    "stage are censored and belong in neither."
                ),
                evidence={
                    "count": len(inflated),
                    "rows": [
                        {
                            "Stage": r["stage"],
                            "Measured (cohort) %": r["forward_rate_pct"],
                            "n (resolved)": r["n_resolved"],
                            "Snapshot (wrong) %": r["naive_rate_pct"],
                            "n (snapshot)": r["naive_n"],
                            "Overstated by (pp)": r["naive_inflation_pp"],
                        }
                        for r in sorted(inflated, key=lambda r: -r["naive_inflation_pp"])
                    ],
                },
                effort="medium", owner_hint="RevOps",
            ))

        if non_discriminating:
            first = non_discriminating[0]
            doc.add(Finding(
                id="non-discriminating-stage-pair", severity="high",
                title=(
                    f"{len(non_discriminating)} adjacent stage pair"
                    f"{'s' if len(non_discriminating) != 1 else ''} cannot be told apart - merge them"
                ),
                what=(
                    f"{first['stage_a']} converts forward at {first['rate_a_pct']}% (n={first['n_a']}) and "
                    f"{first['stage_b']} at {first['rate_b_pct']}% (n={first['n_b']}) - a difference of "
                    f"{first['difference_pp']} points, p={first['p_value']}. On this much data that is not "
                    "a real difference. Two stages that predict the same outcome are one stage wearing two hats."
                ),
                why_it_matters=(
                    "A stage exists to change your estimate of whether the deal will close. If moving from "
                    "one to the next does not change that estimate, the stage is costing rep time, "
                    "distorting weighted-pipeline maths with a probability that is not real, and adding a "
                    "forecast category that carries no information."
                ),
                recommended_fix=(
                    f"Merge {first['stage_a']} and {first['stage_b']} into one stage, and give the survivor a "
                    "single buyer-verifiable exit criterion (see the proposed criteria in this report). If you "
                    "keep both, the honest justification is operational, not predictive - say so, and stop "
                    "reporting separate conversion rates for them."
                ),
                evidence={
                    "count": len(non_discriminating),
                    "rows": [
                        {
                            "Stage A": r["stage_a"], "Rate A %": r["rate_a_pct"], "n A": r["n_a"],
                            "Stage B": r["stage_b"], "Rate B %": r["rate_b_pct"], "n B": r["n_b"],
                            "Diff (pp)": r["difference_pp"], "p": r["p_value"], "Verdict": r["verdict"],
                        }
                        for r in non_discriminating
                    ],
                },
                effort="project", owner_hint="Sales leadership + RevOps",
            ))

        skip_flag = float(config.get("skip_rate_flag_pct", 25.0))
        skipped_stages = [
            r for r in stage_rows
            if r["has_traffic"] and r["skip_rate_pct"] is not None
            and r["skip_rate_pct"] >= skip_flag and r["n_got_past"] >= min_n
        ]
        if skipped_stages:
            worst = max(skipped_stages, key=lambda r: r["skip_rate_pct"])
            doc.add(Finding(
                id="stage-skipped-by-majority", severity="high" if worst["skip_rate_pct"] >= 50 else "medium",
                title=f"{worst['stage']} is skipped by {worst['skip_rate_pct']}% of the deals that pass it",
                what=(
                    f"Of {worst['n_got_past']} deals that reached a stage beyond {worst['stage']}, "
                    f"{worst['n_skipped_stage']} never entered it at all. "
                    f"{len(skipped_stages)} stage(s) are skipped above the {skip_flag}% threshold."
                ),
                why_it_matters=(
                    "A stage most deals jump is not a stage, it is an optional field. It breaks stage-based "
                    "forecasting (the cohort is self-selected), it makes time-in-stage benchmarks meaningless, "
                    "and it quietly tells you the process on the wiki is not the process being run."
                ),
                recommended_fix=(
                    "Decide which it is. If the work genuinely happens but is not recorded, make the stage "
                    "mandatory with a buyer-verifiable exit criterion and enforce sequence. If the work does "
                    "not happen for this motion, delete the stage - or split the pipeline, because you have "
                    "two motions sharing one ladder."
                ),
                evidence={
                    "count": len(skipped_stages),
                    "rows": [
                        {
                            "Stage": r["stage"], "Skipped by %": r["skip_rate_pct"],
                            "Deals that got past it": r["n_got_past"],
                            "Of those, never entered it": r["n_skipped_stage"],
                            "Entered it": r["n_entered"],
                        }
                        for r in sorted(skipped_stages, key=lambda r: -r["skip_rate_pct"])
                    ],
                },
                effort="medium", owner_hint="Sales leadership",
            ))

        if regression_rate >= float(config.get("regression_flag_pct", 8.0)) and regressed:
            doc.add(Finding(
                id="backwards-stage-movement", severity="medium",
                title=f"{regression_rate}% of deals moved backwards at least once",
                what=(
                    f"{len(regressed)} of {total} deals regressed to an earlier stage. The most common "
                    f"movement is {regression_rows[0]['movement']} ({regression_rows[0]['deals']} deals)."
                ),
                why_it_matters=(
                    "Backwards movement means a stage was entered before its criteria were met - the deal was "
                    "advanced on optimism and later corrected. It is the cleanest available signal that exit "
                    "criteria are rep-asserted rather than buyer-verifiable, and it silently corrupts "
                    "time-in-stage and conversion maths for the stages involved."
                ),
                recommended_fix=(
                    "Look at the top movement pair first - that is the stage boundary reps cannot judge. "
                    "Rewrite its exit criterion so a buyer-produced artifact is required to enter, then track "
                    "regression rate as the measure of whether the rewrite worked."
                ),
                evidence={"count": len(regressed), "rows": regression_rows[:20]},
                effort="medium", owner_hint="Sales leadership",
            ))

        zero_hours = float(config.get("zero_dwell_hours", 24))
        zero_dwell = [
            r for r in stage_rows
            if r["dwell_median_days"] is not None and r["dwell_n"] >= min_n
            and r["dwell_median_days"] * 24.0 < zero_hours
        ]
        if zero_dwell:
            worst = min(zero_dwell, key=lambda r: r["dwell_median_days"])
            doc.add(Finding(
                id="zero-dwell-stages", severity="medium",
                title=f"{worst['stage']} has a median dwell of {worst['dwell_median_days']} days - nothing happens there",
                what=(
                    f"Median time in {worst['stage']} is {worst['dwell_median_days']} days across "
                    f"{worst['dwell_n']} completed dwells. {len(zero_dwell)} stage(s) have a median under "
                    f"{zero_hours} hours."
                ),
                why_it_matters=(
                    "A stage deals pass through in hours is a checkbox, not a phase of the sale. Reps are "
                    "dragging deals through it to satisfy the process, usually at the moment they update the "
                    "record for something else. It inflates the stage count, and any probability attached to "
                    "it is fiction."
                ),
                recommended_fix=(
                    "Merge it into the neighbouring stage where the work actually happens, or convert it into "
                    "a field or a checklist item on that stage. If it must survive as a stage, it needs work "
                    "that takes real time and a buyer artifact that proves the work happened."
                ),
                evidence={
                    "count": len(zero_dwell),
                    "rows": [
                        {
                            "Stage": r["stage"], "Median days": r["dwell_median_days"],
                            "Mean days": r["dwell_mean_days"], "p90 days": r["dwell_p90_days"],
                            "Completed dwells (n)": r["dwell_n"],
                        }
                        for r in zero_dwell
                    ],
                },
                effort="quick", owner_hint="RevOps",
            ))

        multiple = float(config.get("zombie_p90_multiple", 6.0))
        zombies = [
            r for r in stage_rows
            if r["dwell_median_days"] and r["dwell_p90_days"] and r["dwell_n"] >= min_n
            and r["dwell_p90_days"] >= r["dwell_median_days"] * multiple
        ]
        if zombies:
            worst = max(zombies, key=lambda r: (r["dwell_p90_days"] or 0) / max(r["dwell_median_days"] or 1, 0.01))
            doc.add(Finding(
                id="zombie-dwell-tail", severity="medium",
                title=f"{worst['stage']} hides zombie deals: median {worst['dwell_median_days']}d, p90 {worst['dwell_p90_days']}d",
                what=(
                    f"In {worst['stage']} the median deal takes {worst['dwell_median_days']} days but the 90th "
                    f"percentile takes {worst['dwell_p90_days']} days - and the mean, "
                    f"{worst['dwell_mean_days']} days, sits well above the median because of that tail "
                    f"(n={worst['dwell_n']})."
                ),
                why_it_matters=(
                    "Whoever quotes 'average days in stage' is quoting a number that no deal experiences. The "
                    "tail is dead deals parked in an active stage, and they are counted in pipeline coverage as "
                    "though they were live."
                ),
                recommended_fix=(
                    "Report median and p90, never the mean. Then set an age threshold per stage at roughly p75 "
                    "and force a disposition - advance, close-lost, or re-date with a buyer-supplied reason."
                ),
                evidence={
                    "count": len(zombies),
                    "rows": [
                        {
                            "Stage": r["stage"], "Median days": r["dwell_median_days"],
                            "p75 days": r["dwell_p75_days"], "p90 days": r["dwell_p90_days"],
                            "Mean days": r["dwell_mean_days"], "n": r["dwell_n"],
                        }
                        for r in zombies
                    ],
                },
                effort="quick", owner_hint="Sales leadership",
            ))

        if history_coverage_pct < float(config.get("history_coverage_floor_pct", 80.0)):
            doc.add(Finding(
                id="stage-history-incomplete", severity="high",
                title=f"Only {history_coverage_pct}% of deals have any stage history",
                what=(
                    f"{total - history_covered} of {total} in-window opportunities have no transition rows. "
                    "Those deals were treated as single-point timelines, which understates entries into every "
                    "early stage."
                ),
                why_it_matters=(
                    "Salesforce field history is retained 18 months (24 with Field Audit Trail), and deals "
                    "loaded during a CRM migration have history that starts at the load date. Either way the "
                    "missing rows are not random - they are the oldest deals, which are disproportionately the "
                    "long, lost ones. Conversion rates computed over a partially covered set skew optimistic."
                ),
                recommended_fix=(
                    "Shorten the analysis window to the period with full history coverage, or switch the "
                    "history source (Salesforce: OpportunityHistory rather than OpportunityFieldHistory). "
                    "Set history_lookback_days in ~/.leanscale-gtm/stage-architect.json to match."
                ),
                evidence={"count": total - history_covered, "value": history_coverage_pct},
                effort="quick", owner_hint="RevOps",
            ))

    # Without transition history, "entered" collapses to "is sitting here right now",
    # so an empty stage is not evidence of a dead stage - it is evidence of nothing.
    # Suppress rather than guess.
    if dead_stages and not has_history:
        doc.unavailable.append("Dead-stage detection (needs stage history to know what was entered)")
    if dead_stages and has_history:
        doc.add(Finding(
            id="stages-with-no-traffic", severity="medium",
            title=f"{len(dead_stages)} stage(s) had no deal enter them in this window",
            what=(
                "These stages are on the picklist and in the enablement doc, but no opportunity entered them "
                f"in the {window.get('start', '?')} to {window.get('end', '?')} window: "
                + ", ".join(model.label(s) for s in dead_stages) + "."
            ),
            why_it_matters=(
                "Dead stages are not harmless. They appear in every stage dropdown, every funnel chart and "
                "every dashboard filter, they make the process look longer and more rigorous than it is, and "
                "new reps read them as real steps. A stage that a deal currently sits in but nobody has "
                "entered recently is worse - those deals entered before the window and have not moved since."
            ),
            recommended_fix=(
                "Deactivate them on the picklist (do not delete - deactivation preserves history and reporting). "
                "Move any deals currently parked there to a live stage first, and check whether they should be "
                "closed-lost instead."
            ),
            evidence={
                "count": len(dead_stages),
                "rows": [
                    {
                        "Stage": model.label(s),
                        "Deals entered in window": 0,
                        "Currently parked here (all time)": (model.by_id.get(s) or {}).get("record_count_open", "unknown"),
                        "Position on ladder": model.pos(s),
                    }
                    for s in dead_stages
                ],
            },
            effort="quick", owner_hint="RevOps",
        ))

    if belief_rows and headline_gap is not None:
        first = belief_rows[0]
        direction = "optimistic" if first["gap_pp"] < 0 else "pessimistic"
        # A double-digit error in the headline conversion assumption makes the number
        # the executive team is planning against wrong. That is the spec's definition
        # of critical, not a matter of taste.
        severity = "critical" if headline_gap >= 12 else ("high" if headline_gap >= 6 else "medium")
        wrong_stage_beliefs = [r for r in belief_rows[1:] if r["gap_pp"] is not None and abs(r["gap_pp"]) >= 10]
        doc.add(Finding(
            id="belief-vs-reality-gap", severity=severity,
            title=(
                f"The team believes {first['metric'].lower()} is {first['believed_pct']}%. It is "
                f"{first['measured_pct']}%."
            ),
            what=(
                f"Captured at setup, before any measurement was shown: the team's stated belief was "
                f"{first['believed_pct']}%. Measured over {first['n']:,} resolved deals it is "
                f"{first['measured_pct']}% - a {headline_gap} point gap, and the team is {direction}."
                + (
                    f" {len(wrong_stage_beliefs)} individual stage belief"
                    f"{'s are' if len(wrong_stage_beliefs) != 1 else ' is'} also off by 10 points or more."
                    if wrong_stage_beliefs else ""
                )
            ),
            why_it_matters=(
                "Every capacity number downstream is built on the believed rate: pipeline coverage targets, "
                "quota-to-pipeline ratios, headcount plans, the number of meetings an SDR is asked to book. "
                f"A {headline_gap} point error in the conversion assumption compounds through all of them, and "
                "nobody finds out until a quarter misses."
            ),
            recommended_fix=(
                "Restate the planning model with the measured rates and the n behind each one, then re-derive "
                "pipeline coverage. Do this before touching stage definitions - the coverage error is costing "
                "money this quarter; the stage redesign pays back next year."
            ),
            # No `count` here on purpose: the shared renderer prints count as
            # "N records affected", and "4 records affected" for a belief gap is
            # nonsense. The gap itself is tracked as the belief_gap_pp score.
            evidence={
                "value": f"{headline_gap} percentage points",
                "rows": [
                    {
                        "Metric": r["metric"], "Team believes %": r["believed_pct"],
                        "Measured %": r["measured_pct"], "Gap (pp)": r["gap_pp"], "n": r["n"],
                    }
                    for r in belief_rows
                ],
            },
            effort="quick", owner_hint="Sales leadership + Finance",
        ))
    elif not beliefs:
        doc.add(Finding(
            id="belief-not-captured", severity="low",
            title="No believed conversion rates were captured, so there is no gap to show",
            what=(
                "believed_conversion_rates is empty in ~/.leanscale-gtm/stage-architect.json, so this run can "
                "only report the measured reality with nothing to contrast it against."
            ),
            why_it_matters=(
                "The measured number on its own rarely changes a decision. The gap between it and what the "
                "leadership team has been assuming is what changes the plan, and it can only be captured "
                "honestly before the measurement is shown."
            ),
            recommended_fix=(
                "Re-run /stage-architect:setup and answer the belief question before reading this report. "
                "Ask the CRO and the finance partner separately - the spread between their two answers is "
                "itself a finding."
            ),
            evidence={"count": 0, "value": "not captured"},
            effort="quick", owner_hint="RevOps",
        ))

    if lost_records and loss_fill_pct < float(config.get("loss_reason_fill_floor_pct", 80.0)):
        doc.add(Finding(
            id="closed-lost-reason-incomplete", severity="high",
            title=f"Closed-lost reason is populated on only {loss_fill_pct}% of losses",
            what=(
                f"{len(lost_records) - loss_filled} of {len(lost_records)} closed-lost deals have no reason "
                f"recorded in {loss_field}."
            ),
            why_it_matters=(
                "Loss analysis built on a partially populated field is a survey of the reps who bothered to "
                "answer, and the reps who skip it are not a random sample - they skip it on the losses that "
                "were most embarrassing. Win/loss conclusions drawn from this are systematically wrong in the "
                "direction that flatters the process."
            ),
            recommended_fix=(
                "Make the reason required by validation rule on transition INTO closed-lost, not by asking "
                "nicely. Pair it with a free-text detail field - a required picklist alone produces the "
                "dominant-value problem below."
            ),
            evidence={"count": len(lost_records) - loss_filled, "value": loss_fill_pct},
            effort="quick", owner_hint="RevOps",
        ))

    if top_reason and top_reason["share_of_populated_pct"] >= float(config.get("loss_reason_dominance_pct", 60.0)):
        doc.add(Finding(
            id="closed-lost-reason-not-discriminating", severity="medium",
            title=f"'{top_reason['reason']}' accounts for {top_reason['share_of_populated_pct']}% of recorded losses",
            what=(
                f"One value covers {top_reason['deals']} of {loss_filled} populated loss reasons."
                + (f" {len(unused_reasons)} picklist value(s) were never used at all: "
                   + ", ".join(unused_reasons) + "." if unused_reasons else "")
            ),
            why_it_matters=(
                "A picklist where one value swallows the majority is not classifying anything - it is the "
                "path of least resistance in a required field. You cannot act on it: 'no decision' names the "
                "outcome, not the cause, and it hides the stage where the deal actually died."
            ),
            recommended_fix=(
                "Replace the dominant catch-all with causes a rep can distinguish in five seconds, and capture "
                "the stage the deal died in alongside the reason - that pairing is what makes loss data "
                "actionable. Retire the values nobody has used."
            ),
            evidence={"count": top_reason["deals"], "rows": [
                {"Reason": r["reason"], "Deals": r["deals"], "Share of populated %": r["share_of_populated_pct"]}
                for r in loss_rows
            ]},
            effort="medium", owner_hint="RevOps",
        ))

    if undefined_stages or rep_asserted_stages:
        intent = str(config.get("stages_are_meant_to_be", "buyer_verifiable"))
        count = len(undefined_stages) + len(rep_asserted_stages)
        doc.add(Finding(
            id="exit-criteria-not-buyer-verifiable",
            severity="high" if intent == "buyer_verifiable" else "medium",
            title=f"{count} of {len(model.open_ids)} stages cannot be exited on evidence the buyer produced",
            what=(
                (f"{len(undefined_stages)} stage(s) have no written definition at all: "
                 + ", ".join(undefined_stages) + ". " if undefined_stages else "")
                + (f"{len(rep_asserted_stages)} have a definition that only a rep can assert: "
                   + ", ".join(rep_asserted_stages) + "." if rep_asserted_stages else "")
                + f" The team's stated intent is that stages are {intent.replace('_', '-')}."
            ),
            why_it_matters=(
                "'Rep believes there is budget' is unfalsifiable, so it is always true when the rep needs it "
                "to be. Every downstream number - stage conversion, forecast category, weighted pipeline - "
                "inherits that. Buyer-verifiable criteria are the only kind that survive a rep having a bad "
                "month, and they are why two reps' Stage 3 can mean the same thing."
            ),
            recommended_fix=(
                "Adopt the proposed exit criteria in this report. Each one names a buyer-produced artifact - a "
                "reply, an accepted invite, a returned redline - so the stage is enterable only when something "
                "outside your own CRM changed. Start with the stage that showed the most backwards movement."
            ),
            evidence={
                "count": count,
                "rows": [
                    {
                        "Stage": r["stage"],
                        "Definition today": r["today_the_definition_is"],
                        "Proposed buyer-verifiable criterion": r["proposed_exit_criterion_buyer_verifiable"],
                        "Proof artifact": r["proof_artifact"],
                    }
                    for r in criteria_rows
                    if r["stage"] in set(undefined_stages) | set(rep_asserted_stages)
                ],
            },
            effort="project", owner_hint="Sales leadership + Enablement",
        ))

    if lifecycle_summary and lifecycle_summary["died_at_or_before_acceptance_pct"] >= 50:
        doc.add(Finding(
            id="lifecycle-dies-before-sales-accepted", severity="high",
            title=(
                f"{lifecycle_summary['died_at_or_before_acceptance_pct']}% of the lead funnel never reaches "
                f"{lifecycle_summary['accepted_stage'] or 'sales acceptance'}"
            ),
            what=(
                f"Of {lifecycle_summary['total_records']:,} records in the lead lifecycle, "
                f"{lifecycle_summary['reached_sales_accepted']:,} reached "
                f"{lifecycle_summary['accepted_stage'] or 'the accepted stage'} "
                f"({lifecycle_summary['sales_acceptance_rate_pct']}%), and "
                f"{lifecycle_summary['rejection_rate_pct']}% were explicitly rejected or recycled."
            ),
            why_it_matters=(
                "The lead lifecycle and the opportunity ladder are usually designed by different people and "
                "meet at a handoff nobody owns. When most of the funnel dies at or before acceptance, the "
                "opportunity-stage conversion rates everyone quotes describe a heavily pre-filtered "
                "population - and the real bottleneck is upstream of the stage everyone is arguing about."
            ),
            recommended_fix=(
                "Define sales-accepted as a two-sided event with an SLA: marketing asserts, sales accepts or "
                "rejects with a reason inside a fixed window, and silence counts as acceptance. Then report "
                "acceptance rate by source - it is the fastest way to find the channel manufacturing volume "
                "that sales will not touch."
            ),
            evidence={
                "count": lifecycle_summary["died_at_or_before_acceptance"],
                "rows": [
                    {"Lifecycle stage": r["lifecycle_stage"], "Records": r["records"], "Share %": r["share_pct"]}
                    for r in lifecycle_rows
                ],
            },
            effort="project", owner_hint="Marketing ops + RevOps",
        ))

    if other_pipeline_rows:
        biggest = max(other_pipeline_rows, key=lambda r: r["share_of_deals_pct"])
        if biggest["share_of_deals_pct"] >= 20:
            doc.add(Finding(
                id="secondary-pipeline-unanalysed", severity="low",
                title=f"{biggest['pipeline']} holds {biggest['share_of_deals_pct']}% of deals and was not analysed in depth",
                what=(
                    f"The full stage analysis ran on {model.pipeline_label}, the pipeline with the most deals. "
                    f"{len(other_pipeline_rows)} other in-scope pipeline(s) have their own stage ladders and "
                    "only received headline numbers."
                ),
                why_it_matters=(
                    "Pipelines with separate ladders are separate sales processes wearing one report. Blending "
                    "them produces a conversion rate that describes no motion that actually exists."
                ),
                recommended_fix=(
                    "Re-run with pipelines_in_scope set to the other pipeline to get its full analysis, and "
                    "keep the two sets of rates apart in planning."
                ),
                evidence={
                    "count": len(other_pipeline_rows),
                    "rows": [
                        {
                            "Pipeline": r["pipeline"], "Deals": r["deals"],
                            "Share of deals %": r["share_of_deals_pct"], "Open stages": r["stages"],
                            "Win rate %": r["win_rate_pct"], "Closed deals": r["closed_deals"],
                        }
                        for r in other_pipeline_rows
                    ],
                },
                effort="quick", owner_hint="RevOps",
            ))

    if unmapped_stages:
        doc.add(Finding(
            id="stage-values-not-on-picklist", severity="medium",
            title=f"{sum(unmapped_stages.values())} deals sit in stage values that are not on the picklist",
            what=(
                "These stage values appear on records but not in the pipeline definition: "
                + ", ".join(f"{k} ({v})" for k, v in sorted(unmapped_stages.items(), key=lambda kv: -kv[1])[:10])
                + ". They were excluded from the analysis."
            ),
            why_it_matters=(
                "Orphaned picklist values come from deactivated stages, a migration, or an integration writing "
                "raw strings. Deals holding them are invisible to every stage-filtered report and dashboard - "
                "including the forecast."
            ),
            recommended_fix=(
                "Map each orphan to a live stage and mass-update, then block free-text writes to the stage "
                "field from integrations."
            ),
            evidence={"count": sum(unmapped_stages.values()), "rows": [
                {"Unmapped stage value": k, "Deals": v} for k, v in sorted(unmapped_stages.items(), key=lambda kv: -kv[1])
            ]},
            effort="medium", owner_hint="RevOps",
        ))

    # ---- structured detail for the report ----------------------------------
    doc.sections = {
        "method": {
            "cohort_rule": (
                "Conversion is cohort-controlled by the stage a deal ENTERED, read from stage-transition "
                "history. Denominator = deals that entered the stage AND have since resolved (advanced past "
                "it, or closed lost). Numerator = deals that ever reached a later ladder position. Deals still "
                "sitting at or below the stage are censored and appear in neither - they are reported "
                "separately as 'still open here'."
            ),
            "naive_rule": (
                "The snapshot rate is the same calculation done the way a stock CRM funnel report does it: "
                "from the stage each deal is sitting in today. Lost deals now sit in Closed Lost, which is off "
                "the ladder, so they leave every earlier stage's denominator. That is survivorship bias and it "
                "inflates every rate. It is shown only so the gap is visible."
            ),
            "significance_rule": (
                f"Two adjacent stages are called non-discriminating only when a pooled two-proportion z-test "
                f"fails to reject at alpha={alpha} AND the rates are within {band} percentage points AND both "
                f"stages have at least {min_n} resolved deals. The n gate matters most: on small cohorts "
                f"everything looks identical, which is ignorance, not equivalence."
            ),
            "dwell_rule": (
                "Time-in-stage is measured per occurrence, so a deal that re-enters a stage contributes two "
                "dwells. Median, p75 and p90 are reported; the mean is shown only to demonstrate how far the "
                "tail drags it."
            ),
            "as_of": as_of.strftime("%Y-%m-%d"),
            "primary_pipeline": model.pipeline_label,
            "history_source": meta.get("history_source", "unknown"),
            "history_coverage_pct": history_coverage_pct,
            "min_cohort_size": min_n,
            "equivalence_band_pp": band,
            "significance_alpha": alpha,
            "deals_below_material_floor_excluded": below_floor,
            "material_deal_floor": floor,
        },
        "totals": {
            "opportunities_analysed": total,
            "won": len(won),
            "lost": len(lost),
            "open": len(open_deals),
            "measured_win_rate_pct": measured_win_rate,
            "open_stages": len(model.open_ids),
            "live_stages": len(live_stages),
            "dead_stages": len(dead_stages),
        },
        "stage_table": stage_rows,
        "pair_table": pair_rows,
        "belief_table": belief_rows,
        "exit_criteria": criteria_rows,
        "regression_table": regression_rows,
        "loss_reason_table": loss_rows,
        "loss_reason_summary": {
            "field": loss_field,
            "lost_deals": len(lost_records),
            "populated": loss_filled,
            "fill_rate_pct": loss_fill_pct,
            "declared_picklist_values": declared_reasons,
            "never_used_values": unused_reasons,
        },
        "lifecycle_table": lifecycle_rows,
        "lifecycle_summary": lifecycle_summary,
        "other_pipelines": other_pipeline_rows,
    }
    return doc


# ------------------------------------------------------------------------ main


def _newest_run_dir() -> Optional[Path]:
    base = Path.cwd() / "gtm-agents" / PLUGIN
    if not base.exists():
        return None
    runs = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
    return runs[-1] if runs else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="stage-architect: raw/*.json -> findings.json")
    parser.add_argument("--run-dir", help="Run directory containing raw/. Defaults to the newest run.")
    parser.add_argument("--raw", help="Read raw/*.json from here instead of <run-dir>/raw.")
    parser.add_argument(
        "--out",
        help="Output directory for findings.json (suite contract). A path ending in .json is "
             "also accepted and used as the file name directly.",
    )
    parser.add_argument("--config", help="Use this config file instead of ~/.leanscale-gtm/stage-architect.json.")
    parser.add_argument("--as-of", help="Treat this date as today (YYYY-MM-DD). Makes runs reproducible.")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).resolve() if args.run_dir else _newest_run_dir()
    raw_dir = Path(args.raw).resolve() if args.raw else (run_dir / "raw" if run_dir else None)
    if raw_dir is None or not raw_dir.exists():
        parser.error(
            "No raw/ directory found. Pass --raw, or --run-dir pointing at a run that has one. "
            "The :run skill creates it."
        )
    # --out is a DIRECTORY under the suite-wide CLI contract; a .json path is
    # accepted too so the flag is hard to get wrong.
    out_path: Optional[Path] = None
    if args.out:
        candidate = Path(args.out).resolve()
        out_path = candidate if candidate.suffix.lower() == ".json" else candidate / "findings.json"
    if run_dir is None:
        run_dir = out_path.parent if out_path else raw_dir.parent
    if out_path is None:
        out_path = run_dir / "findings.json"

    as_of = parse_dt(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if as_of is None:
        parser.error("--as-of must be YYYY-MM-DD")

    config = _layer_config(Path(args.config).resolve() if args.config else None)

    try:
        doc = analyse(raw_dir, config, as_of, run_dir)
    except SourceEmptyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = doc.write(out_path.parent)
    if written != out_path:
        written.replace(out_path)

    totals = doc.sections["totals"]
    print(f"stage-architect: analysed {totals['opportunities_analysed']:,} opportunities "
          f"on '{doc.sections['method']['primary_pipeline']}' "
          f"({totals['live_stages']} live stages, {totals['dead_stages']} with no traffic)")
    print(f"  measured win rate {totals['measured_win_rate_pct']}% · "
          f"{len(doc.findings)} findings · findings.json -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
