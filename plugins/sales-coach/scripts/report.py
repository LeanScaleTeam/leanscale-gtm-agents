#!/usr/bin/env python3
"""
report.py — findings.json -> report.md + report.html

The shared renderer in lib/render.py produces the hero, the score row, the
severity-sorted findings and the method table, so every plugin in the suite
looks like one product. This file adds the sections a sales manager needs on
top of that, in the order they get used on a Monday:

    the team scorecard        which dimension is thin, across everyone
    the coaching agenda       one topic for the room, one line per rep
    the rep table             tenure alongside score, because they interact
    the mechanics table       talk ratio, monologue, questions — targets labelled
    exemplar calibration      does the framework agree with the manager
    deal linkage              which gaps precede the deals that slip
    per-call reviews          off by default; the manager opts in

Per-call reviews are deliberately last and deliberately optional. Reps ignore
per-call feedback; managers act on patterns.

Python 3.9+, standard library only. No network. Nothing is uploaded anywhere.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    BASELINE_RUN_NOTE,
    load_manifest,
    load_profile,
    redact_name,
    render_html,
    render_markdown,
)

PLUGIN = "sales-coach"

METHOD_ANCHOR = '<section><div class="sec-head"><div class="kick">Method</div>'

BAND_NOTE = {
    "ramping": "ramping — coach fundamentals only",
    "developing": "developing — fundamentals first, one advanced dimension",
    "tenured": "tenured — coach the advanced dimensions",
    "unknown": "tenure unknown — add a start_date in config",
}

STATUS_STYLE = {
    "met": ("#33420a", "#E8FFCF"),
    "partial": ("#8a4a12", "#fdf0e2"),
    "missing": ("#8a1c3b", "#fdeaf0"),
    "not_applicable": ("#595959", "#F1F1EF"),
    "unscored": ("#595959", "#F1F1EF"),
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _pct_cell(value: Optional[float]) -> str:
    if value is None:
        return '<td style="color:#8a8a8a">—</td>'
    if value >= 80:
        fg, bg = "#33420a", "#E8FFCF"
    elif value >= 60:
        fg, bg = "#642585", "#F3EAF7"
    elif value >= 30:
        fg, bg = "#8a4a12", "#fdf0e2"
    else:
        fg, bg = "#8a1c3b", "#fdeaf0"
    return f'<td style="color:{fg};background:{bg};font-weight:800">{value:.0f}%</td>'


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(c if str(c).startswith("<td") else f"<td>{_e(c)}</td>"
                                    for c in row) + "</tr>" for row in rows)
    return f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _section(kick: str, title: str, body: str, lede: str = "") -> str:
    lede_html = f'<p class="sub" style="margin-bottom:18px">{_e(lede)}</p>' if lede else ""
    return (f'<section><div class="sec-head"><div class="kick">{_e(kick)}</div>'
            f"<h2>{_e(title)}</h2></div>{lede_html}{body}</section>")


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
    out.append("")
    return out


# ------------------------------------------------------------------ redaction

def build_redaction_map(doc: Dict[str, Any]) -> Dict[str, str]:
    people = (doc.get("sections", {}).get("people") or {})
    names: List[str] = []
    for group in ("reps", "others"):
        names += [n for n in (people.get(group) or []) if n and len(str(n).strip()) > 2]
    # Longest first so "Dana Whitfield" is replaced before a bare "Dana".
    mapping: Dict[str, str] = {}
    for name in sorted(set(names), key=lambda n: -len(str(n))):
        mapping[str(name)] = redact_name(name)
        parts = str(name).split()
        if len(parts) > 1 and len(parts[0]) > 2:
            mapping.setdefault(parts[0], redact_name(name))
    return mapping


def redact(value: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for name, pseudonym in mapping.items():
            out = re.sub(r"\b" + re.escape(name) + r"\b", pseudonym, out)
        # Emails go too — they are the other half of the identity.
        return re.sub(r"\b[\w.+-]+@[\w.-]+\.\w{2,}\b",
                      lambda m: f"contact-{redact_name(m.group(0))[-6:]}@redacted", out)
    if isinstance(value, list):
        return [redact(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, mapping) for k, v in value.items()}
    return value


# ------------------------------------------------------------------ sections

def team_scorecard_html(doc: Dict[str, Any]) -> str:
    sections = doc.get("sections", {})
    dims = (sections.get("team") or {}).get("dimensions") or []
    reps = sections.get("reps") or []
    if not dims:
        return ""

    headers = ["Dimension", "Tier", "Team"] + [r["rep"] for r in reps] + ["Missing on"]
    rows = []
    for dim in dims:
        cells: List[Any] = [dim["label"], dim["tier"].title(), _pct_cell(dim["coverage_pct"])]
        for rep in reps:
            cell = next((d for d in rep["dimensions"] if d["key"] == dim["key"]), None)
            cells.append(_pct_cell(cell["coverage_pct"] if cell else None))
        cells.append(f"{dim['missing']} of {dim['scored_calls']} calls"
                     if dim["scored_calls"] else "not applicable in this window")
        rows.append(cells)

    legend = ('<div class="meta">Coverage is points over points available: met = 2, partial = 1, '
              'missing = 0. Dimensions marked not applicable on a call are excluded from both sides, '
              'so an early discovery call is not penalised for having no paper process. '
              '<b>80%+</b> green · <b>60–79%</b> purple · <b>30–59%</b> amber · <b>under 30%</b> red.</div>')
    return _section(
        "Team scorecard",
        f"{sections.get('framework', {}).get('name', 'Framework')} coverage, by dimension and by rep",
        _table(headers, rows) + legend,
        "Read down the Team column for what to coach the room, and across a rep's column for what to "
        "coach them individually. They are usually different.",
    )


def coaching_agenda_html(doc: Dict[str, Any]) -> str:
    sections = doc.get("sections", {})
    team = sections.get("team") or {}
    weakest = team.get("weakest")
    calls = sections.get("calls") or []
    reps = sections.get("reps") or []
    if not weakest:
        return ""

    moments = []
    for call in calls:
        cell = next((d for d in call["dimensions"] if d["key"] == weakest["key"]), None)
        if cell and (cell.get("missed_moment") or {}).get("quote"):
            moments.append((call, cell["missed_moment"]))
    moments = moments[:3]

    play = ""
    if moments:
        play = "<ul style='margin:10px 0 0 18px'>" + "".join(
            f"<li style='margin-bottom:8px'><b>{_e(call.get('title') or call['call_id'])}</b> "
            f"at <b>{_e(moment.get('timestamp'))}</b> — “{_e(str(moment.get('quote'))[:180])}” "
            f"<span style='color:#595959'>({_e(moment.get('speaker'))}, "
            f"{_e(call.get('rep'))}'s call)</span></li>"
            for call, moment in moments) + "</ul>"

    team_block = (
        '<article class="find">'
        f'<div class="find-top"><span class="pill" style="color:#33420a;background:#E8FFCF">'
        f'The one topic</span><h3>{_e(weakest["label"])}</h3></div>'
        f'<p>Team coverage is <b>{weakest["coverage_pct"]}%</b> — missing outright on '
        f'<b>{weakest["missing"]} of {weakest["scored_calls"]}</b> calls where it applied. '
        f'Play these moments, ask the room what they would have said next, and agree one question '
        f'everyone asks this week.</p>{play}</article>'
    )

    rep_rows = []
    for rep in reps:
        focus = rep.get("focus_dimensions") or []
        if not rep.get("enough_evidence"):
            line = (f"Only {rep['calls']} call{'s' if rep['calls'] != 1 else ''} in the window — "
                    f"not enough to coach on. Check their recordings are being captured.")
        elif focus:
            call_id = None
            for call in calls:
                if call.get("rep") != rep["rep"]:
                    continue
                cell = next((d for d in call["dimensions"] if d["key"] == focus[0]["key"]), None)
                if cell and cell["status"] in ("missing", "partial"):
                    stamp = (cell.get("missed_moment") or {}).get("timestamp")
                    call_id = f"{call.get('title') or call['call_id']}" + (f" at {stamp}" if stamp else "")
                    break
            line = (f"<b>{_e(focus[0]['label'])}</b> ({focus[0]['coverage_pct']}%, missing on "
                    f"{focus[0]['missing']} of {focus[0]['scored_calls']})"
                    + (f" — listen to {_e(call_id)}" if call_id else ""))
        else:
            line = "No dimension below target in this window."
        rep_rows.append([
            rep["rep"],
            f"{BAND_NOTE.get(rep['tenure_band'], rep['tenure_band'])}"
            + (f" ({rep['tenure_days']}d)" if rep.get("tenure_days") is not None else ""),
            f"{rep['coverage_pct']}%" if rep.get("coverage_pct") is not None else "—",
            f"<td>{line}</td>",
        ])

    return _section(
        "Monday",
        "The coaching agenda",
        team_block + "<h3 style='margin:26px 0 10px;font-size:17px;font-weight:800;color:#301934'>"
        "One line per rep</h3>" + _table(["Rep", "Tenure", "Coverage", "Coach this"], rep_rows),
        "One topic for the team meeting, then one thing per person. Not nine call reviews — a team "
        "changes one habit at a time.",
    )


def rep_table_html(doc: Dict[str, Any]) -> str:
    reps = doc.get("sections", {}).get("reps") or []
    targets = doc.get("sections", {}).get("targets") or {}
    if not reps:
        return ""
    rows = []
    for rep in reps:
        rows.append([
            rep["rep"],
            rep.get("tenure_band", "unknown"),
            str(rep.get("calls", 0)),
            _pct_cell(rep.get("coverage_pct")),
            (rep.get("strength_dimension") or {}).get("label", "—"),
            ", ".join(f["label"] for f in (rep.get("focus_dimensions") or [])) or "—",
            f"{rep['talk_ratio_pct']}%" if rep.get("talk_ratio_pct") is not None else "n/a",
            str(rep.get("questions_per_30min") if rep.get("questions_per_30min") is not None else "n/a"),
            f"{rep['next_step_set_pct']}%" if rep.get("next_step_set_pct") is not None else "—",
        ])
    note = (f'<div class="meta">Talk ratio and question rate are averaged over calls with reliable '
            f'speaker attribution only. Targets: your side under '
            f'{targets.get("rep_talk_ratio_max_pct")}% of speaking time, at least '
            f'{targets.get("min_questions_per_30min")} questions per 30 minutes — '
            f'<b>{_e(targets.get("labelled_as", "industry default"))}</b>, editable in '
            f'~/.leanscale-gtm/{PLUGIN}.json.</div>')
    return _section(
        "By rep",
        "Score against tenure",
        _table(["Rep", "Tenure", "Calls", "Coverage", "Strongest", "Coach next",
                "Talk", "Q/30min", "Next step set"], rows) + note,
        "Tenure is in the table because it changes what the score means. A ramping rep at 40% and a "
        "three-year rep at 40% are two different conversations.",
    )


def mechanics_html(doc: Dict[str, Any]) -> str:
    calls = doc.get("sections", {}).get("calls") or []
    targets = doc.get("sections", {}).get("targets") or {}
    if not calls:
        return ""
    rows = []
    for call in calls:
        mech = call.get("mechanics") or {}
        if not mech.get("eligible"):
            rows.append([
                call.get("title") or call["call_id"], call.get("rep"), call.get("call_type"),
                "<td colspan='5' style='color:#8a1c3b;background:#fdeaf0'>excluded — "
                + _e(mech.get("suppressed_reason") or "attribution unreliable") + "</td>",
            ])
            continue
        pricing = mech.get("first_pricing") or {}
        rows.append([
            call.get("title") or call["call_id"],
            call.get("rep"),
            call.get("call_type"),
            f"{mech['talk_ratio_pct']}%" if mech.get("talk_ratio_pct") is not None else "—",
            f"{(mech['longest_monologue']['seconds'] or 0)/60:.1f} min at "
            f"{mech['longest_monologue']['ts'] or '--:--'}",
            str(mech.get("questions_per_30min") if mech.get("questions_per_30min") is not None else "—"),
            (f"{pricing.get('ts')} ({pricing.get('position_pct')}% in, "
             f"{pricing.get('raised_by')} raised it)"
             + (" · keyword locator, verify it"
                if str(pricing.get("method", "")).startswith("keyword") else "")
             if pricing else "not discussed"),
            ", ".join(sorted({m["competitor"] for m in mech.get("competitor_mentions") or []})) or "—",
        ])
    headers = ["Call", "Rep", "Type", "Your side", "Longest stretch", "Q/30min",
               "First pricing moment", "Competitors named"]
    # An excluded row already spans the remaining columns; only pad the others.
    squared = []
    for row in rows:
        spans = any("colspan" in str(c) for c in row)
        squared.append(row if spans or len(row) == len(headers)
                       else list(row) + [""] * (len(headers) - len(row)))
    rows = squared
    note = (f'<div class="meta">Speaking time comes from the transcript\'s own timestamps where the '
            f'export provides them, and is otherwise estimated from word count at 150 wpm — the per-call '
            f'basis is recorded in findings.json. Every threshold here is an '
            f'<b>{_e(targets.get("labelled_as", "industry default"))}</b>: your side under '
            f'{targets.get("rep_talk_ratio_max_pct")}%, longest stretch under '
            f'{targets.get("longest_monologue_max_sec")}s, at least {targets.get("min_questions_per_30min")} '
            f'questions per 30 minutes, pricing after the first '
            f'{targets.get("earliest_pricing_position_pct")}% of the call. Override them in '
            f'~/.leanscale-gtm/{PLUGIN}.json.</div>')
    return _section("Mechanics", "The measurable half", _table(headers, rows) + note,
                    "None of these is a verdict on its own. They are the questions to ask before you "
                    "listen to a call, not the answer.")


def calibration_html(doc: Dict[str, Any]) -> str:
    calib = doc.get("sections", {}).get("calibration") or {}
    if not calib.get("available"):
        return _section("Calibration", "Not calibrated in this run",
                        f'<div class="note warn">{_e(calib.get("reason", "no exemplar calls nominated"))}. '
                        f'Nominate two or three calls you consider genuinely good in '
                        f'<code>exemplar_call_ids</code> and re-run — the scores mean more once you know '
                        f'whether the framework agrees with you.</div>')
    rows = [[d["label"],
             f"{d['exemplar_pct']}%" if d["exemplar_pct"] is not None else "—",
             f"{d['rest_pct']}%" if d["rest_pct"] is not None else "—"]
            for d in calib.get("per_dimension") or []]
    exemplars = "".join(
        f"<li><b>{_e(c.get('title') or c['call_id'])}</b> ({_e(c.get('rep'))}) — "
        f"{c.get('coverage_pct')}% coverage</li>" for c in calib.get("exemplar_calls") or [])
    gap = calib.get("gap")
    banner_class = "note" if (gap or 0) >= 5 else "note warn"
    body = (
        f'<div class="{banner_class}"><b>Exemplars {calib.get("exemplar_coverage_pct")}% · '
        f'everyone else {calib.get("rest_coverage_pct")}% · gap {gap:+.0f} points.</b> '
        f'{_e(calib.get("verdict", ""))}</div>'
        f"<ul style='margin:14px 0 14px 18px'>{exemplars}</ul>"
        + _table(["Dimension", "Your exemplars", "Everyone else"], rows)
    )
    return _section("Calibration", "Does the framework agree with you?", body,
                    "Before trusting a score, check it against calls you already know are good. "
                    "This is that check.")


def linkage_html(doc: Dict[str, Any]) -> str:
    linkage = doc.get("sections", {}).get("deal_linkage") or {}
    if not linkage.get("available"):
        return _section("Deal linkage", "Not available in this run",
                        f'<div class="note warn">{_e(linkage.get("reason", "no CRM connected"))}. '
                        f'Connect a CRM and re-run to see which framework gaps precede the deals that '
                        f'slip or lose — that is the correlation that justifies the coaching time.</div>')
    outcomes = linkage.get("outcomes", {})
    rows = [[c["label"], f"{c['won_coverage_pct']}%", f"{c['adverse_coverage_pct']}%",
             f"{c['spread']:+.0f} pts"] for c in linkage.get("comparisons") or []]
    caveat = ('<div class="note warn"><b>Directional, not proven.</b> Fewer than five deals on one side '
              'of this comparison. Treat the top row as the hypothesis to test over the next two runs.</div>'
              if linkage.get("directional") else "")
    summary = (f'<p class="sub">{linkage.get("linked_calls")} calls matched to opportunities: '
               f'{outcomes.get("won", 0)} won · {outcomes.get("lost", 0)} lost · '
               f'{outcomes.get("slipped", 0)} slipped · {outcomes.get("open", 0)} open.</p>')
    return _section(
        "Deal linkage", "Which gaps precede the deals that do not close",
        summary + caveat + _table(["Dimension", "Coverage on won deals",
                                   "Coverage on slipped or lost", "Spread"], rows),
        "The same dimension scores, split by what happened to the deal.",
    )


def per_call_html(doc: Dict[str, Any]) -> str:
    sections = doc.get("sections", {})
    if not (sections.get("coverage") or {}).get("include_per_call_reviews"):
        return ""
    cards = []
    for call in sections.get("calls") or []:
        chips = ""
        for dim in call["dimensions"]:
            fg, bg = STATUS_STYLE.get(dim["status"], ("#595959", "#eee"))
            chips += (f'<span class="pill" style="color:{fg};background:{bg}">'
                      f'{_e(dim["label"])}: {_e(dim["status"].replace("_", " "))}</span> ')
        evidence = ""
        for dim in call["dimensions"]:
            if dim.get("quote"):
                flag = "" if dim.get("quote_verified") is not False else " <b>(quote not found in transcript)</b>"
                evidence += (f'<p><span class="lbl">{_e(dim["label"])} · {_e(dim["timestamp"])}</span> '
                             f'“{_e(dim["quote"])}” <span style="color:#595959">— {_e(dim.get("speaker"))}'
                             f'</span>{flag}</p>')
            elif (dim.get("missed_moment") or {}).get("quote"):
                moment = dim["missed_moment"]
                evidence += (f'<p><span class="lbl">{_e(dim["label"])} · missed at '
                             f'{_e(moment.get("timestamp"))}</span> “{_e(moment.get("quote"))}” '
                             f'<span style="color:#595959">— {_e(moment.get("speaker"))}</span></p>')
        next_step = call.get("next_step") or {}
        caveat = (f'<div class="note warn">{_e(call["attribution_caveat"])}</div>'
                  if call.get("attribution_caveat") else "")
        cards.append(
            '<article class="find">'
            f'<div class="find-top"><span class="pill" style="color:#642585;background:#F3EAF7">'
            f'{call.get("coverage_pct")}%</span><h3>{_e(call.get("title") or call["call_id"])}</h3></div>'
            f'<div class="meta">{_e(call.get("rep"))} · {_e(call.get("date"))} · '
            f'{_e(call.get("call_type"))} · {call.get("duration_min")} min · source '
            f'{_e(call.get("source"))}{" · exemplar" if call.get("is_exemplar") else ""}</div>'
            f'{caveat}<div class="tally" style="margin-top:14px">{chips}</div>{evidence}'
            f'<p><span class="lbl">Next step.</span> '
            f'{"Set — " if next_step.get("set") else "Not set — "}'
            f'{_e(next_step.get("detail") or next_step.get("quote") or "nothing agreed on the call")}</p>'
            "</article>"
        )
    return _section("Appendix", "Per-call reviews", "".join(cards),
                    "Included because include_per_call_reviews is on in your config. The team pattern "
                    "above is the thing to act on; these are for the one-to-ones.")


# --------------------------------------------------------------------- markdown

def extra_markdown(doc: Dict[str, Any]) -> List[str]:
    sections = doc.get("sections", {})
    team = sections.get("team") or {}
    lines: List[str] = []

    weakest = team.get("weakest")
    if weakest:
        lines += ["## Monday — the coaching agenda", "",
                  f"**One topic for the room: {weakest['label']}.** Team coverage "
                  f"{weakest['coverage_pct']}%, missing outright on {weakest['missing']} of "
                  f"{weakest['scored_calls']} calls where it applied.", ""]
        for call in sections.get("calls") or []:
            cell = next((d for d in call["dimensions"] if d["key"] == weakest["key"]), None)
            moment = (cell or {}).get("missed_moment") or {}
            if moment.get("quote"):
                lines.append(f"- Play **{call.get('title') or call['call_id']}** at "
                             f"**{moment.get('timestamp')}** — \"{str(moment.get('quote'))[:180]}\"")
        lines.append("")

    reps = sections.get("reps") or []
    if reps:
        lines += ["### One line per rep", ""]
        lines += _md_table(
            ["Rep", "Tenure", "Calls", "Coverage", "Coach this"],
            [[r["rep"], f"{r['tenure_band']}"
              + (f" ({r['tenure_days']}d)" if r.get("tenure_days") is not None else ""),
              r["calls"],
              f"{r['coverage_pct']}%" if r.get("coverage_pct") is not None else "—",
              (", ".join(f["label"] for f in r.get("focus_dimensions") or []) or "—")
              if r.get("enough_evidence") else "not enough calls to coach on"]
             for r in reps])

    dims = team.get("dimensions") or []
    if dims:
        lines += ["## Team scorecard", ""]
        lines += _md_table(
            ["Dimension", "Tier", "Coverage", "Met", "Partial", "Missing", "Calls scored"],
            [[d["label"], d["tier"], f"{d['coverage_pct']}%" if d["coverage_pct"] is not None else "—",
              d["met"], d["partial"], d["missing"], d["scored_calls"]] for d in dims])

    targets = sections.get("targets") or {}
    calls = sections.get("calls") or []
    if calls:
        lines += ["## Mechanics", "",
                  f"Thresholds below are **{targets.get('labelled_as', 'industry default')}** — "
                  f"override them in `~/.leanscale-gtm/{PLUGIN}.json`.", ""]
        rows = []
        for call in calls:
            mech = call.get("mechanics") or {}
            if not mech.get("eligible"):
                rows.append([call.get("title") or call["call_id"], call.get("rep"),
                             "excluded", "—", "—", "—"])
                continue
            pricing = mech.get("first_pricing") or {}
            rows.append([
                call.get("title") or call["call_id"], call.get("rep"),
                f"{mech.get('talk_ratio_pct')}%",
                f"{(mech['longest_monologue']['seconds'] or 0)/60:.1f} min",
                mech.get("questions_per_30min"),
                f"{pricing.get('ts')} ({pricing.get('raised_by')})" if pricing else "not discussed",
            ])
        lines += _md_table(["Call", "Rep", "Your side", "Longest stretch", "Q/30min", "First pricing"], rows)

    calib = sections.get("calibration") or {}
    if calib.get("available"):
        lines += ["## Calibration", "",
                  f"Exemplars {calib.get('exemplar_coverage_pct')}% · everyone else "
                  f"{calib.get('rest_coverage_pct')}% · gap {calib.get('gap'):+.0f} points.", "",
                  calib.get("verdict", ""), ""]

    linkage = sections.get("deal_linkage") or {}
    if linkage.get("available"):
        lines += ["## Deal linkage", ""]
        lines += _md_table(
            ["Dimension", "Won", "Slipped or lost", "Spread"],
            [[c["label"], f"{c['won_coverage_pct']}%", f"{c['adverse_coverage_pct']}%",
              f"{c['spread']:+.0f}"] for c in linkage.get("comparisons") or []])
        if linkage.get("directional"):
            lines += ["_Directional only — fewer than five deals on one side of the comparison._", ""]
    return lines


def rep_cards(doc: Dict[str, Any], run_dir: Path) -> List[Path]:
    """One page per rep, written only when output_audience is 'reps'."""
    sections = doc.get("sections", {})
    if (sections.get("coverage") or {}).get("output_audience") != "reps":
        return []
    out_dir = Path(run_dir) / "coaching"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    calls = sections.get("calls") or []
    for rep in sections.get("reps") or []:
        slug = re.sub(r"[^a-z0-9]+", "-", str(rep["rep"]).lower()).strip("-") or "rep"
        lines = [f"# Coaching card — {rep['rep']}", "",
                 f"Window {doc['window'].get('start')} → {doc['window'].get('end')} · "
                 f"{rep['calls']} calls · {rep.get('tenure_band')} "
                 f"({rep.get('tenure_days')} days) · coverage {rep.get('coverage_pct')}%", ""]
        if rep.get("strength_dimension"):
            lines += [f"**What you already do well.** {rep['strength_dimension']['label']} — "
                      f"{rep['strength_dimension']['coverage_pct']}% coverage.", ""]
        lines += ["**What to work on next.**", ""]
        for focus in rep.get("focus_dimensions") or []:
            lines.append(f"- **{focus['label']}** — {focus['coverage_pct']}%, missing on "
                         f"{focus['missing']} of {focus['scored_calls']} calls.")
        lines.append("")
        for call in calls:
            if call.get("rep") != rep["rep"]:
                continue
            lines += [f"### {call.get('title') or call['call_id']} — {call.get('coverage_pct')}%", ""]
            for dim in call["dimensions"]:
                moment = dim.get("missed_moment") or {}
                if dim.get("quote"):
                    lines.append(f"- **{dim['label']}: {dim['status']}** — {dim['timestamp']} "
                                 f"\"{dim['quote'][:180]}\"")
                elif moment.get("quote"):
                    lines.append(f"- **{dim['label']}: {dim['status']}** — the moment was there at "
                                 f"{moment.get('timestamp')}: \"{str(moment.get('quote'))[:180]}\"")
                else:
                    lines.append(f"- **{dim['label']}: {dim['status']}**")
            lines.append("")
        path = out_dir / f"{slug}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        written.append(path)
    return written


# -------------------------------------------------------------------- main

def build(run_dir: Path) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        raise FileNotFoundError(
            f"No findings.json in {run_dir}. Run scripts/analyze.py first — report.py only renders."
        )
    with findings_path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    manifest = load_manifest(run_dir)

    profile = load_profile(required=False)
    if profile.get("redact_pii_in_reports"):
        mapping = build_redaction_map(doc)
        doc = redact(doc, mapping)
        doc.setdefault("sections", {})["redacted"] = True

    extra = "".join([
        coaching_agenda_html(doc),
        team_scorecard_html(doc),
        rep_table_html(doc),
        mechanics_html(doc),
        calibration_html(doc),
        linkage_html(doc),
        per_call_html(doc),
    ])

    html_out = render_html(doc, manifest)
    if METHOD_ANCHOR in html_out:
        html_out = html_out.replace(METHOD_ANCHOR, extra + METHOD_ANCHOR, 1)
    else:
        html_out = html_out.replace("<footer>", extra + "<footer>", 1)

    md_out = render_markdown(doc, manifest)
    extra_md = "\n".join(extra_markdown(doc))
    marker = "\n---\n"
    if extra_md and marker in md_out:
        head, _, tail = md_out.rpartition(marker)
        md_out = head + "\n" + extra_md + marker + tail
    elif extra_md:
        md_out += "\n" + extra_md

    (run_dir / "report.html").write_text(html_out, encoding="utf-8")
    (run_dir / "report.md").write_text(md_out, encoding="utf-8")
    cards = rep_cards(doc, run_dir)

    return {"doc": doc, "cards": cards,
            "html": run_dir / "report.html", "markdown": run_dir / "report.md",
            "redacted": bool(profile.get("redact_pii_in_reports"))}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render the sales-coach report.")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)

    try:
        result = build(Path(args.run_dir))
    except FileNotFoundError as exc:
        print(f"sales-coach: {exc}", file=sys.stderr)
        return 1

    doc = result["doc"]
    print(f"report.html  {result['html']}")
    print(f"report.md    {result['markdown']}")
    for card in result["cards"]:
        print(f"coaching     {card}")
    if result["redacted"]:
        print("PII redaction is ON — names and emails are pseudonymised in the reports; "
              "raw/ and findings.json are untouched.")
    if doc.get("is_baseline_run"):
        print("\nBASELINE RUN")
        print(BASELINE_RUN_NOTE)
    elif doc.get("compared_to"):
        print(f"\nCompared against the snapshot taken {doc['compared_to']}.")
        for score in doc.get("scores", []):
            delta = score.get("delta_vs_last")
            if delta not in (None, 0):
                print(f"  {score['label']}: {score['value']} ({delta:+g} vs last run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
