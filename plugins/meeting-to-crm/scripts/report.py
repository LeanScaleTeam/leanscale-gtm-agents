#!/usr/bin/env python3
"""
meeting-to-crm — findings.json -> report.md + report.html

Layer 3. Renders with the shared LeanScale renderer (core/lib/render.py) and
then injects the one thing this plugin has that the others do not: the diff
table. Every proposed change, with the record it targets, the value that is
there today, the value being proposed, the verbatim quote and timestamp that
justifies it, and a confidence.

The report is a local file. It is never uploaded anywhere.

    python3 scripts/report.py --run ./gtm-agents/meeting-to-crm/2026-08-10-1400
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    apply_deltas,
    load_manifest,
    load_profile,
    save_baseline,
    write_reports,
)
from lib.config import ConfigError  # noqa: E402
from lib.crmutil import redact_name  # noqa: E402

import diff as diffmod  # noqa: E402

PLUGIN = "meeting-to-crm"

STATUS_STYLE = {
    "ready": ("Ready", "#2f6b34", "#E8FFCF"),
    "dropped": ("Dropped", "#8a4a12", "#fdf0e2"),
}
DROP_STYLE = {
    "critical": ("#8a1c3b", "#fdeaf0"),
    "high": ("#8a4a12", "#fdf0e2"),
    "medium": ("#642585", "#F3EAF7"),
    "low": ("#4a5a20", "#E8FFCF"),
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _short(value: Any, limit: int = 90) -> str:
    return diffmod._short(value, limit)


# ---------------------------------------------------------------------- redaction


class Redactor:
    """
    Honours profile.redact_pii_in_reports. raw/ and findings.json stay
    unredacted on disk — only what a human might forward gets pseudonymised.
    """

    def __init__(self, enabled: bool, names: List[str]):
        self.enabled = enabled
        self.names = sorted({n.strip() for n in names if n and len(n.strip()) > 2}, key=len, reverse=True)

    def text(self, value: Any) -> Any:
        if not self.enabled or not isinstance(value, str):
            return value
        out = EMAIL_RE.sub(lambda m: f"contact-{redact_name(m.group(0))[-6:]}@redacted", value)
        for name in self.names:
            if name in out:
                out = out.replace(name, redact_name(name))
        return out

    def walk(self, node: Any) -> Any:
        if not self.enabled:
            return node
        if isinstance(node, dict):
            return {k: self.walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self.walk(v) for v in node]
        return self.text(node)


def _people_from_raw(run: Path) -> List[str]:
    """
    Every person this run knows by name: meeting attendees, transcript speakers,
    contacts already on the matched records, and stakeholders proposed for creation.

    Caveat worth knowing: a third party named only in the middle of a spoken
    sentence is not detectable this way. Redaction here is a strong reduction,
    not a guarantee — see SETUP.md.
    """
    names: List[str] = []
    raw = run / "raw"

    doc = diffmod.read_json(raw / "meetings.json", default={}) or {}
    for meeting in (doc.get("meetings") if isinstance(doc, dict) else doc) or []:
        for att in meeting.get("attendees") or []:
            if att.get("name"):
                names.append(str(att["name"]))
        for seg in meeting.get("transcript") or []:
            if seg.get("speaker"):
                names.append(str(seg["speaker"]))

    records = diffmod.read_json(raw / "crm_records.json", default={}) or {}
    for rec in (records.get("records") if isinstance(records, dict) else records) or []:
        for pool in (rec.get("children") or {}).values():
            for item in pool or []:
                for key in ("ContactName", "Name", "name"):
                    if item.get(key):
                        names.append(str(item[key]))

    proposals = diffmod.read_json(raw / "proposals.json", default={}) or {}
    for child in proposals.get("child_records") or []:
        values = child.get("values") or {}
        first = values.get("FirstName") or values.get("firstname") or ""
        last = values.get("LastName") or values.get("lastname") or ""
        if first and last:
            names.append(f"{first} {last}")
        for key in ("ContactName", "Name"):
            if values.get(key):
                names.append(str(values[key]))
    return names


# ------------------------------------------------------------------- diff section


def _quote_cell(row: Dict[str, Any], red: Redactor) -> str:
    quote = _e(red.text(_short(row.get("quote"), 220)))
    who = _e(red.text(row.get("quote_speaker") or "speaker unknown"))
    when = _e(row.get("quote_ts") or "??:??")
    title = _e(red.text(_short(row.get("meeting_title"), 60)))
    if not row.get("quote"):
        return '<span style="color:#8a1c3b">no quote — dropped</span>'
    return (f'“{quote}”<div class="meta" style="margin:6px 0 0;padding:0;border:0">'
            f'{who} · {when} · {title}</div>')


def _status_cell(row: Dict[str, Any]) -> str:
    if row.get("status") == "ready":
        label, fg, bg = STATUS_STYLE["ready"]
        policy = row.get("overwrite_policy", "if_blank")
        note = {"if_blank": "fills a blank", "always": "replaces existing", "append": "appends below existing"}.get(policy, policy)
        return (f'<span class="pill" style="color:{fg};background:{bg}">{label}</span>'
                f'<div class="meta" style="margin:6px 0 0;padding:0;border:0">{_e(note)}</div>')
    reasons = row.get("drop_reasons") or []
    code = reasons[0]["code"] if reasons else "dropped"
    sev = diffmod.DROP_SEVERITY.get(code, "low")
    fg, bg = DROP_STYLE.get(sev, DROP_STYLE["low"])
    message = _e(_short(reasons[0]["message"] if reasons else "", 190))
    return (f'<span class="pill" style="color:{fg};background:{bg}">{_e(code)}</span>'
            f'<div class="meta" style="margin:6px 0 0;padding:0;border:0">{message}</div>')


def _rows_table(rows: List[Dict[str, Any]], red: Redactor) -> str:
    if not rows:
        return '<p class="sub">Nothing in this group.</p>'
    head = ("<tr><th>#</th><th>Object · record</th><th>Field</th><th>Current value</th>"
            "<th>Proposed value</th><th>Evidence from the call</th><th>Conf.</th><th>Status</th></tr>")
    body = []
    for row in rows:
        record = red.text(row.get("record_name") or "") or "(unresolved)"
        rid = row.get("record_id") or ""
        current = _short(red.text(row.get("current_value")), 120)
        body.append(
            "<tr>"
            f'<td><code>{_e(row.get("id"))}</code></td>'
            f'<td><b>{_e(row.get("object"))}</b><div class="meta" style="margin:4px 0 0;padding:0;border:0">'
            f'{_e(record)}<br><code>{_e(rid)}</code></div></td>'
            f'<td><code>{_e(row.get("field"))}</code><div class="meta" style="margin:4px 0 0;padding:0;border:0">'
            f'{_e(row.get("field_label"))}</div></td>'
            f'<td>{_e(current) or "<i>(blank)</i>"}</td>'
            f'<td><b>{_e(_short(red.text(row.get("final_value")), 200))}</b></td>'
            f"<td>{_quote_cell(row, red)}</td>"
            f'<td>{_e(row.get("confidence"))}</td>'
            f"<td>{_status_cell(row)}</td>"
            "</tr>"
        )
    return f'<div class="tblwrap"><table><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>'


def diff_section_html(diff: Dict[str, Any], red: Redactor, run: Path) -> str:
    rows = diff.get("rows") or []
    ready = [r for r in rows if r["status"] == "ready"]
    dropped = [r for r in rows if r["status"] == "dropped"]
    stats = diff.get("stats") or {}
    token = diff.get("token", "")
    applied = diffmod.read_json(run / "applied.json", default=None)

    if applied:
        banner = (
            f'<div class="note"><b>Applied.</b> {applied.get("fields_applied", 0)} field(s) across '
            f'{applied.get("records_touched", 0)} record(s), approved by '
            f'<b>{_e(applied.get("approved_by"))}</b>. Every write is on one line in '
            f'<code>{_e(applied.get("audit_log"))}</code> with its old value, its new value and the call it '
            f'came from.</div>'
        )
    else:
        banner = (
            '<div class="note"><b>Dry run — nothing has been written.</b> This agent proposes; a named human '
            'disposes. To apply, a person reads the table below and then, on a later turn, runs the approve '
            f'command with the token <code>{_e(token)}</code>. That token is a fingerprint of exactly these '
            'rows: change one value and the token no longer matches, so an unread batch cannot be approved.</div>'
        )

    approve_block = (
        "<details><summary>The exact approval command (read the table first)</summary>"
        f"<pre>python3 \"${{CLAUDE_PLUGIN_ROOT}}/scripts/diff.py\" approve \\\n"
        f"    --run {_e(run)} \\\n"
        f"    --approved-by \"Your Name\" \\\n"
        f"    --token {_e(token)} \\\n"
        f"    --apply\n\n"
        "# approve a subset instead:   --only p-101,p-104,c-001\n"
        "# without --apply it refuses. With no rendered report it refuses.\n"
        "# with a token that does not match these rows it refuses.</pre></details>"
    )

    return f"""
<section>
  <div class="sec-head"><div class="kick">The diff</div>
  <h2>{len(ready)} change(s) proposed, {len(dropped)} stopped by the guards</h2></div>
  {banner}
  <div class="tally">
    <span class="pill" style="color:#2f6b34;background:#E8FFCF">{stats.get('ready', 0)} ready</span>
    <span class="pill" style="color:#642585;background:#F3EAF7">{stats.get('records_touched', 0)} records</span>
    <span class="pill" style="color:#4a5a20;background:#E8FFCF">{stats.get('blanks_filled', 0)} blanks filled</span>
    <span class="pill" style="color:#8a4a12;background:#fdf0e2">{stats.get('existing_preserved', 0)} existing values preserved</span>
  </div>
  <h3 style="margin:18px 0 6px;font-size:17px;font-weight:800;color:#301934">Proposed — awaiting approval</h3>
  {_rows_table(ready, red)}
  <h3 style="margin:26px 0 6px;font-size:17px;font-weight:800;color:#301934">Stopped by the guards</h3>
  <p class="sub" style="margin-bottom:8px">Each of these was drafted and then dropped in Python, before any
  approval was possible. The reason is on every row.</p>
  {_rows_table(dropped, red)}
  {approve_block}
</section>
"""


def diff_section_markdown(diff: Dict[str, Any], red: Redactor) -> str:
    rows = diff.get("rows") or []
    lines = ["", "## The diff", "",
             "| # | Object | Record | Field | Current | Proposed | Quote | At | Conf | Status |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        reasons = row.get("drop_reasons") or []
        status = "ready" if row["status"] == "ready" else f"dropped: {reasons[0]['code'] if reasons else '?'}"
        cells = [
            row["id"], row["object"], _short(red.text(row.get("record_name")), 28) or (row.get("record_id") or "—"),
            row["field"], _short(red.text(row.get("current_value")), 40) or "(blank)",
            _short(red.text(row.get("final_value")), 60),
            _short(red.text(row.get("quote")), 70), f"{row.get('quote_ts') or '??'}",
            str(row.get("confidence")), status,
        ]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    lines += ["", f"Approval token: `{diff.get('token', '')}` — required to apply, and only valid for "
                  "exactly these rows.", ""]
    return "\n".join(lines)


def _inject(html_text: str, section: str) -> str:
    anchor = '<section>\n  <div class="sec-head"><div class="kick">Findings</div>'
    if anchor in html_text:
        return html_text.replace(anchor, section + anchor, 1)
    if "<section>" in html_text:
        return html_text.replace("<section>", section + "<section>", 1)
    return html_text.replace("<footer>", section + "<footer>", 1)


# -------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="meeting-to-crm: render report.md and report.html.")
    parser.add_argument("--run", required=True, help="run directory containing findings.json")
    parser.add_argument("--no-baseline", action="store_true",
                        help="re-render without taking another baseline snapshot")
    args = parser.parse_args(argv)

    run = Path(args.run)
    doc = diffmod.read_json(run / "findings.json")
    if doc is None:
        print(f"No findings.json in {run}. Run analyze.py first.", file=sys.stderr)
        return 1
    diff = diffmod.read_json(run / "diff.json", default={}) or {}
    manifest = load_manifest(run)

    try:
        profile = load_profile(required=False) or {}
    except ConfigError:
        profile = {}
    red = Redactor(bool(profile.get("redact_pii_in_reports")), _people_from_raw(run))

    # If a batch has since been approved and applied, the headline numbers come from
    # the audit trail rather than from the proposal — "fields applied" must mean
    # fields that actually landed, not fields we hoped would.
    applied = diffmod.read_json(run / "applied.json", default=None)
    if applied:
        for score in doc.get("scores") or []:
            if score.get("key") == "fields_applied":
                score["value"] = int(applied.get("fields_applied", 0))
                score["context"] = (f"approved by {applied.get('approved_by')}"
                                    + (f" · {applied.get('failed')} failed" if applied.get("failed") else ""))
            elif score.get("key") == "records_touched":
                score["value"] = int(applied.get("records_touched", 0))
                score["context"] = "records actually changed"
        (run / "findings.json").write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")

    doc = apply_deltas(doc, PLUGIN)
    rendered = red.walk(doc)
    # Redact the whole diff once rather than cell by cell: drop-reason messages and
    # row labels carry names too, and a leak in a footnote is still a leak.
    diff = red.walk(diff)

    paths = write_reports(rendered, run, manifest)
    section = diff_section_html(diff, red, run)
    html_text = paths["html"].read_text(encoding="utf-8")
    html_text = html_text.replace(
        "Read-only — nothing in your systems was modified.",
        "Dry run by default — nothing is written to your CRM without a named human approving this exact diff.",
    ).replace(
        "Produced by a LeanScale GTM Agent. Read-only: nothing in your CRM was modified.",
        "Produced by a LeanScale GTM Agent.",
    )
    paths["html"].write_text(_inject(html_text, section), encoding="utf-8")

    md = paths["markdown"].read_text(encoding="utf-8")
    md = md.replace(
        "Produced by a LeanScale GTM Agent. Read-only: nothing in your CRM was modified.",
        "Produced by a LeanScale GTM Agent. This report proposes; it does not write. Applying requires "
        "`--apply`, a named approver, and the token above.",
    )
    paths["markdown"].write_text(md + diff_section_markdown(diff, red), encoding="utf-8")

    marker = run / ".baseline-saved"
    if not args.no_baseline and not marker.exists():
        snapshot = save_baseline(PLUGIN, doc)
        marker.write_text(str(snapshot), encoding="utf-8")
        note = f"baseline  {snapshot}"
    else:
        note = "baseline  (already taken for this run)"

    print(f"report    {paths['html']}")
    print(f"          {paths['markdown']}")
    print(note)
    if doc.get("is_baseline_run"):
        print("\nThis is your baseline run. The comparison starts next run.")
    if not applied:
        print("\nDRY RUN — nothing in your CRM was modified.")
        print(f"Approval token: {diff.get('token', '')}")
        print("Open report.html, read the diff table, and approve on a LATER turn if it is right.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
