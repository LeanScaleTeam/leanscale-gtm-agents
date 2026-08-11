#!/usr/bin/env python3
"""
report.py — findings.json -> report.md + report.html, with the baseline diff.

Layer 3. Pure stdlib, offline. Rendering is the shared core renderer so this
report is visually identical to every other agent in the suite; this file adds
exactly two extra sections that the shared envelope has nowhere to put:

  1. How the Source Integrity Score was computed — every component, its weight,
     its reweighted contribution, and which components could not be measured.
     A score whose formula is not on the page is a vibe with a decimal point.
  2. The full canonical taxonomy mapping proposal — the complete old -> new
     table, not the 25-row preview the findings section shows. This is the
     artefact a customer actually works from, and it is a proposal: nothing has
     been merged, and every row carries the record count behind it.

Baseline handling follows the suite contract: run one takes a snapshot and says
so in plain words; every run after it shows movement against the last snapshot.
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
    render_html,
    render_markdown,
    save_baseline,
)

PLUGIN = "lead-source"

SCORE_METHOD_INTRO = (
    "The Source Integrity Score is a weighted mean of up to five components, each on a 0-100 "
    "scale. Components that could not be measured in this run are dropped and the remaining "
    "weights are rescaled to sum to 1 — so the score is always stated alongside which "
    "components went into it. Bands: 85+ trustworthy · 70-84 usable with caveats · "
    "50-69 directional at best · under 50 the channel report is fiction."
)

MAPPING_INTRO = (
    "Every row below is a PROPOSAL awaiting human confirmation. Nothing was merged and nothing "
    "in your CRM was changed. The record count is shown for each value so you can judge the "
    "blast radius before agreeing to anything, and the evidence tier tells you how the link was "
    "made: <b>exact</b> and <b>similar</b> are typographic (two spellings of one string), "
    "<b>synonym</b> and <b>subset</b> are semantic and need a human who owns the channel "
    "definitions to rule on them."
)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _table(rows: Sequence[Dict[str, Any]], limit: int = 400) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(r.get(h, ''))}</td>" for h in headers) + "</tr>"
        for r in rows[:limit]
    )
    more = (f'<div class="meta">Showing {limit} of {len(rows):,} rows — the full set is in '
            f'findings.json.</div>' if len(rows) > limit else "")
    return f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def inject_section(page: str, kicker: str, title: str, intro: str,
                   rows: Sequence[Dict[str, Any]]) -> str:
    """Add a section before the footer, reusing the shared renderer's own classes
    so it reads as one document rather than a bolted-on annex."""
    block = (f'<section><div class="sec-head"><div class="kick">{_e(kicker)}</div>'
             f'<h2>{_e(title)}</h2></div><p class="sub">{intro}</p>{_table(rows)}</section>\n')
    marker = "<footer>"
    if marker not in page:
        return page + block
    return page.replace(marker, block + marker, 1)


def append_markdown(doc_md: str, title: str, intro: str, rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return doc_md
    headers = list(rows[0].keys())
    lines = [f"## {title}", "", intro.replace("<b>", "**").replace("</b>", "**"), "",
             "| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows[:400]:
        lines.append("| " + " | ".join(str(row.get(h, "")).replace("|", "\\|") for h in headers) + " |")
    lines.append("")
    marker = "\n---\n"
    body = "\n".join(lines)
    if marker in doc_md:
        head, _, tail = doc_md.rpartition(marker)
        return head + "\n" + body + marker + tail
    return doc_md + "\n" + body


def score_rows(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    detail = (doc.get("sections") or {}).get("integrity_score") or {}
    rows = list(detail.get("components") or [])
    if rows:
        rows.append({"Component": "TOTAL", "Weight": "100%",
                     "Score": str(detail.get("score", "")),
                     "Contribution": str(detail.get("band", ""))})
    return rows


def mapping_rows(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(((doc.get("sections") or {}).get("taxonomy") or {}).get("proposed_mapping") or [])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render the lead-source report from findings.json.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--no-baseline", action="store_true",
                        help="Render without recording a new baseline snapshot (useful for re-renders)")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        print(f"No findings.json in {run_dir}. Run analyze.py first.", file=sys.stderr)
        return 2

    with findings_path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)

    doc = apply_deltas(doc, PLUGIN)
    manifest = load_manifest(run_dir)

    page = render_html(doc, manifest)
    page = inject_section(page, "Method", "How the Source Integrity Score was computed",
                          SCORE_METHOD_INTRO, score_rows(doc))
    mapping = mapping_rows(doc)
    if mapping:
        page = inject_section(page, "Proposal", "Canonical taxonomy mapping — for your confirmation",
                              MAPPING_INTRO, mapping)

    markdown = render_markdown(doc, manifest)
    markdown = append_markdown(markdown, "How the Source Integrity Score was computed",
                               SCORE_METHOD_INTRO, score_rows(doc))
    markdown = append_markdown(markdown, "Canonical taxonomy mapping — for your confirmation",
                               MAPPING_INTRO, mapping)

    (run_dir / "report.html").write_text(page, encoding="utf-8")
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    with findings_path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
        fh.write("\n")

    snapshot = None
    if not args.no_baseline:
        snapshot = save_baseline(PLUGIN, doc)

    print(f"report.html · report.md -> {run_dir}")
    if doc.get("is_baseline_run"):
        print(f"\nBASELINE RUN. {BASELINE_RUN_NOTE}\n")
    elif doc.get("compared_to"):
        print(f"Compared against the snapshot taken {doc['compared_to']}.")
        for score in doc.get("scores", []):
            delta = score.get("delta_vs_last")
            if delta not in (None, 0):
                print(f"  {score['label']}: {'+' if delta > 0 else ''}{delta} vs last run")
    if snapshot:
        print(f"Snapshot saved: {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
