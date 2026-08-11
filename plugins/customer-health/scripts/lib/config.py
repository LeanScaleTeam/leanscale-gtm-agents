"""
Config loading for the GTM Agents suite.

Config lives in the USER'S HOME, never inside the plugin:

    ~/.leanscale-gtm/
        profile.json              shared org profile, written once, read by all plugins
        <plugin>.json             per-plugin settings
        baselines/<plugin>/       dated baseline snapshots
        audit/<plugin>.log        append-only write log (write-capable plugins only)

Rationale: a marketplace-installed plugin lives in a read-only cache that is
replaced on every update. Anything written into ${CLAUDE_PLUGIN_ROOT} is lost.
${CLAUDE_PLUGIN_DATA} survives updates but is scoped per-plugin, and our profile
is deliberately shared across all nine.
"""

from __future__ import annotations

import json
import os
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


def profile_summary(profile: Dict[str, Any]) -> str:
    """One-line human summary — print this at the top of a run so the user sees the assumptions."""
    crm = (profile.get("crm") or {}).get("system", "unknown")
    return (
        f"{profile.get('org_name', 'Unknown org')} · CRM={crm} · "
        f"FY starts month {profile.get('fiscal_year_start_month', '?')} · "
        f"{profile.get('quota_carrying_reps', '?')} quota-carrying reps · "
        f"material deal floor {profile.get('material_deal_floor', 0):,}"
    )
