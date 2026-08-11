#!/usr/bin/env python3
"""
Vendor core/lib into every plugin, and verify the copies stay identical.

An installed plugin lives in a read-only cache and CANNOT read files outside its
own directory — `../core/lib` will not resolve. So the shared library is copied
into each plugin at scripts/lib/. Build in core/lib, then run this.

    python3 tools/vendor.py           # copy core/lib -> plugins/*/scripts/lib
    python3 tools/vendor.py --check   # verify copies match; exit 1 if drifted
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_LIB = ROOT / "core" / "lib"
PLUGINS = ROOT / "plugins"

HEADER = "# VENDORED FROM core/lib — DO NOT EDIT HERE. Edit core/lib and run tools/vendor.py.\n"


def source_files():
    return sorted(p for p in CORE_LIB.glob("*.py"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def plugin_dirs():
    if not PLUGINS.exists():
        return []
    return sorted(p for p in PLUGINS.iterdir() if p.is_dir() and (p / ".claude-plugin").exists())


def vendor(check_only: bool = False) -> int:
    files = source_files()
    if not files:
        print(f"error: no python files in {CORE_LIB}")
        return 1

    targets = plugin_dirs()
    if not targets:
        print(f"error: no plugins found under {PLUGINS}")
        return 1

    drift, copied = [], 0
    for plugin in targets:
        dest = plugin / "scripts" / "lib"
        if check_only:
            if not dest.exists():
                drift.append(f"{plugin.name}: scripts/lib missing")
                continue
            for src in files:
                target = dest / src.name
                if not target.exists():
                    drift.append(f"{plugin.name}: {src.name} missing")
                elif not filecmp.cmp(src, target, shallow=False):
                    drift.append(f"{plugin.name}: {src.name} DRIFTED from core")
        else:
            dest.mkdir(parents=True, exist_ok=True)
            for src in files:
                shutil.copy2(src, dest / src.name)
                copied += 1
            (dest / "VENDORED.txt").write_text(
                "This directory is a verbatim copy of core/lib.\n"
                "Do not edit these files here — edit core/lib and re-run tools/vendor.py.\n\n"
                + "".join(f"{p.name}  sha256:{digest(p)}\n" for p in files),
                encoding="utf-8",
            )

    if check_only:
        if drift:
            print("VENDOR DRIFT DETECTED:")
            for line in drift:
                print(f"  · {line}")
            print("\nRun: python3 tools/vendor.py")
            return 1
        print(f"ok — core/lib matches all {len(targets)} plugins ({len(files)} modules each)")
        return 0

    print(f"vendored {len(files)} modules into {len(targets)} plugins ({copied} files written)")
    for plugin in targets:
        print(f"  · {plugin.name}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify only; do not write")
    args = ap.parse_args()
    sys.exit(vendor(check_only=args.check))
