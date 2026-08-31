"""
Config loading for the GTM Agents suite.

Config lives in the USER'S HOME, never inside the plugin:

    ~/.leanscale-gtm/
        profile.json              shared org profile, written once, read by all plugins
        <plugin>.json             per-plugin settings (incl. agent_root)
        mcp.json                  optional LeanScale MCP key, mode 0600
        bin/<plugin>              generated shim that locates this plugin's scripts
        baselines/<plugin>/       dated baseline snapshots
        audit/<plugin>.log        append-only write log (write-capable plugins only)

Rationale: a marketplace-installed plugin lives in a read-only cache that is
replaced on every update. Anything written into ${CLAUDE_PLUGIN_ROOT} is lost.
${CLAUDE_PLUGIN_DATA} survives updates but is scoped per-plugin, and our profile
is deliberately shared across all ten.

${CLAUDE_PLUGIN_ROOT} also only exists inside Claude Code. The skills are plain
SKILL.md and run on Cursor, VS Code, Codex and Gemini CLI too, where that variable
expands to nothing and every script invocation would break. So setup resolves the
plugin root ONCE, verifies it, and writes a shim to bin/<plugin>. Skills call the
shim and carry no path logic. The shim re-prefers ${CLAUDE_PLUGIN_ROOT} at call
time, so a marketplace update that relocates the cache cannot leave a stale path.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional

GTM_HOME = Path(os.environ.get("LEANSCALE_GTM_HOME", Path.home() / ".leanscale-gtm"))

PROFILE_SCHEMA_VERSION = 1

# Keys every plugin is entitled to assume exist once setup has run.
REQUIRED_PROFILE_KEYS = (
    "org_name",
    "crm",
    "fiscal_year_start_month",
    "quota_carrying_reps",
)


class ConfigError(RuntimeError):
    """Raised when config is missing or unusable. Message must tell the user how to fix it."""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON ({exc}).\n"
            f"Fix the file by hand, or delete it and re-run the plugin's :setup skill."
        ) from exc


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")
    tmp.replace(path)
    return path


def profile_path() -> Path:
    return GTM_HOME / "profile.json"


def plugin_config_path(plugin: str) -> Path:
    return GTM_HOME / f"{plugin}.json"


def load_profile(required: bool = True) -> Dict[str, Any]:
    """Load the shared org profile. Raises ConfigError with a fix if it's missing."""
    path = profile_path()
    if not path.exists():
        if not required:
            return {}
        raise ConfigError(
            f"No org profile at {path}.\n"
            f"Run this plugin's setup skill first — it creates the profile and is shared "
            f"across every LeanScale GTM agent, so you only do it once."
        )
    profile = _read_json(path)

    missing = [k for k in REQUIRED_PROFILE_KEYS if not profile.get(k)]
    if missing and required:
        raise ConfigError(
            f"{path} is missing required keys: {', '.join(missing)}.\n"
            f"Re-run the setup skill — it is idempotent and will fill only what's absent."
        )

    version = profile.get("schema_version", 1)
    if version > PROFILE_SCHEMA_VERSION:
        raise ConfigError(
            f"{path} was written by a newer version of the suite "
            f"(schema_version {version} > {PROFILE_SCHEMA_VERSION}). Update this plugin."
        )
    return profile


def load_plugin_config(plugin: str, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Load per-plugin settings, layered over the supplied defaults.
    Keys beginning with '_' are documentation and are stripped.
    """
    merged: Dict[str, Any] = dict(defaults or {})
    path = plugin_config_path(plugin)
    if path.exists():
        for key, value in _read_json(path).items():
            if key.startswith("_"):
                continue
            merged[key] = value
    return merged


def save_profile(profile: Dict[str, Any]) -> Path:
    profile.setdefault("schema_version", PROFILE_SCHEMA_VERSION)
    return _write_json(profile_path(), profile)


def save_plugin_config(plugin: str, config: Dict[str, Any]) -> Path:
    return _write_json(plugin_config_path(plugin), config)


def fiscal_period(profile: Dict[str, Any], year: int, month: int) -> str:
    """
    Map a calendar year/month to the customer's fiscal quarter label, e.g. 'FY27-Q3'.

    Never assume a January fiscal year — plenty of these customers don't use one.

    Two naming conventions exist and companies feel strongly about theirs, so
    `fiscal_year_naming` in the profile picks between them:
      "ends_in"   (default) FY is named for the calendar year it ENDS in.
                  Feb-2026 start -> FY2027. This is the NVIDIA/Salesforce style.
      "starts_in" FY is named for the calendar year it BEGINS in.
                  Feb-2026 start -> FY2026.
    Ask during setup; don't guess.
    """
    start = int(profile.get("fiscal_year_start_month", 1))
    naming = profile.get("fiscal_year_naming", "ends_in")
    offset = (month - start) % 12
    quarter = offset // 3 + 1

    if start == 1:
        fiscal_year = year
    else:
        starts_in = year if month >= start else year - 1
        fiscal_year = starts_in if naming == "starts_in" else starts_in + 1
    return "FY{:02d}-Q{}".format(fiscal_year % 100, quarter)


# ----------------------------------------------------------------------------
# Agent root, the shim, and the optional MCP key
# ----------------------------------------------------------------------------

# Every plugin ships this. Used to prove a candidate root is really a plugin root
# before we bake it into a shim.
ROOT_SENTINEL = "scripts/analyze.py"

SHIM_TEMPLATE = """\
#!/bin/sh
# LeanScale GTM Agents — shim for {plugin}.
# Generated by /{plugin}:setup. Re-run that skill to regenerate. Safe to delete.
#
# Resolution order: $CLAUDE_PLUGIN_ROOT, then the newest sibling of a versioned
# cache path, then the baked path.
#
# $CLAUDE_PLUGIN_ROOT is NOT exported into shell subprocesses, so it is almost
# never set in practice — it is kept only for hosts that do export it. The real
# protection is the versioned-sibling step: `claude plugin update` writes a NEW
# versioned directory and leaves the old ones in place, so a baked path stays
# valid forever and silently keeps running the version it was baked against.
BAKED={baked}
if [ -z "$1" ]; then
    echo "usage: {plugin} <script> [args...]   e.g. {plugin} analyze --run-dir ./run" >&2
    echo "       {plugin} --root              print the plugin directory" >&2
    exit 2
fi

# If BAKED sits in a versioned plugin cache (.../<plugin>/<version>), find the
# newest sibling version that still looks like a plugin. Version sort is `sort -V`
# where available, plain sort otherwise; both beat pinning to a stale directory.
NEWEST=
case "$BAKED" in
    */plugins/cache/*)
        _parent=$(dirname "$BAKED")
        if [ -d "$_parent" ]; then
            # Ascending sort, keep the LAST valid candidate. No `tac`/`tail -r`:
            # those differ between GNU and BSD and silently strand the shim on
            # whichever platform lacks the one you picked.
            for _cand in $(ls -1 "$_parent" 2>/dev/null | (sort -V 2>/dev/null || sort)); do
                if [ -f "$_parent/$_cand/{sentinel}" ]; then
                    NEWEST="$_parent/$_cand"
                fi
            done
        fi
        ;;
esac

# Resolve: $CLAUDE_PLUGIN_ROOT, then newest cache sibling, then the baked path.
RESOLVED=
for root in "$CLAUDE_PLUGIN_ROOT" "$NEWEST" "$BAKED"; do
    if [ -n "$root" ] && [ -f "$root/{sentinel}" ]; then
        RESOLVED="$root"
        break
    fi
done
if [ -z "$RESOLVED" ]; then
    echo "{plugin}: cannot locate the plugin under \\$CLAUDE_PLUGIN_ROOT or $BAKED" >&2
    echo "{plugin}: it has probably moved or been updated — re-run /{plugin}:setup" >&2
    exit 2
fi

# --root lets a skill reach non-script files: config.example.json, fixtures/, README.
if [ "$1" = "--root" ]; then
    printf '%s\\n' "$RESOLVED"
    exit 0
fi

script="$1"
shift
if [ ! -f "$RESOLVED/scripts/$script.py" ]; then
    echo "{plugin}: no such script '$script' (looked for $RESOLVED/scripts/$script.py)" >&2
    exit 2
fi
exec python3 "$RESOLVED/scripts/$script.py" "$@"
"""


def bin_dir() -> Path:
    return GTM_HOME / "bin"


def shim_path(plugin: str) -> Path:
    return bin_dir() / plugin


def mcp_key_path() -> Path:
    return GTM_HOME / "mcp.json"


def verify_agent_root(root: Path) -> Path:
    """
    Prove a candidate directory really is this plugin's root before anything is
    persisted. A wrong path must fail here, once, rather than silently on every run.
    """
    root = Path(root).expanduser().resolve()
    if not (root / ROOT_SENTINEL).is_file():
        raise ConfigError(
            f"{root} does not look like a plugin root — no {ROOT_SENTINEL} inside it.\n"
            f"Pass the directory that contains scripts/, skills/ and .claude-plugin/."
        )
    return root


def agent_root(plugin: str) -> Path:
    """
    Where this plugin's scripts live. $CLAUDE_PLUGIN_ROOT wins when set; otherwise
    the value setup persisted. Raises ConfigError naming the fix if neither works.
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env and (Path(env) / ROOT_SENTINEL).is_file():
        return Path(env).resolve()

    stored = load_plugin_config(plugin).get("agent_root")
    if stored and (Path(stored) / ROOT_SENTINEL).is_file():
        return Path(stored)

    raise ConfigError(
        f"Don't know where the {plugin} plugin lives.\n"
        f"Run /{plugin}:setup — it records the location and writes "
        f"{shim_path(plugin)}."
    )


def write_shim(plugin: str, root: Path) -> Path:
    """Generate bin/<plugin>. Returns the path written."""
    path = shim_path(plugin)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        SHIM_TEMPLATE.format(
            plugin=plugin, baked=shlex.quote(str(root)), sentinel=ROOT_SENTINEL
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def install_shim(plugin: str, root: Path) -> Dict[str, str]:
    """
    Verify the root, persist it into <plugin>.json, and write the shim.
    This is what a setup skill calls once it has resolved the plugin directory.
    """
    resolved = verify_agent_root(root)
    config = load_plugin_config(plugin)
    config["agent_root"] = str(resolved)
    save_plugin_config(plugin, config)
    return {
        "plugin": plugin,
        "agent_root": str(resolved),
        "shim": str(write_shim(plugin, resolved)),
        "config": str(plugin_config_path(plugin)),
    }


def load_mcp_key() -> Optional[str]:
    """The optional LeanScale MCP key. Absent is normal — every agent runs without it."""
    env = os.environ.get("LEANSCALE_MCP_KEY")
    if env:
        return env
    path = mcp_key_path()
    if not path.exists():
        return None
    return _read_json(path).get("key") or None


def save_mcp_key(key: str) -> Path:
    """
    Store the key at mode 0600. Deliberately NOT written to the user's shell rc:
    .mcp.json can only interpolate a real environment variable, so setup prints the
    export line instead of editing a file it does not own.

    Written through os.open with the mode set at CREATION rather than chmod'ed after.
    The tmp-file-then-rename path in _write_json would leave the key briefly readable
    at the process umask, which is not acceptable for a credential.
    """
    path = mcp_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"key": key}, fh, indent=2)
        fh.write("\n")
    # An existing file keeps its old mode through O_CREAT, so enforce it explicitly.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def profile_summary(profile: Dict[str, Any]) -> str:
    """One-line human summary — print this at the top of a run so the user sees the assumptions."""
    crm = (profile.get("crm") or {}).get("system", "unknown")
    return (
        f"{profile.get('org_name', 'Unknown org')} · CRM={crm} · "
        f"FY starts month {profile.get('fiscal_year_start_month', '?')} · "
        f"{profile.get('quota_carrying_reps', '?')} quota-carrying reps · "
        f"material deal floor {profile.get('material_deal_floor', 0):,}"
    )


# ----------------------------------------------------------------------------
# CLI — so a setup skill can install the shim without importing anything.
#
#   python3 "$AGENT_ROOT/scripts/lib/config.py" install-shim \
#       --plugin crm-hygiene --root "$AGENT_ROOT"
#
# This is the one bootstrap that cannot go through the shim, because it is what
# creates the shim.
# ----------------------------------------------------------------------------

def _main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="config.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_shim = sub.add_parser("install-shim", help="verify a plugin root, persist it, write bin/<plugin>")
    p_shim.add_argument("--plugin", required=True)
    p_shim.add_argument("--root", required=True, help="the plugin directory (contains scripts/, skills/)")

    p_key = sub.add_parser("save-mcp-key", help="store the LeanScale MCP key, read from stdin, mode 0600")

    sub.add_parser("mcp-key-status", help="report whether an MCP key is present, without printing it")

    args = parser.parse_args(argv)

    try:
        if args.command == "install-shim":
            result = install_shim(args.plugin, Path(args.root))
            print(json.dumps(result, indent=2))
            print(
                f"\nCall it as:  \"$HOME/.leanscale-gtm/bin/{args.plugin}\" analyze --help",
                file=sys.stderr,
            )
            return 0

        if args.command == "save-mcp-key":
            # stdin, not argv — an API key in a command line is visible in the
            # process list and lands in shell history.
            key = sys.stdin.read().strip()
            if not key:
                print("no key on stdin", file=sys.stderr)
                return 2
            path = save_mcp_key(key)
            print(f"wrote {path} (mode 0600)")
            print(
                "\nOne manual step. A .mcp.json can only interpolate a real environment\n"
                "variable, and this setup does not edit files it doesn't own — so add\n"
                "this line to your shell profile (~/.zshrc, ~/.bashrc) yourself:\n"
                f"\n    export LEANSCALE_MCP_KEY={shlex.quote(key)}\n"
                "\nThen restart your client. Until you do, the three plugins that use the\n"
                "LeanScale corpus still run — they just won't cite playbooks or benchmarks.\n"
            )
            return 0

        if args.command == "mcp-key-status":
            key = load_mcp_key()
            source = (
                "environment" if os.environ.get("LEANSCALE_MCP_KEY")
                else "file" if key else "absent"
            )
            print(json.dumps({"present": bool(key), "source": source,
                              "path": str(mcp_key_path())}, indent=2))
            return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
