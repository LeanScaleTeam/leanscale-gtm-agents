#!/usr/bin/env python3
"""
The gate that actually matters: run every plugin end-to-end against its own
bundled fixtures and assert it produces a real report.

    python3 tools/smoke.py

Static checks prove a plugin is well-formed. This proves it WORKS. It discovers
each plugin's CLI shape from its own --help (the suite has three variants, all
invoked only by that plugin's own SKILL.md) and drives it accordingly.

Runs against a throwaway LEANSCALE_GTM_HOME so a smoke test can never touch a
real customer's config or contaminate their baseline trend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

FAILS: list = []
NOTES: list = []


def helptext(script: Path) -> str:
    p = subprocess.run([sys.executable, str(script), "--help"],
                       capture_output=True, text=True, timeout=90)
    return p.stdout + p.stderr


def _pick(candidates, raw_dir: Path, flavour: str):
    """
    Choose the config/profile that belongs to THIS fixture set.

    Proximity beats name-matching: a plugin that lays out
    fixtures/salesforce/{raw,config.json} means the sibling config, full stop.
    Handing a Salesforce fixture a HubSpot config makes a healthy plugin abort
    on an empty required source and look broken.
    """
    candidates = [c for c in candidates if c.exists()]
    if not candidates:
        return None
    for scope in (raw_dir, raw_dir.parent, raw_dir.parent.parent):
        siblings = [c for c in candidates if c.parent == scope]
        if len(siblings) == 1:
            return siblings[0]
        for c in siblings:
            if flavour in c.name.lower():
                return c
        if siblings:
            hits = [c for c in siblings if "hubspot" not in c.name.lower()]
            if flavour == "salesforce" and hits:
                return hits[0]
            return siblings[0]
    for c in candidates:                      # fall back to a flavour-tagged name
        if flavour in c.name.lower() or flavour in str(c.parent).lower():
            return c
    generic = [c for c in candidates if "hubspot" not in str(c).lower()]
    return generic[0] if (flavour == "salesforce" and generic) else candidates[0]


def find_fixture_sets(plugin: Path):
    """Every fixture dir holding raw/*.json, with the config and profile that belong to it."""
    sets = []
    fixtures = plugin / "fixtures"
    if not fixtures.exists():
        return sets

    configs = [c for c in fixtures.rglob("*.json") if "config" in c.name.lower()]
    configs.append(plugin / "config.example.json")
    profiles = [p for p in fixtures.rglob("*.json") if "profile" in p.name.lower()]

    for d in sorted({p.parent for p in fixtures.rglob("*.json")}):
        if not d.name.startswith("raw"):
            continue
        flavour = "hubspot" if "hubspot" in str(d).lower() else "salesforce"
        sets.append((d, _pick(configs, d, flavour), _pick(profiles, d, flavour), flavour))
    return sets


def run(cmd, cwd, env):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)


def smoke(plugin: Path) -> None:
    name = plugin.name
    analyze, report = plugin / "scripts" / "analyze.py", plugin / "scripts" / "report.py"
    if not analyze.exists() or not report.exists():
        FAILS.append(f"{name}: missing analyze.py or report.py")
        return

    sets = find_fixture_sets(plugin)
    if not sets:
        FAILS.append(f"{name}: no fixtures/raw*/ directory — cannot prove it runs")
        return

    ah, rh = helptext(analyze), helptext(report)
    ran_any = False

    for raw_dir, cfg, prof, flavour in sets:
        home = Path(tempfile.mkdtemp(prefix=f"smoke-{name}-"))
        out = Path(tempfile.mkdtemp(prefix=f"smokeout-{name}-"))
        env = dict(os.environ, LEANSCALE_GTM_HOME=str(home))

        # Plugins that read the shared org profile need one present. Install the
        # plugin's own demo profile into the throwaway home; without it a healthy
        # plugin correctly aborts and reads as a failure.
        if prof is not None:
            shutil.copy2(prof, home / "profile.json")

        # Some plugins expect raw/ INSIDE the run dir; stage it both ways.
        shutil.copytree(raw_dir, out / "raw", dirs_exist_ok=True)

        if "--out" in ah:
            cmd = [sys.executable, str(analyze), "--raw", str(raw_dir), "--out", str(out)]
        elif "--run-dir" in ah:
            cmd = [sys.executable, str(analyze), "--run-dir", str(out), "--raw", str(raw_dir)]
        else:
            cmd = [sys.executable, str(analyze), "--run", str(out), "--raw", str(raw_dir)]
        if cfg and "--config" in ah:
            cmd += ["--config", str(cfg)]

        p = run(cmd, plugin, env)
        if p.returncode != 0:
            FAILS.append(f"{name} [{flavour}]: analyze exited {p.returncode}\n"
                         f"      cmd: {' '.join(cmd[1:])}\n"
                         f"      {(p.stdout + p.stderr).strip()[-400:]}")
            continue

        findings = out / "findings.json"
        if not findings.exists():
            FAILS.append(f"{name} [{flavour}]: analyze produced no findings.json")
            continue

        if "--run-dir" in rh:
            rcmd = [sys.executable, str(report), "--run-dir", str(out)]
        elif "--run" in rh:
            rcmd = [sys.executable, str(report), "--run", str(out)]
        else:
            rcmd = [sys.executable, str(report), "--findings", str(findings), "--out", str(out)]

        p = run(rcmd, plugin, env)
        if p.returncode != 0:
            FAILS.append(f"{name} [{flavour}]: report exited {p.returncode}\n"
                         f"      {(p.stdout + p.stderr).strip()[-400:]}")
            continue

        html = out / "report.html"
        if not html.exists():
            FAILS.append(f"{name} [{flavour}]: no report.html produced")
            continue

        text = html.read_text(encoding="utf-8", errors="ignore")
        doc = json.loads(findings.read_text(encoding="utf-8"))
        n = len(doc.get("findings", []))

        problems = []
        if len(text) < 6000:
            problems.append(f"report.html is only {len(text)} bytes")
        if not text.lstrip().lower().startswith("<!doctype html>"):
            problems.append("report.html is not a complete document")
        if re.search(r'(?:src|href)\s*=\s*["\'](?!data:|#)(https?:|//)', text):
            problems.append("report.html makes an external request")
        if "<style>" not in text:
            problems.append("report.html has no inlined CSS")
        if n == 0:
            problems.append("fixtures produced ZERO findings — they don't exercise the checks")
        if problems:
            FAILS.append(f"{name} [{flavour}]: " + "; ".join(problems))
            continue

        ran_any = True
        NOTES.append(f"  ok   {name:22s} [{flavour:10s}] {n:3d} findings · "
                     f"{len(text) // 1024:3d} KB report")
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)

    if not ran_any and not any(name in f for f in FAILS):
        FAILS.append(f"{name}: no fixture set completed")


def main() -> int:
    targets = sorted(p for p in PLUGINS.iterdir() if p.is_dir() and (p / ".claude-plugin").exists())
    print(f"smoke-testing {len(targets)} plugins against their own fixtures\n")
    for plugin in targets:
        smoke(plugin)
    for n in NOTES:
        print(n)
    if FAILS:
        print(f"\n{len(FAILS)} FAILURES:\n")
        for f in FAILS:
            print(f"  · {f}")
        return 1
    print(f"\nall {len(targets)} plugins run end-to-end and produce real reports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
