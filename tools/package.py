#!/usr/bin/env python3
"""
Build the distributable zips for the catalog site.

    python3 tools/package.py

Produces, into site/dist/:
    leanscale-gtm-agents.zip   the whole marketplace (all nine + marketplace.json)
    <plugin>.zip               one plugin, as its own single-plugin marketplace

Each single-plugin zip carries its OWN .claude-plugin/marketplace.json so a
customer can take just one agent and still run
`/plugin marketplace add <folder>` — the same two commands either way.

Run tools/vendor.py first: a zip missing scripts/lib is a broken install.
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
DIST = ROOT / "site" / "dist"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"

SKIP_DIRS = {"__pycache__", ".git", ".DS_Store", "node_modules", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def keep(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.suffix in SKIP_SUFFIX:
        return False
    return path.name != ".DS_Store"


def add_tree(zf: zipfile.ZipFile, src: Path, arc_root: str) -> int:
    count = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file() or not keep(f.relative_to(src)):
            continue
        zf.write(f, f"{arc_root}/{f.relative_to(src)}")
        count += 1
    return count


def main() -> int:
    if not MARKETPLACE.exists():
        print(f"error: {MARKETPLACE} missing")
        return 1
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    entries = {p["name"]: p for p in marketplace["plugins"]}

    built = [p for p in sorted(PLUGINS.iterdir()) if p.is_dir() and (p / ".claude-plugin").exists()]
    if not built:
        print("error: no built plugins found")
        return 1

    missing_lib = [p.name for p in built if not (p / "scripts" / "lib" / "config.py").exists()]
    if missing_lib:
        print(f"error: these plugins have no vendored lib: {', '.join(missing_lib)}")
        print("run: python3 tools/vendor.py")
        return 1

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # ---- full marketplace ----
    full = DIST / "leanscale-gtm-agents.zip"
    total = 0
    with zipfile.ZipFile(full, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "leanscale-gtm-agents/.claude-plugin/marketplace.json",
            json.dumps(marketplace, indent=2) + "\n",
        )
        for name in ("README.md", "INSTALL.md"):
            f = ROOT / name
            if f.exists():
                zf.write(f, f"leanscale-gtm-agents/{name}")
        for plugin in built:
            total += add_tree(zf, plugin, f"leanscale-gtm-agents/plugins/{plugin.name}")
    print(f"  {full.name:38s} {full.stat().st_size / 1024:7.0f} KB  ({total} files, {len(built)} plugins)")

    # ---- one zip per plugin, each a standalone marketplace ----
    for plugin in built:
        entry = dict(entries.get(plugin.name, {"name": plugin.name}))
        entry["source"] = f"./plugins/{plugin.name}"
        solo = {
            "name": "leanscale-gtm",
            "description": marketplace["description"],
            "owner": marketplace["owner"],
            "plugins": [entry],
        }
        out = DIST / f"{plugin.name}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                f"{plugin.name}/.claude-plugin/marketplace.json",
                json.dumps(solo, indent=2) + "\n",
            )
            n = add_tree(zf, plugin, f"{plugin.name}/plugins/{plugin.name}")
        print(f"  {out.name:38s} {out.stat().st_size / 1024:7.0f} KB  ({n} files)")

    print(f"\npackaged {len(built) + 1} archives into {DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
