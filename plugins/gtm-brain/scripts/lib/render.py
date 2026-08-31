"""
Report rendering — markdown + a self-contained HTML file.

The HTML is one file with the CSS inlined and NO external requests: no CDN, no
webfont fetch, no analytics. It has to open from a laptop with the wifi off and
survive being forwarded to a CFO. The font stack degrades to system sans when
Plus Jakarta Sans isn't installed locally.

Design tokens are the LeanScale Modern Design Standard (light system).
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .baseline import BASELINE_RUN_NOTE

SEV_LABEL = {
    "critical": ("Critical", "#8a1c3b", "#fdeaf0"),
    "high": ("High", "#8a4a12", "#fdf0e2"),
    "medium": ("Medium", "#642585", "#F3EAF7"),
    "low": ("Low", "#4a5a20", "#E8FFCF"),
}

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%23FFFBFF'/%3E"
    "%3Ccircle cx='16' cy='16' r='5' fill='%23642585'/%3E"
    "%3Ccircle cx='16' cy='16' r='8.5' fill='none' stroke='%23E8FFCF' stroke-width='2.5'/%3E%3C/svg%3E"
)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_value(value: Any, unit: str = "") -> str:
    if isinstance(value, float):
        text = f"{value:,.1f}".rstrip("0").rstrip(".")
    elif isinstance(value, int):
        text = f"{value:,}"
    else:
        text = str(value)
    if unit == "percent":
        text += "%"
    elif unit == "currency":
        text = "$" + text
    return text


def _delta_html(score: Dict[str, Any]) -> str:
    delta = score.get("delta_vs_last")
    if delta in (None, 0):
        return '<span class="d flat">no change</span>' if delta == 0 else '<span class="d flat">baseline</span>'
    good_dir = score.get("direction_good", "down")
    improving = (delta < 0 and good_dir == "down") or (delta > 0 and good_dir == "up")
    arrow = "▼" if delta < 0 else "▲"
    cls = "good" if improving else "bad"
    return f'<span class="d {cls}">{arrow} {_fmt_value(abs(delta))} vs last run</span>'


# --------------------------------------------------------------------------- markdown


def render_markdown(doc: Dict[str, Any], manifest: Optional[Dict[str, Any]] = None) -> str:
    org = doc.get("org_name") or "Your organization"
    window = doc.get("window", {})
    lines: List[str] = [
        f"# {doc['plugin'].replace('-', ' ').title()} — {org}",
        "",
        f"Generated {doc.get('generated_at', '')} · "
        f"window {window.get('start', '?')} → {window.get('end', '?')}",
        "",
    ]

    if doc.get("is_baseline_run"):
        lines += [f"> **Baseline run.** {BASELINE_RUN_NOTE}", ""]
    elif doc.get("compared_to"):
        lines += [f"> Compared against the snapshot taken {doc['compared_to']}.", ""]

    if doc.get("scores"):
        lines += ["## Headline", ""]
        for score in doc["scores"]:
            delta = score.get("delta_vs_last")
            suffix = ""
            if delta not in (None, 0):
                suffix = f" ({'+' if delta > 0 else ''}{_fmt_value(delta)} vs last run)"
            lines.append(
                f"- **{score['label']}:** {_fmt_value(score['value'], score.get('unit', ''))}{suffix}"
                + (f" — {score['context']}" if score.get("context") else "")
            )
        lines.append("")

    counts = doc.get("counts_by_severity", {})
    if any(counts.values()):
        summary = " · ".join(f"{v} {k}" for k, v in counts.items() if v)
        lines += [f"**{len(doc.get('findings', []))} findings:** {summary}", ""]

    if doc.get("unavailable"):
        lines += [
            "> **Not covered in this run:** "
            + ", ".join(doc["unavailable"])
            + ". These sections are unavailable, not clean — see the manifest.",
            "",
        ]

    lines += ["## Findings", ""]
    for i, finding in enumerate(doc.get("findings", []), 1):
        lines += [
            f"### {i}. [{finding['severity'].upper()}] {finding['title']}",
            "",
            finding["what"],
            "",
            f"**Why it matters.** {finding['why_it_matters']}",
            "",
            f"**Fix.** {finding['recommended_fix']}  \n"
            f"*Effort: {finding.get('effort', 'medium')} · Owner: {finding.get('owner_hint', 'RevOps')}*",
            "",
        ]
        ev = finding.get("evidence") or {}
        if ev.get("count") is not None:
            delta = finding.get("delta_vs_last")
            trend = ""
            if delta not in (None, 0):
                trend = f" ({'+' if delta > 0 else ''}{_fmt_value(delta)} vs last run)"
            lines.append(f"**Evidence.** {_fmt_value(ev['count'])} records affected{trend}.")
        if ev.get("sample_ids"):
            lines.append(f"Sample IDs: `{'`, `'.join(str(x) for x in ev['sample_ids'][:10])}`")
        if ev.get("rows"):
            lines += ["", _table_markdown(ev["rows"])]
        if ev.get("query"):
            lines += ["", "```sql", str(ev["query"]).strip(), "```"]
        lines.append("")

    if manifest:
        lines += ["## Method", "", "| Source | Tool | Records |", "|---|---|---|"]
        for src in manifest.get("sources", []):
            lines.append(f"| {src['name']} | `{src['tool']}` | {src['record_count']:,} |")
        lines.append("")

    lines += [
        "---",
        "",
        "Produced by a LeanScale GTM Agent. Read-only: nothing in your CRM was modified.",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------- html

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#1a1420;--gray:#595959;--dpurple:#301934;--purple:#642585;--soft:#F3EAF7;
--lime:#E8FFCF;--limed:#C7F59B;--line:#E9E9E7;--bg:#FFFBFF;--radius:18px;
--shadow:0 1px 2px rgba(48,25,52,.04),0 8px 30px rgba(48,25,52,.06)}
body{font-family:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--ink);line-height:1.6;font-size:16px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:0 34px}
header.hero{padding:64px 0 8px}
.eyebrow{display:inline-flex;align-items:center;gap:9px;font-size:12px;font-weight:700;letter-spacing:.13em;
text-transform:uppercase;color:var(--purple);background:var(--soft);padding:7px 14px;border-radius:100px;margin-bottom:22px}
.eyebrow .pip{width:7px;height:7px;border-radius:50%;background:var(--purple)}
h1{font-size:clamp(2.1rem,5vw,3.4rem);line-height:1.05;letter-spacing:-.035em;font-weight:800;
color:var(--dpurple);max-width:20ch}
h1 .hl{background:linear-gradient(transparent 62%,var(--limed) 62%)}
.sub{color:var(--gray);font-size:17px;margin-top:16px;max-width:70ch}
.note{background:var(--lime);border-radius:14px;padding:16px 20px;margin:26px 0;font-size:14.5px;
color:#33420a;border:1px solid var(--limed)}
.note b{font-weight:800}
.warn{background:#fdf0e2;border:1px solid #f0d4ae;color:#6b3d0c}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px;margin:34px 0 10px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px 22px;box-shadow:var(--shadow)}
.kpi .lab{font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--gray)}
.kpi .val{font-size:38px;font-weight:800;letter-spacing:-.03em;color:var(--dpurple);line-height:1.1;margin:6px 0 2px}
.kpi .ctx{font-size:13px;color:var(--gray)}
.d{display:inline-block;font-size:12px;font-weight:700;margin-top:6px}
.d.good{color:#2f6b34}.d.bad{color:#a32a2a}.d.flat{color:var(--gray);font-weight:600}
section{padding:34px 0}
.sec-head{margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid var(--line)}
.kick{font-size:12px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--purple)}
h2{font-size:26px;font-weight:800;letter-spacing:-.02em;color:var(--dpurple);margin-top:4px}
.tally{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 26px}
.pill{font-size:12.5px;font-weight:800;padding:6px 13px;border-radius:100px;border:1px solid transparent}
.find{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:24px 26px;
margin-bottom:16px;box-shadow:var(--shadow);transition:box-shadow .15s,border-color .15s}
.find:hover{box-shadow:0 12px 40px rgba(100,37,133,.14);border-color:var(--purple)}
.find-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.find h3{font-size:19px;font-weight:800;letter-spacing:-.015em;color:var(--dpurple);flex:1;min-width:260px}
.find p{margin:10px 0;font-size:15px}
.find .lbl{font-weight:800;color:var(--dpurple)}
.meta{font-size:12.5px;color:var(--gray);margin-top:14px;padding-top:12px;border-top:1px dashed var(--line)}
.meta code{background:var(--soft);border-radius:5px;padding:2px 6px;font-size:12px}
details{margin-top:12px}
summary{cursor:pointer;font-size:13px;font-weight:700;color:var(--purple)}
pre{background:#2a1f33;color:#e8dcf0;border-radius:12px;padding:14px 16px;overflow-x:auto;
font-size:12.5px;line-height:1.55;margin-top:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:10px}
th{text-align:left;font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--gray);
padding:8px 10px;border-bottom:2px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid var(--line)}
tbody tr:hover{background:var(--soft)}
.tblwrap{overflow-x:auto}
footer{margin-top:40px;padding:30px 0 60px;border-top:1px solid var(--line);color:var(--gray);font-size:13.5px}
footer b{color:var(--dpurple)}
.brand{display:flex;align-items:center;gap:11px;font-weight:800;font-size:16px;color:var(--ink);margin-bottom:10px}
.brand .dot{width:11px;height:11px;border-radius:50%;background:var(--purple);box-shadow:0 0 0 4px var(--lime)}
@media(max-width:640px){.wrap{padding:0 20px}header.hero{padding:40px 0 4px}}
@media print{.find,.kpi{break-inside:avoid;box-shadow:none}body{font-size:12pt}}
"""


def _findings_html(doc: Dict[str, Any]) -> str:
    out: List[str] = []
    for i, finding in enumerate(doc.get("findings", []), 1):
        label, fg, bg = SEV_LABEL.get(finding["severity"], ("Note", "#595959", "#eee"))
        ev = finding.get("evidence") or {}
        out.append('<article class="find">')
        out.append('<div class="find-top">')
        out.append(
            f'<span class="pill" style="color:{fg};background:{bg}">{label}</span>'
            f'<h3>{i}. {_e(finding["title"])}</h3></div>'
        )
        out.append(f'<p>{_e(finding["what"])}</p>')
        out.append(
            f'<p><span class="lbl">Why it matters.</span> {_e(finding["why_it_matters"])}</p>'
        )
        out.append(
            f'<p><span class="lbl">Fix.</span> {_e(finding["recommended_fix"])}</p>'
        )

        bits: List[str] = []
        if ev.get("count") is not None:
            trend = ""
            delta = finding.get("delta_vs_last")
            if delta not in (None, 0):
                word = "more" if delta > 0 else "fewer"
                trend = f' · <b>{_fmt_value(abs(delta))} {word}</b> than last run'
            bits.append(f"<b>{_fmt_value(ev['count'])}</b> records affected{trend}")
        bits.append(f"Effort: {_e(finding.get('effort', 'medium'))}")
        bits.append(f"Owner: {_e(finding.get('owner_hint', 'RevOps'))}")
        out.append(f'<div class="meta">{" · ".join(bits)}</div>')

        if ev.get("sample_ids"):
            ids = " ".join(f"<code>{_e(x)}</code>" for x in ev["sample_ids"][:12])
            out.append(f'<div class="meta">Sample records: {ids}</div>')
        if ev.get("rows"):
            out.append(_table_html(ev["rows"]))
        if ev.get("query"):
            out.append(
                "<details><summary>Verify this yourself — the exact query</summary>"
                f"<pre>{_e(str(ev['query']).strip())}</pre></details>"
            )
        out.append("</article>")
    return "\n".join(out)


def _table_markdown(rows: List[Dict[str, Any]], limit: int = 25) -> str:
    """
    Render an evidence table for the markdown report.

    The HTML report has always shown evidence["rows"]; markdown used to drop them
    silently, so the two reports disagreed about what evidence existed. Pipes are
    escaped so a CRM value containing "|" can't break the table.
    """
    if not rows:
        return ""

    def cell(value: Any) -> str:
        return str("" if value is None else value).replace("|", "\\|").replace("\n", " ")

    headers = list(rows[0].keys())
    out = ["| " + " | ".join(cell(h) for h in headers) + " |",
           "|" + "---|" * len(headers)]
    for row in rows[:limit]:
        out.append("| " + " | ".join(cell(row.get(h, "")) for h in headers) + " |")
    if len(rows) > limit:
        out.append("")
        out.append(f"_Showing {limit} of {len(rows):,} rows — full set in findings.json._")
    return "\n".join(out)


def _table_html(rows: List[Dict[str, Any]], limit: int = 25) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = ""
    for row in rows[:limit]:
        body += "<tr>" + "".join(f"<td>{_e(row.get(h, ''))}</td>" for h in headers) + "</tr>"
    more = (
        f'<div class="meta">Showing {limit} of {len(rows):,} rows — full set in findings.json.</div>'
        if len(rows) > limit
        else ""
    )
    return f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def render_html(doc: Dict[str, Any], manifest: Optional[Dict[str, Any]] = None) -> str:
    org = doc.get("org_name") or "Your organization"
    title = doc["plugin"].replace("-", " ").title()
    window = doc.get("window", {})
    counts = doc.get("counts_by_severity", {})

    kpis = ""
    for score in doc.get("scores", []):
        kpis += (
            '<div class="kpi">'
            f'<div class="lab">{_e(score["label"])}</div>'
            f'<div class="val">{_e(_fmt_value(score["value"], score.get("unit", "")))}</div>'
            f'{_delta_html(score)}'
            + (f'<div class="ctx">{_e(score["context"])}</div>' if score.get("context") else "")
            + "</div>"
        )

    tally = ""
    for sev in ("critical", "high", "medium", "low"):
        if counts.get(sev):
            label, fg, bg = SEV_LABEL[sev]
            tally += f'<span class="pill" style="color:{fg};background:{bg}">{counts[sev]} {label.lower()}</span>'

    notes = ""
    if doc.get("is_baseline_run"):
        notes += f'<div class="note"><b>Baseline run.</b> {_e(BASELINE_RUN_NOTE)}</div>'
    elif doc.get("compared_to"):
        notes += (
            f'<div class="note">Compared against the snapshot taken '
            f'<b>{_e(doc["compared_to"])}</b>. Deltas below are movement since then.</div>'
        )
    if doc.get("unavailable"):
        notes += (
            '<div class="note warn"><b>Not covered in this run:</b> '
            + _e(", ".join(doc["unavailable"]))
            + ". Those sections are <b>unavailable, not clean</b> — the connector was missing "
            "or returned nothing. Don't read their absence as a pass.</div>"
        )

    method = ""
    if manifest:
        rows = [
            {"Source": s["name"], "Tool": s["tool"], "Records": f"{s['record_count']:,}"}
            for s in manifest.get("sources", [])
        ]
        method = (
            '<section><div class="sec-head"><div class="kick">Method</div>'
            "<h2>What this run actually read</h2></div>" + _table_html(rows, limit=50) + "</section>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} — {_e(org)}</title>
<link rel="icon" href="{FAVICON}">
<style>{_CSS}</style></head><body>
<div class="wrap">
<header class="hero">
  <div class="eyebrow"><span class="pip"></span>LeanScale GTM Agent</div>
  <h1>{_e(title)} for <span class="hl">{_e(org)}</span></h1>
  <p class="sub">Window {_e(window.get('start', '?'))} → {_e(window.get('end', '?'))}.
  Generated {_e(doc.get('generated_at', ''))}. Read-only — nothing in your systems was modified.</p>
  {notes}
  <div class="kpis">{kpis}</div>
</header>
<section>
  <div class="sec-head"><div class="kick">Findings</div>
  <h2>{len(doc.get('findings', []))} findings, most severe first</h2></div>
  <div class="tally">{tally}</div>
  {_findings_html(doc)}
</section>
{method}
<footer>
  <div class="brand"><span class="dot"></span>LeanScale</div>
  <b>Every finding above is verifiable in your own system.</b> Each one carries the record
  count and the exact query that produced it — open the "verify this yourself" toggle and run it.
  This report was generated locally and never left your machine.
</footer>
</div></body></html>"""


def write_reports(
    doc: Dict[str, Any], run_dir: Path, manifest: Optional[Dict[str, Any]] = None
) -> Dict[str, Path]:
    """Write report.md + report.html into the run directory. Returns the paths."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path, html_path = run_dir / "report.md", run_dir / "report.html"
    md_path.write_text(render_markdown(doc, manifest), encoding="utf-8")
    html_path.write_text(render_html(doc, manifest), encoding="utf-8")
    return {"markdown": md_path, "html": html_path}


def load_manifest(run_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
