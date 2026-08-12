#!/usr/bin/env python3
"""
Install the agents into a client that reads SKILL.md but is not Claude Code —
Cursor, VS Code / Copilot, OpenAI Codex CLI, Gemini CLI.

    python3 tools/install-skills.py --target ~/.cursor/skills
    python3 tools/install-skills.py --target .github/skills --plugin crm-hygiene
    python3 tools/install-skills.py --target ~/.cursor/skills --dry-run

Claude Code users do not need this — use `/plugin install` instead, which is a
better experience (one command, bundled lifecycle, command namespacing).

Two things this does that a plain `cp -r` does not, and both matter:

1. **Renames the skills.** Every plugin ships skills literally called `run` and
   `setup`. Inside Claude Code that is fine because commands are namespaced
   (`/crm-hygiene:run`). Copy ten plugins into one flat skills directory and you
   get ten skills called `run`, which is a collision the host resolves by luck.
   So each is installed as `<plugin>-run` / `<plugin>-setup`, and the frontmatter
   `name:` is rewritten to match — a skill whose directory and `name:` disagree is
   ignored by some hosts.

2. **Writes the shim, pointing back at THIS checkout.** The skills invoke
   `~/.leanscale-gtm/bin/<plugin>`, which must resolve to a directory containing
   `scripts/`. That is here, in the clone — not in the target skills directory,
   which holds only markdown. Setup would otherwise resolve the plugin root to
   wherever the copied SKILL.md landed and fail its own verification.

Standard library only, like everything else in this repo.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

sys.path.insert(0, str(ROOT / "core"))
from lib import config  # noqa: E402


def plugin_dirs():
    return sorted(p for p in PLUGINS.iterdir() if p.is_dir() and (p / ".claude-plugin").exists())


def rewrite_name(text: str, new_name: str) -> str:
    """Point frontmatter `name:` at the installed directory name."""
    return re.sub(r"^name:\s*.+$", f"name: {new_name}", text, count=1, flags=re.M)


def install(plugin: Path, target: Path, dry_run: bool) -> list:
    installed = []
    for skill in sorted((plugin / "skills").iterdir()):
        src = skill / "SKILL.md"
        if not src.is_file():
            continue
        name = f"{plugin.name}-{skill.name}"
        dest = target / name
        installed.append(name)
        if dry_run:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill, dest, dirs_exist_ok=True)
        out = dest / "SKILL.md"
        out.write_text(rewrite_name(src.read_text(encoding="utf-8"), name), encoding="utf-8")
    return installed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True,
                    help="the client's skills directory, e.g. ~/.cursor/skills")
    ap.add_argument("--plugin", action="append",
                    help="install only this plugin (repeatable); default is all")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen")
    args = ap.parse_args()

    target = Path(args.target).expanduser()
    wanted = set(args.plugin or [])
    chosen = [p for p in plugin_dirs() if not wanted or p.name in wanted]

    unknown = wanted - {p.name for p in plugin_dirs()}
    if unknown:
        print(f"unknown plugin(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    if not chosen:
        print("no plugins matched", file=sys.stderr)
        return 2

    if not args.dry_run:
        target.mkdir(parents=True, exist_ok=True)

    for plugin in chosen:
        names = install(plugin, target, args.dry_run)
        if args.dry_run:
            print(f"would install {plugin.name}: {', '.join(names)}")
            print(f"  would point the shim at {plugin}")
            continue
        result = config.install_shim(plugin.name, plugin)
        print(f"{plugin.name}")
        print(f"  skills -> {target}/{{{', '.join(names)}}}")
        print(f"  shim   -> {result['shim']}")

    if args.dry_run:
        return 0

    print(f"\nInstalled {len(chosen)} plugin(s) into {target}.")
    print("The shims point back at this checkout — keep it where it is, or re-run this "
          "script after moving it.")
    print("\nNext: ask your assistant to run the setup skill, e.g. \"set up crm hygiene\". "
          "Its step 0 will find the shim already in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
