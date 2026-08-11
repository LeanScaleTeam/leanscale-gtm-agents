#!/usr/bin/env python3
"""
Normalize plugin manifests to what the shipping CLI actually accepts.

    python3 tools/normalize.py

The published plugin docs list manifest keys the shipping `claude` binary
rejects outright — `displayName` fails with `root: Unrecognized key` and takes
the whole manifest down with it. Customers will be on a range of CLI versions,
so we ship the conservative manifest that validates everywhere.

Idempotent. Run after the plugins are written and before QA.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

# Documented but rejected by the shipping validator (verified on CLI 2.1.128).
BANNED = ("displayName", "strict", "relevance", "defaultEnabled")

# The order we want keys written in, for readable diffs.
ORDER = ("name", "version", "description", "author", "homepage", "license", "keywords")


def main() -> int:
    changed = 0
    for manifest_path in sorted(PLUGINS.glob("*/.claude-plugin/plugin.json")):
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        removed = [k for k in BANNED if k in data]
        for key in removed:
            data.pop(key)

        ordered = {k: data[k] for k in ORDER if k in data}
        ordered.update({k: v for k, v in data.items() if k not in ordered})

        new = json.dumps(ordered, indent=2) + "\n"
        if new != manifest_path.read_text(encoding="utf-8"):
            manifest_path.write_text(new, encoding="utf-8")
            changed += 1
            note = f" (removed {', '.join(removed)})" if removed else " (reordered)"
            print(f"  fixed {manifest_path.parent.parent.name}{note}")

    print(f"\nnormalized {changed} manifest(s)" if changed else "\nall manifests already clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
