#!/usr/bin/env python3
"""
crm-hygiene — Layer 3. findings.json -> report.md + report.html, then bank the baseline.

    python3 report.py --findings <run>/findings.json --out <run>

Rendering comes from the shared core library so every LeanScale GTM agent's report
looks like the same product. This script adds three things on top of it:

  * the Hygiene Index derivation table, appended as a finding-shaped section, because
    a score whose arithmetic is hidden is a score nobody trusts;
  * PII redaction when the shared profile asks for it — report.md / report.html get
    pseudonyms, findings.json and raw/ stay untouched on the local disk;
  * the baseline write. It happens HERE, after the report exists, so a run that dies
    during rendering does not silently bank a snapshot it never showed anyone.

Offline, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    BASELINE_RUN_NOTE,
    ConfigError,
    crmutil,
    load_manifest,
    load_profile,
    save_baseline,
    write_reports,
)

PLUGIN = "crm-hygiene"

# Column headers whose values name a human. Everything else in an evidence table is a
# record id, a count, a field API name or a domain, none of which are personal data.
PII_COLUMNS = re.compile(r"(owner|contact|rep|user|name|email|champion)", re.I)
NON_PII_EXACT = {"api name", "field", "object", "label", "type", "rule", "record type",
                 "colliding values", "value", "normalized name", "pipeline", "stage",
                 "names", "account", "opportunity"}
EMAIL_IN_TEXT = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PROSE_FIELDS = ("title", "what", "why_it_matters", "recommended_fix")


def redact_value(value: Any) -> str:
    text = str(value)
    if "@" in text:
        return EMAIL_IN_TEXT.sub(lambda m: crmutil.redact_name(m.group(0)) + "@redacted", text)
    return crmutil.redact_name(text)


def redact(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pseudonymise people in the rendered report only — findings.json and raw/ stay
    unredacted on the local disk, because the customer needs the real ids to act.

    Two passes, and both are needed. Table columns whose header names a person get
    replaced by value. Then the known person names are scrubbed out of the prose:
    a finding that says "top holders: Dana Whitfield (14)" leaks the name in the most
    readable place in the document, and column-based redaction never touches it.
    """
    people = [str(p) for p in ((doc.get("sections") or {}).get("people") or []) if str(p).strip()]
    people.sort(key=len, reverse=True)                 # longest first: "Ana Ruiz" before "Ana"
    name_re = (re.compile("|".join(re.escape(p) for p in people)) if people else None)

    def scrub(text: Any) -> str:
        out = str(text)
        if name_re is not None:
            out = name_re.sub(lambda m: crmutil.redact_name(m.group(0)), out)
        return EMAIL_IN_TEXT.sub(
            lambda m: crmutil.redact_name(m.group(0)) + "@redacted", out)

    for finding in doc.get("findings", []):
        evidence = finding.get("evidence") or {}
        rows = evidence.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for key, value in list(row.items()):
                    header = str(key).strip().lower()
                    if header in NON_PII_EXACT or not PII_COLUMNS.search(header):
                        continue
                    if not crmutil.is_blank(value):
                        row[key] = redact_value(value)
        for field in PROSE_FIELDS:
            if finding.get(field):
                finding[field] = scrub(finding[field])
    for score in doc.get("scores", []):
        if score.get("context"):
            score["context"] = scrub(score["context"])
    doc["_redacted"] = True
    return doc


def index_section(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Render the Hygiene Index derivation as an extra card at the bottom of the report.

    It rides in as a `low` finding because the shared renderer only knows how to draw
    findings — and because the derivation genuinely belongs in the same document as
    the number, not in a footnote nobody opens.
    """
    section = (doc.get("sections") or {}).get("hygiene_index")
    if not section or not section.get("pillars"):
        return None
    excluded = section.get("excluded_pillars") or []
    tail = ""
    if excluded:
        tail = (" Excluded from the calculation because nothing measurable came back: "
                + ", ".join(excluded) + ". The remaining weights were renormalized, so the "
                "score stays comparable across runs — but it is measuring less.")
    return {
        "id": "hygiene-index-derivation",
        "severity": "low",
        "title": f"How the Hygiene Index of {section.get('value')} was calculated",
        "what": ("Six pillars, each a measured clean rate between 0 and 1, combined as a "
                 "weighted average: 100 x sum(weight x clean rate) / sum(weight)." + tail),
        "why_it_matters": ("A score you cannot reproduce is a score you cannot argue with, and a "
                           "score nobody argues with is one nobody acts on. Every input below is "
                           "a count from this run and every weight is in your config file, so "
                           "you can rebuild this number by hand."),
        "recommended_fix": ("Reweight the pillars in ~/.leanscale-gtm/crm-hygiene.json under "
                            "hygiene_index_weights if your priorities differ. Keep the weights "
                            "stable after that — changing them mid-programme makes the trend "
                            "line meaningless."),
        "evidence": {"count": section.get("value"), "rows": section["pillars"],
                     "query": section.get("formula", "")},
        "effort": "quick",
        "owner_hint": "RevOps",
        "delta_vs_last": None,
    }


def coverage_note(doc: Dict[str, Any], manifest: Optional[Dict[str, Any]]) -> None:
    """Fold the method caveats into `unavailable` so they render where people look."""
    notes = (doc.get("sections") or {}).get("notes") or []
    if notes:
        doc.setdefault("method_notes", notes)
    if manifest:
        truncated = [s["name"] for s in manifest.get("sources", []) if "truncated" in
                     str(s.get("note", "")).lower()]
        if truncated:
            doc.setdefault("unavailable", []).append(
                "Complete counts for " + ", ".join(truncated) +
                " — the fetch hit a page limit, so those numbers are a floor")
        # analyze.py records a truncated fetch as a manifest WARNING, not as a source
        # note, and the note-only path above never saw it. A truncated fetch that never
        # reaches the page turns "1,204 accounts share 511 domains" from a floor into a
        # stated total — the exact confident-wrong number the fail-loud contract exists
        # to prevent. Fold the warnings in too, skipping any already covered above.
        for warning in manifest.get("warnings", []):
            text = str(warning)
            if "truncated" not in text.lower():
                continue
            source = text.split(":", 1)[0].strip()
            if source in truncated:
                continue
            doc.setdefault("unavailable", []).append(
                f"Complete counts for {source} — the fetch was truncated, so those "
                "numbers are a floor, not a total")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="crm-hygiene reporter (offline, stdlib only)")
    ap.add_argument("--findings", required=True, help="path to findings.json")
    ap.add_argument("--out", required=True, help="run directory to write report.md / report.html")
    ap.add_argument("--no-baseline", action="store_true",
                    help="render only; do not save a baseline snapshot")
    ap.add_argument("--redact", action="store_true",
                    help="force PII redaction regardless of the profile setting")
    args = ap.parse_args(argv)

    findings_path = Path(args.findings).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not findings_path.exists():
        print(f"No findings at {findings_path}. Run analyze.py first.", file=sys.stderr)
        return 3
    doc = json.loads(findings_path.read_text(encoding="utf-8"))

    try:
        profile = load_profile(required=False)
    except ConfigError:
        profile = {}
    if args.redact or profile.get("redact_pii_in_reports"):
        doc = redact(doc)

    manifest = load_manifest(out_dir) or load_manifest(findings_path.parent)
    coverage_note(doc, manifest)

    derivation = index_section(doc)
    if derivation:
        doc.setdefault("findings", []).append(derivation)
        counts = doc.setdefault("counts_by_severity", {})
        counts["low"] = counts.get("low", 0) + 1

    paths = write_reports(doc, out_dir, manifest)

    run = (doc.get("sections") or {}).get("run") or {}
    baseline_key = run.get("baseline_key") or PLUGIN
    fixture = bool(run.get("fixture"))
    allowed = run.get("baseline_enabled", True) and not args.no_baseline
    if fixture and "LEANSCALE_GTM_HOME" not in os.environ:
        allowed = False

    if allowed:
        # bank the comparable slice of the ORIGINAL doc, without the derivation card
        snapshot_doc = json.loads(findings_path.read_text(encoding="utf-8"))
        snapshot = save_baseline(baseline_key, snapshot_doc)
        if doc.get("is_baseline_run"):
            print("BASELINE RUN — " + BASELINE_RUN_NOTE)
        else:
            print(f"Compared against the snapshot taken {doc.get('compared_to', 'previously')}.")
        print(f"baseline  : {snapshot}")
    else:
        reason = ("fixture run — no baseline is written for sample data"
                  if fixture else "baseline writing was disabled for this run")
        print(f"baseline  : skipped ({reason})")

    if doc.get("_redacted"):
        print("redaction : person names and emails replaced with stable pseudonyms in the "
              "report; findings.json is unchanged")
    print(f"markdown  : {paths['markdown']}")
    print(f"html      : {paths['html']}")
    print(f"findings  : {len(doc.get('findings', []))} rendered, "
          f"{len(doc.get('unavailable', []))} check group(s) unavailable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
