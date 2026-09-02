#!/usr/bin/env python3
"""
executive-reporting — Layer 3. findings.json -> report.md + report.html.

The shared renderer draws the standard shape every LeanScale GTM agent shares
(KPI row, severity-sorted findings, evidence tables, method footer). This adds the
pack-specific sections on top, using the same CSS classes so it still reads as one
document:

  1. Reporting Readiness — every component, weight and subscore, with the sentence
     that produced it. If the score is below the band, the headline numbers are
     withheld and this section explains exactly why.
  2. The metric spine — plan attainment, the rolling window, the funnel with any
     impossible rate flagged rather than printed.
  3. Cohort conversion — the definition in full, every cohort, and which were
     suppressed for being unripe.
  4. Channel and owner views, with the quadrant dividers stated numerically.
  5. Reconciliation — measured against what leadership currently quotes.
  6. The executive email — a drafted note the sponsor can send with the pack.

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

from lib import load_manifest, save_baseline, write_reports  # noqa: E402

PLUGIN = "executive-reporting"


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _table(rows: Sequence[Dict[str, Any]], limit: int = 30) -> str:
    if not rows:
        return ""
    headers = [h for h in rows[0].keys() if not str(h).startswith("_")]
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(r.get(h, ''))}</td>" for h in headers) + "</tr>"
        for r in rows[:limit])
    more = (f'<div class="meta">Showing {limit} of {len(rows):,} rows — the full set is in '
            f'findings.json.</div>' if len(rows) > limit else "")
    return (f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>{more}")


def _section(kick: str, heading: str, inner: str) -> str:
    return (f'<section><div class="sec-head"><div class="kick">{_e(kick)}</div>'
            f"<h2>{_e(heading)}</h2></div>{inner}</section>")


def _h3(text: str) -> str:
    return (f'<h3 style="margin:26px 0 6px;font-size:17px;font-weight:800;'
            f'color:var(--dpurple)">{_e(text)}</h3>')


def readiness_html(doc: Dict[str, Any]) -> str:
    r = (doc.get("sections") or {}).get("readiness") or {}
    if not r:
        return ""
    inner = (
        f'<p class="sub">Scored <b>{_e(r.get("score"))}/100 — {_e(r.get("band"))}</b>. '
        f'{_e(r.get("formula"))} Bands: {_e(" · ".join(r.get("bands") or []))}.</p>'
        f'<div class="note">Measurable weight this run: <b>{_e(r.get("measurable_weight"))} of 100</b>. '
        f'Unweighted score across what could be measured was {_e(r.get("raw_score"))}; a coverage '
        f'multiplier of {_e(r.get("coverage_multiplier"))} was then applied, so a component that '
        f'cannot be measured never quietly helps the score.</div>'
        + _table(r.get("rows") or [], limit=12))
    return _section("Reporting Readiness", "Can this CRM support the pack at all?", inner)


def spine_html(doc: Dict[str, Any]) -> str:
    sec = doc.get("sections") or {}
    plan, months, funnel = sec.get("plan") or {}, sec.get("months") or {}, sec.get("funnel") or {}
    if not plan and not months:
        return ""
    inner = ""
    if plan.get("rows"):
        inner += _h3("Against plan")
        if not plan.get("configured"):
            inner += ('<div class="note warn">No targets are configured, so these are absolute '
                      'numbers with nothing to land against. An executive cannot act on a number '
                      'without a target beside it.</div>')
        else:
            inner += (f'<p class="meta">Compared against the <b>{_e(plan.get("level"))}</b>-level '
                      f'goal set.</p>')
        inner += _table(plan["rows"], limit=10)
    if months.get("rows"):
        inner += _h3(f"The rolling window") + f'<p class="meta">{_e(months.get("note"))}</p>'
        inner += _table(months["rows"], limit=14)
    if funnel.get("rows"):
        inner += _h3("The funnel")
        if funnel.get("withheld"):
            inner += ('<div class="note warn"><b>Rates withheld: '
                      f'{_e(", ".join(funnel["withheld"]))}.</b> These read above 100%, which is '
                      'arithmetically impossible on a real funnel — a later stage holds more '
                      'records than the stage above it. That is a missing stamp, not performance, '
                      'so the counts are shown and the rate is not.</div>')
        inner += _table(funnel["rows"], limit=10)
    return _section("The metric spine", "What gets reported, and against what", inner)


def cohort_html(doc: Dict[str, Any]) -> str:
    c = (doc.get("sections") or {}).get("cohorts") or {}
    if not c.get("rows"):
        return ""
    blended = c.get("blended")
    inner = (
        f'<div class="note"><b>Definition.</b> {_e(c.get("definition"))}</div>'
        f'<p class="sub">Blended across ripe cohorts: '
        f'<b>{(blended * 100):.1f}%</b> on {_e(c.get("resolved"))} resolved deals. '
        f'A cohort is reported once it is {_e(c.get("ripeness_days"))} days old; '
        f'{_e(c.get("suppressed"))} cohorts were suppressed as unripe.</p>'
        if blended is not None else
        f'<div class="note"><b>Definition.</b> {_e(c.get("definition"))}</div>'
        f'<div class="note warn">No cohort is ripe enough to publish a rate yet.</div>')
    inner += _table(c["rows"], limit=14)
    return _section("Conversion", "Cohorted by created date", inner)


def channel_html(doc: Dict[str, Any]) -> str:
    sec = doc.get("sections") or {}
    ch, reps = sec.get("channels") or {}, sec.get("reps") or {}
    if not ch.get("rows") and not reps.get("rows"):
        return ""
    inner = ""
    if ch.get("rows"):
        bl = ch.get("blended_conversion")
        inner += _h3("By channel")
        inner += (f'<div class="note">{_e(ch.get("divider_note"))} For this portfolio that is a '
                  f'blended conversion of <b>{(bl * 100):.1f}%</b> and mean bookings of '
                  f'<b>${ch.get("mean_bookings", 0):,.0f}</b> per channel — anything above and to '
                  f'the right of both is the quadrant worth funding.</div>'
                  if bl is not None else
                  f'<div class="note">{_e(ch.get("divider_note"))}</div>')
        inner += _table(ch["rows"], limit=20)
    if reps.get("rows"):
        inner += _h3("By owner") + _table(reps["rows"], limit=25)
    return _section("Where it came from", "Channel and owner", inner)


def concentration_html(doc: Dict[str, Any]) -> str:
    c = (doc.get("sections") or {}).get("concentration") or {}
    if not c.get("rows"):
        return ""
    share = c.get("top_share")
    inner = (f'<p class="sub">{_e(c.get("accounts"))} accounts carry recurring revenue '
             f'({_e(c.get("cadence"))} cadence). Top {_e(c.get("top_n"))} are '
             f'<b>{(share * 100):.1f}%</b> of the book.</p>' if share is not None else "")
    if c.get("negatives"):
        inner += (f'<div class="note warn">{_e(c.get("negatives"))} accounts carry a <b>negative</b> '
                  f'balance. They are included in the total — excluding them makes the account list '
                  f'stop reconciling to the book.</div>')
    inner += _table(c["rows"], limit=20)
    return _section("Concentration", "How much of the book sits in how few accounts", inner)


def recon_html(doc: Dict[str, Any]) -> str:
    r = (doc.get("sections") or {}).get("reconciliation") or {}
    if not r.get("rows"):
        return _section(
            "Reconciliation", "Nothing to reconcile against",
            '<div class="note warn">No currently-quoted numbers were configured, so this pack '
            'cannot tell you where it disagrees with the last board deck. Set '
            '<code>believed_conversion</code> and <code>believed_metrics</code> in the config and '
            're-run — finding that gap before the meeting is the entire point.</div>')
    return _section("Reconciliation", "Where this disagrees with what they quote today",
                    f'<div class="note">{_e(r.get("note"))}</div>' + _table(r["rows"], limit=12))


def email_text(doc: Dict[str, Any]) -> List[str]:
    """The drafted executive email. Plain lines so it copies cleanly."""
    sec = doc.get("sections") or {}
    ready = sec.get("readiness") or {}
    plan = sec.get("plan") or {}
    coh = sec.get("cohorts") or {}
    funnel = sec.get("funnel") or {}
    crit = [f for f in doc.get("findings") or [] if f.get("severity") == "critical"]
    high = [f for f in doc.get("findings") or [] if f.get("severity") == "high"]
    org = doc.get("org_name") or "the team"

    lines = [f"Subject: GTM reporting pack — {doc.get('window', {}).get('end', '')}", ""]
    lines.append(f"Team — the reporting pack is attached. Three things before you open it.")
    lines.append("")
    if plan.get("rows"):
        first = plan["rows"][0]
        lines.append(f"1. Against plan. {first.get('Metric')} is at {first.get('Attainment')} "
                     f"({first.get('Actual')} against {first.get('Plan')}). The full set of "
                     f"headline metrics and their attainment is on the plan slide.")
        lines.append("")
    if coh.get("blended") is not None:
        lines.append(f"2. The conversion number may not match what you have seen before. This pack "
                     f"measures {coh['blended'] * 100:.1f}%, cohorted by the date a deal was created, "
                     f"dividing wins by everything that reached a decision. Deals still open are "
                     f"excluded until they resolve. If a previous number was calculated on deals "
                     f"that closed in the period, it will read differently — that is a definition "
                     f"difference, not a performance change.")
        lines.append("")
    n = 3 if coh.get("blended") is not None else 2
    if crit or funnel.get("withheld"):
        bits = []
        if funnel.get("withheld"):
            bits.append(f"we have withheld the {', '.join(funnel['withheld'])} conversion rate "
                        f"because it computes above 100%, which means a stage is not being stamped "
                        f"rather than that the funnel is performing impossibly")
        if crit:
            bits.append(f"there {'is' if len(crit) == 1 else 'are'} {len(crit)} critical data "
                        f"finding{'' if len(crit) == 1 else 's'} in the appendix")
        lines.append(f"{n}. What we have deliberately not published — " + "; and ".join(bits) + ".")
    else:
        lines.append(f"{n}. Readiness. The underlying data scored "
                     f"{ready.get('score')}/100 for reporting readiness "
                     f"({ready.get('band')}), so everything here is publishable as it stands.")
    lines.append("")
    if high:
        lines.append(f"There are {len(high)} items marked high in the appendix that are worth "
                     f"fifteen minutes before the next review.")
        lines.append("")
    lines.append("Every chart in the pack has the underlying records behind it — if you want to see "
                 "which accounts sit inside any number, ask and we will send the row-level view "
                 "rather than a new summary.")
    return lines


def email_html(doc: Dict[str, Any]) -> str:
    body = "".join(f"<p>{_e(l)}</p>" if l else "" for l in email_text(doc))
    return _section(
        "Ships with the pack", "The executive email",
        '<p class="sub">A drafted note for the sponsor to send with the pack. Edit the tone, keep '
        'the reconciliation paragraph — that is the one that protects you.</p>'
        f'<div class="note" style="line-height:1.6">{body}</div>')


def extra_markdown(doc: Dict[str, Any]) -> str:
    sec = doc.get("sections") or {}
    ready, coh, funnel = sec.get("readiness") or {}, sec.get("cohorts") or {}, sec.get("funnel") or {}
    out: List[str] = ["", "## Reporting Readiness", ""]
    out.append(f"**{ready.get('score')}/100 — {ready.get('band')}.** {ready.get('formula')}")
    out.append("")
    out += _md_table(ready.get("rows") or [])
    out.append("")
    if funnel.get("rows"):
        out += ["## The funnel", ""]
        if funnel.get("withheld"):
            out += [f"> **Rates withheld: {', '.join(funnel['withheld'])}.** They compute above "
                    f"100%, which is impossible on a real funnel — a later stage holds more records "
                    f"than the one above it. Missing stamp, not performance.", ""]
        out += _md_table(funnel["rows"]) + [""]
    if coh.get("rows"):
        out += ["## Conversion — cohorted by created date", "", f"{coh.get('definition')}", ""]
        if coh.get("blended") is not None:
            out += [f"**Blended: {coh['blended'] * 100:.1f}%** across {coh.get('resolved')} "
                    f"resolved deals in ripe cohorts.", ""]
        out += _md_table(coh["rows"]) + [""]
    for title, key in (("Against plan", "plan"), ("By channel", "channels"),
                       ("By owner", "reps"), ("Concentration", "concentration"),
                       ("Reconciliation", "reconciliation"), ("Scope", "scope")):
        rows = (sec.get(key) or {}).get("rows")
        if rows:
            out += [f"## {title}", ""] + _md_table(rows) + [""]
    out += ["## The executive email", "", "```"] + email_text(doc) + ["```", ""]
    return "\n".join(out)


def _md_table(rows: Sequence[Dict[str, Any]], limit: int = 40) -> List[str]:
    if not rows:
        return []
    headers = [h for h in rows[0].keys() if not str(h).startswith("_")]
    out = ["| " + " | ".join(str(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    for r in rows[:limit]:
        out.append("| " + " | ".join(str(r.get(h, "")) for h in headers) + " |")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render executive-reporting findings into report.md/.html")
    ap.add_argument("--run", required=True, help="run directory containing findings.json")
    ap.add_argument("--no-baseline", action="store_true",
                    help="render only; do not save a baseline snapshot")
    args = ap.parse_args(argv)

    run_dir = Path(args.run)
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        print(f"No findings.json in {run_dir}. Run analyze.py first.", file=sys.stderr)
        return 2

    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    manifest = load_manifest(run_dir)
    paths = write_reports(doc, run_dir, manifest)

    extra = (readiness_html(doc) + spine_html(doc) + cohort_html(doc) + channel_html(doc)
             + concentration_html(doc) + recon_html(doc) + email_html(doc))
    if extra:
        page = paths["html"].read_text(encoding="utf-8")
        page = page.replace("<footer>", extra + "\n<footer>", 1)
        paths["html"].write_text(page, encoding="utf-8")
        md = paths["markdown"].read_text(encoding="utf-8")
        marker = "\n---\n"
        idx = md.rfind(marker)
        md = (md[:idx] + extra_markdown(doc) + md[idx:]) if idx != -1 else md + extra_markdown(doc)
        paths["markdown"].write_text(md, encoding="utf-8")

    # Bank the snapshot AFTER the report exists, so a run that dies during rendering
    # never banks a baseline it did not show anyone. analyze.py already calls
    # apply_deltas() to read a previous snapshot — but nothing ever wrote one, so
    # every run reported itself as the baseline forever and the pack's "run it again
    # next week to see the movement" promise could never come true. This is SPEC §0
    # Rule 2, and it is the discipline the whole suite's proof story rests on.
    if not args.no_baseline:
        snapshot = save_baseline(PLUGIN, json.loads(findings_path.read_text(encoding="utf-8")))
        print(f"baseline  : {snapshot}")

    if doc.get("is_baseline_run"):
        print("Baseline run — this report is the starting point; the comparison begins next run.")
    print(f"wrote {paths['markdown']}")
    print(f"wrote {paths['html']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
