#!/usr/bin/env python3
"""
analyze.py — raw/*.json -> findings.json

The division of labour matters here and is not negotiable:

  Claude  reads the transcripts and judges each framework dimension, writing
          raw/scored_calls.json with a verbatim quote and a timestamp for
          every met or partial. Qualification judgment is not a regex.

  Python  (this file) parses, verifies, counts and compares. It checks that
          every quote really appears in the transcript, computes the mechanics,
          rolls the scores up per rep and per team, correlates gaps against deal
          outcomes, and produces findings. It never decides whether a rep
          identified an economic buyer.

Reads
    raw/calls.json               call index written by the :run skill
    raw/transcripts/*            the transcripts themselves, any supported shape
    raw/normalized_calls.json    optional; built here if absent
    raw/scored_calls.json        Claude's framework judgment
    raw/deals.json               optional CRM pull for deal linkage
    raw/_fetch_log.json          optional provenance breadcrumb

Writes
    manifest.json                provenance; aborts if a required source is empty
    findings.json                the shared findings envelope (SPEC §6)

Python 3.9+, standard library only. No network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    ConfigError,
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    SourceEmptyError,
    apply_deltas,
    load_plugin_config,
    load_profile,
    median,
    normalize_records,
    pct,
    save_baseline,
)

import transcripts as tx  # noqa: E402

PLUGIN = "sales-coach"

STATUS_POINTS = {"met": 2, "partial": 1, "missing": 0}
SCORABLE = ("met", "partial", "missing")
VALID_STATUS = SCORABLE + ("not_applicable", "unscored")

# Quote drift tolerance. Exports merge adjacent sentences into one turn, so a
# cited timestamp can sit a minute inside the turn that contains it and still be
# honest. Past this, the citation is treated as unreliable and reported.
TS_DRIFT_TOLERANCE_SEC = 180

_INTERROGATIVE = re.compile(
    r"^(who|what|whats|when|where|why|how|hows|which|can|could|would|will|do|does|did|is|are|was|were|"
    r"have|has|had|should|any|tell me|walk me|talk me|help me understand|say more|give me a sense)\b",
    re.I,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+")


# --------------------------------------------------------------- frameworks

def _dim(key: str, label: str, tier: str, evidence_rule: str, met_means: str) -> Dict[str, str]:
    return {"key": key, "label": label, "tier": tier,
            "evidence_rule": evidence_rule, "met_means": met_means}


# Fallback definitions, used when config names a framework but carries no
# dimensions. Setup normally writes the full list into config so the customer
# can edit the evidence rules — these are the shipping defaults for each.
FRAMEWORKS: Dict[str, Dict[str, Any]] = {
    "MEDDPICC": {"label": "MEDDPICC", "dimensions": [
        _dim("metrics", "Metrics", "fundamental",
             "A quantified business impact in the buyer's own words.",
             "A number and what it is a number of."),
        _dim("economic_buyer", "Economic Buyer", "fundamental",
             "The person who can approve the spend is named, with their authority or criteria stated.",
             "Named, plus in the room / approval threshold stated / what they will need described."),
        _dim("decision_criteria", "Decision Criteria", "advanced",
             "What the buyer compares options against, or what would disqualify one.",
             "Two criteria, or one plus a disqualifier."),
        _dim("decision_process", "Decision Process", "advanced",
             "The steps, forums and people between today and a signature.",
             "A sequence with at least one date or forum."),
        _dim("paper_process", "Paper Process", "advanced",
             "How a contract actually gets signed: legal, procurement, security, required artefacts.",
             "One concrete constraint with a duration or an artefact."),
        _dim("identify_pain", "Identify Pain", "fundamental",
             "A business consequence the buyer feels, beyond a process complaint.",
             "The buyer names a consequence, not an inconvenience."),
        _dim("champion", "Champion", "fundamental",
             "Someone who sells internally when you are not in the room, evidenced by an action.",
             "They commit to do something internally."),
        _dim("competition", "Competition", "advanced",
             "What else is being considered, including do-nothing and in-house build.",
             "The alternative is named and something is learned about how it is weighed."),
    ]},
    "MEDDIC": {"label": "MEDDIC", "dimensions": [
        _dim("metrics", "Metrics", "fundamental", "A quantified business impact in the buyer's words.", "A number and what it measures."),
        _dim("economic_buyer", "Economic Buyer", "fundamental", "The approver is named with authority or criteria stated.", "Named plus authority or criteria."),
        _dim("decision_criteria", "Decision Criteria", "advanced", "What options are compared against.", "Two criteria, or one plus a disqualifier."),
        _dim("decision_process", "Decision Process", "advanced", "Steps and forums between today and signature.", "A sequence with a date or forum."),
        _dim("identify_pain", "Identify Pain", "fundamental", "A felt business consequence.", "A consequence, not an inconvenience."),
        _dim("champion", "Champion", "fundamental", "Someone selling internally, evidenced by an action.", "A committed internal action."),
    ]},
    "BANT": {"label": "BANT", "dimensions": [
        _dim("budget", "Budget", "fundamental", "Money exists, or the path to it is described, by the buyer.", "An amount, a range, or a named funding source."),
        _dim("authority", "Authority", "fundamental", "Who approves the spend, named.", "Named plus how approval works."),
        _dim("need", "Need", "fundamental", "A business consequence the buyer feels.", "A consequence, not a feature wish."),
        _dim("timing", "Timing", "advanced", "A date driven by something in the buyer's world.", "A date attached to a real event, not a preference."),
    ]},
    "SPICED": {"label": "SPICED", "dimensions": [
        _dim("situation", "Situation", "fundamental", "How the buyer's current process actually works, in their words.", "The current-state mechanics, specifically."),
        _dim("pain", "Pain", "fundamental", "What that situation costs them.", "A named consequence."),
        _dim("impact", "Impact", "fundamental", "The quantified value of removing the pain.", "A number the buyer agrees with."),
        _dim("critical_event", "Critical Event", "advanced", "A dated event that forces a decision.", "A date plus what happens if it is missed."),
        _dim("decision", "Decision", "advanced", "Who decides, how, and against what criteria.", "People, process and criteria together."),
    ]},
    "CHALLENGER": {"label": "Challenger", "dimensions": [
        _dim("warmer", "Warmer", "fundamental", "The rep demonstrates understanding of the buyer's world before teaching.", "Hypothesis stated and confirmed by the buyer."),
        _dim("reframe", "Reframe", "advanced", "An insight that changes how the buyer sees their problem.", "The buyer reacts to a new idea, not a restated one."),
        _dim("rational_drowning", "Rational Drowning", "advanced", "The cost of the status quo, quantified.", "Numbers applied to the buyer's own situation."),
        _dim("emotional_impact", "Emotional Impact", "fundamental", "The buyer sees themselves in the story.", "The buyer describes their own version of it."),
        _dim("new_way", "A New Way", "advanced", "The capability required, framed before the product.", "Capabilities described without vendor names."),
        _dim("solution", "Your Solution", "fundamental", "Why this vendor delivers the new way better.", "Differentiation tied to a stated criterion."),
    ]},
    "COMMAND_OF_THE_MESSAGE": {"label": "Command of the Message", "dimensions": [
        _dim("before_scenario", "Before Scenario", "fundamental", "The buyer's current state in their own words.", "Current-state mechanics, specifically."),
        _dim("negative_consequences", "Negative Consequences", "fundamental", "What the before scenario costs.", "A named business consequence."),
        _dim("after_scenario", "After Scenario", "fundamental", "What the buyer's world looks like once it is solved.", "The buyer describes the after state."),
        _dim("positive_business_outcomes", "Positive Business Outcomes", "advanced", "The measurable outcome the buyer would claim.", "An outcome with a metric attached."),
        _dim("required_capabilities", "Required Capabilities", "advanced", "What any solution must do, agreed before product.", "Capabilities agreed by the buyer."),
        _dim("metrics", "Metrics", "fundamental", "The number that proves the outcome.", "A number the buyer confirms."),
        _dim("differentiation", "Differentiation", "advanced", "Why this vendor, against the named alternative.", "Differentiation tied to a stated criterion."),
        _dim("decision_criteria", "Decision Criteria", "advanced", "How the decision gets made and by whom.", "People, process and criteria."),
    ]},
}

DEFAULTS: Dict[str, Any] = {
    "framework": {"name": "MEDDPICC", "custom": False, "dimensions": []},
    "call_types": ["discovery", "demo"],
    "reps": [],
    "tenure_bands": {"ramping_days": 90, "developing_days": 365},
    "mechanics_targets": {
        "rep_talk_ratio_max_pct": 55,
        "longest_monologue_max_sec": 150,
        "min_questions_per_30min": 8,
        "next_step_set_target_pct": 90,
        "earliest_pricing_position_pct": 25,
        "source": "industry default",
    },
    "competitors": [],
    "pricing_keywords": ["pricing", "list price", "price point", "discount", "quote", "per seat",
                         "per user", "what this costs", "what does this cost",
                         "how much does it cost", "how much would it cost", "ballpark"],
    "output_audience": "manager",
    "include_per_call_reviews": False,
    "exemplar_call_ids": [],
    "transcript_source": {"provider": "local_directory", "internal_domains": []},
    "window_days": 30,
    "min_call_duration_sec": 300,
    "min_calls_per_rep": 3,
    "min_instances_for_pattern": 3,
    "coverage_warn_pct": 60,
    "material_deal_floor": None,
    "demote_unverified_quotes": True,
    "cadence": "monthly",
}


def resolve_framework(config: Dict[str, Any]) -> Dict[str, Any]:
    """Config's own dimensions win; a bare framework name falls back to the registry."""
    raw = config.get("framework") or {}
    if isinstance(raw, str):
        raw = {"name": raw}
    name = str(raw.get("name") or "MEDDPICC")
    dims = raw.get("dimensions") or []
    if not dims:
        preset = FRAMEWORKS.get(name.upper().replace(" ", "_").replace("-", "_"))
        if not preset:
            raise ConfigError(
                f"Framework {name!r} has no dimensions in ~/.leanscale-gtm/{PLUGIN}.json and is not "
                f"one of the built-ins ({', '.join(sorted(FRAMEWORKS))}).\n"
                f"Re-run /sales-coach:setup, or add a dimensions array to the framework block."
            )
        dims = preset["dimensions"]
    out = []
    for dim in dims:
        entry = dict(dim)
        entry.setdefault("label", entry.get("key", "?").replace("_", " ").title())
        entry.setdefault("tier", "fundamental")
        out.append(entry)
    return {"name": name, "custom": bool(raw.get("custom")), "dimensions": out}


# ---------------------------------------------------------------- loading

def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_raw(raw_dir: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    raw_dir = Path(raw_dir)
    if not raw_dir.exists():
        raise FileNotFoundError(
            f"No raw/ directory at {raw_dir}. The :run skill fetches calls into it before this "
            f"script runs — run /sales-coach:run, or /sales-coach:setup if nothing is connected yet."
        )

    domains = list((config.get("transcript_source") or {}).get("internal_domains") or [])
    roster = config.get("reps") or []

    normalized_path = raw_dir / "normalized_calls.json"
    if normalized_path.exists():
        payload = _read_json(normalized_path)
        calls = payload.get("calls") if isinstance(payload, dict) else payload
        warnings = (payload.get("warnings") if isinstance(payload, dict) else []) or []
    else:
        calls, warnings = tx.normalize_all(raw_dir, domains, roster)

    scored: Dict[str, Any] = {}
    scored_path = raw_dir / "scored_calls.json"
    if scored_path.exists():
        payload = _read_json(scored_path)
        entries = payload.get("calls") if isinstance(payload, dict) else payload
        for entry in entries or []:
            if entry.get("call_id"):
                scored[str(entry["call_id"])] = entry

    deals: List[Dict[str, Any]] = []
    deals_path = raw_dir / "deals.json"
    if deals_path.exists():
        payload = _read_json(deals_path)
        records = payload.get("records") if isinstance(payload, dict) else payload
        deals = normalize_records(records or [])

    fetch_log: Dict[str, Any] = {}
    log_path = raw_dir / "_fetch_log.json"
    if log_path.exists():
        fetch_log = _read_json(log_path)

    return {"calls": calls or [], "warnings": warnings, "scored": scored,
            "deals": deals, "fetch_log": fetch_log}


# -------------------------------------------------------------- mechanics

def count_questions(text: str) -> int:
    total = 0
    for sentence in _SENTENCE_SPLIT.split(str(text or "")):
        clean = sentence.strip()
        if not clean:
            continue
        if clean.endswith("?"):
            total += 1
        elif _INTERROGATIVE.match(clean) and len(clean.split()) >= 3:
            total += 1
    return total


def _find_keyword(text: str, keywords: Sequence[str]) -> Optional[str]:
    low = str(text or "").lower()
    for word in keywords:
        needle = str(word).lower().strip()
        if not needle:
            continue
        if " " in needle:
            if needle in low:
                return word
        elif re.search(r"\b" + re.escape(needle) + r"\b", low):
            return word
    return None


def call_mechanics(call: Dict[str, Any], config: Dict[str, Any], competitors: Sequence[str],
                   scored: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Everything measurable without judgment. Suppressed entirely when speaker
    attribution is low-confidence: a talk ratio computed over speakers we cannot
    place is a number that looks precise and is not.
    """
    turns = call.get("turns") or []
    attribution = call.get("attribution") or {}
    eligible = attribution.get("confidence") in ("high", "medium") and bool(turns)

    internal_sec = external_sec = unknown_sec = 0.0
    internal_words = external_words = 0
    questions = 0
    for turn in turns:
        seconds = float(turn.get("duration_sec") or 0)
        if turn.get("is_internal") is True:
            internal_sec += seconds
            internal_words += int(turn.get("words") or 0)
            questions += count_questions(turn.get("text"))
        elif turn.get("is_internal") is False:
            external_sec += seconds
            external_words += int(turn.get("words") or 0)
        else:
            unknown_sec += seconds

    # Longest uninterrupted stretch by one of ours.
    longest = {"seconds": 0.0, "ts": None, "speaker": None}
    run_sec, run_start, run_speaker = 0.0, None, None
    for turn in turns:
        if turn.get("is_internal") is True and turn.get("speaker") == run_speaker:
            run_sec += float(turn.get("duration_sec") or 0)
        elif turn.get("is_internal") is True:
            run_speaker, run_sec, run_start = turn.get("speaker"), float(turn.get("duration_sec") or 0), turn.get("start_sec")
        else:
            run_speaker, run_sec, run_start = None, 0.0, None
        if run_sec > longest["seconds"]:
            longest = {"seconds": round(run_sec, 1), "ts": tx.format_ts(run_start), "speaker": run_speaker}

    duration = float(call.get("duration_sec") or 0) or (internal_sec + external_sec + unknown_sec)

    # Pricing: prefer Claude's judgment from the scoring pass, because "the
    # pricing model she never got to build" is not a pricing discussion and no
    # keyword list will ever know that. Fall back to a keyword locator, clearly
    # labelled as one, when the scoring pass did not record it.
    pricing = None
    judged = (scored or {}).get("pricing") or {}
    if judged:
        if judged.get("discussed"):
            hit = tx.find_quote(judged.get("quote", ""), turns) if judged.get("quote") else None
            start = tx.parse_ts(judged.get("first_at"))
            if start is None and hit is not None:
                start = hit.get("start_sec")
            pricing = {
                "ts": tx.format_ts(start) if start is not None else judged.get("first_at"),
                "position_pct": round(100.0 * float(start) / duration, 1) if (start is not None and duration) else None,
                "raised_by": judged.get("raised_by") or "unknown",
                "speaker": judged.get("speaker") or (hit or {}).get("speaker"),
                "quote": str(judged.get("quote") or "")[:220],
                "method": "judged on the call transcript",
                "quote_verified": None if not judged.get("quote") else hit is not None,
            }
    else:
        keywords = config.get("pricing_keywords") or DEFAULTS["pricing_keywords"]
        for turn in turns:
            hit_word = _find_keyword(turn.get("text"), keywords)
            if hit_word:
                pricing = {
                    "ts": turn.get("ts") or tx.format_ts(turn.get("start_sec")),
                    "position_pct": round(100.0 * float(turn.get("start_sec") or 0) / duration, 1) if duration else None,
                    "raised_by": {True: "rep", False: "customer"}.get(turn.get("is_internal"), "unknown"),
                    "speaker": turn.get("speaker"),
                    "keyword": hit_word,
                    "quote": (turn.get("text") or "")[:220],
                    "method": "keyword locator — verify before coaching on it",
                }
                break

    mentions: List[Dict[str, Any]] = []
    for turn in turns:
        hit = _find_keyword(turn.get("text"), competitors)
        if hit:
            mentions.append({
                "competitor": hit,
                "ts": turn.get("ts") or tx.format_ts(turn.get("start_sec")),
                "raised_by": {True: "rep", False: "customer"}.get(turn.get("is_internal"), "unknown"),
                "speaker": turn.get("speaker"),
                "quote": (turn.get("text") or "")[:220],
            })

    talk_total = internal_sec + external_sec
    out = {
        "eligible": eligible,
        "suppressed_reason": None if eligible else (
            "speaker attribution is low-confidence on this call, so talk time cannot be split reliably"
        ),
        "talk_ratio_pct": round(100.0 * internal_sec / talk_total, 1) if talk_total else None,
        "internal_sec": round(internal_sec, 1),
        "external_sec": round(external_sec, 1),
        "unattributed_sec": round(unknown_sec, 1),
        "internal_words": internal_words,
        "external_words": external_words,
        "questions": questions,
        "questions_per_30min": round(questions / (duration / 1800.0), 1) if duration else None,
        "longest_monologue": longest,
        "first_pricing": pricing,
        "competitor_mentions": mentions,
        "timing_method": attribution.get("timing_method"),
    }
    if not eligible:
        # Blank the derived numbers rather than leaving plausible-looking ones in
        # findings.json for somebody to quote later.
        out.update({"talk_ratio_pct": None, "questions": None, "questions_per_30min": None,
                    "longest_monologue": {"seconds": 0.0, "ts": None, "speaker": None}})
    return out


# ----------------------------------------------------------------- scoring

def _coverage(points: float, possible: float) -> Optional[float]:
    return round(100.0 * points / possible, 1) if possible else None


def score_call(
    call: Dict[str, Any],
    scored: Dict[str, Any],
    framework: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Take Claude's judgment for one call and make it verifiable: every met or
    partial must carry a quote and a timestamp, and the quote must actually
    appear in the transcript. Anything that fails becomes 'unscored' — it is
    not counted as a pass and it is not counted as a fail.
    """
    turns = call.get("turns") or []
    demote = bool(config.get("demote_unverified_quotes", True))
    by_key = {str(d.get("key")): d for d in (scored.get("dimensions") or [])}

    rows: List[Dict[str, Any]] = []
    problems: List[Dict[str, Any]] = []
    points = possible = 0.0

    for dim in framework["dimensions"]:
        key = dim["key"]
        given = by_key.get(key) or {}
        status = str(given.get("status") or "unscored").lower()
        if status not in VALID_STATUS:
            problems.append({"call_id": call["call_id"], "dimension": dim["label"],
                             "issue": f"unrecognised status {status!r}"})
            status = "unscored"

        evidence = dict(given.get("evidence") or {})
        quote = str(evidence.get("quote") or "").strip()
        stamp = str(evidence.get("timestamp") or "").strip()
        verified: Optional[bool] = None
        drift: Optional[float] = None

        if status in ("met", "partial"):
            if not quote or not stamp:
                problems.append({"call_id": call["call_id"], "dimension": dim["label"],
                                 "issue": "scored met/partial with no quote or no timestamp"})
                status = "unscored"
            else:
                hit = tx.find_quote(quote, turns)
                verified = hit is not None
                if hit is not None:
                    cited = tx.parse_ts(stamp)
                    if cited is not None and hit.get("start_sec") is not None:
                        drift = round(abs(cited - float(hit["start_sec"])), 1)
                else:
                    problems.append({
                        "call_id": call["call_id"], "dimension": dim["label"],
                        "issue": "the supporting quote does not appear in the transcript",
                        "quote": quote[:160],
                    })
                    if demote:
                        status = "unscored"
        elif status == "not_applicable" and not str(given.get("rationale") or "").strip():
            problems.append({"call_id": call["call_id"], "dimension": dim["label"],
                             "issue": "marked not_applicable with no rationale"})

        missed = dict(given.get("missed_moment") or {})
        if missed.get("quote") and tx.find_quote(missed["quote"], turns) is None:
            problems.append({"call_id": call["call_id"], "dimension": dim["label"],
                             "issue": "the missed-moment quote does not appear in the transcript",
                             "quote": str(missed["quote"])[:160]})
            missed = {}

        if status in SCORABLE:
            points += STATUS_POINTS[status]
            possible += 2

        rows.append({
            "key": key, "label": dim["label"], "tier": dim.get("tier", "fundamental"),
            "status": status, "points": STATUS_POINTS.get(status),
            "quote": quote, "timestamp": stamp,
            "speaker": evidence.get("speaker"), "speaker_side": evidence.get("speaker_side"),
            "quote_verified": verified, "timestamp_drift_sec": drift,
            "missed_moment": missed or None,
            "rationale": given.get("rationale", ""),
        })

    next_step = dict(scored.get("next_step") or {})
    if next_step.get("set") and next_step.get("quote"):
        next_step["quote_verified"] = tx.find_quote(next_step["quote"], turns) is not None

    return {"dimensions": rows, "points": points, "possible": possible,
            "coverage_pct": _coverage(points, possible), "next_step": next_step,
            "problems": problems, "notable": scored.get("notable") or {},
            "attribution_caveat": scored.get("attribution_caveat")}


# --------------------------------------------------------------- roll-ups

def tenure_band(start_date: Any, as_of: datetime, bands: Dict[str, int]) -> Tuple[Optional[int], str]:
    start = tx.parse_ts(None)  # placeholder for typing clarity
    parsed = None
    if start_date:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = datetime.strptime(str(start_date)[:19], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if parsed is None:
        return None, "unknown"
    days = (as_of - parsed).days
    if days < int(bands.get("ramping_days", 90)):
        return days, "ramping"
    if days < int(bands.get("developing_days", 365)):
        return days, "developing"
    return days, "tenured"


def dimension_rollup(calls: Sequence[Dict[str, Any]], framework: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for dim in framework["dimensions"]:
        points = possible = 0.0
        counts = {"met": 0, "partial": 0, "missing": 0, "not_applicable": 0, "unscored": 0}
        for call in calls:
            row = next((r for r in call["scorecard"]["dimensions"] if r["key"] == dim["key"]), None)
            if not row:
                continue
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            if row["status"] in SCORABLE:
                points += STATUS_POINTS[row["status"]]
                possible += 2
        out.append({
            "key": dim["key"], "label": dim["label"], "tier": dim.get("tier", "fundamental"),
            "points": points, "possible": possible, "coverage_pct": _coverage(points, possible),
            "scored_calls": int(possible / 2), **counts,
        })
    return out


def rep_rollup(calls: Sequence[Dict[str, Any]], framework: Dict[str, Any],
               config: Dict[str, Any], as_of: datetime) -> List[Dict[str, Any]]:
    bands = config.get("tenure_bands") or DEFAULTS["tenure_bands"]
    roster = {str(r.get("name", "")).strip().lower(): r for r in (config.get("reps") or [])}
    by_rep: Dict[str, List[Dict[str, Any]]] = {}
    for call in calls:
        by_rep.setdefault(call.get("rep") or "(unassigned)", []).append(call)

    out = []
    for name, rep_calls in sorted(by_rep.items()):
        entry = roster.get(name.strip().lower(), {})
        days, band = tenure_band(entry.get("start_date"), as_of, bands)
        dims = dimension_rollup(rep_calls, framework)
        points = sum(d["points"] for d in dims)
        possible = sum(d["possible"] for d in dims)

        mech = [c["mechanics"] for c in rep_calls if c["mechanics"]["eligible"]]
        talk = [m["talk_ratio_pct"] for m in mech if m["talk_ratio_pct"] is not None]
        qrate = [m["questions_per_30min"] for m in mech if m["questions_per_30min"] is not None]
        monologue = [m["longest_monologue"]["seconds"] for m in mech]
        worst_monologue = max(
            (m["longest_monologue"] for m in mech), key=lambda x: x["seconds"], default={"seconds": 0, "ts": None}
        )
        next_steps = [c["scorecard"]["next_step"].get("set") for c in rep_calls]

        # Coach a ramping rep on the fundamentals and nothing else. Telling
        # someone ten weeks in that their paper process is weak is noise.
        scope = ("fundamental",) if band == "ramping" else ("fundamental", "advanced")
        candidates = [d for d in dims if d["tier"] in scope and d["coverage_pct"] is not None and d["scored_calls"] > 0]
        focus = sorted(candidates, key=lambda d: (d["coverage_pct"], d["label"]))[:2]
        strengths = sorted(candidates, key=lambda d: (-d["coverage_pct"], d["label"]))[:1]

        out.append({
            "rep": name,
            "email": entry.get("email"),
            "role": entry.get("role"),
            "segment": entry.get("segment"),
            "start_date": entry.get("start_date"),
            "tenure_days": days,
            "tenure_band": band,
            "calls": len(rep_calls),
            "calls_with_mechanics": len(mech),
            "coverage_pct": _coverage(points, possible),
            "dimensions": dims,
            "focus_dimensions": [{"key": d["key"], "label": d["label"], "coverage_pct": d["coverage_pct"],
                                  "tier": d["tier"], "missing": d["missing"], "scored_calls": d["scored_calls"]}
                                 for d in focus],
            "strength_dimension": ({"label": strengths[0]["label"], "coverage_pct": strengths[0]["coverage_pct"]}
                                   if strengths else None),
            "talk_ratio_pct": round(sum(talk) / len(talk), 1) if talk else None,
            "questions_per_30min": round(sum(qrate) / len(qrate), 1) if qrate else None,
            "longest_monologue_sec": max(monologue) if monologue else None,
            "longest_monologue_at": worst_monologue.get("ts"),
            "next_step_set_pct": pct(sum(1 for n in next_steps if n), len(next_steps)) if next_steps else None,
            "call_ids": [c["call_id"] for c in rep_calls],
            "enough_evidence": len(rep_calls) >= int(config.get("min_calls_per_rep", 3)),
        })
    return out


def _first(deal: Dict[str, Any], *names: str) -> Any:
    """First present, non-blank value among CRM field aliases."""
    for name in names:
        value = deal.get(name)
        if value not in (None, ""):
            return value
    return None


def deal_outcome(deal: Dict[str, Any]) -> str:
    # HubSpot names these hs_is_closed / hs_is_closed_won / closedate. Reading only
    # the Salesforce names made every HubSpot deal fall through to "open", which
    # silently deleted won/lost/slipped coaching — the plugin's headline finding —
    # on a portal whose data was present and simply never looked at.
    closed = str(_first(deal, "IsClosed", "is_closed", "hs_is_closed") or "").lower() \
        in ("true", "1")
    won = str(_first(deal, "IsWon", "is_won", "hs_is_closed_won") or "").lower() \
        in ("true", "1")
    if closed:
        return "won" if won else "lost"
    pushes = _first(deal, "Close_Date_Push_Count__c", "close_date_pushes") or 0
    try:
        pushes = int(float(pushes))
    except (TypeError, ValueError):
        pushes = 0
    # No HubSpot equivalent of an original-close-date field ships by default, so a
    # HubSpot deal reports "slipped" only when the portal has a mapped push counter.
    # Inventing an alias here would fabricate slippage rather than measure it.
    original = _first(deal, "Original_Close_Date__c", "original_close_date")
    current = _first(deal, "CloseDate", "close_date", "closedate")
    if pushes > 0 or (original and current and str(current) > str(original)):
        return "slipped"
    return "open"


def deal_linkage(calls: Sequence[Dict[str, Any]], deals: Sequence[Dict[str, Any]],
                 framework: Dict[str, Any]) -> Dict[str, Any]:
    """
    Correlate framework gaps against what happened to the deal. Sample sizes in
    a single window are small — the output states n explicitly and calls itself
    directional, because a coach who over-reads six deals stops trusting the
    tool on the seventh.
    """
    index = {str(d.get("Id") or d.get("id") or "").strip(): d for d in deals}
    linked = []
    for call in calls:
        deal = index.get(str(call.get("deal_id") or "").strip())
        if not deal:
            continue
        linked.append({"call": call, "deal": deal, "outcome": deal_outcome(deal)})

    if not linked:
        return {"available": False, "reason": "no call could be matched to a CRM opportunity"}

    buckets = {"won": [], "lost": [], "slipped": [], "open": []}
    for row in linked:
        buckets[row["outcome"]].append(row)
    adverse = buckets["lost"] + buckets["slipped"]
    favourable = buckets["won"]

    comparisons = []
    for dim in framework["dimensions"]:
        def coverage(rows: Sequence[Dict[str, Any]]) -> Tuple[Optional[float], int]:
            points = possible = 0.0
            for row in rows:
                cell = next((r for r in row["call"]["scorecard"]["dimensions"] if r["key"] == dim["key"]), None)
                if cell and cell["status"] in SCORABLE:
                    points += STATUS_POINTS[cell["status"]]
                    possible += 2
            return _coverage(points, possible), int(possible / 2)

        won_pct, won_n = coverage(favourable)
        bad_pct, bad_n = coverage(adverse)
        if won_pct is None or bad_pct is None:
            continue
        comparisons.append({
            "key": dim["key"], "label": dim["label"],
            "won_coverage_pct": won_pct, "won_n": won_n,
            "adverse_coverage_pct": bad_pct, "adverse_n": bad_n,
            "spread": round(won_pct - bad_pct, 1),
        })
    # Biggest spread first, but a tie goes to the better-evidenced comparison —
    # a 100-point spread across two deals is not a finding.
    comparisons.sort(key=lambda c: (-c["spread"], -(c["won_n"] + c["adverse_n"])))

    return {
        "available": True,
        "linked_calls": len(linked),
        "outcomes": {k: len(v) for k, v in buckets.items()},
        "comparisons": comparisons,
        "deal_ids": [str(row["deal"].get("Id") or row["deal"].get("id") or "") for row in linked],
        "rows": [{
            "Deal": row["deal"].get("Name") or row["deal"].get("dealname"),
            "Amount": row["deal"].get("Amount") or row["deal"].get("amount"),
            "Outcome": row["outcome"],
            "Rep": row["call"].get("rep"),
            "Call coverage %": row["call"]["scorecard"]["coverage_pct"],
        } for row in linked],
        "directional": len(favourable) < 5 or len(adverse) < 5,
    }


def calibration(calls: Sequence[Dict[str, Any]], exemplar_ids: Sequence[str],
                framework: Dict[str, Any]) -> Dict[str, Any]:
    ids = {str(i) for i in exemplar_ids or []}
    if not ids:
        return {"available": False, "reason": "no exemplar calls were nominated during setup"}
    exemplars = [c for c in calls if str(c["call_id"]) in ids]
    others = [c for c in calls if str(c["call_id"]) not in ids]
    if not exemplars:
        return {"available": False,
                "reason": f"none of the nominated exemplar calls ({', '.join(sorted(ids))}) are in this window"}

    def coverage(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
        points = sum(c["scorecard"]["points"] for c in rows)
        possible = sum(c["scorecard"]["possible"] for c in rows)
        return _coverage(points, possible)

    ex_cov, rest_cov = coverage(exemplars), coverage(others)
    gap = None if ex_cov is None or rest_cov is None else round(ex_cov - rest_cov, 1)

    per_dim = []
    for dim in framework["dimensions"]:
        def cov(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
            points = possible = 0.0
            for call in rows:
                cell = next((r for r in call["scorecard"]["dimensions"] if r["key"] == dim["key"]), None)
                if cell and cell["status"] in SCORABLE:
                    points += STATUS_POINTS[cell["status"]]
                    possible += 2
            return _coverage(points, possible)
        per_dim.append({"label": dim["label"], "exemplar_pct": cov(exemplars), "rest_pct": cov(others)})

    if gap is None:
        verdict = "Not enough scored dimensions to calibrate."
    elif gap >= 20:
        verdict = ("The framework agrees with you. Your exemplar calls score well above the rest, "
                   "which means the scores below are measuring something you already recognise as good.")
    elif gap >= 5:
        verdict = ("The framework broadly agrees with you, but not emphatically. Your exemplars score "
                   "moderately above the rest — read the dimension table for where they diverge.")
    else:
        verdict = ("The framework does not agree with you, and that is the most useful thing on this page. "
                   "Your exemplar calls score no better than the rest, so either those calls are good for "
                   "reasons this framework does not measure, or the team is stronger than the headline "
                   "suggests. Look at what your exemplars do well that the dimensions ignore, and either "
                   "add a dimension or change the exemplars.")

    return {"available": True, "exemplar_ids": sorted(ids),
            "exemplar_calls": [{"call_id": c["call_id"], "title": c.get("title"), "rep": c.get("rep"),
                                "coverage_pct": c["scorecard"]["coverage_pct"]} for c in exemplars],
            "exemplar_coverage_pct": ex_cov, "rest_coverage_pct": rest_cov, "gap": gap,
            "per_dimension": per_dim, "verdict": verdict}


# ---------------------------------------------------------------- findings

def _call_row(call: Dict[str, Any], dim_key: Optional[str] = None) -> Dict[str, Any]:
    row = {
        "Call": call.get("title") or call["call_id"],
        "Rep": call.get("rep"),
        "Date": (call.get("started_at") or "")[:10],
        "Deal": f"${float(call.get('deal_amount') or 0):,.0f}" if call.get("deal_amount") else "—",
    }
    if dim_key:
        cell = next((r for r in call["scorecard"]["dimensions"] if r["key"] == dim_key), None)
        row["Status"] = (cell or {}).get("status", "—")
        missed = (cell or {}).get("missed_moment") or {}
        row["Moment it was there to take"] = (
            f"{missed.get('timestamp', '')} — “{str(missed.get('quote'))[:130]}”"
            if missed.get("quote") else "—"
        )
    return row


def build_findings(ctx: Dict[str, Any]) -> FindingsDoc:
    config, framework = ctx["config"], ctx["framework"]
    calls, reps = ctx["calls"], ctx["reps"]
    dims, targets = ctx["dimensions"], ctx["targets"]
    floor = ctx["material_deal_floor"]
    min_instances = int(config.get("min_instances_for_pattern", 3))

    doc = FindingsDoc(
        plugin=PLUGIN,
        window=ctx["window"],
        org_name=ctx["org_name"],
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    # ---- 1. The one thing: the team's weakest dimension.
    ranked = [d for d in dims if d["coverage_pct"] is not None and d["scored_calls"] >= min_instances]
    weakest = ranked[0] if ranked else None
    if ranked:
        ranked = sorted(ranked, key=lambda d: (d["coverage_pct"], d["label"]))
        weakest = ranked[0]

    if weakest:
        gap_calls = [c for c in calls
                     if next((r for r in c["scorecard"]["dimensions"]
                              if r["key"] == weakest["key"] and r["status"] in ("missing", "partial")), None)]
        material = [c for c in gap_calls if float(c.get("deal_amount") or 0) >= floor]
        missing_only = [c for c in gap_calls
                        if next((r for r in c["scorecard"]["dimensions"]
                                 if r["key"] == weakest["key"] and r["status"] == "missing"), None)]
        scope = material if material else gap_calls
        money = sum(float(c.get("deal_amount") or 0) for c in scope)
        below_floor = len(gap_calls) - len(scope)
        severity = "critical" if weakest["coverage_pct"] < 30 and material else "high"
        doc.add(Finding(
            id=f"team-weakest-{weakest['key']}",
            severity=severity,
            title=(f"{weakest['label']} is the team's weakest dimension — "
                   f"{len(missing_only)} of {weakest['scored_calls']} scored calls have it missing entirely"),
            what=(
                f"Across {weakest['scored_calls']} calls where {weakest['label']} was applicable, the team scored "
                f"{weakest['coverage_pct']}% coverage: {weakest['met']} met, {weakest['partial']} partial, "
                f"{weakest['missing']} missing. On deals at or above ${floor:,.0f} it is unresolved on "
                f"{len(material)} call{'s' if len(material) != 1 else ''} carrying "
                f"${money:,.0f} of pipeline. The table lists every one, with the moment on the call where the "
                f"buyer opened the door and nobody walked through it."
                + (f" A further {below_floor} call{'s' if below_floor != 1 else ''} below the "
                   f"${floor:,.0f} materiality floor "
                   f"{'shows' if below_floor == 1 else 'show'} the same gap; "
                   f"{'it is' if below_floor == 1 else 'they are'} in findings.json."
                   if below_floor else "")
            ),
            why_it_matters=(
                "This is a team pattern, not a person problem — it shows up across reps and tenures, which "
                "means it is a habit the room shares and one coaching session can move. Individually each of "
                "these is a call that felt fine; together they are the reason deals of this size stall at the "
                "same stage."
            ),
            recommended_fix=(
                f"Take one item to Monday's team meeting: {weakest['label']}. Play the two timestamped moments "
                f"in the table, ask the team what they would have said next, and agree one question everyone "
                f"asks on every call this week. Re-run this in two weeks and the coverage number moves or the "
                f"coaching did not land."
            ),
            evidence={
                "count": len(scope),
                "rows": [_call_row(c, weakest["key"]) for c in scope],
                "sample_ids": [c["call_id"] for c in scope[:10]],
                "query": "\n".join(
                    [f"Verify in your own recordings — {weakest['label']}:"]
                    + [f"  {c['call_id']}  {c.get('title')}  ({(c.get('started_at') or '')[:10]}, {c.get('rep')})"
                       + (f"\n      jump to {(next((r for r in c['scorecard']['dimensions'] if r['key'] == weakest['key']), {}) or {}).get('missed_moment', {}).get('timestamp')}"
                          if (next((r for r in c["scorecard"]["dimensions"] if r["key"] == weakest["key"]), {}) or {}).get("missed_moment") else "")
                       for c in scope]
                ),
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

        secondary = [d for d in ranked[1:] if d["coverage_pct"] is not None
                     and d["coverage_pct"] < float(config.get("coverage_warn_pct", 60))]
        if secondary:
            doc.add(Finding(
                id="secondary-dimension-gaps",
                severity="medium",
                title=f"{len(secondary)} more dimensions sit below {config.get('coverage_warn_pct', 60)}% coverage",
                what=("Behind the headline gap, these dimensions are also thin across the team. They are listed "
                      "in order so you can work them one per cycle rather than all at once."),
                why_it_matters=("Coaching more than one dimension at a time produces no measurable change in any "
                                "of them. This is the queue behind the headline, not this week's work."),
                recommended_fix=("Work the top of this list only after the headline dimension has moved. One "
                                 "dimension per fortnight is the realistic rate of change for a team."),
                evidence={
                    "count": len(secondary),
                    "rows": [{"Dimension": d["label"], "Tier": d["tier"], "Coverage": f"{d['coverage_pct']}%",
                              "Met": d["met"], "Partial": d["partial"], "Missing": d["missing"],
                              "Calls scored": d["scored_calls"]} for d in secondary],
                },
                effort="medium",
                owner_hint="Sales manager",
            ))

    # ---- 2. Next steps.
    scored_calls = [c for c in calls if c["scorecard"]["next_step"]]
    with_next = [c for c in scored_calls if c["scorecard"]["next_step"].get("set")]
    without = [c for c in scored_calls if not c["scorecard"]["next_step"].get("set")]
    rate = pct(len(with_next), len(scored_calls)) if scored_calls else None
    target = float(targets.get("next_step_set_target_pct", 90))
    if without and rate is not None and rate < target:
        doc.add(Finding(
            id="next-step-not-set",
            severity="high" if rate < target - 20 else "medium",
            title=f"{len(without)} of {len(scored_calls)} calls ended without a dated next step",
            what=(f"A next step counts when a specific date and the people involved were agreed on the call. "
                  f"{rate}% of calls cleared that bar against a target of {target:.0f}% "
                  f"({targets.get('source', 'industry default')}). The table shows how each of the others ended."),
            why_it_matters=("'I'll follow up in a couple of weeks' hands control of the deal to the buyer and is "
                            "the single most reliable predictor of a deal that goes quiet. It is also the "
                            "cheapest habit on this page to fix, because it is one sentence at the end of a call."),
            recommended_fix=("Make the last two minutes of every call a scripted close: name the next step, the "
                             "date, who is on it, and what each side brings. Inspect it in the pipeline review by "
                             "asking for the date, not for the status."),
            evidence={
                "count": len(without),
                "rows": [{**_call_row(c),
                          "How it ended": f"{c['scorecard']['next_step'].get('timestamp', '')} — "
                                          f"“{str(c['scorecard']['next_step'].get('quote'))[:120]}”"}
                         for c in without],
                "sample_ids": [c["call_id"] for c in without],
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

    # ---- 3. Tenure-aware coaching.
    ramping = [r for r in reps if r["tenure_band"] == "ramping" and r["enough_evidence"] is not False]
    tenured = [r for r in reps if r["tenure_band"] == "tenured"]
    for rep in ramping:
        fundamentals = [d for d in rep["dimensions"] if d["tier"] == "fundamental" and d["possible"]]
        points = sum(d["points"] for d in fundamentals)
        possible = sum(d["possible"] for d in fundamentals)
        cov = _coverage(points, possible)
        peers = [r for r in reps if r["rep"] != rep["rep"] and r["tenure_band"] in ("developing", "tenured")]
        peer_cov = None
        if peers:
            ppoints = sum(d["points"] for r in peers for d in r["dimensions"] if d["tier"] == "fundamental")
            ppossible = sum(d["possible"] for r in peers for d in r["dimensions"] if d["tier"] == "fundamental")
            peer_cov = _coverage(ppoints, ppossible)
        if cov is None:
            continue
        doc.add(Finding(
            id=f"ramping-fundamentals-{re.sub(r'[^a-z0-9]+', '-', rep['rep'].lower()).strip('-')}",
            severity="high" if peer_cov and cov < peer_cov - 20 else "medium",
            title=(f"{rep['rep']} is {rep['tenure_days']} days in and scoring {cov}% on the fundamentals"
                   + (f" against {peer_cov}% for the rest of the team" if peer_cov is not None else "")),
            what=(f"Coaching for a ramping rep is scoped to the fundamental dimensions only — "
                  f"{', '.join(d['label'] for d in fundamentals)} — because the advanced ones do not stick before "
                  f"these do. Across {rep['calls']} call{'s' if rep['calls'] != 1 else ''}, the weakest are: "
                  + "; ".join(f"{d['label']} at {d['coverage_pct']}%" for d in rep["focus_dimensions"]) + "."),
            why_it_matters=("A rep this early is forming habits, and the habit that forms first is the one that "
                            "sticks. Feedback on paper process or decision criteria right now is noise — it "
                            "spends the coaching attention they have on the wrong thing."),
            recommended_fix=(f"One dimension, one week: {rep['focus_dimensions'][0]['label'] if rep['focus_dimensions'] else 'the weakest fundamental'}. "
                             f"Give them the exact question to ask, listen for it on the next call, and do not "
                             f"introduce a second dimension until it appears without prompting."),
            evidence={
                "count": rep["calls"],
                "rows": [{"Dimension": d["label"], "Coverage": f"{d['coverage_pct']}%",
                          "Missing on": f"{d['missing']} of {d['scored_calls']} calls"}
                         for d in rep["dimensions"] if d["tier"] == "fundamental" and d["possible"]],
                "sample_ids": rep["call_ids"],
            },
            effort="quick",
            owner_hint=f"Manager of {rep['rep']}",
        ))

    for rep in tenured:
        advanced = [d for d in rep["dimensions"] if d["tier"] == "advanced" and d["possible"]]
        if not advanced:
            continue
        points = sum(d["points"] for d in advanced)
        possible = sum(d["possible"] for d in advanced)
        cov = _coverage(points, possible)
        fundamentals = [d for d in rep["dimensions"] if d["tier"] == "fundamental" and d["possible"]]
        fcov = _coverage(sum(d["points"] for d in fundamentals), sum(d["possible"] for d in fundamentals))
        if cov is None or fcov is None or cov >= 50 or fcov - cov < 25:
            continue
        doc.add(Finding(
            id=f"tenured-advanced-gap-{re.sub(r'[^a-z0-9]+', '-', rep['rep'].lower()).strip('-')}",
            severity="high",
            title=(f"{rep['rep']} runs the fundamentals at {fcov}% and the advanced dimensions at {cov}%"),
            what=(f"{rep['rep']} has been here {rep['tenure_days']} days and asks excellent pain and metrics "
                  f"questions. The gap is entirely in the dimensions that decide whether a qualified deal closes "
                  f"this quarter or next: " + ", ".join(f"{d['label']} ({d['coverage_pct']}%)" for d in advanced) + "."),
            why_it_matters=("This is the most expensive pattern in the report and the easiest to miss, because "
                            "the calls sound good. A tenured rep filling the top of the funnel with well-qualified "
                            "pain and no decision path produces a pipeline that reviews well and slips quietly."),
            recommended_fix=("Do not coach discovery with this rep — they are better at it than the framework "
                             "score suggests. Coach the second half of the call: who signs, what happens after "
                             "you agree, and what the paperwork needs. Role-play the transition, not the opener."),
            evidence={
                "count": rep["calls"],
                "rows": [{"Dimension": d["label"], "Tier": d["tier"], "Coverage": f"{d['coverage_pct']}%",
                          "Missing on": f"{d['missing']} of {d['scored_calls']} calls"}
                         for d in rep["dimensions"] if d["possible"]],
                "sample_ids": rep["call_ids"],
            },
            effort="quick",
            owner_hint=f"Manager of {rep['rep']}",
        ))

    # ---- 4. Deal linkage — the finding that gets budget.
    linkage = ctx["linkage"]
    if linkage.get("available") and linkage.get("comparisons"):
        top = linkage["comparisons"][0]
        if top["spread"] >= 25 and top["won_n"] >= 1 and top["adverse_n"] >= 2:
            adverse_total = linkage["outcomes"]["lost"] + linkage["outcomes"]["slipped"]
            doc.add(Finding(
                id=f"outcome-linkage-{top['key']}",
                severity="critical" if top["spread"] >= 50 else "high",
                title=(f"{top['label']} coverage is {top['won_coverage_pct']}% on deals you won and "
                       f"{top['adverse_coverage_pct']}% on deals that slipped or lost"),
                what=(f"Matching {linkage['linked_calls']} scored calls to their opportunity records: "
                      f"{linkage['outcomes']['won']} won, {linkage['outcomes']['lost']} lost, "
                      f"{linkage['outcomes']['slipped']} slipped, {linkage['outcomes']['open']} still open. "
                      f"{top['label']} separates the two groups by {top['spread']} points — the widest spread of "
                      f"any dimension in the framework."
                      + (" Sample sizes this small make it directional, not proven: treat it as the hypothesis "
                         "to test over the next two runs, not as a finished causal claim."
                         if linkage.get("directional") else "")),
                why_it_matters=(f"This is the number that justifies the coaching time. It is not 'reps should "
                                f"follow the process' — it is {adverse_total} deals where the dimension was thin "
                                f"and the deal did not close on time."),
                recommended_fix=(f"Put {top['label']} on the pipeline-review checklist as a gate, not a field: no "
                                 f"deal advances past the qualification stage without it evidenced by a quote from "
                                 f"a call. Re-run after two cycles and compare the spread."),
                evidence={
                    "count": linkage["linked_calls"],
                    "rows": [{"Dimension": c["label"],
                              f"Won (n={c['won_n']})": f"{c['won_coverage_pct']}%",
                              f"Slipped or lost (n={c['adverse_n']})": f"{c['adverse_coverage_pct']}%",
                              "Spread": f"{c['spread']:+.0f} pts"} for c in linkage["comparisons"]],
                    "query": (
                        "SELECT Id, Name, Amount, StageName, IsClosed, IsWon, CloseDate,\n"
                        "       Original_Close_Date__c, Close_Date_Push_Count__c, Owner.Name\n"
                        "FROM Opportunity\n"
                        f"WHERE Id IN ({', '.join(repr(i) for i in linkage['deal_ids'][:25] if i)})\n"
                        "-- then open the calls listed in the per-call section and check the dimension yourself"
                    ),
                },
                effort="medium",
                owner_hint="Sales leadership",
            ))

    # ---- 5. Mechanics.
    eligible = [c for c in calls if c["mechanics"]["eligible"]]
    talk_target = float(targets.get("rep_talk_ratio_max_pct", 55))
    over_talk = [c for c in eligible if (c["mechanics"]["talk_ratio_pct"] or 0) > talk_target]
    if over_talk:
        doc.add(Finding(
            id="talk-ratio-over-target",
            severity="medium" if max((c["mechanics"]["talk_ratio_pct"] or 0) for c in over_talk) < 70 else "high",
            title=f"Your side spoke for more than {talk_target:.0f}% of the time on {len(over_talk)} of {len(eligible)} calls",
            what=(f"Talk-to-listen measured over speaking time, not wall clock. The target of {talk_target:.0f}% is "
                  f"an {targets.get('source', 'industry default')} — change `mechanics_targets` in "
                  f"~/.leanscale-gtm/{PLUGIN}.json if your team works to a different number. Calls where speaker "
                  f"attribution was unreliable are excluded rather than estimated."),
            why_it_matters=("On a discovery call the ratio is a proxy for whether the rep is collecting "
                            "information or delivering it. It is not a virtue in itself — a demo legitimately "
                            "runs hot — but a discovery call at 70%+ has usually skipped qualification."),
            recommended_fix=("Pair the ratio with the question count below before coaching it. A rep who talks a "
                             "lot and asks a lot is teaching; a rep who talks a lot and asks little is pitching. "
                             "Only the second one needs the conversation."),
            evidence={
                "count": len(over_talk),
                "rows": [{**_call_row(c), "Type": c.get("call_type"),
                          "Your side": f"{c['mechanics']['talk_ratio_pct']}%",
                          "Questions / 30 min": c["mechanics"]["questions_per_30min"],
                          "Timing basis": c["mechanics"]["timing_method"]} for c in over_talk],
                "sample_ids": [c["call_id"] for c in over_talk],
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

    mono_target = float(targets.get("longest_monologue_max_sec", 150))
    long_mono = [c for c in eligible if c["mechanics"]["longest_monologue"]["seconds"] > mono_target]
    if long_mono:
        doc.add(Finding(
            id="monologue-over-target",
            severity="medium",
            title=(f"{len(long_mono)} call{'s' if len(long_mono) != 1 else ''} "
                   f"{'contains' if len(long_mono) == 1 else 'contain'} an uninterrupted stretch "
                   f"over {mono_target/60:.1f} minutes"),
            what=(f"The longest unbroken stretch by one of your people on each call, with the timestamp it starts "
                  f"at. Target {mono_target:.0f} seconds ({targets.get('source', 'industry default')})."),
            why_it_matters=("A three-minute monologue in the first five minutes of a discovery call is the "
                            "company overview, and it is the most reliable sign that the call was structured "
                            "around the seller rather than the buyer. It is also the easiest thing on this page "
                            "to hear yourself doing once someone plays it back."),
            recommended_fix=("Play the timestamp back in a one-to-one. Do not describe the behaviour — play it. "
                             "Then agree a hard rule: no more than ninety seconds before a question."),
            evidence={
                "count": len(long_mono),
                "rows": [{**_call_row(c),
                          "Longest stretch": f"{c['mechanics']['longest_monologue']['seconds']/60:.1f} min",
                          "Starts at": c["mechanics"]["longest_monologue"]["ts"],
                          "Speaker": c["mechanics"]["longest_monologue"]["speaker"]} for c in long_mono],
                "sample_ids": [c["call_id"] for c in long_mono],
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

    q_target = float(targets.get("min_questions_per_30min", 8))
    low_q = [c for c in eligible if (c["mechanics"]["questions_per_30min"] or 0) < q_target]
    if low_q:
        doc.add(Finding(
            id="question-rate-below-target",
            severity="medium",
            title=f"{len(low_q)} call{'s' if len(low_q) != 1 else ''} ran below {q_target:.0f} questions per 30 minutes",
            what=(f"Questions counted from your side only, normalised for call length so a 25-minute call and a "
                  f"55-minute call are comparable. Target {q_target:.0f} per 30 minutes "
                  f"({targets.get('source', 'industry default')})."),
            why_it_matters=("Question rate is the mechanic that most closely tracks the framework score: the "
                            "dimensions do not get met by accident, they get met because somebody asked."),
            recommended_fix=("Give the low-rate reps five questions to carry into the next call, drawn from the "
                             "weakest dimension above. Count them on the next run."),
            evidence={
                "count": len(low_q),
                "rows": [{**_call_row(c), "Type": c.get("call_type"),
                          "Questions": c["mechanics"]["questions"],
                          "Per 30 min": c["mechanics"]["questions_per_30min"],
                          "Framework coverage": f"{c['scorecard']['coverage_pct']}%"} for c in low_q],
                "sample_ids": [c["call_id"] for c in low_q],
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

    price_target = float(targets.get("earliest_pricing_position_pct", 25))
    early_price = [c for c in eligible
                   if c["mechanics"]["first_pricing"]
                   and (c["mechanics"]["first_pricing"].get("position_pct") or 100) < price_target]
    if early_price:
        by_customer = [c for c in early_price if c["mechanics"]["first_pricing"]["raised_by"] == "customer"]
        doc.add(Finding(
            id="pricing-raised-early",
            severity="medium",
            title=f"Pricing came up in the first {price_target:.0f}% of {len(early_price)} call{'s' if len(early_price) != 1 else ''}",
            what=(f"The first pricing moment in each call, who raised it, and how far into the call it landed. "
                  f"{len(by_customer)} of {len(early_price)} were raised by the buyer, not the rep. This is a "
                  f"keyword locator, not an interpreter — it tells you where to listen."),
            why_it_matters=("When the buyer raises price in the first quarter of a discovery call it usually "
                            "means no value has been established yet and they are trying to disqualify you "
                            "cheaply. Answering with a number ends the discovery; answering with a question "
                            "about what would make it worth it continues it."),
            recommended_fix=("Agree a standard response to an early price question and rehearse it. The response "
                             "is not a refusal to answer — it is a range plus a question about what would make "
                             "the range worth paying."),
            evidence={
                "count": len(early_price),
                "rows": [{**_call_row(c), "At": c["mechanics"]["first_pricing"]["ts"],
                          "% into call": c["mechanics"]["first_pricing"]["position_pct"],
                          "Raised by": c["mechanics"]["first_pricing"]["raised_by"],
                          "Quote": str(c["mechanics"]["first_pricing"]["quote"])[:110]} for c in early_price],
                "sample_ids": [c["call_id"] for c in early_price],
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

    comp_calls = [c for c in calls if c["mechanics"]["competitor_mentions"]]
    if comp_calls:
        comp_dim = next((d for d in framework["dimensions"]
                         if d["key"] in ("competition", "differentiation")), None)
        unworked = []
        for call in comp_calls:
            cell = next((r for r in call["scorecard"]["dimensions"]
                         if comp_dim and r["key"] == comp_dim["key"]), None)
            if not cell or cell["status"] in ("missing", "partial", "unscored"):
                unworked.append(call)
        if unworked:
            doc.add(Finding(
                id="competitor-named-not-worked",
                severity="high" if any(float(c.get("deal_amount") or 0) >= floor for c in unworked) else "medium",
                title=f"A competitor was named on {len(comp_calls)} calls and worked properly on {len(comp_calls) - len(unworked)}",
                what=("Mention counting is a plain keyword scan against your competitor list; whether the "
                      "competition was actually worked is the framework judgment beside it. These are the calls "
                      "where a named alternative was on the table and the dimension did not come out met."),
                why_it_matters=("A competitor named out loud by the buyer is the cheapest intelligence in the "
                                "deal, and it expires. Two weeks later they will not repeat it."),
                recommended_fix=("When a competitor is named, the next sentence is a question — what they were "
                                 "quoted for, what they liked, what worries them. Not a differentiation claim. "
                                 "The claim lands on nothing until you know which criterion it should attach to."),
                evidence={
                    "count": len(unworked),
                    "rows": [{**_call_row(c),
                              "Competitor": c["mechanics"]["competitor_mentions"][0]["competitor"],
                              "At": c["mechanics"]["competitor_mentions"][0]["ts"],
                              "Raised by": c["mechanics"]["competitor_mentions"][0]["raised_by"],
                              "Dimension": (next((r["status"] for r in c["scorecard"]["dimensions"]
                                                  if comp_dim and r["key"] == comp_dim["key"]), "—"))}
                             for c in unworked],
                    "sample_ids": [c["call_id"] for c in unworked],
                },
                effort="quick",
                owner_hint="Sales manager",
            ))

    # ---- 6. Calibration.
    calib = ctx["calibration"]
    if calib.get("available") and calib.get("gap") is not None and calib["gap"] < 5:
        doc.add(Finding(
            id="exemplar-calibration-mismatch",
            severity="medium",
            title=(f"Your exemplar calls score {calib['exemplar_coverage_pct']}% against "
                   f"{calib['rest_coverage_pct']}% for the rest — the framework is not measuring what you value"),
            what=calib["verdict"],
            why_it_matters=("Every number in this report is only as good as the agreement between the framework "
                            "and your own judgment. If the calls you would hand a new hire do not score better "
                            "than the calls you would not, the scores will not survive contact with your team."),
            recommended_fix=("Listen to one exemplar with the dimension list in front of you and write down what "
                             "made it good that no dimension captured. If it recurs, add it as a dimension in "
                             f"~/.leanscale-gtm/{PLUGIN}.json. If it does not, pick better exemplars."),
            evidence={
                "count": len(calib.get("exemplar_calls") or []),
                "rows": [{"Dimension": d["label"], "Exemplars": f"{d['exemplar_pct']}%" if d["exemplar_pct"] is not None else "—",
                          "Everyone else": f"{d['rest_pct']}%" if d["rest_pct"] is not None else "—"}
                         for d in calib["per_dimension"]],
                "sample_ids": calib["exemplar_ids"],
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

    # ---- 7. Data quality: attribution and unverifiable evidence.
    degraded = [c for c in calls if (c.get("attribution") or {}).get("confidence") == "low"]
    if degraded:
        doc.add(Finding(
            id="speaker-attribution-degraded",
            severity="high" if len(degraded) > 0.2 * max(len(calls), 1) else "medium",
            title=f"{len(degraded)} of {len(calls)} calls could not be split reliably into your side and theirs",
            what=("These exports do not say who is internal, and the speaker could not be resolved by email "
                  "domain or by the roster. Rather than guess, every talk-time, monologue and question number "
                  "for these calls has been excluded, and quotes from the unresolved speakers are attributed "
                  "exactly as the transcript labels them. Framework scoring still ran, because the content of "
                  "what was said is usually unambiguous even when the label is not."),
            why_it_matters=("A coaching report that credits a customer's discovery question to the rep is worse "
                            "than no report — it is a number the team can disprove, and once they do they stop "
                            "believing the rest of the page."),
            recommended_fix=("Two fixes, in order of effort. Cheap: add every internal email domain to "
                             f"`transcript_source.internal_domains` in ~/.leanscale-gtm/{PLUGIN}.json and every "
                             "rep to `reps`, then re-run. Structural: ask people not to share a room mic, or "
                             "move to a source that carries an internal/external flag per speaker."),
            evidence={
                "count": len(degraded),
                "rows": [{**_call_row(c), "Unresolved speakers": ", ".join(c["attribution"]["unresolved_speakers"]),
                          "Share of words": f"{c['attribution']['unresolved_word_share']:.0%}",
                          "Source": c.get("source")} for c in degraded],
                "sample_ids": [c["call_id"] for c in degraded],
            },
            effort="quick",
            owner_hint="RevOps",
        ))

    problems = [p for c in calls for p in c["scorecard"]["problems"]]
    if problems:
        doc.add(Finding(
            id="evidence-failed-verification",
            severity="medium",
            title=f"{len(problems)} scored dimensions were dropped for missing or unverifiable evidence",
            what=("Every met and partial must carry a verbatim quote and a timestamp, and the quote must be "
                  "findable in the transcript. These did not clear that bar, so they were recorded as unscored "
                  "and excluded from every coverage number — neither a pass nor a fail."),
            why_it_matters=("No evidence, no score. A coaching conversation that opens with a quote the rep "
                            "cannot find in the recording is over before it starts."),
            recommended_fix=("Usually a transcript problem rather than a scoring problem: a paraphrase in the "
                             "transcript, a cut-off recording, or a merged speaker. Re-run after fixing the "
                             "source; if a specific dimension keeps failing, its evidence_rule in config is "
                             "probably too vague to score consistently."),
            evidence={
                "count": len(problems),
                "rows": [{"Call": p["call_id"], "Dimension": p["dimension"], "Problem": p["issue"],
                          "Quote": str(p.get("quote", ""))[:110] or "—"} for p in problems[:25]],
            },
            effort="quick",
            owner_hint="RevOps",
        ))

    thin = [r for r in reps if not r["enough_evidence"]]
    if thin:
        doc.add(Finding(
            id="insufficient-calls-per-rep",
            severity="low",
            title=f"{len(thin)} rep{'s' if len(thin) != 1 else ''} had too few calls in the window to coach fairly",
            what=(f"Below {config.get('min_calls_per_rep', 3)} calls, one bad conversation moves a rep's score "
                  f"more than their actual habits do. These reps are reported but not ranked."),
            why_it_matters=("Coaching someone on a single call is how a manager loses the argument about whether "
                            "the number means anything, and the tool with it."),
            recommended_fix=("Either widen `window_days` in config, or check whether their calls are being "
                             "recorded at all — a rep with two recorded calls in a month usually has a recording "
                             "problem, not a volume problem."),
            evidence={
                "count": len(thin),
                "rows": [{"Rep": r["rep"], "Calls in window": r["calls"], "Tenure": r["tenure_band"],
                          "Coverage": f"{r['coverage_pct']}%" if r["coverage_pct"] is not None else "—"}
                         for r in thin],
            },
            effort="quick",
            owner_hint="Sales manager",
        ))

    # ---- Headline scores (max five).
    team_points = sum(d["points"] for d in dims)
    team_possible = sum(d["possible"] for d in dims)
    team_cov = _coverage(team_points, team_possible) or 0.0
    doc.add_score(Score(
        key="team_framework_coverage", label=f"{framework['name']} coverage", value=team_cov,
        unit="percent", direction_good="up",
        context=f"{int(team_possible/2)} scored dimension checks across {len(calls)} calls",
    ))
    doc.add_score(Score(
        key="weakest_dimension_coverage",
        label="Weakest dimension", value=(weakest["coverage_pct"] if weakest else 0),
        unit="percent", direction_good="up",
        context=(f"{weakest['label']} — missing on {weakest['missing']} of {weakest['scored_calls']} calls"
                 if weakest else "not enough scored calls to name one"),
    ))
    doc.add_score(Score(
        key="next_step_set_rate", label="Next step set", value=rate if rate is not None else 0,
        unit="percent", direction_good="up",
        context=f"target {target:.0f}% ({targets.get('source', 'industry default')})",
    ))
    doc.add_score(Score(
        key="calls_analyzed", label="Calls analysed", value=len(calls), unit="count", direction_good="up",
        context=(f"{len(eligible)} with reliable speaker attribution"
                 f"{f'; {len(degraded)} excluded from mechanics' if degraded else ''}"),
    ))
    return doc


# -------------------------------------------------------------------- main

def run(run_dir: Path, raw_dir: Path, config_path: Optional[Path], write_baseline: bool = True) -> Dict[str, Any]:
    profile = load_profile(required=False)
    if config_path:
        with Path(config_path).open("r", encoding="utf-8") as fh:
            config = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
        config = {**DEFAULTS, **config}
    else:
        config = load_plugin_config(PLUGIN, DEFAULTS)

    framework = resolve_framework(config)
    targets = {**DEFAULTS["mechanics_targets"], **(config.get("mechanics_targets") or {})}
    competitors = list(profile.get("competitors") or []) + list(config.get("competitors") or [])
    floor = config.get("material_deal_floor")
    if floor is None:
        floor = profile.get("material_deal_floor") or 0
    floor = float(floor)

    raw = load_raw(raw_dir, config)
    manifest = RunManifest(PLUGIN, run_dir)

    # Filter to the call types being coached, and out of the noise.
    wanted = {str(t).lower() for t in (config.get("call_types") or [])}
    min_duration = float(config.get("min_call_duration_sec", 0) or 0)
    calls: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for call in raw["calls"]:
        if wanted and str(call.get("call_type", "")).lower() not in wanted:
            skipped.append(f"{call['call_id']} (call type {call.get('call_type')!r} not coached)")
            continue
        if float(call.get("duration_sec") or 0) < min_duration:
            skipped.append(f"{call['call_id']} (shorter than {min_duration:.0f}s)")
            continue
        calls.append(call)

    scored_count = 0
    for call in calls:
        scored = raw["scored"].get(str(call["call_id"]))
        if scored:
            scored_count += 1
        call["scorecard"] = score_call(call, scored or {}, framework, config)
        call["mechanics"] = call_mechanics(call, config, competitors, scored)
        call["rep"] = call.get("rep") or (scored or {}).get("rep")

    # Provenance. Declared counts come from the fetch log the skill wrote;
    # observed counts come from what actually loaded. A mismatch is reported.
    declared = {s["name"]: s for s in (raw["fetch_log"].get("sources") or [])}
    window = raw["fetch_log"].get("window") or {}
    if not window:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(config.get("window_days", 30)))
        window = {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")}
    manifest.window = window

    def record(name: str, tool: str, count: int, required: bool, diagnosis: str, query: str = "") -> None:
        source = declared.get(name, {})
        manifest.record(name, tool=source.get("tool", tool), count=count, required=required,
                        query=source.get("query", query), diagnosis=source.get("diagnosis", diagnosis),
                        note=source.get("note", ""))
        if source and int(source.get("count", count)) != count:
            manifest.warn(f"{name}: the fetch log declared {source.get('count')} records, "
                          f"{count} were usable after parsing.")

    record("calls listed (transcript source)", "transcript source", len(raw["calls"]), True,
           "No calls came back. The connected identity may only be able to see its own recordings "
           "(a manager needs workspace or team scope), the window may predate retention, or the "
           "configured folder may be empty.")
    record("transcripts fetched", "transcript source", sum(1 for c in raw["calls"] if c.get("turns")), True,
           "Calls were listed but no transcript content parsed — recordings may exist without "
           "transcription enabled, or the export layout is one the parsers do not recognise. "
           "Run: python3 scripts/transcripts.py inspect <file>")
    record("framework scoring pass", "claude", scored_count, True,
           "Transcripts normalized but nothing was scored — the scoring step of the :run skill did not "
           "run, or scored_calls.json is malformed.")
    record("crm deals (optional linkage)", "crm.query", len(raw["deals"]), False,
           "Optional. Without it the report still scores every call; it just cannot correlate gaps "
           "against deals that slipped or lost.")
    for warning in raw["warnings"]:
        manifest.warn(warning)
    for note in skipped:
        manifest.warn(f"skipped {note}")
    manifest.finalize()

    as_of = datetime.now(timezone.utc)
    dims = dimension_rollup(calls, framework)
    reps = rep_rollup(calls, framework, config, as_of)
    linkage = deal_linkage(calls, raw["deals"], framework) if raw["deals"] else {
        "available": False, "reason": "no CRM connected in this run"}
    calib = calibration(calls, config.get("exemplar_call_ids") or [], framework)

    doc = build_findings({
        "config": config, "framework": framework, "calls": calls, "reps": reps,
        "dimensions": dims, "targets": targets, "material_deal_floor": floor,
        "window": window, "org_name": profile.get("org_name", ""),
        "linkage": linkage, "calibration": calib,
    })

    if not raw["deals"]:
        doc.unavailable.append("CRM deal linkage (no CRM records in this run)")
    if not calib.get("available"):
        doc.unavailable.append(f"exemplar calibration ({calib.get('reason')})")

    ranked = sorted([d for d in dims if d["coverage_pct"] is not None],
                    key=lambda d: (d["coverage_pct"], d["label"]))
    doc.sections = {
        "framework": {"name": framework["name"], "custom": framework["custom"],
                      "dimensions": framework["dimensions"]},
        "targets": {**targets, "labelled_as": targets.get("source", "industry default")},
        "team": {
            "coverage_pct": _coverage(sum(d["points"] for d in dims), sum(d["possible"] for d in dims)),
            "dimensions": dims,
            "weakest": ranked[0] if ranked else None,
            "strongest": ranked[-1] if ranked else None,
            "median_call_coverage_pct": median([c["scorecard"]["coverage_pct"] for c in calls
                                                if c["scorecard"]["coverage_pct"] is not None]),
        },
        "reps": reps,
        "calls": [{
            "call_id": c["call_id"], "title": c.get("title"), "account": c.get("account"),
            "rep": c.get("rep"), "date": (c.get("started_at") or "")[:10], "call_type": c.get("call_type"),
            "source": c.get("source"), "duration_min": round(float(c.get("duration_sec") or 0) / 60.0, 1),
            "deal_id": c.get("deal_id"), "deal_amount": c.get("deal_amount"),
            "coverage_pct": c["scorecard"]["coverage_pct"],
            "attribution": c.get("attribution"),
            "dimensions": c["scorecard"]["dimensions"],
            "next_step": c["scorecard"]["next_step"],
            "notable": c["scorecard"]["notable"],
            "attribution_caveat": c["scorecard"].get("attribution_caveat"),
            "mechanics": c["mechanics"],
            "is_exemplar": str(c["call_id"]) in {str(x) for x in (config.get("exemplar_call_ids") or [])},
        } for c in calls],
        "calibration": calib,
        "deal_linkage": linkage,
        "people": {
            "reps": sorted({c.get("rep") for c in calls if c.get("rep")}),
            "others": sorted({p.get("name") for c in calls for p in (c.get("participants") or [])
                              if p.get("name")}),
        },
        "coverage": {
            "calls_in_window": len(raw["calls"]),
            "calls_coached": len(calls),
            "calls_skipped": skipped,
            "calls_with_mechanics": sum(1 for c in calls if c["mechanics"]["eligible"]),
            "output_audience": config.get("output_audience", "manager"),
            "include_per_call_reviews": bool(config.get("include_per_call_reviews")),
        },
    }

    payload = doc.to_dict()
    apply_deltas(payload, PLUGIN)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "findings.json").open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.write("\n")
    if write_baseline:
        save_baseline(PLUGIN, payload)
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score coached calls and write findings.json.")
    parser.add_argument("--run-dir", required=True, help="the run directory; findings.json is written here")
    parser.add_argument("--raw", help="input raw/ directory (default <run-dir>/raw)")
    parser.add_argument("--config", help="explicit config file (default ~/.leanscale-gtm/sales-coach.json)")
    parser.add_argument("--no-baseline", action="store_true",
                        help="do not write a baseline snapshot (use for test runs against fixtures)")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    raw_dir = Path(args.raw) if args.raw else run_dir / "raw"

    try:
        payload = run(run_dir, raw_dir, Path(args.config) if args.config else None,
                      write_baseline=not args.no_baseline)
    except SourceEmptyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ConfigError, FileNotFoundError) as exc:
        print(f"sales-coach: {exc}", file=sys.stderr)
        return 1

    counts = payload["counts_by_severity"]
    print(f"findings.json written to {run_dir / 'findings.json'}")
    print(f"  {len(payload['findings'])} findings: "
          + " · ".join(f"{v} {k}" for k, v in counts.items() if v))
    for score in payload["scores"]:
        print(f"  {score['label']}: {score['value']}{'%' if score['unit'] == 'percent' else ''}"
              + (f" — {score['context']}" if score.get("context") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
