#!/usr/bin/env python3
"""
Semantic Layer readiness — findings.json -> report.md / report.html

    report.py --findings <path> --out <run-dir>

Stdlib only. Renders through the shared library so this report looks like every
other report in the suite.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import asdict  # noqa: E402

from lib import (  # noqa: E402
    Score,
    load_manifest,
    save_baseline,
    write_reports,
)

PLUGIN = "gtm-brain"


def _apply_draft_score(doc: dict, run_dir: Path) -> None:
    """Surface the drafted metrics in the headline, from draft/assumptions.json.

    Render-time and in-memory only: findings.json stays owned by analyze.py,
    so analyze/draft/report can re-run in any order and the report is the same.
    """
    ledger_path = run_dir / "draft" / "assumptions.json"
    if not ledger_path.exists():
        return
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    drafted = ledger.get("metrics_drafted") or []
    open_count = ledger.get("assumptions_open", 0)
    if not drafted:
        return
    scores = [s for s in doc.get("scores", []) if s.get("key") != "metrics_drafted"]
    scores.append(asdict(Score(
        key="metrics_drafted",
        label="Metrics drafted, awaiting review",
        value=len(drafted),
        unit="count",
        direction_good="up",
        context="%d assumption(s) to confirm — draft/DRAFTS.md is the agenda."
                % open_count,
    )))
    doc["scores"] = scores


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render the semantic layer readiness report")
    ap.add_argument("--findings", required=True, help="path to findings.json")
    ap.add_argument("--out", required=True,
                    help="run directory to write report.md / report.html")
    ap.add_argument("--no-baseline", action="store_true",
                    help="suppress the baseline note even on a first run")
    args = ap.parse_args(argv)

    findings_path = Path(args.findings)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not findings_path.exists():
        print(f"No findings at {findings_path}. Run analyze.py first.", file=sys.stderr)
        return 3

    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    if args.no_baseline:
        doc["is_baseline_run"] = False
    _apply_draft_score(doc, out_dir)

    manifest = load_manifest(out_dir) or load_manifest(findings_path.parent)
    paths = write_reports(doc, out_dir, manifest)

    # Banked after the report exists, so a run that dies while rendering never
    # leaves a snapshot behind that nobody was shown.
    if not args.no_baseline:
        snapshot = save_baseline(PLUGIN, json.loads(findings_path.read_text(encoding="utf-8")))
        print(f"baseline written: {snapshot}")

    for p in paths:
        print(f"report written: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
