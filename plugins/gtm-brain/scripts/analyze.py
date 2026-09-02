#!/usr/bin/env python3
"""
Semantic Layer readiness — raw/*.json -> findings.json

Reads the describe snapshot the run skill wrote and reports what stands between
this org and a semantic layer it can defend. Every finding is something a human
has to decide, not something an engineer has to build — which is the whole point.

    analyze.py --raw <dir> --out <run-dir>

Stdlib only. Read-only: it touches nothing but the files it is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    apply_deltas,
    load_plugin_config,
)

PLUGIN = "gtm-brain"

# Stage labels that signal a motion other than new business sharing one picklist.
RENEWAL_MARKERS = ("renewal", "expansion", "upsell", "cross-sell", "churn", "review complete")

# Currency fields that are qualification-framework scratch, never bookings.
FRAMEWORK_MARKERS = ("bant", "champ", "medd", "anum", "gpctba", "spiced", "scotsman")


def measured(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Only the fields whose fill rate was actually measured.

    Fill rate comes from `crm.query`, which is optional AND partial: a probe may
    measure six fields out of twenty-seven. Absent is not zero. Treating it as zero
    reports untouched fields as empty — a confident lie, and precisely the failure
    this plugin exists to prevent.

    Every fill-rate check must run over THIS list, never the full list, and must say
    how many fields it could not see. Filtering by "did any field get measured" is
    not enough; that is the same bug one level down.
    """
    return [f for f in fields if f.get("fill_rate") is not None]


def has_fill_rates(fields: List[Dict[str, Any]]) -> bool:
    return bool(measured(fields))


def _rate(field: Dict[str, Any]) -> float:
    return field.get("fill_rate") or 0.0


def _read(raw: Path, name: str) -> Optional[Any]:
    p = raw / name
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


def _is_second_motion(stage: Dict[str, Any], won_sort: Optional[int]) -> bool:
    """
    Does this stage belong to a motion other than the new-business funnel?

    Sorting after Closed Won is NOT sufficient. Closed Lost almost always sorts
    after Closed Won, and so do terminal parks like Nurture or Disqualified —
    they are outcomes of the SAME funnel, not a different one. Flagging them as
    a second motion is a false positive on a high-severity finding, which is
    worse than missing a real one.

    A stage is a second motion when it is still OPEN and sorts past the won
    stage (a renewal funnel continues after a win), or when its label names a
    different motion outright.
    """
    if any(m in stage["value"].lower() for m in RENEWAL_MARKERS):
        return True
    if stage.get("is_closed"):
        return False          # terminal outcome of this funnel, not another motion
    return won_sort is not None and stage.get("sort", 0) > won_sort


def analyze(raw: Path, run_dir: Path, config: Dict[str, Any]) -> FindingsDoc:
    window = {"label": "current org schema"}
    man = RunManifest(PLUGIN, run_dir, window=window)

    describe = _read(raw, "describe.json")
    # Count what the schema actually CONTAINS, not whether the file parsed. The
    # realistic permission failure is an API returning HTTP 200 with a well-formed
    # but empty schema because the connected identity cannot see the object — a
    # truthy dict. Counting `1 if describe else 0` let that through the fail-loud
    # gate, so the run exited 0 and published a confident CRITICAL about a schema
    # it had never seen. SPEC §5 exists to stop exactly this.
    _schema_elements = 0
    if isinstance(describe, dict):
        _schema_elements = (
            len((describe.get("stages") or {}).get("values") or [])
            + len(describe.get("currency_fields") or [])
            + len(describe.get("stage_date_fields") or [])
            + len(describe.get("source_channel_fields") or [])
            + len(describe.get("segment_picklists") or {})
        )
    man.record(
        "describe",
        tool="crm.describe",
        count=_schema_elements,
        query="object describe: Opportunity (StageName picklist, currency fields, date fields)",
        required=True,
        diagnosis="The describe call returned no schema — no stages, no currency fields, "
                  "no stage-date fields. Either no crm.describe tool resolved, or the "
                  "connected identity can reach the object but cannot see its fields, "
                  "which is what a permissions gap looks like from here: a valid, empty "
                  "answer. Re-run /gtm-brain:setup --check to see which.",
    )

    doc = FindingsDoc(plugin=PLUGIN, window=window,
                      org_name=config.get("org_name", ""),
                      generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    if not _schema_elements:
        man.finalize()  # raises — a readiness report with no schema is not a report
        return doc

    stages: List[Dict[str, Any]] = describe.get("stages", {}).get("values", [])
    currency: List[Dict[str, Any]] = describe.get("currency_fields", [])
    stage_dates: List[Dict[str, Any]] = describe.get("stage_date_fields", [])

    # ---- 1. mixed stage set --------------------------------------------------
    won_sort = next((s.get("sort") for s in stages if s.get("is_won")), None)
    post_close = [s["value"] for s in stages
                  if _is_second_motion(s, won_sort)]
    if post_close:
        doc.add(Finding(
            id="mixed-stage-set",
            severity="high",
            title="One stage picklist is carrying more than one motion",
            what=f"{len(post_close)} stage(s) sit after Closed Won or name a renewal/expansion "
                 f"motion: {', '.join(post_close)}. They share a picklist with the new-business funnel.",
            why_it_matters="A win rate computed across this picklist blends new business with "
                           "renewals, which convert at completely different rates. The number is "
                           "meaningless and nobody can see why by looking at it.",
            recommended_fix="Pick one funnel to define first. Record the others as excluded "
                            "stages and filter them explicitly in every metric, where a human "
                            "can see the filter.",
            evidence={"count": len(post_close), "rows": [{"stage": s} for s in post_close]},
            effort="quick",
            owner_hint="RevOps",
        ))

    # ---- 2. no agreed bookings field ----------------------------------------
    if currency:
        framework = [c for c in currency
                     if any(m in c["name"].lower() for m in FRAMEWORK_MARKERS)]
        candidates = [c for c in currency if c not in framework]
        agreed = (config.get("bookings_amount_field") or "").strip()
        if not agreed:
            rated = has_fill_rates(candidates)
            top = (sorted(candidates, key=_rate, reverse=True)[:3] if rated
                   else candidates[:3])
            doc.add(Finding(
                id="no-agreed-bookings-field",
                severity="high",
                title=f"{len(currency)} currency fields, and none is agreed as bookings",
                what=f"The opportunity object carries {len(currency)} currency fields "
                     f"({len(framework)} of them qualification-framework amounts that are not "
                     f"revenue). No definition names which one means bookings.",
                why_it_matters="Every revenue metric silently picks one. Two people picking "
                               "differently is how two dashboards disagree and both get defended.",
                recommended_fix="Name one field as bookings, in writing, with an owner. "
                                + (f"Highest-filled candidates: " if rated
                                   else "Candidates (fill rates unavailable — no query tool resolved): ")
                                + f"{', '.join(c['name'] for c in top) or 'none'}.",
                evidence={"count": len(currency),
                          "rows": [{"field": c["name"], "fill_rate": c.get("fill_rate")}
                                   for c in (sorted(candidates, key=_rate, reverse=True)
                                             if rated else candidates)[:10]]},
                effort="quick",
                owner_hint="CFO or RevOps",
            ))

        measured_cur = measured(candidates)
        unmeasured_cur = len(candidates) - len(measured_cur)
        if unmeasured_cur:
            doc.unavailable.append(
                f"Currency field fill rates — {unmeasured_cur} of {len(candidates)} "
                f"field(s) were not measured, so they are excluded from dead-field "
                f"detection. Absent is not zero: reporting them as empty would be a "
                f"guess, and one of them may be the field you actually use."
            )
        dead = [c for c in measured_cur if _rate(c) < 0.05]
        if len(dead) >= 3:
            doc.add(Finding(
                id="dead-currency-fields",
                severity="low",
                title=f"{len(dead)} currency fields are effectively empty",
                what=f"{len(dead)} of the {len(measured_cur)} measured currency field(s) "
                     f"are populated on under 5% of records.",
                why_it_matters="Empty fields widen the menu when someone is choosing which "
                               "amount to use, and every one of them is a wrong answer.",
                recommended_fix="Exclude them from the definition conversation, and consider "
                                "deprecating them once the semantic layer names the real one.",
                evidence={"count": len(dead),
                          "rows": [{"field": c["name"], "fill_rate": c.get("fill_rate")}
                                   for c in dead[:10]]},
                effort="medium",
                owner_hint="CRM admin",
            ))

    # ---- 3. stage timestamp coverage ----------------------------------------
    if not stage_dates:
        doc.add(Finding(
            id="no-stage-timestamps",
            severity="critical",
            title="No per-stage timestamps — cycle time is unmeasurable",
            what="No stage-entry datetime fields were found on the opportunity object.",
            why_it_matters="If a human can type the date a deal entered a stage, cycle time "
                           "cannot be trusted. You cannot backfill a timestamp that was never "
                           "written, so history is gone.",
            recommended_fix="Instrument stage entry and exit as system-stamped fields now. "
                            "You start measuring from today; the past is not recoverable.",
            evidence={"count": 0, "value": "no stage date fields present"},
            effort="project",
            owner_hint="CRM admin",
        ))
    else:
        covered = len(stage_dates)
        total = len([s for s in stages if not _is_second_motion(s, won_sort)]) or covered
        if post_close:
            doc.add(Finding(
                id="stage-timestamps-partial",
                severity="medium",
                title="Stage timestamps stop before the second motion",
                what=f"{covered} stage-entry datetime field(s) exist, covering the "
                     f"new-business funnel. The {len(post_close)} renewal/expansion stage(s) "
                     f"have none.",
                why_it_matters="Cycle time is measurable for one motion and not the other. "
                               "A team that does not know this will ask for renewal cycle time "
                               "and get a number built on nothing.",
                recommended_fix="Either instrument the second motion's stages, or state in the "
                                "cycle-time definition that it covers new business only.",
                evidence={"count": covered,
                          "rows": [{"field": f["name"], "fill_rate": f.get("fill_rate")}
                                   for f in stage_dates]},
                effort="medium",
                owner_hint="CRM admin",
            ))
        measured_sd = measured(stage_dates)
        unmeasured_sd = len(stage_dates) - len(measured_sd)
        if unmeasured_sd:
            doc.unavailable.append(
                f"Stage timestamp fill rates — {unmeasured_sd} of {len(stage_dates)} "
                f"field(s) were not measured. Whether a stage field is actually populated "
                f"is the difference between a cycle time you can trust and one computed "
                f"over a biased sample. Unmeasured, not clean."
            )
        thin = [f for f in measured_sd if _rate(f) < 0.5]
        if thin:
            doc.add(Finding(
                id="stage-timestamps-thin",
                severity="high",
                title=f"{len(thin)} of {len(measured_sd)} measured stage timestamp "
                      f"field(s) are under 50% populated",
                what="Stage-entry fields exist but are sparsely filled, which usually means "
                     "they are typed by a human or written by an automation that was added late.",
                why_it_matters="A cycle-time metric computed over half-populated timestamps is "
                               "computed over a biased sample of deals, not over the pipeline.",
                recommended_fix="Confirm what writes these fields. If a rep can edit them, "
                                "cycle time measures compliance, not velocity.",
                evidence={"count": len(thin),
                          "rows": [{"field": f["name"], "fill_rate": f.get("fill_rate")}
                                   for f in thin]},
                effort="medium",
                owner_hint="RevOps",
            ))

    # ---- 4. source vs channel ------------------------------------------------
    sc = describe.get("source_channel_fields", [])
    if len(sc) < 2:
        doc.add(Finding(
            id="source-channel-conflated",
            severity="medium",
            title="Source and channel look like one field",
            what=f"Only {len(sc)} source/channel field(s) found: "
                 f"{', '.join(f['name'] for f in sc) or 'none'}.",
            why_it_matters="Source is immutable first touch; channel is what marketing spends "
                           "against and changes. Conflate them and you can never restate history.",
            recommended_fix="Split into two fields — one immutable, one mutable — before the "
                            "attribution definitions are written.",
            evidence={"count": len(sc), "value": [f["name"] for f in sc]},
            effort="medium",
            owner_hint="Marketing ops",
        ))

    # ---- scores --------------------------------------------------------------
    blocking = sum(1 for f in doc.findings if f.severity in ("critical", "high"))
    doc.add_score(Score(
        key="decisions_outstanding", label="Definition decisions outstanding",
        value=len(doc.findings), unit="count", direction_good="down",
        context="Each one is a human decision, not an engineering task.",
    ))
    doc.add_score(Score(
        key="blocking", label="Blocking before metrics can be trusted",
        value=blocking, unit="count", direction_good="down",
        context="Critical and high findings. Settle these before generating definitions.",
    ))
    doc.add_score(Score(
        key="fiscal_year_start", label="Fiscal year starts", unit="month",
        value=describe.get("fiscal_year_start_month", "unknown"),
        direction_good="flat", context="Never assume January.",
    ))

    doc.sections["stages_read"] = [s["value"] for s in stages]
    man.finalize()
    return doc


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Semantic layer readiness analysis")
    ap.add_argument("--raw", required=True,
                    help="directory of raw/*.json written by the run skill")
    ap.add_argument("--out", required=True,
                    help="run directory to write findings.json + manifest.json")
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

    doc = analyze(raw, run_dir, config)
    # SPEC §0 Rule 2: run one is the baseline, run two shows what moved. This plugin
    # implemented neither half, so every run announced itself as the baseline forever
    # and the readiness score could never be shown to improve — on the one plugin
    # whose entire pitch is that you re-run it and watch the number move.
    payload = apply_deltas(doc.to_dict(), PLUGIN)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "findings.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    counts = doc.counts_by_severity()
    print(f"findings written: {path}")
    print(f"  critical={counts['critical']} high={counts['high']} "
          f"medium={counts['medium']} low={counts['low']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
