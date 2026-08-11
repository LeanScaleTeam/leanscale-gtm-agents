#!/usr/bin/env python3
"""
customer-health / report.py — findings.json -> report.md + report.html

Layer 3. Offline, Python 3.9+ standard library only.

Three jobs:

  1. Baseline diff. Compare this run's scores against the last snapshot, attach
     deltas, flag run one as the baseline run, then save a fresh snapshot.
  2. Render through the shared library so all nine agents look like one product.
  3. Splice in the two sections this plugin owns and the shared renderer does not
     know about: the SENTIMENT x COMMERCIAL RISK QUADRANT, and MOVEMENT SINCE
     KICKOFF. Both use the shared stylesheet's own classes — no second design
     system, no external requests.

Usage
    python3 report.py --run-dir ./gtm-agents/customer-health/2026-08-10-0900
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    BASELINE_RUN_NOTE,
    apply_deltas,
    load_manifest,
    render_html,
    render_markdown,
    save_baseline,
)

PLUGIN = "customer-health"

# Where each quadrant sits in the 2x2. Row 1 is happy, row 2 is unhappy;
# column 1 is at-risk, column 2 is safe. The dangerous cell is top-left, which
# is exactly where a blended health score would have shown you nothing.
GRID = [
    ("happy_but_exposed", "Happy but exposed", "High sentiment · high commercial risk", True),
    ("healthy", "Healthy", "High sentiment · low commercial risk", False),
    ("burning", "Burning", "Low sentiment · high commercial risk", False),
    ("grumbling_but_safe", "Grumbling but safe", "Low sentiment · low commercial risk", False),
]

KICKOFF_EXPLAINER = (
    "The kickoff baseline is the starting reading captured for each account when the "
    "relationship began — sentiment, ARR and engaged contacts on day one. It is the only "
    "thing that turns a health score into evidence. \"They are at 64\" is a number; "
    "\"they started at 41 and they are at 64\" is a renewal conversation. Accounts without "
    "a kickoff baseline are listed as unprovable, not as fine."
)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _num(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.0f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


# ------------------------------------------------------------------ quadrant (html)


def quadrant_html(doc: Dict[str, Any]) -> str:
    quad = (doc.get("sections") or {}).get("quadrant")
    if not quad:
        return ""
    counts, arr, accounts = quad["counts"], quad["arr"], quad["accounts"]
    definition = quad["definition"]

    cells: List[str] = []
    for key, label, axis, danger in GRID:
        listing = accounts.get(key, [])
        border = "#8a1c3b" if danger and counts.get(key) else "var(--line)"
        bg = "#fdeaf0" if danger and counts.get(key) else "#fff"
        rows = "".join(
            "<tr>"
            f"<td>{_e(a['account'])}</td>"
            f"<td>{_e(_money(a['arr']))}</td>"
            f"<td>{_e(_num(a['sentiment']))}</td>"
            f"<td>{_e(_num(a['risk']))}</td>"
            f"<td>{_e(a.get('renewal') or 'unknown')}</td>"
            f"<td>{'signed' if a.get('signed') else 'UNSIGNED' if a.get('signed') is not None else 'unknown'}</td>"
            "</tr>"
            for a in listing[:12]
        )
        table = (
            '<div class="tblwrap"><table><thead><tr><th>Account</th><th>ARR</th>'
            "<th>Sentiment</th><th>Risk</th><th>Renewal</th><th>Paper</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
            if rows
            else '<div class="meta">No accounts in this quadrant.</div>'
        )
        cells.append(
            f'<div class="find" style="margin:0;border-color:{border};background:{bg}">'
            f'<div class="find-top"><h3>{_e(label)}</h3>'
            f'<span class="pill" style="color:var(--purple);background:var(--soft)">'
            f"{counts.get(key, 0)} · {_e(_money(arr.get(key, 0)))}</span></div>"
            f'<div class="meta" style="border:0;padding:0;margin:0 0 8px">{_e(axis)}</div>'
            f"{table}</div>"
        )

    extra = ""
    if counts.get("commercial_only"):
        listing = accounts.get("commercial_only", [])
        rows = "".join(
            "<tr>"
            f"<td>{_e(a['account'])}</td><td>{_e(_money(a['arr']))}</td>"
            f"<td>{_e(_num(a['risk']))}</td><td>{_e(a.get('renewal') or 'unknown')}</td>"
            "</tr>"
            for a in listing[:20]
        )
        extra = (
            '<div class="note warn" style="margin-top:16px"><b>'
            f"{counts['commercial_only']} accounts ({_e(_money(arr.get('commercial_only', 0)))}) "
            "sit outside the grid entirely</b> — no call transcript or shared message thread was "
            "available, so only the commercial half of the model ran. They are <b>unmeasured, not "
            "healthy</b>. Do not read their absence from the quadrant as a pass."
            '</div><div class="tblwrap"><table><thead><tr><th>Account</th><th>ARR</th>'
            f"<th>Commercial risk</th><th>Renewal</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )

    return f"""
<section>
  <div class="sec-head"><div class="kick">The quadrant</div>
  <h2>Sentiment against commercial risk, never blended</h2></div>
  <p class="sub" style="margin:0 0 18px">{_e(definition['why'])}
  An account reads happy at sentiment {_e(definition['sentiment_floor'])} or above, and at risk at
  a commercial-risk score of {_e(definition['risk_threshold'])} or above.</p>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px">
    {''.join(cells)}
  </div>
  {extra}
</section>
"""


def kickoff_html(doc: Dict[str, Any]) -> str:
    section = (doc.get("sections") or {}).get("kickoff_baseline")
    if not section:
        return ""
    movement = section.get("movement", [])
    missing = section.get("missing", [])

    rows = "".join(
        "<tr>"
        f"<td>{_e(m['account'])}</td>"
        f"<td>{_e(m.get('kickoff_date') or '—')}</td>"
        f"<td>{_e(_num(m.get('sentiment_at_kickoff')))} → {_e(_num(m.get('sentiment_now')))}</td>"
        f"<td>{_e('—' if m.get('sentiment_delta') is None else ('+' if m['sentiment_delta'] > 0 else '') + str(m['sentiment_delta']))}</td>"
        f"<td>{_e(_money(m.get('arr_at_kickoff')))} → {_e(_money(m.get('arr_now')))}</td>"
        f"<td>{_e('—' if m.get('arr_delta_pct') is None else str(m['arr_delta_pct']) + '%')}</td>"
        "</tr>"
        for m in movement
    )
    table = (
        '<div class="tblwrap"><table><thead><tr><th>Account</th><th>Kickoff</th>'
        "<th>Sentiment</th><th>Move</th><th>ARR</th><th>Change</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        if rows
        else '<div class="note warn"><b>No kickoff baseline has been captured for any account.</b> '
        "Nothing in this report can prove movement yet. Re-run <code>/customer-health:setup</code> "
        "and fill in the kickoff block — it takes ten minutes per account and it is the difference "
        "between a score and an argument.</div>"
    )
    warn = ""
    if missing:
        warn = (
            f'<div class="note warn" style="margin-top:14px"><b>{len(missing)} accounts have no '
            "kickoff baseline</b> and are therefore unprovable: " + _e(", ".join(missing[:20])) + ".</div>"
        )
    return f"""
<section>
  <div class="sec-head"><div class="kick">Movement since kickoff</div>
  <h2>What a year of work actually changed</h2></div>
  <p class="sub" style="margin:0 0 18px">{_e(KICKOFF_EXPLAINER)}</p>
  {table}
  {warn}
</section>
"""


def inject_html(document: str, blocks: str) -> str:
    """Splice our sections in ahead of the shared renderer's findings section."""
    if not blocks:
        return document
    marker = '<div class="kick">Findings</div>'
    idx = document.find(marker)
    if idx > -1:
        start = document.rfind("<section>", 0, idx)
        if start > -1:
            return document[:start] + blocks + document[start:]
    tail = document.rfind("<footer>")
    if tail > -1:
        return document[:tail] + blocks + document[tail:]
    return document + blocks


# ------------------------------------------------------------------ quadrant (md)


def quadrant_markdown(doc: Dict[str, Any]) -> str:
    quad = (doc.get("sections") or {}).get("quadrant")
    if not quad:
        return ""
    counts, arr, accounts = quad["counts"], quad["arr"], quad["accounts"]
    lines = [
        "## The quadrant — sentiment against commercial risk",
        "",
        quad["definition"]["why"],
        "",
        f"Happy is sentiment ≥ {quad['definition']['sentiment_floor']}; at risk is a commercial-risk "
        f"score ≥ {quad['definition']['risk_threshold']}.",
        "",
        "| Quadrant | Accounts | ARR |",
        "|---|---|---|",
    ]
    for key, label, _axis, _danger in GRID:
        lines.append(f"| {label} | {counts.get(key, 0)} | {_money(arr.get(key, 0))} |")
    if counts.get("commercial_only"):
        lines.append(
            f"| Outside the grid — sentiment unavailable | {counts['commercial_only']} | "
            f"{_money(arr.get('commercial_only', 0))} |"
        )
    lines.append("")

    for key, label, axis, _danger in GRID:
        listing = accounts.get(key, [])
        if not listing:
            continue
        lines += [f"### {label} — {axis}", "",
                  "| Account | ARR | Sentiment | Risk | Renewal | Paper |", "|---|---|---|---|---|---|"]
        for a in listing[:20]:
            paper = "signed" if a.get("signed") else ("UNSIGNED" if a.get("signed") is not None else "unknown")
            lines.append(
                f"| {a['account']} | {_money(a['arr'])} | {_num(a['sentiment'])} | "
                f"{_num(a['risk'])} | {a.get('renewal') or 'unknown'} | {paper} |"
            )
        lines.append("")

    if counts.get("commercial_only"):
        lines += [
            "> **Outside the grid.** "
            f"{counts['commercial_only']} accounts ({_money(arr.get('commercial_only', 0))}) had no "
            "conversation source, so only the commercial half of the model ran. They are unmeasured, "
            "not healthy.",
            "",
        ]
    return "\n".join(lines)


def kickoff_markdown(doc: Dict[str, Any]) -> str:
    section = (doc.get("sections") or {}).get("kickoff_baseline")
    if not section:
        return ""
    lines = ["## Movement since kickoff", "", KICKOFF_EXPLAINER, ""]
    movement = section.get("movement", [])
    if movement:
        lines += ["| Account | Kickoff | Sentiment | Move | ARR | Change |", "|---|---|---|---|---|---|"]
        for m in movement:
            delta = m.get("sentiment_delta")
            delta_text = "—" if delta is None else f"{'+' if delta > 0 else ''}{delta}"
            arr_pct = m.get("arr_delta_pct")
            lines.append(
                f"| {m['account']} | {m.get('kickoff_date') or '—'} | "
                f"{_num(m.get('sentiment_at_kickoff'))} → {_num(m.get('sentiment_now'))} | {delta_text} | "
                f"{_money(m.get('arr_at_kickoff'))} → {_money(m.get('arr_now'))} | "
                f"{'—' if arr_pct is None else str(arr_pct) + '%'} |"
            )
        lines.append("")
    else:
        lines += ["**No kickoff baseline has been captured for any account.** Nothing here can prove "
                  "movement yet — re-run `/customer-health:setup` and fill in the kickoff block.", ""]
    if section.get("missing"):
        lines += [f"> Unprovable (no kickoff baseline): {', '.join(section['missing'][:20])}.", ""]
    return "\n".join(lines)


def inject_markdown(document: str, blocks: str) -> str:
    if not blocks:
        return document
    marker = "## Findings"
    idx = document.find(marker)
    if idx > -1:
        return document[:idx] + blocks + "\n" + document[idx:]
    return document + "\n" + blocks


# ---------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="customer-health: findings.json -> report.md/.html")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--no-save-baseline", action="store_true",
                        help="Render without writing a new baseline snapshot. Re-runs and dry runs only.")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser().resolve()
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        print(f"No findings.json in {run_dir}. Run analyze.py first.", file=sys.stderr)
        return 2

    with findings_path.open("r", encoding="utf-8") as fh:
        doc: Dict[str, Any] = json.load(fh)

    # 1. Baseline diff — must run BEFORE the new snapshot is written.
    doc = apply_deltas(doc, PLUGIN)
    if not args.no_save_baseline:
        snapshot = save_baseline(PLUGIN, doc)
    else:
        snapshot = None

    with findings_path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
        fh.write("\n")

    manifest = load_manifest(run_dir)

    # 2 + 3. Render through the shared library, then splice in our sections.
    markdown = inject_markdown(
        render_markdown(doc, manifest), quadrant_markdown(doc) + "\n" + kickoff_markdown(doc)
    )
    document = inject_html(render_html(doc, manifest), quadrant_html(doc) + kickoff_html(doc))

    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    (run_dir / "report.html").write_text(document, encoding="utf-8")

    quad = (doc.get("sections") or {}).get("quadrant", {}).get("counts", {})
    print(f"report.md      -> {run_dir / 'report.md'}")
    print(f"report.html    -> {run_dir / 'report.html'}")
    if snapshot:
        print(f"baseline       -> {snapshot}")
    print("")
    if doc.get("is_baseline_run"):
        print("BASELINE RUN")
        print(BASELINE_RUN_NOTE)
    else:
        print(f"Compared against the snapshot taken {doc.get('compared_to')}.")
    print("")
    print("KICKOFF BASELINE")
    print(KICKOFF_EXPLAINER)
    kickoff = (doc.get("sections") or {}).get("kickoff_baseline", {})
    print(
        f"  captured for {len(kickoff.get('captured', []))} accounts · "
        f"missing for {len(kickoff.get('missing', []))}"
    )
    print("")
    print(
        "QUADRANT  happy-but-exposed {} · burning {} · grumbling-but-safe {} · healthy {} · "
        "sentiment unavailable {}".format(
            quad.get("happy_but_exposed", 0), quad.get("burning", 0),
            quad.get("grumbling_but_safe", 0), quad.get("healthy", 0), quad.get("commercial_only", 0),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
