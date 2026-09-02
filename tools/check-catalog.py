#!/usr/bin/env python3
"""
Catalog gate — the customer-facing surfaces must agree with what actually ships.

    python3 tools/check-catalog.py

site/dist/ is gitignored and deployed from a workstation, so CI can never see the
zips themselves. What CI *can* see is every tracked surface that describes them,
and that is where the damage actually happened: gtm-brain shipped in
marketplace.json for a day while being absent from the catalog page, both READMEs
and INSTALL.md — invisible to anyone who scanned the QR code.

So this gate asserts the things that are checkable from git:

  1. Every plugin in marketplace.json has a download row and an agent card in
     site/index.html, and a row in README.md's table.
  2. site/index.html links no zip that has no plugin behind it.
  3. Every stated count ("Eleven agents", "Ten are strictly read-only") equals the
     real plugin count and the real read-only count.
  4. plugins/ on disk, marketplace.json and core/SPEC.md §7 list the same plugins.

Stdlib only, like everything else here.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
SITE = ROOT / "site" / "index.html"
README = ROOT / "README.md"
INSTALL = ROOT / "INSTALL.md"
SPEC = ROOT / "core" / "SPEC.md"

# The one plugin that can write. Everything else must be read-only, and the
# customer-facing copy says so in words that have to stay true.
WRITE_CAPABLE = {"meeting-to-crm"}

NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
    14: "fourteen", 15: "fifteen",
}

FAILURES: list[str] = []
CHECKS = 0


def check(ok: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILURES.append(msg)


def main() -> int:
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    listed = [p["name"] for p in marketplace["plugins"]]
    on_disk = sorted(
        p.name for p in (ROOT / "plugins").iterdir()
        if p.is_dir() and (p / ".claude-plugin" / "plugin.json").exists()
    )
    site = SITE.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    spec = SPEC.read_text(encoding="utf-8")

    total = len(listed)
    read_only = total - len([p for p in listed if p in WRITE_CAPABLE])

    # 1. marketplace vs disk
    check(sorted(listed) == on_disk,
          f"marketplace.json lists {sorted(listed)} but plugins/ holds {on_disk} — "
          "a plugin that ships without a marketplace entry is uninstallable; one "
          "listed without a directory breaks `/plugin marketplace add`.")

    # 2. every plugin reachable from the catalog page and the README
    for name in listed:
        check(f'dist/{name}.zip' in site,
              f"site/index.html has no download link for {name} — it is in the "
              f"marketplace but nobody scanning the QR code can get it.")
        check(f'/{name}:run' in site,
              f"site/index.html never mentions /{name}:run — the agent has no card "
              "on the catalog page.")
        check(f'`{name}`' in readme,
              f"README.md's plugin table has no row for {name}.")
        check(f'`{name}`' in spec,
              f"core/SPEC.md §7 does not list {name} — the build spec and the "
              "shipped suite disagree.")

    # 3. no dangling download links
    for linked in sorted(set(re.findall(r'dist/([a-z0-9-]+)\.zip', site))):
        check(linked in listed or linked == "leanscale-gtm-agents",
              f"site/index.html links dist/{linked}.zip, which no plugin produces.")

    # 4. stated counts must equal reality
    word = NUMBER_WORDS.get(total, str(total))
    ro_word = NUMBER_WORDS.get(read_only, str(read_only))
    for label, text, path in (
        ("site/index.html", site, SITE),
        ("README.md", readme, README),
        ("INSTALL.md", install, INSTALL),
    ):
        # Any "<number-word> agents/plugins" claim must use the right number.
        for m in re.finditer(r"\b([A-Za-z]+)\s+(?:GTM and RevOps\s+)?(agents|plugins)\b",
                             text, re.I):
            said = m.group(1).lower()
            if said in {w for w in NUMBER_WORDS.values()}:
                check(said == word,
                      f"{label} says '{m.group(0)}' but {total} plugins ship "
                      f"— should read '{word}'.")
        # Read-only claims: "Ten are strictly read-only", "Ten of eleven"
        for m in re.finditer(r"\b([A-Za-z]+)\s+(?:of\s+the\s+[a-z]+\s+)?"
                             r"(?:are\s+strictly\s+read-only|cannot write)", text, re.I):
            said = m.group(1).lower()
            if said in {w for w in NUMBER_WORDS.values()}:
                check(said == ro_word,
                      f"{label} says '{m.group(0)}' but {read_only} of {total} "
                      f"plugins are read-only — should read '{ro_word}'.")

    print(f"catalog gate: {CHECKS} assertions, {len(FAILURES)} failures")
    if FAILURES:
        print(f"\n{len(FAILURES)} BLOCKING ISSUES:\n")
        for f in FAILURES:
            print(f"  · {f}")
        print("\nsite/dist is gitignored, so rebuild and redeploy it by hand after "
              "fixing:\n  python3 tools/vendor.py && python3 tools/package.py")
        return 1
    print(f"  {total} plugins, {read_only} read-only — catalog, README, INSTALL "
          "and SPEC all agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
