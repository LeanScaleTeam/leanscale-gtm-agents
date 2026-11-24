#!/usr/bin/env python3
"""
crm-hygiene on an org that has no opportunities.

A public user ran crm-hygiene on a practice Salesforce org built like a creator program:
accounts, contacts and campaigns, but no deals. The run aborted and blamed the connection
or permissions. Partner and creator programs will hit this every time.

What has to hold:
  * the fetch proved the org has none (verified_empty + verified_by)  -> the run completes,
    and the report says the deal checks did not run (never that they passed)
  * an empty opportunities file with no proof                          -> still aborts (exit 2)
  * a verified_empty flag with no verified_by                          -> still aborts

    python3 tools/test_crm_hygiene_no_opps.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "crm-hygiene"
ANALYZE = PLUGIN / "scripts" / "analyze.py"
FIXTURES = PLUGIN / "fixtures" / "raw"


def run_with_opportunities(envelope_extra: dict) -> tuple[int, str, dict]:
    work = Path(tempfile.mkdtemp(prefix="crmh-noopps-"))
    raw, out, home = work / "raw", work / "out", work / "home"
    shutil.copytree(FIXTURES, raw)
    for name in ("opportunities", "opportunity_contact_roles"):
        path = raw / f"{name}.json"
        env = json.loads(path.read_text())
        env["records"], env["record_count"] = [], 0
        if name == "opportunities":
            env.update(envelope_extra)
        path.write_text(json.dumps(env))
    env_vars = {**os.environ, "LEANSCALE_GTM_HOME": str(home)}
    p = subprocess.run([sys.executable, str(ANALYZE), "--raw", str(raw), "--out", str(out)],
                       capture_output=True, text=True, env=env_vars)
    findings = {}
    if (out / "findings.json").exists():
        findings = json.loads((out / "findings.json").read_text())
    return p.returncode, p.stdout + p.stderr, findings


class NoOpportunities(unittest.TestCase):
    def test_verified_empty_org_completes_and_says_the_deal_checks_did_not_run(self):
        code, log, findings = run_with_opportunities({
            "verified_empty": True,
            "verified_by": "SELECT COUNT() FROM Opportunity = 0 as a user with View All Data",
        })
        self.assertEqual(code, 0, log)
        unavailable = " ".join(findings.get("unavailable") or [])
        self.assertIn("Deal checks", unavailable)
        self.assertIn("did not run", unavailable)
        ids = {f.get("id") for f in findings.get("findings") or []}
        self.assertNotIn("dupe-open-opportunities", ids)
        self.assertTrue(ids, "the account and contact checks should still produce findings")

    def test_an_empty_pull_with_no_proof_still_aborts(self):
        code, log, _ = run_with_opportunities({})
        self.assertEqual(code, 2, log)
        self.assertIn("opportunities", log)

    def test_a_flag_without_evidence_still_aborts(self):
        code, log, _ = run_with_opportunities({"verified_empty": True})
        self.assertEqual(code, 2, log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
