#!/usr/bin/env python3
"""
forecast-agent — Layer 3. findings.json -> report.md + report.html.

The shared renderer in lib/render.py draws the standard shape every LeanScale GTM
agent shares (KPI row, severity-sorted findings, evidence tables, method footer).
This script adds two forecast-specific sections on top of it, using the same CSS
classes so the page still reads as one document:

  1. How the Forecast Integrity Score was computed — every component, its weight,
     its subscore and the sentence that produced it. A score nobody can audit is
     a vibe with a number attached.
  2. The call — worst / likely / best, the delta against what the reps called,
     and every assumption written out in full.

Offline, stdlib only. Reads and writes local files.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import load_manifest, write_reports  # noqa: E402

PLUGIN = "forecast-agent"


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _money(v: Any) -> str:
    try:
        return "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _table(rows: Sequence[Dict[str, Any]], limit: int = 25) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(r.get(h, ''))}</td>" for h in headers) + "</tr>"
        for r in rows[:limit])
    more = (f'<div class="meta">Showing {limit} of {len(rows):,} rows — the full set is in '
            f'findings.json.</div>' if len(rows) > limit else "")
    return f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def _section(kick: str, heading: str, inner: str) -> str:
    return (f'<section><div class="sec-head"><div class="kick">{_e(kick)}</div>'
            f"<h2>{_e(heading)}</h2></div>{inner}</section>")


def integrity_html(doc: Dict[str, Any]) -> str:
    integ = (doc.get("sections") or {}).get("integrity") or {}
    if not integ:
        return ""
    inner = (
        f'<p class="sub">Scored <b>{_e(integ.get("score"))}/100 — {_e(integ.get("band"))}</b>. '
        f'{_e(integ.get("formula"))} Bands: {_e(" · ".join(integ.get("bands") or []))}.</p>'
        f'<div class="note">Measurable weight this run: <b>{_e(integ.get("measurable_weight"))} of 100</b>. '
        f'Unweighted score across what could be measured was {_e(integ.get("raw_score"))}; the coverage '
        f'multiplier of {_e(integ.get("coverage_multiplier"))} was then applied. A component that cannot '
        f'be measured never helps the score.</div>'
        + _table(integ.get("rows") or [], limit=12))
    return _section("Forecast Integrity Score", "How the score was built", inner)


def call_html(doc: Dict[str, Any]) -> str:
    fc = (doc.get("sections") or {}).get("forecast") or {}
    if not fc:
        return ""
    if not fc.get("produced"):
        return _section(
            "The call", "Withheld",
            '<div class="note warn"><b>No forecast call was published.</b> The integrity audit scored '
            'this CRM below the threshold in your config, so a three-number call would be a precise '
            'answer built on close dates the audit shows are not load-bearing. Fix the critical and high '
            'findings above, re-run, and the call publishes itself. To override, re-run with '
            '<code>--force</code>.</div>')

    delta = fc.get("delta") or 0
    kpis = "".join([
        f'<div class="kpi"><div class="lab">Worst case</div><div class="val">{_e(_money(fc.get("worst")))}</div>'
        f'<div class="ctx">Wilson lower bound on every measured rate, timing shifted '
        f'{_e(fc.get("slip_step_days"))} days pessimistic</div></div>',
        f'<div class="kpi"><div class="lab">Likely</div><div class="val">{_e(_money(fc.get("likely")))}</div>'
        f'<div class="ctx">Measured entered-cohort conversion × your own slip distribution</div></div>',
        f'<div class="kpi"><div class="lab">Best case</div><div class="val">{_e(_money(fc.get("best")))}</div>'
        f'<div class="ctx">Wilson upper bound, timing shifted {_e(fc.get("slip_step_days"))} days '
        f'optimistic</div></div>',
        f'<div class="kpi"><div class="lab">Called by the team</div>'
        f'<div class="val">{_e(_money(fc.get("rep_called")))}</div>'
        f'<div class="ctx">Delta vs likely: <b>{_e(_money(delta))}</b> '
        f'({_e(fc.get("delta_pct"))}%)</div></div>',
    ])

    coverage = ""
    if fc.get("coverage_ratio"):
        coverage = (f'<div class="note">Pipeline coverage <b>{_e(fc.get("coverage_ratio"))}×</b> — '
                    f'{_e(_money(fc.get("open_pipeline_in_period")))} of open in-period pipeline against a '
                    f'quota of {_e(_money(fc.get("quota")))} (from {_e(fc.get("quota_source"))}) with '
                    f'{_e(_money(fc.get("banked")))} already banked.</div>')
    else:
        coverage = ('<div class="note warn">No quota was configured, so no coverage ratio is shown. '
                    'That is deliberate — a coverage number against a made-up denominator is worse than '
                    'no coverage number.</div>')

    assumptions = "".join(f"<li>{_e(a)}</li>" for a in fc.get("assumptions") or [])
    inner = (
        f'<p class="sub">{_e(fc.get("period"))} · {_e(fc.get("period_start"))} to '
        f'{_e(fc.get("period_end"))} · as of {_e(fc.get("as_of"))} · methodology '
        f'<b>{_e(fc.get("methodology"))}</b> · measure <b>{_e(fc.get("measure"))}</b>. '
        f'Never present one of these numbers on its own.</p>'
        f'<div class="kpis">{kpis}</div>{coverage}'
        f'<h3 style="margin:26px 0 6px;font-size:17px;font-weight:800;color:var(--dpurple)">'
        f'Every assumption, written out</h3>'
        f'<ul style="margin:0 0 8px 20px;font-size:14.5px;color:var(--gray)">{assumptions}</ul>'
        f'<h3 style="margin:26px 0 6px;font-size:17px;font-weight:800;color:var(--dpurple)">'
        f'Roll-up: manager and org</h3>'
        f'<p class="meta">Hierarchy from <code>{_e(fc.get("roll_up_field"))}</code>.</p>'
        f'{_table(fc.get("manager_rows") or [], limit=40)}'
        f'<h3 style="margin:26px 0 6px;font-size:17px;font-weight:800;color:var(--dpurple)">'
        f'Roll-up: rep</h3>{_table(fc.get("owner_rows") or [], limit=40)}'
        f'<h3 style="margin:26px 0 6px;font-size:17px;font-weight:800;color:var(--dpurple)">'
        f'Deal-by-deal derivation (top 20 by contribution)</h3>{_table(fc.get("deal_rows") or [], limit=20)}'
        f'<h3 style="margin:26px 0 6px;font-size:17px;font-weight:800;color:var(--dpurple)">'
        f'Your measured slip distribution</h3>{_table(fc.get("slip_rows") or [], limit=10)}')
    return _section("The call", "Worst, likely, best — and the delta", inner)


def rollup_html(doc: Dict[str, Any]) -> str:
    ru = (doc.get("sections") or {}).get("rollup") or {}
    if not ru.get("rows"):
        return ""
    return _section(
        "Roll-up", "The commit book, by rep",
        f'<p class="sub">Hierarchy resolved from <code>{_e(ru.get("roll_up_field"))}</code>. '
        f'"At risk" means the deal has at least one of: a close date that has already moved, no next '
        f'step, a single contact role, or no activity inside the staleness window.</p>'
        + _table(ru.get("rows") or [], limit=40))


def scope_html(doc: Dict[str, Any]) -> str:
    sec = doc.get("sections") or {}
    scope, measured, period = sec.get("scope") or {}, sec.get("measured") or {}, sec.get("period") or {}
    if not scope:
        return ""
    rows = [
        {"Setting": "Fiscal period", "Value": f"{period.get('label')} "
                                              f"({period.get('start')} to {period.get('end')})"},
        {"Setting": "Fiscal year starts", "Value": f"month {period.get('fiscal_year_start_month')}, "
                                                   f"named by the year it {period.get('naming')}"},
        {"Setting": "Methodology", "Value": scope.get("methodology")},
        {"Setting": "Commit buckets", "Value": ", ".join(scope.get("commit_buckets") or [])},
        {"Setting": "Amount field summed", "Value": scope.get("amount_field")},
        {"Setting": "Currency handling", "Value": scope.get("currency_note")},
        {"Setting": "Open deals in scope", "Value": scope.get("open_in_scope")},
        {"Setting": "Closed deals measured", "Value": f"{scope.get('closed_in_scope')} across "
                                                      f"{measured.get('quarters')} quarters"},
        {"Setting": "Excluded — below deal floor",
         "Value": f"{(scope.get('open_excluded') or {}).get('below_floor', 0)} open / "
                  f"{(scope.get('closed_excluded') or {}).get('below_floor', 0)} closed"},
        {"Setting": "Excluded — type not counted",
         "Value": f"{(scope.get('open_excluded') or {}).get('excluded_type', 0)} open / "
                  f"{(scope.get('closed_excluded') or {}).get('excluded_type', 0)} closed"},
        {"Setting": "Measured win rate", "Value": f"{(measured.get('win_rate') or 0) * 100:.1f}%"},
        {"Setting": "Push penalty (measured)",
         "Value": f"×{measured.get('push_penalty')} — pushed deals win at "
                  f"{(measured.get('win_rate_pushed') or 0) * 100:.0f}% vs "
                  f"{(measured.get('win_rate_unpushed') or 0) * 100:.0f}% un-pushed"},
        {"Setting": "Median sales cycle (won)", "Value": f"{measured.get('cycle_days_median')} days "
                                                         f"(p25 {measured.get('cycle_days_p25')})"},
    ]
    return _section("Scope", "What was counted, and how", _table(rows, limit=30))


def extra_markdown(doc: Dict[str, Any]) -> str:
    sec = doc.get("sections") or {}
    integ, fc = sec.get("integrity") or {}, sec.get("forecast") or {}
    out: List[str] = ["", "## Forecast Integrity Score — how it was built", ""]
    out.append(f"**{integ.get('score')}/100 — {integ.get('band')}.** {integ.get('formula')}")
    out.append("")
    out.append("| Component | Weight | Subscore | What it measures | Measured |")
    out.append("|---|---|---|---|---|")
    for r in integ.get("rows") or []:
        out.append(f"| {r.get('Component')} | {r.get('Weight')} | {r.get('Subscore')} | "
                   f"{r.get('What it measures')} | {r.get('Measured')} |")
    out.append("")
    if fc:
        out += ["## The call", ""]
        if not fc.get("produced"):
            out += ["The call was withheld: the integrity score is below the configured threshold. "
                    "Re-run with `--force` to publish it anyway.", ""]
        else:
            out += [
                f"| Scenario | {fc.get('period')} |", "|---|---|",
                f"| Worst | {_money(fc.get('worst'))} |",
                f"| Likely | {_money(fc.get('likely'))} |",
                f"| Best | {_money(fc.get('best'))} |",
                f"| Called by the team | {_money(fc.get('rep_called'))} |",
                f"| **Delta (called − likely)** | **{_money(fc.get('delta'))} "
                f"({fc.get('delta_pct')}%)** |", "",
                "### Assumptions", ""]
            out += [f"- {a}" for a in fc.get("assumptions") or []]
            out.append("")
            for title, rows in (("Roll-up: manager and org", fc.get("manager_rows")),
                                ("Roll-up: rep", fc.get("owner_rows"))):
                out += [f"### {title}", ""] + _md_table(rows or []) + [""]

    ru = sec.get("rollup") or {}
    if ru.get("rows"):
        out += ["## The commit book, by rep", "",
                f"Hierarchy from `{ru.get('roll_up_field')}`. \"At risk\" means the deal has at least "
                f"one of: a close date that has already moved, no next step, a single contact role, or "
                f"no activity inside the staleness window.", ""]
        out += _md_table(ru["rows"]) + [""]
    return "\n".join(out)


def _md_table(rows: Sequence[Dict[str, Any]], limit: int = 40) -> List[str]:
    if not rows:
        return []
    headers = list(rows[0].keys())
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "---|" * len(headers)]
    for r in rows[:limit]:
        out.append("| " + " | ".join(str(r.get(h, "")) for h in headers) + " |")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render forecast-agent findings into report.md/.html")
    ap.add_argument("--run", required=True, help="run directory containing findings.json")
    args = ap.parse_args(argv)

    run_dir = Path(args.run)
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        print(f"No findings.json in {run_dir}. Run analyze.py first.", file=sys.stderr)
        return 2

    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    manifest = load_manifest(run_dir)
    paths = write_reports(doc, run_dir, manifest)

    extra = integrity_html(doc) + call_html(doc) + rollup_html(doc) + scope_html(doc)
    if extra:
        page = paths["html"].read_text(encoding="utf-8")
        page = page.replace("<footer>", extra + "\n<footer>", 1)
        paths["html"].write_text(page, encoding="utf-8")
        md = paths["markdown"].read_text(encoding="utf-8")
        marker = "\n---\n"
        idx = md.rfind(marker)
        md = (md[:idx] + extra_markdown(doc) + md[idx:]) if idx != -1 else md + extra_markdown(doc)
        paths["markdown"].write_text(md, encoding="utf-8")

    if doc.get("is_baseline_run"):
        print("Baseline run — this report is the starting point; the comparison begins next run.")
    print(f"wrote {paths['markdown']}")
    print(f"wrote {paths['html']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
