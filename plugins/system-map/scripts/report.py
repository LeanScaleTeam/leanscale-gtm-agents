#!/usr/bin/env python3
"""
system-map — report.py

Layer 3. Turns findings.json into report.md and report.html, attaches the delta
against the previous run, and saves this run's baseline snapshot. Standard
library only; nothing here touches the network and nothing is uploaded.

    python3 report.py --run-dir ./gtm-agents/system-map/2026-08-10-1422

Run one prints the baseline message and says so in the report. Run two onward
shows what moved.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from lib import (  # noqa: E402
        BASELINE_RUN_NOTE,
        ConfigError,
        apply_deltas,
        load_manifest,
        load_profile,
        redact_name,
        render_html,
        render_markdown,
        save_baseline,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - packaging guard
    raise SystemExit(
        "The shared library is missing from this plugin install "
        f"(expected {Path(__file__).resolve().parent / 'lib'}). "
        "Reinstall the plugin — scripts/lib is vendored at package time and cannot be "
        f"resolved from anywhere else. Original error: {exc}"
    ) from exc

PLUGIN = "system-map"

# Table columns whose values are people. Redacted when the shared profile sets
# redact_pii_in_reports. raw/ and findings.json are deliberately left untouched —
# the customer keeps the real names locally.
PERSON_COLUMNS = {
    "modified by", "last modified by", "installed by", "by", "owner",
    "owner of last change", "installed by", "login", "name",
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}[A-Za-z0-9.]*")


def _scrub_emails(text: Any) -> str:
    """
    Replace any address-shaped token with a stable pseudonym, leaving the rest of
    the sentence intact. Used on free text — tool-detection signals, notes — where
    a whole-value swap would destroy the meaning.
    """
    return _EMAIL_RE.sub(lambda m: f"svc-{redact_name(m.group(0))[-6:]}@redacted.invalid", str(text or ""))


def _redact_value(value: Any) -> str:
    text = str(value or "")
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"svc-{redact_name(local)[-6:]}@{domain.split('.')[-1] if '.' in domain else 'redacted'}.invalid"
    if not text or text in ("—", "unknown", "none detected", "never", "yes", "no"):
        return text
    suffix = ""
    for tag in (" (inactive)", " (active)", " (deactivated)"):
        if text.endswith(tag):
            suffix, text = tag, text[: -len(tag)]
            break
    return redact_name(text) + suffix


def redact_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Swap person names for stable pseudonyms in the report-facing payload only.

    What is redacted: person names, and every email address anywhere in the
    report-facing payload. What is NOT: service-account and connected-app DISPLAY
    names ("ZoomInfo Integration", "Legacy Data Loader"). Those are system names
    rather than people, and pseudonymising them would leave a finding nobody can
    act on. raw/ and findings.json keep the real values locally.
    """
    for finding in doc.get("findings", []):
        evidence = finding.get("evidence") or {}
        for row in evidence.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for key in list(row.keys()):
                if str(key).strip().lower() in PERSON_COLUMNS:
                    row[key] = _redact_value(row[key])
                elif isinstance(row[key], str) and "@" in row[key]:
                    # Free-text cells (tool-detection signals, orphan reasons) can
                    # carry a service-account address inside a sentence.
                    row[key] = _scrub_emails(row[key])
            # Setup-audit display text is free-form and routinely names people and
            # service accounts. There is no safe way to redact it field by field,
            # so under redaction it is withheld rather than half-scrubbed.
            if str(row.get("Type", "")).startswith("Setup audit"):
                row["Automation"] = "(setup change — detail withheld under PII redaction)"
        if finding.get("id") == "integration-user-owner-departed":
            names = ", ".join(
                f"{r.get('Name', '?')} (owner {r.get('Owner', 'unknown')})"
                for r in (evidence.get("rows") or [])[:6]
            )
            finding["what"] = (
                "These service accounts are still active and still authenticating, but the "
                "person listed as their manager or creator no longer has an active account: "
                f"{names}."
            )

    # The stack map quotes the signals that produced each detection, and those
    # quotes include integration-user logins. Service-account DISPLAY names stay —
    # they are the finding — but their addresses go.
    stack = (doc.get("sections") or {}).get("stack_map") or {}
    for tools in (stack.get("clusters") or {}).values():
        for tool in tools:
            tool["signals"] = [_scrub_emails(sig) for sig in tool.get("signals", [])]
    return doc


# --------------------------------------------------------------------------- appendix
#
# The core renderer covers scores, findings and the method table. Two things
# this plugin produces don't fit a finding: the clustered stack map, and the
# per-surface readability table that proves what the run could and could not
# see. Both are appended using the renderer's own CSS classes so the page stays
# one design, not two.

CONFIDENCE_COLOR = {
    "high": ("#4a5a20", "#E8FFCF"),
    "medium": ("#642585", "#F3EAF7"),
    "low": ("#8a4a12", "#fdf0e2"),
}


def _stack_map_html(sections: Dict[str, Any]) -> str:
    stack = sections.get("stack_map") or {}
    clusters = stack.get("clusters") or {}
    if not clusters and not stack.get("claimed_not_found"):
        return ""

    believed = set(stack.get("believed") or [])
    confirmed = set(stack.get("confirmed") or [])

    out = [
        '<section><div class="sec-head"><div class="kick">Stack map</div>',
        f'<h2>{stack.get("detected_count", 0)} tools detected, '
        f'{len(believed)} named by your team</h2></div>',
        '<p class="sub">Grouped by what each tool is for. A lime chip was on your list. '
        'A purple chip was not — those are the ones worth a conversation. Confidence reflects how '
        'distinctive the signature is: a managed-package namespace is proof, a matching service-account '
        'name is a lead.</p>',
    ]
    for cluster, tools in clusters.items():
        out.append('<article class="find">')
        out.append(f"<div class=\"find-top\"><h3>{_e(cluster)}</h3></div>")
        chips = []
        for tool in tools:
            named = tool["tool"] in confirmed
            fg, bg = ("#33420a", "#E8FFCF") if named else ("#642585", "#F3EAF7")
            chips.append(
                f'<span class="pill" style="color:{fg};background:{bg}" '
                f'title="{_e("; ".join(tool.get("signals", [])))}">{_e(tool["tool"])}'
                f' · {_e(tool.get("confidence", ""))}</span>'
            )
        out.append(f'<div class="tally">{"".join(chips)}</div>')
        rows = [
            {"Tool": t["tool"], "On your list": "yes" if t["tool"] in confirmed else "no",
             "Confidence": t.get("confidence", ""),
             "Detected via": "; ".join(t.get("signals", [])[:3])}
            for t in tools
        ]
        out.append(_simple_table(rows))
        out.append("</article>")

    if stack.get("claimed_not_found"):
        out.append(
            '<div class="note warn"><b>Named in setup, no trace in the instance:</b> '
            + _e(", ".join(stack["claimed_not_found"]))
            + ". Either the connection is not live, or it runs through middleware this run cannot see.</div>"
        )
    out.append("</section>")
    return "\n".join(out)


def _surface_table_html(doc: Dict[str, Any], manifest: Dict[str, Any]) -> str:
    """
    The pass/fail table for metadata access. This is what stops a permissions
    failure from reading as a clean org.
    """
    unavailable = doc.get("unavailable") or []
    rows = []
    for source in (manifest or {}).get("sources", []):
        readable = source["record_count"] > 0
        rows.append({
            "Metadata surface": source["name"],
            "Records read": f"{source['record_count']:,}",
            "Readable": "yes" if readable else "NO",
            "Required": "yes" if source.get("required") else "no",
        })
    if not rows and not unavailable:
        return ""

    out = [
        '<section><div class="sec-head"><div class="kick">Coverage</div>',
        "<h2>What this run could and could not read</h2></div>",
        '<p class="sub">A surface listed as unreadable below is <b>unavailable, not clean</b>. '
        'Each line names the exact permission that would fix it.</p>',
        _simple_table(rows),
    ]
    if unavailable:
        items = "".join(f"<li>{_e(item)}</li>" for item in unavailable)
        out.append(
            '<article class="find"><div class="find-top"><h3>Not covered — and how to fix each one</h3></div>'
            f'<ul style="margin:10px 0 0 18px;font-size:14.5px;line-height:1.7">{items}</ul></article>'
        )
    out.append("</section>")
    return "\n".join(out)


def _simple_table(rows: List[Dict[str, Any]], limit: int = 60) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(r.get(h, ''))}</td>" for h in headers) + "</tr>"
        for r in rows[:limit]
    )
    more = (f'<div class="meta">Showing {limit} of {len(rows):,} rows — full set in findings.json.</div>'
            if len(rows) > limit else "")
    return f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def inject_appendix(page: str, extra: str) -> str:
    """Insert plugin-specific sections just before the shared footer."""
    if not extra:
        return page
    anchor = "<footer>"
    if anchor in page:
        return page.replace(anchor, extra + "\n" + anchor, 1)
    return page.replace("</div></body></html>", extra + "\n</div></body></html>", 1)


def markdown_appendix(doc: Dict[str, Any], manifest: Dict[str, Any]) -> str:
    sections = doc.get("sections") or {}
    stack = sections.get("stack_map") or {}
    lines: List[str] = ["## Stack map", ""]
    confirmed = set(stack.get("confirmed") or [])
    for cluster, tools in (stack.get("clusters") or {}).items():
        names = ", ".join(
            f"{t['tool']}{'' if t['tool'] in confirmed else ' (not named in setup)'}" for t in tools
        )
        lines.append(f"- **{cluster}:** {names}")
    if stack.get("claimed_not_found"):
        lines += ["", f"Named in setup with no trace in the instance: {', '.join(stack['claimed_not_found'])}."]

    lines += ["", "## Metadata coverage", "", "| Surface | Records | Readable | Required |", "|---|---|---|---|"]
    for source in (manifest or {}).get("sources", []):
        lines.append(
            f"| {source['name']} | {source['record_count']:,} | "
            f"{'yes' if source['record_count'] else 'NO'} | "
            f"{'yes' if source.get('required') else 'no'} |"
        )
    if doc.get("unavailable"):
        lines += ["", "### Not covered — and the permission that fixes it", ""]
        for item in doc["unavailable"]:
            lines.append(f"- {item}")
    lines.append("")

    method = (sections.get("method") or {})
    if method:
        lines += ["## Method", "", method.get("conflict_detection", ""), ""]
        basis = method.get("write_extraction_basis") or []
        if basis:
            lines.append(f"Field-write extraction basis: {', '.join(basis)}.")
        lines += ["", f"Dormancy basis: {method.get('dormancy_basis', '')}.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="system-map — findings.json to report.md/.html")
    parser.add_argument("--run-dir", required=True, help="run directory containing findings.json")
    parser.add_argument("--no-baseline", action="store_true",
                        help="render without saving a new baseline snapshot (used by smoke tests)")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    findings_path = run_dir / "findings.json"
    if not findings_path.exists():
        print(f"No findings.json in {run_dir}. Run analyze.py first.", file=sys.stderr)
        return 2

    with findings_path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    manifest = load_manifest(run_dir) or {}

    # Baseline first: apply_deltas decides is_baseline_run and attaches movement.
    doc = apply_deltas(doc, PLUGIN)

    try:
        profile = load_profile(required=False)
    except ConfigError:
        profile = {}
    if profile.get("redact_pii_in_reports"):
        doc = redact_doc(doc)

    md = render_markdown(doc, manifest) + "\n" + markdown_appendix(doc, manifest)
    page = inject_appendix(
        render_html(doc, manifest),
        _stack_map_html(doc.get("sections") or {}) + _surface_table_html(doc, manifest),
    )

    (run_dir / "report.md").write_text(md, encoding="utf-8")
    (run_dir / "report.html").write_text(page, encoding="utf-8")

    if not args.no_baseline:
        snapshot = save_baseline(PLUGIN, doc)
        print(f"baseline       -> {snapshot}")

    print(f"report.md      -> {run_dir / 'report.md'}")
    print(f"report.html    -> {run_dir / 'report.html'}")

    if doc.get("is_baseline_run"):
        print("\nBASELINE RUN")
        print(BASELINE_RUN_NOTE)
    elif doc.get("compared_to"):
        print(f"\nCompared against the snapshot taken {doc['compared_to']}.")
        for score in doc.get("scores", []):
            delta = score.get("delta_vs_last")
            if delta:
                print(f"  {score['label']}: {'+' if delta > 0 else ''}{delta}")

    if doc.get("unavailable"):
        print(f"\n{len(doc['unavailable'])} metadata surface(s) were unreadable — unavailable, not clean.")
        for item in doc["unavailable"][:6]:
            print(f"  · {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
