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

from lib import (  # noqa: E402
    load_manifest,
    write_reports,
)


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

    manifest = load_manifest(out_dir) or load_manifest(findings_path.parent)
    paths = write_reports(doc, out_dir, manifest)

    for p in paths:
        print(f"report written: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
