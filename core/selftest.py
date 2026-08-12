#!/usr/bin/env python3
"""
Self-test for the shared core library. Run before vendoring:

    python3 core/selftest.py

Exercises every module end-to-end against a temp GTM_HOME so it never touches
a real config. Exit code 0 = the library is safe to vendor into the plugins.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="gtm-selftest-")
os.environ["LEANSCALE_GTM_HOME"] = TMP

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import config, findings, manifest, baseline, render, crmutil  # noqa: E402

FAILS = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILS.append(label)


print("\nconfig")
profile = {
    "org_name": "Acme",
    "crm": {"system": "salesforce"},
    "fiscal_year_start_month": 2,
    "quota_carrying_reps": 14,
    "material_deal_floor": 5000,
}
config.save_profile(profile)
loaded = config.load_profile()
check("profile round-trips", loaded["org_name"] == "Acme")
check("schema_version stamped", loaded.get("schema_version") == 1)
check("summary renders", "Acme" in config.profile_summary(loaded))

# Feb fiscal start, "ends_in" convention: Feb 2026 is FY27 Q1; Jan 2027 is FY27 Q4.
check("FY Feb-start Q1", config.fiscal_period(loaded, 2026, 2) == "FY27-Q1",
      config.fiscal_period(loaded, 2026, 2))
check("FY Feb-start Q4 wraps", config.fiscal_period(loaded, 2027, 1) == "FY27-Q4",
      config.fiscal_period(loaded, 2027, 1))
starts_in = dict(loaded, fiscal_year_naming="starts_in")
check("FY starts_in convention", config.fiscal_period(starts_in, 2026, 2) == "FY26-Q1",
      config.fiscal_period(starts_in, 2026, 2))
cal = {"fiscal_year_start_month": 1}
check("FY calendar year", config.fiscal_period(cal, 2026, 3) == "FY26-Q1",
      config.fiscal_period(cal, 2026, 3))

config.save_plugin_config("crm-hygiene", {"_comment": "doc", "staleness_days": 45})
cfg = config.load_plugin_config("crm-hygiene", defaults={"staleness_days": 30, "other": True})
check("plugin config overrides defaults", cfg["staleness_days"] == 45)
check("plugin config keeps defaults", cfg["other"] is True)
check("underscore keys stripped", "_comment" not in cfg)

try:
    config.load_profile.__wrapped__  # noqa
except AttributeError:
    pass
os.environ["LEANSCALE_GTM_HOME"] = TMP  # unchanged

print("\nagent root + shim")
# A believable plugin root: the sentinel is all verify_agent_root looks for.
fake_root = Path(TMP) / "fake-plugin"
(fake_root / "scripts").mkdir(parents=True)
(fake_root / "scripts" / "analyze.py").write_text(
    "import sys; print('analyzed', *sys.argv[1:])\n", encoding="utf-8")

check("verify_agent_root accepts a real root",
      config.verify_agent_root(fake_root) == fake_root.resolve())

try:
    config.verify_agent_root(Path(TMP))
    check("verify_agent_root rejects a non-root", False, "no ConfigError raised")
except config.ConfigError as exc:
    check("verify_agent_root rejects a non-root", "does not look like a plugin root" in str(exc))

installed = config.install_shim("fake-plugin", fake_root)
shim = Path(installed["shim"])
check("shim written", shim.is_file())
check("shim is executable", os.access(shim, os.X_OK))
check("agent_root persisted", config.load_plugin_config("fake-plugin")["agent_root"]
      == str(fake_root.resolve()))
check("agent_root() reads it back", config.agent_root("fake-plugin") == fake_root.resolve())

# The baked path must be shell-quoted — plugin caches live under paths with spaces.
spaced = Path(TMP) / "dir with space" / "plug"
(spaced / "scripts").mkdir(parents=True)
(spaced / "scripts" / "analyze.py").write_text("print('ok')\n", encoding="utf-8")
config.install_shim("spaced-plugin", spaced)
import subprocess as _sp  # noqa: E402
res = _sp.run([str(config.shim_path("spaced-plugin")), "analyze"],
              capture_output=True, text=True)
check("shim survives spaces in the path", res.returncode == 0 and "ok" in res.stdout,
      res.stderr.strip()[:80])

res = _sp.run([str(shim), "analyze", "--flag"], capture_output=True, text=True)
check("shim execs the script", "analyzed --flag" in res.stdout, res.stderr.strip()[:80])

res = _sp.run([str(shim)], capture_output=True, text=True)
check("shim with no args explains itself", res.returncode == 2 and "usage:" in res.stderr)

res = _sp.run([str(shim), "nope"], capture_output=True, text=True)
check("shim rejects an unknown script",
      res.returncode == 2 and "no such script 'nope'" in res.stderr, res.stderr.strip()[:80])

# --root is how a skill reaches config.example.json and fixtures/, which are not scripts.
res = _sp.run([str(shim), "--root"], capture_output=True, text=True)
check("shim --root prints the plugin dir",
      res.returncode == 0 and res.stdout.strip() == str(fake_root.resolve()),
      res.stdout.strip()[:80])

res = _sp.run([str(config.shim_path("spaced-plugin")), "--root"], capture_output=True, text=True)
check("shim --root handles spaces", res.stdout.strip() == str(spaced.resolve()),
      res.stdout.strip()[:80])

# A stale or wrong CLAUDE_PLUGIN_ROOT must not strand the user — fall back to baked.
env = dict(os.environ, CLAUDE_PLUGIN_ROOT="/nonexistent/plugin/root")
res = _sp.run([str(shim), "analyze"], capture_output=True, text=True, env=env)
check("shim falls back when CLAUDE_PLUGIN_ROOT is bogus", "analyzed" in res.stdout,
      res.stderr.strip()[:80])

# ...but a valid one wins, so a plugin update that moves the cache is picked up.
other = Path(TMP) / "other-plugin"
(other / "scripts").mkdir(parents=True)
(other / "scripts" / "analyze.py").write_text("print('from-env-root')\n", encoding="utf-8")
env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(other))
res = _sp.run([str(shim), "analyze"], capture_output=True, text=True, env=env)
check("valid CLAUDE_PLUGIN_ROOT takes precedence", "from-env-root" in res.stdout,
      res.stderr.strip()[:80])

try:
    config.agent_root("never-set-up")
    check("agent_root() names the fix when unset", False, "no ConfigError raised")
except config.ConfigError as exc:
    check("agent_root() names the fix when unset", "never-set-up:setup" in str(exc))

print("\noptional MCP key")
check("absent key reads as None", config.load_mcp_key() is None)
key_file = config.save_mcp_key("ls_live_selftest")
check("key round-trips", config.load_mcp_key() == "ls_live_selftest")
check("key file is 0600", oct(key_file.stat().st_mode & 0o777) == "0o600",
      oct(key_file.stat().st_mode & 0o777))
key_file.chmod(0o644)
config.save_mcp_key("ls_live_second")
check("re-save re-tightens a loosened file",
      oct(key_file.stat().st_mode & 0o777) == "0o600",
      oct(key_file.stat().st_mode & 0o777))
os.environ["LEANSCALE_MCP_KEY"] = "from_env"
check("env key beats the file", config.load_mcp_key() == "from_env")
del os.environ["LEANSCALE_MCP_KEY"]

print("\ncrmutil")
sf = [{"attributes": {"type": "Opportunity"}, "Id": "006x", "Amount": 50000,
       "Owner": {"Name": "Dana", "Id": "005y"}, "CloseDate": "2026-03-15"}]
hs = [{"id": "123", "properties": {"amount": "75000", "dealstage": "qualified",
                                   "closedate": "2026-04-01T00:00:00Z"}}]
nsf, nhs = crmutil.normalize_records(sf), crmutil.normalize_records(hs)
check("SF flattens relationships", nsf[0]["Owner.Name"] == "Dana")
check("SF drops attributes noise", "attributes" not in nsf[0])
check("HS lifts properties", nhs[0]["amount"] == "75000")
check("HS maps id -> Id", nhs[0]["Id"] == "123")

check("parse ISO date", crmutil.parse_dt("2026-03-15").year == 2026)
check("parse SF datetime+tz", crmutil.parse_dt("2026-03-15T10:30:00.000+0000").month == 3)
check("parse HS epoch ms", crmutil.parse_dt(1772150400000).year == 2026)
check("parse junk -> None", crmutil.parse_dt("not a date") is None)
check("days_between", crmutil.days_between("2026-01-01", "2026-03-02") == 60)

recs = [{"a": "x"}, {"a": ""}, {"a": None}, {"a": "y"}]
check("fill_rate ignores blanks", crmutil.fill_rate(recs, "a") == 0.5)
check("fill_rate empty-safe", crmutil.fill_rate([], "a") == 0.0)
check("median even", crmutil.median([1, 2, 3, 4]) == 2.5)
check("median empty-safe", crmutil.median([]) is None)
check("percentile p50", crmutil.percentile([10, 20, 30, 40, 50], 50) == 30)
check("percentile p10", round(crmutil.percentile([10, 20, 30, 40, 50], 10), 1) == 14.0)
check("pct guards zero denom", crmutil.pct(5, 0) == 0.0)
check("email_domain", crmutil.email_domain("Dana <d@acme.io>") == "acme.io")
check("normalize_company strips suffix",
      crmutil.normalize_company("Acme, Inc.") == crmutil.normalize_company("ACME LLC"))
check("redact stable", crmutil.redact_name("Dana") == crmutil.redact_name("Dana"))
check("redact distinct", crmutil.redact_name("Dana") != crmutil.redact_name("Sam"))
check("bucket", crmutil.bucket(37, [30, 60, 90], ["<30", "30-60", "60-90", "90+"]) == "30-60")
check("bucket overflow", crmutil.bucket(400, [30, 60], ["a", "b", "c"]) == "c")
check("bucket None", crmutil.bucket(None, [30], ["a", "b"]) == "unknown")

print("\nmanifest")
run_dir = Path(TMP) / "run"
m = manifest.RunManifest("crm-hygiene", run_dir, window={"start": "2026-02-10", "end": "2026-08-10"})
m.record("opportunities", tool="run_soql_query", count=1200, query="SELECT Id FROM Opportunity")
m.record("gong_calls", tool="gong_list", count=0, required=False)
p = m.finalize()
check("manifest written", p.exists())
check("optional empty is tolerated", m.unavailable_optional() == ["gong_calls"])
data = json.loads(p.read_text())
check("manifest totals", data["total_records"] == 1200)

m2 = manifest.RunManifest("crm-hygiene", run_dir)
m2.record("opportunities", tool="run_soql_query", count=0, required=True,
          diagnosis="the integration user may lack read access to Opportunity")
try:
    m2.finalize()
    check("empty required source aborts", False, "no exception raised")
except manifest.SourceEmptyError as exc:
    check("empty required source aborts", True)
    check("abort message carries diagnosis", "integration user" in str(exc))

print("\nfindings")
doc = findings.FindingsDoc(plugin="crm-hygiene", window={"start": "2026-02-10", "end": "2026-08-10"},
                           org_name="Acme")
doc.add_score(findings.Score(key="hygiene_index", label="Hygiene Index", value=61,
                             unit="score_0_100", direction_good="up"))
doc.add(findings.Finding(
    id="dupe-accounts", severity="high", title="1,204 accounts share 511 email domains",
    what="Duplicate accounts by domain.", why_it_matters="Routing splits one buyer across reps.",
    recommended_fix="Merge on domain with a human review queue.",
    evidence={"count": 1204, "sample_ids": ["001a", "001b"], "query": "SELECT Id FROM Account"},
    effort="medium"))
check("valid finding accepted", len(doc.findings) == 1)

try:
    doc.add(findings.Finding(id="x", severity="urgent", title="t", what="w",
                             why_it_matters="y", recommended_fix="f", evidence={"count": 1}))
    check("bad severity rejected", False)
except findings.FindingsError:
    check("bad severity rejected", True)

try:
    doc.add(findings.Finding(id="y", severity="low", title="t", what="w",
                             why_it_matters="y", recommended_fix="f", evidence={}))
    check("evidence-free finding rejected", False)
except findings.FindingsError:
    check("evidence-free finding rejected", True)

try:
    doc.add(findings.Finding(id="dupe-accounts", severity="low", title="t", what="w",
                             why_it_matters="y", recommended_fix="f", evidence={"count": 1}))
    check("duplicate id rejected", False)
except findings.FindingsError:
    check("duplicate id rejected", True)

doc.add(findings.Finding(id="stale-opps", severity="critical", title="Stale open opps",
                         what="w", why_it_matters="y", recommended_fix="f",
                         evidence={"count": 88}))
check("sorted by severity", doc.sorted_findings()[0].severity == "critical")
fpath = doc.write(run_dir)
check("findings.json written", fpath.exists())
payload = json.loads(fpath.read_text())
check("severity counts", payload["counts_by_severity"]["critical"] == 1)

print("\nbaseline")
payload = baseline.apply_deltas(payload, "crm-hygiene")
check("first run flagged as baseline", payload["is_baseline_run"] is True)
baseline.save_baseline("crm-hygiene", payload)

payload["scores"][0]["value"] = 74
payload["findings"][0]["evidence"]["count"] = 50
payload2 = baseline.apply_deltas(payload, "crm-hygiene")
check("second run not baseline", payload2["is_baseline_run"] is False)
check("score delta computed", payload2["scores"][0]["delta_vs_last"] == 13)
crit = [f for f in payload2["findings"] if f["id"] == "stale-opps"][0]
check("finding delta computed", crit["delta_vs_last"] == -38)

print("\nrender")
man = json.loads((run_dir / "manifest.json").read_text())
paths = render.write_reports(payload2, run_dir, man)
md = paths["markdown"].read_text()
htm = paths["html"].read_text()
check("markdown has findings", "Stale open opps" in md)
check("markdown has method table", "run_soql_query" in md)

# Evidence rows must appear in BOTH reports. They used to render only in HTML,
# so the markdown quietly disagreed with the HTML about what evidence existed.
rowdoc = json.loads(fpath.read_text())
rowdoc["findings"][0]["evidence"]["rows"] = [
    {"Deal": "Acme | Q3", "Owner": "Dana", "Days": 91},
    {"Deal": "Globex", "Owner": "Sam", "Days": 44},
]
rowmd = render.render_markdown(rowdoc, man)
check("markdown renders evidence rows", "| Deal | Owner | Days |" in rowmd)
check("markdown escapes pipes in cells", r"Acme \| Q3" in rowmd)
check("html renders evidence rows", "<td>Globex</td>" in render.render_html(rowdoc, man))
check("fiscal_period exported from package", hasattr(__import__("lib"), "fiscal_period"))
check("html is a document", htm.startswith("<!doctype html>"))

# Self-contained means no NETWORK FETCH. The only http:// in the file is the SVG
# xmlns namespace identifier inside the favicon data URI, which browsers never
# resolve — so test the things that actually cause a request.
import re as _re  # noqa: E402
external = _re.findall(r'(?:src|href)\s*=\s*["\'](?!data:|#)(https?:|//)', htm)
external += _re.findall(r'@import', htm)
external += _re.findall(r'url\(\s*["\']?https?:', htm)
check("html makes no external requests", not external, external[:3])
check("html inlines its CSS", "<style>" in htm and "stylesheet" not in htm)
check("html has favicon", "data:image/svg+xml" in htm)
check("html shows deltas", "vs last run" in htm or "than last run" in htm)
check("html escapes markup", "<script>" not in render.render_html(
    dict(payload2, org_name="<script>alert(1)</script>")))
check("html renders KPI", "Hygiene Index" in htm)

baseline_doc = dict(payload2, is_baseline_run=True)
check("baseline note appears", "Baseline run" in render.render_html(baseline_doc))
unavail = dict(payload2, unavailable=["Gong transcripts"])
check("unavailable warned as not-clean", "unavailable, not clean" in render.render_html(unavail))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {', '.join(FAILS)}")
    sys.exit(1)
print("core library: all checks passed")
sys.exit(0)
