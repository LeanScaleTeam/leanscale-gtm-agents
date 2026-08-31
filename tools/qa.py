#!/usr/bin/env python3
"""
Suite-wide QA gate. Run before packaging:

    python3 tools/qa.py

Checks every plugin for the things ten independent authors get wrong:
manifest schema, required files, skill frontmatter, LeanScale-internal leakage,
read-only statements, use of the shared library, and Python compile errors.

Exit 0 = shippable.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

EXPECTED = [
    "crm-hygiene", "pipeline-inspection", "meeting-to-crm", "forecast-agent",
    "sales-coach", "customer-health", "stage-architect", "lead-source", "system-map",
    "executive-reporting", "semantic-layer",
]

REQUIRED_FILES = [
    ".claude-plugin/plugin.json", "README.md", "SETUP.md",
    "skills/run/SKILL.md", "skills/setup/SKILL.md",
    "scripts/analyze.py", "scripts/report.py", "config.example.json",
]

# Strings that must never reach a customer. The author email and homepage are
# allowed and are excluded by the allowlist below.
LEAK_PATTERNS = [
    (r"teamwork", "LeanScale's internal PM tool"),
    (r"leanscale3", "LeanScale's Teamwork subdomain"),
    (r"netlify\.app", "a LeanScale-hosted site"),
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "an MCP server / site UUID"),
    (r"#partners|#internal-", "a LeanScale Slack channel"),
    (r"fireflies\.ai/api|api\.teamwork", "a LeanScale-internal endpoint"),
]
LEAK_ALLOW = [
    "anthony@leanscale.team",
    "https://leanscale.team",
    "leanscale.team",
    "LeanScale",
]

FAILS: list = []
WARNS: list = []


def fail(plugin: str, msg: str) -> None:
    FAILS.append(f"{plugin}: {msg}")


def warn(plugin: str, msg: str) -> None:
    WARNS.append(f"{plugin}: {msg}")


def parse_frontmatter(text: str):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    body = text[3:end]
    data, key = {}, None
    for line in body.splitlines():
        if re.match(r"^[A-Za-z0-9_-]+\s*:", line):
            key, _, value = line.partition(":")
            key = key.strip()
            data[key] = value.strip()
        elif key and line.strip():
            data[key] = (str(data.get(key, "")) + " " + line.strip()).strip()
    return data


def check_plugin(path: Path) -> None:
    name = path.name

    for rel in REQUIRED_FILES:
        if not (path / rel).exists():
            fail(name, f"missing required file {rel}")

    # --- manifest ---
    mf = path / ".claude-plugin" / "plugin.json"
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(name, f"plugin.json is not valid JSON: {exc}")
            manifest = {}
        if manifest.get("name") != name:
            fail(name, f"plugin.json name {manifest.get('name')!r} != directory {name!r}")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", str(manifest.get("name", ""))):
            fail(name, "plugin.json name is not kebab-case")
        for key in ("version", "description", "author"):
            if not manifest.get(key):
                fail(name, f"plugin.json missing {key}")
        if not isinstance(manifest.get("author"), dict):
            fail(name, "plugin.json author must be an object")
        if len(str(manifest.get("description", ""))) < 40:
            warn(name, "plugin.json description is very short for a marketplace listing")
        # The published docs list displayName, but the shipping CLI rejects it and
        # fails the whole manifest. Verified on 2.1.128. Keep it out.
        for banned in ("displayName", "strict", "relevance"):
            if banned in manifest:
                fail(name, f"plugin.json has {banned!r} — the shipping CLI rejects it and validation fails")

    # --- skills ---
    for skill in ("run", "setup"):
        sp = path / "skills" / skill / "SKILL.md"
        if not sp.exists():
            continue
        text = sp.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        if fm is None:
            fail(name, f"skills/{skill}/SKILL.md has no YAML frontmatter")
            continue
        if fm.get("name") != skill:
            fail(name, f"skills/{skill} frontmatter name is {fm.get('name')!r}, expected {skill!r}")
        desc = fm.get("description", "")
        if len(desc) < 60:
            fail(name, f"skills/{skill} description too thin ({len(desc)} chars) — it drives auto-invocation")
        if len(text) < 1500:
            warn(name, f"skills/{skill}/SKILL.md is only {len(text)} chars — likely underspecified")
        if skill == "run":
            low = text.lower()
            if "soql" not in low and "select " not in low:
                fail(name, "skills/run has no Salesforce query — SPEC requires real, copy-pasteable queries")
            if "hubspot" not in low:
                fail(name, "skills/run never mentions HubSpot — 33% of the customer base runs it as CRM")

        # --- portability (SPEC §3, §8) ---------------------------------------
        # ${CLAUDE_PLUGIN_ROOT} exists only inside Claude Code. A run skill that
        # references it is broken on Cursor/VS Code/Codex/Gemini, and silently:
        # the path expands to empty and every script call fails.
        if skill == "run" and "CLAUDE_PLUGIN_ROOT" in text:
            fail(name, "skills/run references ${CLAUDE_PLUGIN_ROOT} — use "
                       f'"$HOME/.leanscale-gtm/bin/{name}" instead (SPEC §8)')
        if skill == "setup":
            if "install-shim" not in text:
                fail(name, "skills/setup never runs `config.py install-shim` — nothing "
                           "creates the shim every other step depends on")
            # Only a real invocation counts; the step-0 prose names the path first.
            elif f'"$HOME/.leanscale-gtm/bin/{name}"' in text.split("install-shim")[0]:
                fail(name, "skills/setup invokes the shim before install-shim creates it")

        if f'"$HOME/.leanscale-gtm/bin/{name}"' not in text and skill == "run":
            fail(name, "skills/run never invokes the shim — scripts would be unreachable")

        # The capability contract must not be ToolSearch-only.
        if "ToolSearch" in text:
            if "Required capabilit" not in text and "| Capability |" not in text:
                fail(name, f"skills/{skill} resolves tools via ToolSearch without declaring "
                           "capabilities — non-Claude-Code clients have no ToolSearch (SPEC §3)")
            if "Otherwise" not in text and "otherwise" not in text:
                warn(name, f"skills/{skill} may lack a non-ToolSearch resolution path")

    # --- shared library usage ---
    for script in ("analyze.py", "report.py"):
        sp = path / "scripts" / script
        if not sp.exists():
            continue
        src = sp.read_text(encoding="utf-8")
        if "from lib" not in src and "import lib" not in src:
            fail(name, f"scripts/{script} does not use the shared core library")
        try:
            ast.parse(src)
        except SyntaxError as exc:
            fail(name, f"scripts/{script} syntax error line {exc.lineno}: {exc.msg}")
    analyze = (path / "scripts" / "analyze.py")
    if analyze.exists():
        src = analyze.read_text(encoding="utf-8")
        if "RunManifest" not in src:
            fail(name, "analyze.py does not use RunManifest — fail-loud contract not implemented")
        if "finalize" not in src:
            fail(name, "analyze.py never calls manifest.finalize() — empty required sources won't abort")

    # --- CLI sanity, and that the skill invokes what actually exists ---
    #
    # Two report.py shapes emerged across the suite: `--run-dir <dir>` and
    # `--findings <file> --out <dir>`. Both are fine — a customer never types
    # these, they type /<plugin>:run and the SKILL.md shells out. So the thing
    # worth enforcing is not one flag spelling, it's that every script runs and
    # that the skill calls it with flags it genuinely accepts. A skill invoking
    # a flag the script doesn't have is a broken plugin that still "validates".
    helptexts = {}
    for script in ("analyze.py", "report.py"):
        sp = path / "scripts" / script
        if not sp.exists():
            continue
        proc = subprocess.run(
            [sys.executable, str(sp), "--help"], capture_output=True, text=True, timeout=60
        )
        helptexts[script] = proc.stdout + proc.stderr
        if proc.returncode != 0:
            fail(name, f"scripts/{script} --help exits {proc.returncode}: {helptexts[script].strip()[:200]}")

    if "analyze.py" in helptexts:
        h = helptexts["analyze.py"]
        if "--raw" not in h:
            fail(name, "analyze.py has no --raw input flag")
        if not any(f in h for f in ("--out", "--run-dir", "--run")):
            fail(name, "analyze.py has no output/run-dir flag")
    if "report.py" in helptexts:
        h = helptexts["report.py"]
        if not any(f in h for f in ("--run-dir", "--run", "--findings")):
            fail(name, "report.py has no way to point it at a run")

    # Cross-check: every flag the run skill passes to a bundled script must exist.
    run_skill = path / "skills" / "run" / "SKILL.md"
    if run_skill.exists() and helptexts:
        skill_text = run_skill.read_text(encoding="utf-8")
        for script, helptext in helptexts.items():
            for m in re.finditer(re.escape(script) + r'"?((?:\s+--?[\w-]+(?:[ =][^\s\\"]+)?)+)', skill_text):
                for flag in re.findall(r"--[\w-]+", m.group(1)):
                    if flag not in helptext:
                        fail(name, f"skills/run invokes {script} {flag}, which the script does not accept")

    # --- read-only / write-safety statement ---
    readme = path / "README.md"
    if readme.exists():
        head = readme.read_text(encoding="utf-8")[:1800].lower()
        if name == "meeting-to-crm":
            # Test the CONCEPT, not one spelling. "dry run", "dry-run" and
            # "never writes on its own" are the same promise; a check that only
            # matches a hyphenated literal fails a README that is more explicit
            # than the check is.
            flat = re.sub(r"[\s\-_]+", "", head)
            contract = {
                "dry-run default": ("dryrun", "neverwrite", "nothingiswritten", "readsandproposes"),
                "human approval": ("approv", "optin", "explicit", "confirm"),
                "field allow-list": ("allowlist", "onlyeverpropose", "fieldsyoulisted", "notonthatlist"),
                "audit log": ("auditlog", "auditgtm", "appendsoneline", "audit"),
                "no overwrite by default": ("fillblanks", "neveroverwrite", "alreadyhasavalue"),
                "no unattended runs": ("refusestorun", "nocron", "unattended", "noscheduler"),
            }
            for label, alts in contract.items():
                if not any(a in flat for a in alts):
                    fail(name, f"README opening does not state the write-safety contract: {label}")
        elif "read-only" not in head and "read only" not in head:
            fail(name, "README opening does not state the plugin is read-only")

    # --- internal leakage ---
    for f in path.rglob("*"):
        if not f.is_file() or f.suffix not in (".md", ".py", ".json", ".txt", ".html"):
            continue
        if "scripts/lib" in str(f) or f.name == "VENDORED.txt":
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scrubbed = text
        for allowed in LEAK_ALLOW:
            scrubbed = scrubbed.replace(allowed, "")
        for pattern, why in LEAK_PATTERNS:
            m = re.search(pattern, scrubbed, re.IGNORECASE)
            if m:
                rel = f.relative_to(path)
                fail(name, f"{rel} leaks {why}: {m.group(0)!r}")

    # --- must not vendor-by-hand ---
    if (path / "scripts" / "lib").exists() and not (path / "scripts" / "lib" / "VENDORED.txt").exists():
        warn(name, "scripts/lib exists but wasn't produced by tools/vendor.py")


def main() -> int:
    if not PLUGINS.exists():
        print(f"error: {PLUGINS} does not exist")
        return 1

    found = sorted(p.name for p in PLUGINS.iterdir() if p.is_dir())
    print(f"plugins found: {len(found)}\n")

    for expected in EXPECTED:
        if expected not in found:
            FAILS.append(f"SUITE: plugin {expected} is missing entirely")

    for name in found:
        check_plugin(PLUGINS / name)

    # --- marketplace manifest ---
    mp = ROOT / ".claude-plugin" / "marketplace.json"
    if not mp.exists():
        FAILS.append("SUITE: .claude-plugin/marketplace.json missing")
    else:
        data = json.loads(mp.read_text(encoding="utf-8"))
        listed = {p["name"] for p in data.get("plugins", [])}
        for name in found:
            if name not in listed:
                FAILS.append(f"SUITE: {name} is built but not listed in marketplace.json")
        for name in listed:
            if name not in found:
                FAILS.append(f"SUITE: marketplace.json lists {name} but it isn't built")
        if not isinstance(data.get("owner"), dict):
            FAILS.append("SUITE: marketplace.json owner must be an object")

    # --- official validator ---
    # Optional by necessity: the `claude` CLI is on a developer's machine, not on a
    # CI runner. Absent it, every other check in this file still runs — they are all
    # pure-Python and cover the same manifest rules. Degrade loudly, never silently:
    # a skipped check that reads like a passed check is how a gate stops being one.
    if shutil.which("claude") is None:
        WARNS.append(
            "SUITE: `claude` CLI not on PATH — skipped `claude plugin validate` on "
            f"{len(found)} plugin(s). The Python manifest checks above still ran. "
            "Install Claude Code to run the official validator locally."
        )
        print("skipping `claude plugin validate` — CLI not on PATH (see warnings)")
    else:
        print("running `claude plugin validate` on each plugin...")
        for name in found:
            proc = subprocess.run(
                ["claude", "plugin", "validate", str(PLUGINS / name)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                FAILS.append(f"{name}: claude plugin validate failed\n{(proc.stdout + proc.stderr).strip()[:600]}")
            else:
                print(f"  ok   {name}")

    print()
    for w in WARNS:
        print(f"  warn {w}")
    if FAILS:
        print(f"\n{len(FAILS)} BLOCKING ISSUES:\n")
        for f in FAILS:
            print(f"  · {f}")
        return 1
    print(f"\nQA passed — {len(found)} plugins shippable ({len(WARNS)} warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
