#!/usr/bin/env python3
"""
Semantic Layer drafts — raw/*.json + config -> a draft semantic layer to react to.

Run one should end with metric files a human can correct, not a findings list a
human has to act on from scratch. This script turns the discovery snapshot into
draft YAML for the three core metrics, choosing a defensible value for every
blank and recording each choice as an explicit, numbered assumption.

The rule: a value the customer already decided (config) is used silently. A
value the data can only suggest is drafted from a stated heuristic and marked
ASSUMPTION — in the file header, in DRAFTS.md, and in assumptions.json. The
interview then walks the assumptions, not a blank page.

    draft.py --raw <dir> --out <run-dir>

Stdlib only. Read-only against the CRM: it reads the snapshot the run skill
wrote and writes draft files into the run directory. Nothing else.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import FRAMEWORK_MARKERS, _is_second_motion, _rate  # noqa: E402
from lib import load_plugin_config  # noqa: E402
from lib.config import load_profile  # noqa: E402

PLUGIN = "gtm-brain"

# Currency fields that are computed from other fields, never entered as bookings.
DERIVED_MARKERS = ("expected", "weighted", "forecast", "probability")

# Stage labels that suggest "this is where qualification happens".
QUALIFIED_MARKERS = ("qualif", "sql", "sal", "discovery", "use case", "evaluation")

# Stage labels that look like parks — outcomes to decide about, not funnel stages.
PARK_MARKERS = ("nurture", "hold", "disqualif", "dormant", "parked", "recycle")

# One row per drafted metric: (metric, template). METRICS derives from it so a
# fourth metric is added in exactly one place.
PLAN = (
    ("win_rate", "metric.yml"),
    ("cycle_time", "cycle_time.yml"),
    ("pipeline_created", "pipeline_created.yml"),
)
METRICS = tuple(m for m, _ in PLAN)


def _yaml_str(value: Any) -> str:
    """Escape a value for interpolation inside a double-quoted YAML scalar."""
    return json.dumps(str(value))[1:-1]


def _fill_label(value: Any) -> str:
    """fill_rate arrives as a float, null, or absent — never print 'None'."""
    if isinstance(value, (int, float)):
        return f"fill rate {value}"
    return "fill rate unmeasured"


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------

class Decision:
    """One value the drafts depend on: either decided (config) or assumed."""

    def __init__(self, key: str, value: Any, source: str, why: str,
                 question: str = "", alternatives: Optional[List[Dict[str, Any]]] = None,
                 metrics: Optional[List[str]] = None) -> None:
        self.key = key
        self.value = value
        self.source = source            # "config" | "heuristic"
        self.why = why
        self.question = question
        self.alternatives = alternatives or []
        self.metrics = metrics or list(METRICS)
        self.aid = 0                    # assigned for assumptions only

    @property
    def assumed(self) -> bool:
        return self.source != "config"

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": f"A{self.aid}" if self.assumed else None,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "why": self.why,
            "question": self.question,
            "alternatives": self.alternatives,
            "metrics": self.metrics,
            "status": "unconfirmed" if self.assumed else "decided_in_setup",
        }


def _decide_funnel(stages: List[Dict[str, Any]], config: Dict[str, Any]) -> Decision:
    """Which stages are out of scope for this pass (second motions, parks)."""
    configured = config.get("excluded_stages") or []
    if configured:
        return Decision("excluded_stages", configured, "config",
                        "Recorded in setup.")
    won_sort = next((s.get("sort") for s in stages if s.get("is_won")), None)
    detected = [s["value"] for s in stages if _is_second_motion(s, won_sort)]

    def is_park(s: Dict[str, Any]) -> bool:
        return any(m in s["value"].lower() for m in PARK_MARKERS)

    parks = [s["value"] for s in stages if is_park(s) and s["value"] not in detected]
    if detected:
        why = ("These stages sit after Closed Won while still open, or name a "
               "renewal/expansion motion — they look like a second funnel "
               "sharing the picklist.")
        question = ("Are these stages a separate motion to exclude from this "
                    "pass, or part of the funnel being defined?")
    else:
        why = ("No second motion detected in the stage picklist. If a renewal "
               "or expansion funnel exists elsewhere, it is not visible here.")
        question = ("Does a renewal or expansion motion live outside this "
                    "picklist?")
    if parks:
        question += (" And %s look(s) like a park, not a funnel stage — do "
                     "deals sitting there count in the win-rate denominator?"
                     % ", ".join("'%s'" % p for p in parks))

    def alt(s: Dict[str, Any]) -> Dict[str, Any]:
        note = " · looks like a park — decide if it counts in win rate" \
            if is_park(s) else ""
        return {"value": s["value"], "evidence": f"sort {s.get('sort')}{note}"}

    ordered = sorted(stages, key=lambda s: (not is_park(s), s.get("sort", 0)))
    return Decision(
        "excluded_stages", detected, "heuristic", why,
        question=question,
        alternatives=[alt(s) for s in ordered],
    )


def _decide_qualified(stages: List[Dict[str, Any]], excluded: List[str],
                      config: Dict[str, Any]) -> Decision:
    """Which stage's ENTRY means qualified. The fight, so always assumption #1
    unless setup already settled it."""
    configured = (config.get("qualified_stage") or "").strip()
    funnel = [s for s in stages
              if s["value"] not in excluded and not s.get("is_closed")]
    alternatives = [{"value": s["value"], "evidence": f"sort {s.get('sort')}"}
                    for s in funnel]
    if configured:
        return Decision("qualified_stage", configured, "config",
                        "Recorded in setup.", alternatives=alternatives)

    named = [s for s in funnel
             if any(m in s["value"].lower() for m in QUALIFIED_MARKERS)]
    if named:
        pick = min(named, key=lambda s: s.get("sort", 0))
        why = (f"'{pick['value']}' is the earliest open stage whose name "
               "suggests qualification. The stage before it reads as funnel "
               "entry, not acceptance.")
    elif len(funnel) > 1:
        pick = sorted(funnel, key=lambda s: s.get("sort", 0))[1]
        why = ("No stage name signals qualification, so this drafts the "
               "second open stage — the first is usually funnel entry, not "
               "sales acceptance.")
    elif funnel:
        pick = funnel[0]
        why = "Only one open stage exists, so it is the only candidate."
    else:
        return Decision("qualified_stage", "", "heuristic",
                        "No open stages found — the picklist is all terminal "
                        "values, which is its own finding.",
                        question="Which stage's entry means an opportunity is "
                                 "qualified?",
                        alternatives=alternatives)
    return Decision(
        "qualified_stage", pick["value"], "heuristic", why,
        question="Does entry into this stage mean 'sales accepted it' — an "
                 "observable event, not a rep's opinion? If qualification "
                 "actually happens at a different stage, name it.",
        alternatives=alternatives,
    )


def _decide_bookings(currency: List[Dict[str, Any]], config: Dict[str, Any]) -> Decision:
    """Which currency field means bookings."""
    configured = (config.get("bookings_amount_field") or "").strip()

    def is_framework(c: Dict[str, Any]) -> bool:
        return any(m in c["name"].lower() for m in FRAMEWORK_MARKERS)

    def is_derived(c: Dict[str, Any]) -> bool:
        return any(m in c["name"].lower() for m in DERIVED_MARKERS)

    candidates = [c for c in currency if not is_framework(c) and not is_derived(c)]
    ranked = sorted(
        candidates,
        key=lambda c: (-_rate(c), 0 if c["name"].lower() == "amount" else 1,
                       len(c["name"])),
    )
    alternatives = [{"value": c["name"], "evidence": _fill_label(c.get("fill_rate"))}
                    for c in ranked[:5]]
    if configured:
        return Decision("bookings_amount_field", configured, "config",
                        "Recorded in setup.", alternatives=alternatives)
    if not ranked:
        return Decision("bookings_amount_field", "Amount", "heuristic",
                        "No usable currency field found after excluding "
                        "framework and derived fields; falling back to the "
                        "CRM standard.",
                        question="Which field means bookings?",
                        alternatives=alternatives)
    pick = ranked[0]
    skipped = len(currency) - len(candidates)
    why = (f"Highest-filled of {len(candidates)} candidates after excluding "
           f"{skipped} derived or framework field(s) (Weighted_*, Expected*, "
           "BANT/MEDD scratch are never bookings).")
    return Decision(
        "bookings_amount_field", pick["name"], "heuristic", why,
        question="Is this the number the board calls bookings? If finance "
                 "reports a different field, that field wins — name it.",
        alternatives=alternatives,
    )


def _stage_date_field(stages: List[Dict[str, Any]], stage_dates: List[Dict[str, Any]],
                      stage_value: str) -> Optional[Dict[str, Any]]:
    """Match a stage to its entry-timestamp field: by label, then by position."""
    if not stage_value:
        return None          # startswith("") matches everything — never guess here
    for f in stage_dates:
        label = (f.get("label") or "").strip().lower()
        if label and (label == stage_value.lower()
                      or stage_value.lower().startswith(label)
                      or label.startswith(stage_value.lower())):
            return f
    idx = next((i for i, s in enumerate(sorted(stages, key=lambda s: s.get("sort", 0)))
                if s["value"] == stage_value), None)
    if idx is not None:
        for f in stage_dates:
            digits = "".join(ch for ch in f["name"] if ch.isdigit())
            if digits and int(digits) == idx:
                return f
    return None


def _decide_clock(stages: List[Dict[str, Any]], stage_dates: List[Dict[str, Any]],
                  qualified: str, crm: str) -> Dict[str, Decision]:
    """Cycle-time clock: start at qualified entry, stop at the close."""
    start = _stage_date_field(stages, stage_dates, qualified)
    if start:
        start_dec = Decision(
            "clock_start_field", start["name"], "heuristic",
            f"'{start['name']}' is the entry timestamp matching "
            f"'{qualified}' ({_fill_label(start.get('fill_rate'))}).",
            question="Is this field written by automation when the deal "
                     "enters the stage — never typed by a human?",
        )
    else:
        start_dec = Decision(
            "clock_start_field",
            f"<entry timestamp for {qualified or 'the qualified stage'}>",
            "heuristic",
            f"No stage-entry timestamp field matches '{qualified}'. Cycle "
            "time cannot be computed until one is instrumented; you measure "
            "from the day it ships.",
            question="Which field records (or will record) entry into the "
                     "qualified stage?",
        )

    won = next((s["value"] for s in stages if s.get("is_won")), "Closed Won")
    stop = _stage_date_field(stages, stage_dates, won)
    default_close = "CloseDate" if crm != "hubspot" else "closedate"
    stop_dec = Decision(
        "clock_stop_field", stop["name"] if stop else default_close, "heuristic",
        (f"'{stop['name']}' is the entry timestamp for '{won}'." if stop
         else f"No entry timestamp exists for '{won}'; drafting the standard "
              f"{default_close}, which reps can edit."),
        question="Should the clock stop at the system-stamped won timestamp "
                 "or the close date reps maintain?",
        metrics=["cycle_time"],
    )
    return {"start": start_dec, "stop": stop_dec}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def _caveat_lines(items: List[str]) -> str:
    # json.dumps quoting keeps the YAML valid whatever the CRM values contain
    return "\n".join("  - %s" % json.dumps(c) for c in items)


def _header(assumed: List[Decision], generated_at: str) -> str:
    lines = [
        f"# DRAFT — generated from your org's schema on {generated_at}.",
        "# Seeded from measured stages, fields and fill rates — not invented.",
    ]
    if assumed:
        lines.append(f"# {len(assumed)} value(s) below rest on assumptions. "
                     "Confirm them in DRAFTS.md before adopting this file:")
        for d in assumed:
            lines.append(f"#   {('A%d' % d.aid)} · {d.key} = {json.dumps(d.value)} — {d.why}")
    else:
        lines.append("# Every value here was decided in setup. Review and adopt.")
    lines.append("")
    return "\n".join(lines)


def _render(template: str, subs: Dict[str, str], extra_caveats: List[str]) -> str:
    text = (_templates_dir() / template).read_text(encoding="utf-8")
    # the hand-fill guidance is for humans copying the template — rendered
    # output has no placeholders left, so the comment would just confuse
    text = "\n".join(
        l for l in text.splitlines()
        if not l.startswith("# Double-brace placeholders")
        and not l.startswith("# are filled by the draft step")) + "\n"
    for key, value in subs.items():
        text = text.replace("{{%s}}" % key, value)
    caveats = _caveat_lines(extra_caveats)
    if caveats:
        text = text.replace("{{EXTRA_CAVEATS}}", caveats)
    else:
        text = text.replace("{{EXTRA_CAVEATS}}\n", "").replace("{{EXTRA_CAVEATS}}", "")
    return text


CONTEXT_TEMPLATES = ("icp.md", "selling-motion.md", "style-guide.md", "competitive.md")


def _bullet_block(items: List[str], empty_note: str) -> str:
    if not items:
        return f"<!-- {empty_note} -->"
    return "\n".join(f"- {i}" for i in items)


def _draft_context(run_dir: Path, describe: Dict[str, Any], config: Dict[str, Any],
                   profile: Dict[str, Any], org_name: str) -> List[Path]:
    """Draft the commercial-context half of the Brain from what setup already knows.

    Pre-fills only what a system actually recorded (segment picklists, the
    profile's motions and competitors); everything else stays an explicit FILL
    section for the working sessions — a context file must never invent facts
    about the business.
    """
    seg_lines: List[str] = []
    for field, values in (describe.get("segment_picklists") or {}).items():
        seg_lines.append(f"Your CRM's `{field}` picklist today: "
                         + ", ".join(str(v) for v in values))
    subs = {
        "ORG_NAME": org_name or "Your",
        "SEGMENTS_BLOCK": _bullet_block(
            seg_lines, "no segment picklist found in the CRM — define tiers below"),
        "MOTIONS_BLOCK": _bullet_block(
            [str(m) for m in (profile.get("motion") or [])],
            "no motions recorded in profile.json — list them below"),
        "COMPETITORS_BLOCK": _bullet_block(
            [str(c) for c in (profile.get("competitors") or config.get("competitors") or [])],
            "no competitors recorded in profile.json — list them below"),
    }
    written: List[Path] = []
    ctx_dir = run_dir / "draft" / "gtm-brain" / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    for name in CONTEXT_TEMPLATES:
        text = (_templates_dir() / "context" / name).read_text(encoding="utf-8")
        for key, value in subs.items():
            text = text.replace("{{%s}}" % key, value)
        path = ctx_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    claude_md = (_templates_dir() / "CLAUDE.md").read_text(encoding="utf-8")
    for key, value in subs.items():
        claude_md = claude_md.replace("{{%s}}" % key, value)
    path = run_dir / "draft" / "gtm-brain" / "CLAUDE.md"
    path.write_text(claude_md, encoding="utf-8")
    written.append(path)
    return written


def _drafts_md(decisions: List[Decision], org_name: str, generated_at: str) -> str:
    assumed = [d for d in decisions if d.assumed]
    decided = [d for d in decisions if not d.assumed]
    out: List[str] = [
        f"# Draft semantic layer — review sheet{(' — ' + org_name) if org_name else ''}",
        "",
        f"Generated {generated_at}. Three metrics are drafted in "
        "`gtm-brain/semantic/metrics/`. Every blank was filled with the "
        "most defensible value the schema supports; the ones that need a "
        "human are numbered below. **This sheet is the interview agenda:** "
        "walk it top to bottom, confirm or correct each assumption, and the "
        "drafts become your definitions.",
        "",
    ]
    if decided:
        out.append("## Already decided in setup")
        out.append("")
        for d in decided:
            out.append(f"- `{d.key}` = `{json.dumps(d.value)}`")
        out.append("")
    out.append(f"## Assumptions to confirm ({len(assumed)})")
    out.append("")
    for d in assumed:
        out.append(f"### A{d.aid} · {d.key} → `{json.dumps(d.value)}`")
        out.append("")
        out.append(f"**Why this draft:** {d.why}")
        out.append("")
        if d.question:
            out.append(f"**Confirm:** {d.question}")
            out.append("")
        if d.alternatives:
            out.append("| Alternative | Evidence |")
            out.append("|---|---|")
            for alt in d.alternatives[:8]:
                out.append(f"| `{alt['value']}` | {alt.get('evidence', '')} |")
            out.append("")
        out.append(f"Used by: {', '.join('`%s`' % m for m in d.metrics)}")
        out.append("")
        out.append("- [ ] Confirmed as drafted   /   corrected to: ______")
        out.append("")
    out.append("---")
    out.append("")
    out.append("When every box is ticked: each metric still needs **one named "
               "human owner** (not a team), and the drafts get promoted into "
               "the `gtm-brain/` repo with CODEOWNERS enforcing that name "
               "on every future change. The run is not done until that repo "
               "exists.")
    out.append("")
    out.append("**Also drafted — the commercial-context half of the Brain** "
               "(`gtm-brain/context/`): icp.md, selling-motion.md, "
               "style-guide.md, competitive.md, plus the repo's CLAUDE.md "
               "enforcement rules. These are pre-filled with what the CRM and "
               "profile already recorded; their FILL sections are working-"
               "session material, and each needs a named owner just like a "
               "metric.")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def draft(raw: Path, run_dir: Path, config: Dict[str, Any],
          profile: Dict[str, Any]) -> Dict[str, Any]:
    describe_path = raw / "describe.json"
    if not describe_path.exists():
        print(f"draft: no describe snapshot at {describe_path}. The describe "
              "call returned nothing or was never made — re-run "
              "/gtm-brain:setup --check. Refusing to draft metrics from "
              "an empty schema.", file=sys.stderr)
        raise SystemExit(3)
    describe = json.loads(describe_path.read_text(encoding="utf-8"))

    # normalize before anything trusts the shapes: a hand-built snapshot can
    # carry stage entries without "value" — skip them rather than KeyError
    stages: List[Dict[str, Any]] = [
        s for s in describe.get("stages", {}).get("values", [])
        if isinstance(s, dict) and s.get("value")]
    currency: List[Dict[str, Any]] = [
        c for c in describe.get("currency_fields", [])
        if isinstance(c, dict) and c.get("name")]
    stage_dates: List[Dict[str, Any]] = [
        f for f in describe.get("stage_date_fields", [])
        if isinstance(f, dict) and f.get("name")]
    stage_field = describe.get("stages", {}).get("field", "StageName")
    opp_object = describe.get("stages", {}).get("object", "Opportunity")
    crm = ((profile.get("crm") or {}).get("system")
           or ("hubspot" if stage_field == "dealstage" else "salesforce"))
    org_name = config.get("org_name") or profile.get("org_name") or ""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    funnel_dec = _decide_funnel(stages, config)
    excluded = funnel_dec.value if isinstance(funnel_dec.value, list) else []
    qualified_dec = _decide_qualified(stages, excluded, config)
    bookings_dec = _decide_bookings(currency, config)
    clock = _decide_clock(stages, stage_dates, qualified_dec.value or "", crm)

    decisions = [qualified_dec, bookings_dec, funnel_dec,
                 clock["start"], clock["stop"]]
    aid = 0
    for d in decisions:
        if d.assumed:
            aid += 1
            d.aid = aid
    assumed = [d for d in decisions if d.assumed]

    # shared caveats computed from the org, not asserted from habit
    shared_caveats: List[str] = []
    if excluded:
        shared_caveats.append(
            "Excludes stages: %s. The excluded motion needs its own "
            "definitions later." % ", ".join(excluded))
    stamps = config.get("stage_timestamps") or {}
    primary_stamp = stamps.get("primary_funnel", "")
    if primary_stamp and primary_stamp != "system_stamped":
        # only assert what setup actually probed — an empty config is unknown,
        # not "absent"
        shared_caveats.append(
            "Stage timestamps are '%s', not system-stamped: history cannot "
            "be backfilled, so time-based metrics measure from adoption "
            "forward." % primary_stamp)

    cycle_caveats = list(shared_caveats)
    for role, dec in (("start", clock["start"]), ("stop", clock["stop"])):
        field = dec.value
        matched = next((f for f in stage_dates if f["name"] == field), None)
        if matched and isinstance(matched.get("fill_rate"), (int, float)) \
                and matched["fill_rate"] < 0.9:
            cycle_caveats.append(
                "%s (clock %s) is %.0f%% filled: deals without it fall out "
                "of the median, so early numbers describe a biased sample." %
                (field, role, matched["fill_rate"] * 100))

    # the excluded-stage filter must appear IN the filter line, where a human
    # can see it — a caveat alone is the two-motion blend the skill forbids
    if excluded:
        quoted = ", ".join("'%s'" % s for s in excluded)
        excluded_filter = " and %s not in (%s)" % (stage_field, quoted)
    else:
        excluded_filter = ""

    subs = {
        "QUALIFIED_STAGE": _yaml_str(qualified_dec.value or "FIXME"),
        "QUALIFIED_STAGE_DATE_FIELD": _yaml_str(clock["start"].value),
        "BOOKINGS_AMOUNT_FIELD": _yaml_str(bookings_dec.value),
        "STAGE_FIELD": _yaml_str(stage_field),
        "CRM_SYSTEM": _yaml_str(crm),
        "OPP_OBJECT": _yaml_str(opp_object),
        "CLOCK_STOP_FIELD": _yaml_str(clock["stop"].value),
        "EXCLUDED_FILTER": _yaml_str(excluded_filter),
    }

    metrics_dir = run_dir / "draft" / "gtm-brain" / "semantic" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    caveats_by_metric = {"win_rate": shared_caveats, "cycle_time": cycle_caveats,
                         "pipeline_created": shared_caveats}
    written: List[Path] = []
    for metric, template in PLAN:
        caveats = caveats_by_metric[metric]
        relevant = [d for d in assumed if metric in d.metrics]
        body = _render(template, subs, caveats)
        path = metrics_dir / f"{metric}.yml"
        path.write_text(_header(relevant, generated_at) + body, encoding="utf-8")
        written.append(path)

    written.extend(_draft_context(run_dir, describe, config, profile, org_name))

    drafts_md = run_dir / "draft" / "DRAFTS.md"
    drafts_md.write_text(_drafts_md(decisions, org_name, generated_at),
                         encoding="utf-8")
    written.append(drafts_md)

    # the single machine-readable record of the draft. report.py reads it at
    # render time — draft.py never writes findings.json, which stays owned by
    # analyze.py, so any re-run order produces the same report.
    ledger = {
        "plugin": PLUGIN,
        "generated_at": generated_at,
        "metrics_drafted": list(METRICS),
        "context_drafted": list(CONTEXT_TEMPLATES),
        "assumptions_open": len(assumed),
        "decisions": [d.to_json() for d in decisions],
    }
    ledger_path = run_dir / "draft" / "assumptions.json"
    ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    written.append(ledger_path)

    return {"written": written, "assumptions": len(assumed)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Draft the three core metrics from the discovery snapshot")
    ap.add_argument("--raw", required=True,
                    help="directory of raw/*.json written by the run skill")
    ap.add_argument("--out", required=True,
                    help="run directory to write draft/ into")
    ap.add_argument("--config", help="explicit config file; "
                                     "default is ~/.leanscale-gtm/gtm-brain.json")
    args = ap.parse_args(argv)

    raw, run_dir = Path(args.raw), Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            config = json.load(fh)
    else:
        config = load_plugin_config(PLUGIN, defaults={})
    profile = load_profile(required=False) or {}

    result = draft(raw, run_dir, config, profile)
    for p in result["written"]:
        print(f"draft written: {p}")
    print(f"  assumptions to confirm: {result['assumptions']} "
          "(draft/DRAFTS.md is the agenda)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
