#!/usr/bin/env python3
"""
stage-architect / report.py

Layer 3. findings.json -> report.md + report.html, plus the baseline/delta cycle.

The shared renderer in lib/render.py draws the shell: scores, severity-sorted
findings, evidence tables, method footer. This file adds the tables that are
specific to stage architecture - the belief-vs-reality delta, the stage ladder
with an n beside every rate, adjacent-pair discrimination, dwell distributions,
and the proposed buyer-verifiable exit criteria - using the same markup and CSS
so the page reads as one document.

Order of operations matters:
    load findings -> apply_deltas (reads the previous baseline)
                  -> rewrite findings.json WITH deltas, unredacted
                  -> redact an in-memory copy if the profile asks for it
                  -> render -> save the new baseline

Usage
    python3 report.py --run-dir ./gtm-agents/stage-architect/2026-08-10-1430
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    BASELINE_RUN_NOTE,
    apply_deltas,
    load_manifest,
    load_profile,
    redact_name,
    render,
    save_baseline,
    write_reports,
)

PLUGIN = "stage-architect"


# --------------------------------------------------------------------- helpers


def _table(rows: List[Dict[str, Any]], limit: int = 60) -> str:
    """Reuse the shared table renderer so extra sections match the findings tables."""
    fn = getattr(render, "_table_html", None)
    if callable(fn):
        return fn(rows, limit=limit)
    if not rows:  # pragma: no cover - only if the shared lib drops the helper
        return ""
    headers = list(rows[0].keys())
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{row.get(h, '')}</td>" for h in headers) + "</tr>"
        for row in rows[:limit]
    )
    return f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _section(kicker: str, title: str, lede: str, body: str) -> str:
    if not body:
        return ""
    # Escape here rather than at each call site: several ledes interpolate customer CRM
    # values (a pipeline name, for one), and a stage or pipeline named with markup was
    # landing in the page verbatim — proven to inject a live <script> tag into a report
    # that gets handed to executives. `body` is already-rendered HTML from _table(),
    # which escapes its own cells, so it is the one argument that must pass through.
    return (
        f'<section><div class="sec-head"><div class="kick">{html.escape(str(kicker))}</div>'
        f'<h2>{html.escape(str(title))}</h2></div>'
        f'<p class="sub">{html.escape(str(lede))}</p>{body}</section>'
    )


def _n(value: Any, suffix: str = "") -> Any:
    return "-" if value is None else (f"{value}{suffix}" if suffix else value)


def _md_table(rows: List[Dict[str, Any]], limit: int = 60) -> List[str]:
    if not rows:
        return []
    headers = list(rows[0].keys())
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows[:limit]:
        out.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    if len(rows) > limit:
        out.append(f"\n_Showing {limit} of {len(rows)} rows - full set in findings.json._")
    return out + [""]


# ----------------------------------------------------------------- table builds


def belief_rows(sections: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for row in sections.get("belief_table") or []:
        gap = row.get("gap_pp")
        verdict = "-"
        if gap is not None:
            verdict = "as believed" if abs(gap) < 3 else (
                f"{abs(gap)} pts worse than believed" if gap < 0 else f"{abs(gap)} pts better than believed"
            )
        out.append({
            "Metric": row.get("metric"),
            "Team believes": _n(row.get("believed_pct"), "%"),
            "Measured": _n(row.get("measured_pct"), "%"),
            "n (resolved deals)": row.get("n"),
            "Verdict": verdict,
        })
    return out


def ladder_rows(sections: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "Stage": r.get("stage"),
        "Entered": r.get("n_entered"),
        "Resolved (n)": r.get("n_resolved"),
        "Still open here": r.get("n_still_open_here"),
        "Measured forward conv.": _n(r.get("forward_rate_pct"), "%"),
        "Win rate from here": _n(r.get("win_rate_from_pct"), "%"),
        "Snapshot rate (wrong)": _n(r.get("naive_rate_pct"), "%"),
        "Overstated by": _n(r.get("naive_inflation_pp"), " pp"),
        "Skipped by": _n(r.get("skip_rate_pct"), "%"),
    } for r in sections.get("stage_table") or []]


def dwell_rows(sections: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "Stage": r.get("stage"),
        "Completed dwells (n)": r.get("dwell_n"),
        "Median days": _n(r.get("dwell_median_days")),
        "p75 days": _n(r.get("dwell_p75_days")),
        "p90 days": _n(r.get("dwell_p90_days")),
        "Mean days (misleading)": _n(r.get("dwell_mean_days")),
        "Open deals here": r.get("currently_here"),
        "Median age of those": _n(r.get("open_here_median_age_days")),
    } for r in sections.get("stage_table") or []]


def pair_rows(sections: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "Earlier stage": r.get("stage_a"),
        "Earlier forward rate (n)": f"{_n(r.get('rate_a_pct'), '%')} (n={r.get('n_a')})",
        "Later stage": r.get("stage_b"),
        "Later forward rate (n)": f"{_n(r.get('rate_b_pct'), '%')} (n={r.get('n_b')})",
        "Difference": _n(r.get("difference_pp"), " pp"),
        "p-value": _n(r.get("p_value")),
        "Verdict": r.get("verdict"),
    } for r in sections.get("pair_table") or []]


def criteria_rows(sections: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "Stage": r.get("stage"),
        "Buyer-verifiable exit criterion": r.get("proposed_exit_criterion_buyer_verifiable"),
        "Artifact that proves it": r.get("proof_artifact"),
        "Replaces this rep-asserted version": r.get("replaces_this_rep_asserted_version"),
    } for r in sections.get("exit_criteria") or []]


def loss_rows(sections: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "Loss reason": r.get("reason"),
        "Deals": r.get("deals"),
        "Share of populated": _n(r.get("share_of_populated_pct"), "%"),
    } for r in sections.get("loss_reason_table") or []]


def lifecycle_rows(sections: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "Lifecycle stage": r.get("lifecycle_stage"),
        "Records": r.get("records"),
        "Share": _n(r.get("share_pct"), "%"),
    } for r in sections.get("lifecycle_table") or []]


# ----------------------------------------------------------------- composition


def gap_section_html(doc: Dict[str, Any]) -> str:
    """
    The deliverable. Rendered ABOVE the findings list, because the delta between
    what the team believes and what their own history says is the reason this
    plugin exists - not a footnote to a list of defects.
    """
    sections = doc.get("sections") or {}
    return _section(
        "The gap",
        "What the team believes, against what the history says",
        "Captured at setup before any measurement was shown. Every rate carries the number of "
        "resolved deals it was computed from. The delta is the deliverable; everything below "
        "explains where it comes from.",
        _table(belief_rows(sections)),
    )


def extra_sections_html(doc: Dict[str, Any]) -> str:
    sections = doc.get("sections") or {}
    method = sections.get("method") or {}
    totals = sections.get("totals") or {}
    out: List[str] = []

    out.append(_section(
        "The ladder",
        "Every stage, cohort-controlled by the stage deals ENTERED",
        method.get("cohort_rule", "") + " " + method.get("naive_rule", ""),
        _table(ladder_rows(sections)),
    ))

    out.append(_section(
        "Adjacent pairs",
        "Which stage boundaries actually change the odds",
        method.get("significance_rule", ""),
        _table(pair_rows(sections)),
    ))

    out.append(_section(
        "Time in stage",
        "Median, p75 and p90 - the mean is shown only to prove it lies",
        method.get("dwell_rule", ""),
        _table(dwell_rows(sections)),
    ))

    lifecycle = sections.get("lifecycle_summary") or {}
    if lifecycle:
        conversion = lifecycle.get("conversion_rate_pct")
        lede = (
            f"{lifecycle.get('sales_acceptance_rate_pct')}% of "
            f"{lifecycle.get('total_records', 0):,} records reached "
            f"{lifecycle.get('accepted_stage') or 'sales acceptance'}; "
            f"{lifecycle.get('died_at_or_before_acceptance_pct')}% died at or before it. "
            + (f"{conversion}% converted to an opportunity."
               if conversion is not None else
               "This CRM has no lead-conversion object, so conversion to an opportunity is not measurable here.")
        )
        out.append(_section("Lead lifecycle", "The funnel ahead of the funnel", lede,
                            _table(lifecycle_rows(sections))))

    loss = sections.get("loss_reason_summary") or {}
    if sections.get("loss_reason_table"):
        never = loss.get("never_used_values") or []
        lede = (
            f"{loss.get('field')} is populated on {loss.get('fill_rate_pct')}% of "
            f"{loss.get('lost_deals', 0):,} closed-lost deals."
            + (f" Never used: {', '.join(never)}." if never else "")
        )
        out.append(_section("Terminal integrity", "Why you lose, as recorded", lede,
                            _table(loss_rows(sections))))

    out.append(_section(
        "The fix",
        "Proposed exit criteria a buyer can verify",
        "A stage is only enterable when something outside your own CRM changed. Each criterion below "
        "names the artifact the buyer produced that proves it, and the rep-asserted sentence it replaces. "
        "Rep-asserted criteria are unfalsifiable, which is why they are always satisfied at quarter end.",
        _table(criteria_rows(sections), limit=200),
    ))

    if sections.get("other_pipelines"):
        out.append(_section(
            "Other pipelines", "Not analysed in depth",
            f"The full analysis ran on {method.get('primary_pipeline')}, the pipeline with the most deals.",
            _table([{
                "Pipeline": r.get("pipeline"), "Deals": r.get("deals"),
                "Share": _n(r.get("share_of_deals_pct"), "%"), "Open stages": r.get("stages"),
                "Win rate": _n(r.get("win_rate_pct"), "%"), "Closed deals": r.get("closed_deals"),
            } for r in sections["other_pipelines"]]),
        ))

    out.append(_section(
        "How this was computed",
        "The arithmetic, so you can argue with it",
        "Every rate in this report carries the n it was computed from. If a number here disagrees with "
        "your CRM's stock funnel report, the difference is almost always the survivorship correction below.",
        _table([
            {"Setting": "Analysis as-of date", "Value": method.get("as_of")},
            {"Setting": "Primary pipeline", "Value": method.get("primary_pipeline")},
            {"Setting": "Stage-history source", "Value": method.get("history_source")},
            {"Setting": "Deals with stage history", "Value": _n(method.get("history_coverage_pct"), "%")},
            {"Setting": "Opportunities analysed", "Value": totals.get("opportunities_analysed")},
            {"Setting": "Won / lost / open", "Value": f"{totals.get('won')} / {totals.get('lost')} / {totals.get('open')}"},
            {"Setting": "Minimum cohort for the equivalence test", "Value": method.get("min_cohort_size")},
            {"Setting": "Equivalence band", "Value": _n(method.get("equivalence_band_pp"), " pp")},
            {"Setting": "Significance alpha", "Value": method.get("significance_alpha")},
            {"Setting": "Material deal floor", "Value": method.get("material_deal_floor")},
            {"Setting": "Deals excluded below that floor", "Value": method.get("deals_below_material_floor_excluded")},
        ]),
    ))
    return "".join(part for part in out if part)


def gap_section_markdown(doc: Dict[str, Any]) -> str:
    sections = doc.get("sections") or {}
    lines = ["## The gap: what the team believes vs what the history says", ""]
    lines += _md_table(belief_rows(sections)) or ["_No believed rates were captured at setup._", ""]
    return "\n".join(lines) + "\n"


def extra_sections_markdown(doc: Dict[str, Any]) -> str:
    sections = doc.get("sections") or {}
    method = sections.get("method") or {}
    lines: List[str] = ["## The ladder", "", method.get("cohort_rule", ""), "", method.get("naive_rule", ""), ""]
    lines += _md_table(ladder_rows(sections))
    lines += ["## Adjacent-pair discrimination", "", method.get("significance_rule", ""), ""]
    lines += _md_table(pair_rows(sections))
    lines += ["## Time in stage", "", method.get("dwell_rule", ""), ""]
    lines += _md_table(dwell_rows(sections))
    if sections.get("lifecycle_table"):
        lines += ["## Lead lifecycle", ""] + _md_table(lifecycle_rows(sections))
    if sections.get("loss_reason_table"):
        lines += ["## Terminal integrity", ""] + _md_table(loss_rows(sections))
    lines += ["## Proposed exit criteria (buyer-verifiable)", ""]
    lines += _md_table(criteria_rows(sections), limit=200)
    return "\n".join(lines) + "\n"


def inject_html(base: str, gap: str, extra: str) -> str:
    """Gap goes above the findings list; the supporting detail goes below it."""
    if gap:
        base = base.replace("</header>", "</header>" + gap, 1)
    return base.replace("<footer>", extra + "<footer>", 1) if extra else base


def inject_markdown(base: str, gap: str, extra: str) -> str:
    if gap:
        marker = "\n## Findings\n"
        index = base.find(marker)
        base = base[:index] + "\n" + gap + base[index:] if index != -1 else gap + base
    tail = "\n---\n\nProduced by a LeanScale GTM Agent."
    index = base.rfind(tail)
    return base[:index] + "\n" + extra + base[index:] if index != -1 else base + "\n" + extra


# ------------------------------------------------------------------- redaction


def redact(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stable pseudonyms for person-shaped values in the rendered report only.
    raw/ and findings.json stay unredacted on the customer's own machine.
    """
    person_keys = ("owner", "rep", "user", "contact", "name")

    def scrub(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, key) for v in value]
        if isinstance(value, str) and any(p in key.lower() for p in person_keys):
            return redact_name(value)
        return value

    copy = json.loads(json.dumps(doc))
    for finding in copy.get("findings", []):
        finding["evidence"] = scrub(finding.get("evidence") or {})
    copy["sections"] = scrub(copy.get("sections") or {})
    return copy


# ------------------------------------------------------------------------ main


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="stage-architect: findings.json -> report.md/.html")
    parser.add_argument("--run-dir", required=True, help="Run directory holding findings.json.")
    parser.add_argument("--no-save-baseline", action="store_true",
                        help="Render without recording a new baseline snapshot.")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        parser.error(f"No findings.json in {run_dir}. Run analyze.py first.")

    with findings_path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)

    doc = apply_deltas(doc, PLUGIN)
    with findings_path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
        fh.write("\n")

    profile = load_profile(required=False)
    rendered = redact(doc) if profile.get("redact_pii_in_reports") else doc

    manifest = load_manifest(run_dir)
    paths = write_reports(rendered, run_dir, manifest)
    paths["html"].write_text(
        inject_html(
            paths["html"].read_text(encoding="utf-8"),
            gap_section_html(rendered),
            extra_sections_html(rendered),
        ),
        encoding="utf-8",
    )
    paths["markdown"].write_text(
        inject_markdown(
            paths["markdown"].read_text(encoding="utf-8"),
            gap_section_markdown(rendered),
            extra_sections_markdown(rendered),
        ),
        encoding="utf-8",
    )

    if not args.no_save_baseline:
        snapshot = save_baseline(PLUGIN, doc)
    else:
        snapshot = None

    if doc.get("is_baseline_run"):
        print(f"BASELINE RUN. {BASELINE_RUN_NOTE}")
    else:
        print(f"Compared against the snapshot taken {doc.get('compared_to')}.")
        for score in doc.get("scores", []):
            delta = score.get("delta_vs_last")
            if delta not in (None, 0):
                print(f"  {score['label']}: {'+' if delta > 0 else ''}{delta} vs last run")

    print(f"  report.md   -> {paths['markdown']}")
    print(f"  report.html -> {paths['html']}")
    if snapshot:
        print(f"  baseline    -> {snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
