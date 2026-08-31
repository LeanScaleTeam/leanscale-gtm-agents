#!/usr/bin/env python3
"""
Version gate. Run in CI on every pull request:

    python3 tools/check-versions.py --base origin/main

Two checks, both of which have already bitten us once:

1. **Bump on change.** If any file under plugins/<name>/ changed, that plugin's
   plugin.json version must have changed too. `claude plugin update` compares
   versions — ship a content-only fix without a bump and every installed customer
   silently keeps running the old skill. Nothing errors. Nothing looks wrong.

2. **Manifests agree.** plugin.json version must equal the marketplace entry's
   version. `claude plugin tag` enforces this at release; catching it at PR time
   is cheaper.

Exit 0 = safe to merge. Stdlib only, no network.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parent.parent
FAILURES: List[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=False).stdout


def changed_files(base: str) -> List[str]:
    out = git("diff", "--name-only", f"{base}...HEAD")
    if not out.strip():
        out = git("diff", "--name-only", base)
    return [ln for ln in out.splitlines() if ln.strip()]


def version_at(ref: str, path: str) -> str:
    raw = git("show", f"{ref}:{path}")
    if not raw.strip():
        return ""          # new file — no previous version to compare
    try:
        return json.loads(raw).get("version", "")
    except json.JSONDecodeError:
        return ""


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plugin version gate")
    ap.add_argument("--base", default="origin/main",
                    help="ref to diff against (default: origin/main)")
    args = ap.parse_args(argv)

    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entries: Dict[str, str] = {e["name"]: e.get("version", "") for e in marketplace["plugins"]}

    checks = 0

    # --- check 2 first: manifests agree (cheap, always runnable) --------------
    for name, mk_version in entries.items():
        pj = ROOT / "plugins" / name / ".claude-plugin" / "plugin.json"
        checks += 1
        if not pj.exists():
            fail(f"{name}: marketplace lists it but plugins/{name}/.claude-plugin/plugin.json is missing")
            continue
        pj_version = json.loads(pj.read_text()).get("version", "")
        if pj_version != mk_version:
            fail(f"{name}: plugin.json says {pj_version!r}, marketplace says {mk_version!r} — "
                 f"`claude plugin tag` requires these to agree")

    # --- check 1: bump on change ---------------------------------------------
    touched: Set[str] = set()
    for f in changed_files(args.base):
        parts = Path(f).parts
        if len(parts) >= 2 and parts[0] == "plugins":
            touched.add(parts[1])

    for name in sorted(touched):
        checks += 1
        rel = f"plugins/{name}/.claude-plugin/plugin.json"
        if not (ROOT / rel).exists():
            # the diff touches a plugin dir that no longer exists — a rename or
            # removal; the new-name side of a rename is checked as its own entry
            print(f"  note: plugins/{name} no longer exists (renamed or removed)")
            continue
        current = json.loads((ROOT / rel).read_text()).get("version", "")
        previous = version_at(args.base, rel)
        if previous and current == previous:
            fail(f"{name}: files changed but version is still {current!r}. "
                 f"Bump plugins/{name}/.claude-plugin/plugin.json AND its marketplace entry, "
                 f"or installed customers keep running the old version — `claude plugin update` "
                 f"compares versions and will report 'already at the latest version'.")

    # A verifier that checked nothing is a failing verifier.
    if checks == 0:
        print("FAIL: zero checks ran — the gate is not wired up correctly", file=sys.stderr)
        return 2

    print(f"version gate: {checks} assertions, {len(FAILURES)} failures")
    if touched:
        print(f"  plugins touched by this diff: {', '.join(sorted(touched))}")
    else:
        print("  no plugin files changed by this diff")

    for msg in FAILURES:
        print(f"  FAIL {msg}", file=sys.stderr)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
