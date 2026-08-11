#!/usr/bin/env python3
"""
pipeline-inspection / report.py

Layer 3. Reads findings.json, attaches the delta against the previous baseline,
renders report.md + report.html with the shared LeanScale renderer, appends the
plugin's structured detail (the measured stage medians, the push distribution,
the clustering histogram, the rep scorecard), and writes call-list.csv — the
file a manager actually works from on a Monday.

Offline, stdlib only. Nothing here touches the network or the CRM.

Usage:
    python3 report.py --run-dir ./gtm-agents/pipeline-inspection/2026-08-10-0900
    python3 report.py --run-dir /tmp/demo --no-baseline
"""

from __future__ import annotations

import argparse
import csv
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
    save_baseline,
    write_reports,
)
from lib.crmutil import redact_name  # noqa: E402

PLUGIN = "pipeline-inspection"

# Columns whose contents are person names when redaction is on.
PERSON_COLUMNS = ("Owner", "Rep", "Contact", "Champion")


# --------------------------------------------------------------------------- redaction


def redact_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pseudonymise people for the rendered report only. findings.json and raw/ stay
    unredacted on disk — the customer needs to act on real names locally; it is the
    forwarded PDF that leaks.
    """
    def scrub_rows(rows: Any) -> Any:
        if not isinstance(rows, list):
            return rows
        out = []
        for row in rows:
            if not isinstance(row, dict):
                out.append(row)
                continue
            clean = dict(row)
            for column in list(clean.keys()):
                if any(column.startswith(p) for p in PERSON_COLUMNS):
                    value = str(clean[column])
                    clean[column] = " / ".join(redact_name(part.strip()) for part in value.split("/"))
            out.append(clean)
        return out

    for finding in doc.get("findings", []):
        evidence = finding.get("evidence") or {}
        if "rows" in evidence:
            evidence["rows"] = scrub_rows(evidence["rows"])
    sections = doc.get("sections") or {}
    for key in ("call_list", "call_list_full", "owner_scorecard"):
        if key in sections:
            sections[key] = scrub_rows(sections[key])
    if isinstance(sections.get("push_distribution"), dict):
        sections["push_distribution"]["by_owner"] = scrub_rows(sections["push_distribution"].get("by_owner"))
    return doc


# ----------------------------------------------------------------------------- tables


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def html_table(rows: Sequence[Dict[str, Any]], limit: int = 40) -> str:
    if not rows:
        return '<p class="sub">Nothing to show for this section.</p>'
    headers = list(rows[0].keys())
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(row.get(h, ''))}</td>" for h in headers) + "</tr>" for row in rows[:limit]
    )
    more = (
        f'<div class="meta">Showing {limit} of {len(rows):,} rows — the full set is in findings.json '
        f"and call-list.csv.</div>"
        if len(rows) > limit
        else ""
    )
    return f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def md_table(rows: Sequence[Dict[str, Any]], limit: int = 40) -> str:
    if not rows:
        return "_Nothing to show for this section._\n"
    headers = list(rows[0].keys())
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(h, "")).replace("|", "/") for h in headers) + " |")
    if len(rows) > limit:
        lines.append(f"\n_Showing {limit} of {len(rows):,} rows — full set in call-list.csv._")
    return "\n".join(lines) + "\n"


def html_section(kicker: str, title: str, blurb: str, body: str) -> str:
    return (
        f'<section><div class="sec-head"><div class="kick">{_e(kicker)}</div>'
        f"<h2>{_e(title)}</h2></div>"
        + (f'<p class="sub">{_e(blurb)}</p>' if blurb else "")
        + body
        + "</section>"
    )


def md_section(title: str, blurb: str, body: str) -> str:
    return f"## {title}\n\n" + (f"{blurb}\n\n" if blurb else "") + body + "\n"


# ---------------------------------------------------------------------------- content


def build_supplements(doc: Dict[str, Any]) -> Dict[str, str]:
    """Returns {'call_list_html', 'sections_html', 'sections_md'}."""
    sections = doc.get("sections") or {}
    totals = sections.get("totals") or {}
    medians = sections.get("stage_medians") or {}
    pushes = sections.get("push_distribution") or {}
    clustering = sections.get("close_date_clustering") or {}
    contacts = sections.get("contact_roles") or {}
    cycle = sections.get("cycle_time") or {}
    coverage = sections.get("coverage") or {}
    thresholds = sections.get("thresholds_used") or {}

    call_list = sections.get("call_list") or []
    call_blurb = (
        f"{totals.get('flagged_deals', 0):,} of {totals.get('open_deals', 0):,} open deals broke at least "
        "one rule. Ranked by how many rules each deal breaks and how severe they are, then by amount. "
        "Work it top-down; the full list is in call-list.csv."
    )
    call_html = html_section(
        "Start here", "The call list", call_blurb, html_table(call_list, limit=30)
    )

    parts: List[str] = []
    md: List[str] = []

    # --- measured medians. Most teams have never seen these numbers.
    measurement = medians.get("measurement") or {}
    median_blurb = (
        (medians.get("note") or "")
        + f" Measured from {measurement.get('intervals_measured', 0):,} completed stage intervals"
        + (
            f"; {measurement.get('inferred_share_pct', 0)}% of those had their start inferred from the "
            "record's create date."
            if measurement.get("inferred_share_pct")
            else "."
        )
    )
    parts.append(
        html_section(
            "Your numbers, not a benchmark",
            "Measured days in stage",
            median_blurb,
            html_table(medians.get("rows") or []),
        )
    )
    md.append(md_section("Measured days in stage", median_blurb, md_table(medians.get("rows") or [])))

    # --- push distribution
    push_coverage = pushes.get("coverage") or {}
    push_blurb = (
        "Close-date history read from "
        + (", ".join(f"{k} ({v:,} rows)" for k, v in (push_coverage.get("shapes") or {}).items()) or "no source")
        + ". Oldest change seen: "
        + str(push_coverage.get("oldest_change_seen") or "none")
        + ". Salesforce retains field history for 18 months (24 with Field Audit Trail) and HubSpot "
        "truncates very old property versions, so a deal older than that window will under-report its "
        "pushes — never over-report."
    )
    parts.append(
        html_section("Highest-signal check", "How often close dates move", push_blurb,
                     html_table(pushes.get("rows") or []) + html_table(pushes.get("by_owner") or [], limit=20))
    )
    md.append(
        md_section("How often close dates move", push_blurb,
                   md_table(pushes.get("rows") or []) + "\n" + md_table(pushes.get("by_owner") or [], limit=20))
    )

    # --- clustering
    cluster_blurb = (
        f"Where the {clustering.get('dated_deals', 0):,} dated open deals land inside the month. A healthy "
        "distribution is roughly flat; a spike on the last day means the date is a placeholder."
    )
    parts.append(html_section("Placeholder detector", "Close-date distribution", cluster_blurb,
                              html_table(clustering.get("rows") or [])))
    md.append(md_section("Close-date distribution", cluster_blurb, md_table(clustering.get("rows") or [])))

    # --- contact roles
    contact_blurb = "How many contacts are attached to open deals, and how that compares to your size-based bar."
    parts.append(
        html_section("Single-threading", "Contacts per deal", contact_blurb,
                     html_table(contacts.get("rows") or []) + html_table(contacts.get("by_band") or []))
    )
    md.append(
        md_section("Contacts per deal", contact_blurb,
                   md_table(contacts.get("rows") or []) + "\n" + md_table(contacts.get("by_band") or []))
    )

    # --- cycle time
    cycle_blurb = (
        f"Median create-to-won cycle: {cycle.get('median_days_create_to_won') or 'not measurable'} days "
        f"across {cycle.get('won_deals_measured', 0):,} won deals (same-day backfills excluded). The table "
        "is what the remaining runway looks like from each stage — the basis for the "
        "'closing faster than history allows' check."
    )
    parts.append(html_section("Time, measured", "Cycle time from each stage", cycle_blurb,
                              html_table(cycle.get("rows") or [])))
    md.append(md_section("Cycle time from each stage", cycle_blurb, md_table(cycle.get("rows") or [])))

    # --- rep scorecard
    scorecard = sections.get("owner_scorecard") or []
    parts.append(
        html_section("Accountability", "By rep",
                     "Same rules, per owner. Read it as a coaching queue, not a leaderboard.",
                     html_table(scorecard, limit=30))
    )
    md.append(md_section("By rep", "Same rules, per owner.", md_table(scorecard, limit=30)))

    # --- coverage + method
    coverage_rows = []
    if coverage.get("period_label"):
        coverage_rows.append(
            {
                "Period": coverage["period_label"],
                "From": coverage.get("period_start"),
                "To": coverage.get("period_end"),
                "Deals closing in period": coverage.get("deals_in_period"),
                "Pipeline in period": f"${coverage.get('pipeline_in_period', 0):,}",
                "Quota": f"${coverage['quota']:,}" if coverage.get("quota") else "not supplied",
                "Coverage": coverage.get("coverage_ratio") or "—",
            }
        )
    if coverage_rows:
        parts.append(
            html_section("Period", "Coverage for the current fiscal quarter",
                         "Quarter boundaries come from the fiscal year start month in your shared profile.",
                         html_table(coverage_rows))
        )
        md.append(md_section("Coverage for the current fiscal quarter", "", md_table(coverage_rows)))

    threshold_rows = [{"Setting": k, "Value": json.dumps(v) if isinstance(v, (list, dict)) else v}
                      for k, v in thresholds.items()]
    scope_rows = [{"Metric": k.replace("_", " "), "Value": v} for k, v in totals.items()]
    parts.append(
        html_section(
            "Audit", "What was inspected and against which thresholds",
            "Every threshold below is editable in ~/.leanscale-gtm/pipeline-inspection.json, or by "
            "re-running /pipeline-inspection:setup.",
            html_table(scope_rows, limit=40) + html_table(threshold_rows, limit=40),
        )
    )
    md.append(
        md_section("What was inspected", "", md_table(scope_rows, limit=40) + "\n" + md_table(threshold_rows, limit=40))
    )

    return {"call_list_html": call_html, "sections_html": "\n".join(parts), "sections_md": "\n".join(md)}


def splice_html(source: str, call_list_html: str, sections_html: str) -> str:
    if "</header>" in source:
        source = source.replace("</header>", "</header>\n" + call_list_html, 1)
    else:
        source = call_list_html + source
    # Keep the shared renderer's "what this run actually read" method table last — the
    # plugin's own detail sections belong with the findings, not after the provenance.
    method_anchor = '<section><div class="sec-head"><div class="kick">Method</div>'
    for anchor in (method_anchor, "<footer>"):
        if anchor in source:
            return source.replace(anchor, sections_html + "\n" + anchor, 1)
    return source + sections_html


def splice_markdown(source: str, sections_md: str) -> str:
    # Same ordering as the HTML: detail sections with the findings, provenance last.
    for marker in ("## Method\n", "---\n\nProduced by a LeanScale GTM Agent."):
        if marker in source:
            head, tail = source.split(marker, 1)
            return head + sections_md + "\n" + marker + tail
    return source + "\n" + sections_md


# -------------------------------------------------------------------------------- csv


def write_call_list_csv(doc: Dict[str, Any], run_dir: Path) -> Optional[Path]:
    rows = (doc.get("sections") or {}).get("call_list_full") or []
    if not rows:
        return None
    path = run_dir / "call-list.csv"
    headers = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# ------------------------------------------------------------------------------- main


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render the pipeline inspection report.")
    parser.add_argument("--run-dir", required=True, help="Run directory holding findings.json.")
    parser.add_argument("--no-baseline", action="store_true", help="Render without saving a new baseline snapshot.")
    parser.add_argument("--force-baseline", action="store_true", help="Save a snapshot even if this run already has one.")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser()
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        print(f"error: {findings_path} not found. Run analyze.py first.", file=sys.stderr)
        return 2

    with findings_path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)

    doc = apply_deltas(doc, PLUGIN)

    # Persist the delta flags back into findings.json so a second render is consistent.
    with findings_path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
        fh.write("\n")

    profile = load_profile(required=False)
    render_doc = json.loads(json.dumps(doc))
    if profile.get("redact_pii_in_reports"):
        render_doc = redact_doc(render_doc)

    manifest = load_manifest(run_dir)
    paths = write_reports(render_doc, run_dir, manifest)

    supplements = build_supplements(render_doc)
    html_path, md_path = paths["html"], paths["markdown"]
    html_path.write_text(
        splice_html(html_path.read_text(encoding="utf-8"), supplements["call_list_html"], supplements["sections_html"]),
        encoding="utf-8",
    )
    md_path.write_text(
        splice_markdown(md_path.read_text(encoding="utf-8"), supplements["sections_md"]), encoding="utf-8"
    )

    csv_path = write_call_list_csv(render_doc, run_dir)

    marker = run_dir / ".baseline-saved"
    saved = None
    if not args.no_baseline and (args.force_baseline or not marker.exists()):
        saved = save_baseline(PLUGIN, doc)
        marker.write_text(str(saved) + "\n", encoding="utf-8")

    scores = {s["key"]: s for s in doc.get("scores", [])}
    print(f"pipeline-inspection report · {doc.get('org_name') or 'your organization'}")
    if doc.get("is_baseline_run"):
        print("\n  BASELINE RUN — " + BASELINE_RUN_NOTE + "\n")
    elif doc.get("compared_to"):
        print(f"  compared against the snapshot taken {doc['compared_to']}")
    for key in ("inspection_score", "at_risk_amount", "flagged_pct", "past_due_amount", "coverage_ratio"):
        score = scores.get(key)
        if not score:
            continue
        delta = score.get("delta_vs_last")
        trend = "" if delta in (None, 0) else f"  ({'+' if delta > 0 else ''}{delta} vs last run)"
        print(f"  {score['label']}: {score['value']}{trend}")
    print(f"  report:    {html_path}")
    print(f"  markdown:  {md_path}")
    if csv_path:
        print(f"  call list: {csv_path}")
    if saved:
        print(f"  baseline:  {saved}")
    if render_doc.get("unavailable"):
        print("  not covered this run:")
        for item in render_doc["unavailable"]:
            print(f"    · {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
