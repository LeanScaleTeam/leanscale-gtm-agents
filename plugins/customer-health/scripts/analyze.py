#!/usr/bin/env python3
"""
customer-health / analyze.py — raw/*.json  ->  findings.json

Layer 2 of the three-layer split. Offline, Python 3.9+ standard library only,
no network, no MCP. Claude fetched the data and made the qualitative calls;
this file does the arithmetic and nothing else.

Two scores, kept deliberately separate:

  SENTIMENT (0-100, higher is better)
      How the relationship feels, composed from Claude's per-interaction
      readings in raw/sentiment.json plus behavioural facts (who still shows
      up, how fast they reply). There is no keyword list in this file — the
      tone of a call is a judgment, and judgment lives in the skill.

  COMMERCIAL RISK (0-100, higher is worse)
      What the paper says, computed deterministically from the CRM. Renewal
      proximity against renewal status is the heaviest weight in the plugin,
      because the account that churns is very often the happy one with an
      unsigned renewal forty days out.

Blending them would hide exactly the account you needed to see, so we don't.
The quadrant they form is the product.

Usage
    python3 analyze.py --run-dir ./gtm-agents/customer-health/2026-08-10-0900
    python3 analyze.py --run-dir <dir> --raw ./fixtures/raw      # offline demo
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    ConfigError,
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    days_between,
    load_plugin_config,
    load_profile,
    median,
    normalize_records,
    parse_dt,
    pct,
    redact_name,
)
from lib.crmutil import is_blank, to_number  # noqa: E402

PLUGIN = "customer-health"

# --------------------------------------------------------------------------- defaults
# Every one of these is overridable in ~/.leanscale-gtm/customer-health.json.
# They are defaults, not opinions about your business.

DEFAULTS: Dict[str, Any] = {
    "window_days": 180,
    "notice_window_days": 60,
    "renewal_source": "renewal_opportunity",
    "customer_definition": {},
    "crm_fields": {
        "account_arr": ["ARR__c", "arr", "annual_contract_value"],
        "account_arr_is_monthly": False,
        "account_name": ["Name", "name"],
        "account_owner": ["CSM__c", "Owner.Name", "OwnerId", "hubspot_owner_id"],
        "account_segment": ["Segment__c", "segment", "Type"],
        "renewal_date": ["Renewal_Date__c", "renewal_date", "Contract_End_Date__c"],
        "contract_start": ["Contract_Start_Date__c", "contract_start_date"],
        "auto_renew": ["Auto_Renew__c", "auto_renew"],
        "champion_contact": ["Champion__c", "champion_contact"],
        "economic_buyer": ["Economic_Buyer__c", "economic_buyer"],
        "contact_is_active": ["Is_Active__c", "is_active", "Active__c"],
        "contact_bounced_at": ["EmailBouncedDate", "email_bounced_at"],
    },
    "quadrant": {"sentiment_floor": 60, "risk_threshold": 50},
    "risk_floors": {
        "unsigned_renewal_inside_window": 55,
        "unsigned_renewal_critical": 80,
        "champion_departed": 62,
    },
    "commercial_weights": {
        "unsigned_renewal": 30,
        "champion_departure": 14,
        "external_company_risk": 10,
        "silence": 9,
        "contract_value_trend": 8,
        "single_threading": 8,
        "exec_touch_gap": 7,
        "support_burden": 6,
        "usage_decline": 6,
        "expansion_absent": 2,
    },
    "sentiment_weights": {
        "interaction_tone": 40,
        "escalation_load": 20,
        "champion_engagement": 15,
        "senior_attendance": 15,
        "responsiveness": 10,
    },
    "thresholds": {
        "recency_half_life_days": 30,
        "champion_silence_days": 60,
        "exec_touch_target_days": 90,
        "min_readings_for_confident_sentiment": 3,
        "arr_tier_high": 100000,
        "arr_tier_mid": 25000,
        "silence_tolerance_days": {"high": 14, "mid": 30, "low": 45},
        "silence_full_risk_days": {"high": 60, "mid": 90, "low": 120},
        "auto_renew_discount": 0.35,
        "downsell_material_pct": 10,
    },
    "external_event_points": {
        "down_round": 35,
        "acquired": 40,
        "runway_risk": 30,
        "layoff": 25,
        "restructuring": 25,
        "exec_change": 20,
        "champion_departure_public": 40,
        "hiring_freeze": 15,
        "acquisition": 15,
        "public_incident": 10,
        "funding_raise": -25,
        "ipo": -15,
        "champion_promotion": -15,
        "expansion_announcement": -10,
    },
    "external_severity_multiplier": {"high": 1.0, "medium": 0.6, "low": 0.3},
    "external_event_decay_days": 365,
    "support_source": None,
    "usage_source": None,
    "accounts": [],
}

RISK_BANDS = ((75, "Critical"), (50, "Elevated"), (25, "Watch"), (0, "Low"))

QUADRANTS = {
    "happy_but_exposed": "Happy but exposed",
    "burning": "Burning",
    "grumbling_but_safe": "Grumbling but safe",
    "healthy": "Healthy",
    "commercial_only": "Commercial only — sentiment unavailable",
}

ACCOUNT_LINK_FIELDS = (
    "account_id", "AccountId", "Account.Id", "accountId",
    "associatedcompanyid", "company_id", "companyId", "hs_object_id_company",
)

# Salesforce Task/Event date fields and the HubSpot engagement equivalents.
ACTIVITY_DATE_FIELDS = (
    "ActivityDate", "activity_date", "hs_timestamp", "timestamp",
    "occurred_at", "hs_lastmodifieddate", "CreatedDate",
)


# --------------------------------------------------------------------------- helpers


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _lerp_score(value: float, at_zero: float, at_hundred: float) -> float:
    """Linear ramp: `at_zero` scores 0, `at_hundred` scores 100, clamped."""
    if at_hundred == at_zero:
        return 0.0 if value <= at_zero else 100.0
    return _clamp(100.0 * (value - at_zero) / (at_hundred - at_zero))


def pick(record: Dict[str, Any], candidates: Sequence[str]) -> Any:
    """First candidate field carrying a real value. One config, two CRM vendors."""
    for name in candidates or ():
        if name in record and not is_blank(record[name]):
            return record[name]
    return None


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{path} is present but unreadable ({exc}). Fix or delete it and re-run.")


def as_list(payload: Any) -> List[Dict[str, Any]]:
    """Accept a bare list, or the {'records': [...]} / {'results': [...]} envelopes
    the Salesforce and HubSpot MCP servers return, so Claude can dump either."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("records", "results", "data", "items", "rows"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def link_account(record: Dict[str, Any]) -> Optional[str]:
    for field in ACCOUNT_LINK_FIELDS:
        value = record.get(field)
        if not is_blank(value):
            return str(value)
    return None


def recency_weight(occurred: Any, now: datetime, half_life_days: float) -> float:
    age = days_between(occurred, now)
    if age is None:
        return 0.0
    age = max(0, age)
    return 0.5 ** (age / max(1.0, float(half_life_days)))


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "yes", "y", "1", "signed", "won", "closed won")


def arr_tier(arr: float, thresholds: Dict[str, Any]) -> str:
    if arr >= float(thresholds.get("arr_tier_high", 100000)):
        return "high"
    if arr >= float(thresholds.get("arr_tier_mid", 25000)):
        return "mid"
    return "low"


def band(score: Optional[float]) -> str:
    if score is None:
        return "unmeasured"
    for floor, label in RISK_BANDS:
        if score >= floor:
            return label
    return "Low"


def weighted_blend(
    components: Dict[str, Optional[float]], weights: Dict[str, Any]
) -> Tuple[Optional[float], Dict[str, Any], List[str]]:
    """
    Blend 0-100 sub-scores using the configured weights, redistributing the
    weight of any component with no data across the components that have some.

    This is the honest-degradation rule: a customer with no ticket feed must not
    score *safer* than one who has tickets. The dropped components come back so
    the report can name them.
    """
    live = {k: v for k, v in components.items() if v is not None}
    dropped = sorted(k for k, v in components.items() if v is None)
    if not live:
        return None, {}, dropped
    total = sum(float(weights.get(k, 0)) for k in live)
    if total <= 0:
        return None, {}, dropped
    detail: Dict[str, Any] = {}
    blended = 0.0
    for key, value in live.items():
        share = float(weights.get(key, 0)) / total
        blended += value * share
        detail[key] = {
            "score": round(value, 1),
            "configured_weight": float(weights.get(key, 0)),
            "effective_weight": round(share * 100, 1),
        }
    return round(blended, 1), detail, dropped


def pct_rank(value: Optional[float], population: Sequence[Optional[float]]) -> Optional[float]:
    vals = sorted(v for v in population if v is not None)
    if value is None or not vals:
        return None
    below = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    return round(100.0 * (below + 0.5 * equal) / len(vals), 1)


# --------------------------------------------------------------------------- loading


def load_raw(raw_dir: Path) -> Dict[str, Any]:
    """Read every raw file the run skill may have written. Absent = empty, not fatal."""
    return {
        "_sources": read_json(raw_dir / "_sources.json", {}),
        "accounts": normalize_records(as_list(read_json(raw_dir / "accounts.json", []))),
        "renewals": normalize_records(as_list(read_json(raw_dir / "renewals.json", []))),
        "expansion": normalize_records(as_list(read_json(raw_dir / "expansion.json", []))),
        "contacts": normalize_records(as_list(read_json(raw_dir / "contacts.json", []))),
        "activities": normalize_records(as_list(read_json(raw_dir / "activities.json", []))),
        "interactions": as_list(read_json(raw_dir / "interactions.json", [])),
        "sentiment": as_list(read_json(raw_dir / "sentiment.json", [])),
        "company_research": as_list(read_json(raw_dir / "company_research.json", [])),
        "tickets": as_list(read_json(raw_dir / "tickets.json", [])),
        "usage": as_list(read_json(raw_dir / "usage.json", [])),
    }


RAW_FILE_FOR_SOURCE = {
    "customer_accounts": "accounts",
    "renewals": "renewals",
    "expansion_pipeline": "expansion",
    "contacts": "contacts",
    "crm_activities": "activities",
    "interactions": "interactions",
    "sentiment_readings": "sentiment",
    "company_research": "company_research",
    "support_tickets": "tickets",
    "product_usage": "usage",
}


def build_manifest(raw: Dict[str, Any], run_dir: Path, window: Dict[str, str]) -> RunManifest:
    """
    Rebuild the manifest from what is ACTUALLY on disk, using _sources.json only
    for provenance metadata (tool, query, required, diagnosis). Counts are
    recomputed so a run can never report a number the data does not support.
    """
    manifest = RunManifest(PLUGIN, run_dir, window=window)
    declared = {s.get("name"): s for s in (raw["_sources"].get("sources") or [])}

    seen = set()
    for name, key in RAW_FILE_FOR_SOURCE.items():
        meta = declared.get(name, {})
        if not meta and not raw[key]:
            continue  # source was never attempted and produced nothing — say nothing
        seen.add(name)
        required = bool(meta.get("required", name == "customer_accounts"))
        manifest.record(
            name,
            tool=meta.get("tool", "unknown"),
            count=len(raw[key]),
            query=meta.get("query", ""),
            required=required,
            note=meta.get("note", ""),
            diagnosis=meta.get(
                "diagnosis",
                "The connector resolved but returned nothing. Check that the connected "
                "identity can read this object, and that the customer filter in "
                "customer_definition is not excluding every record.",
            ),
        )

    for name, meta in declared.items():
        if name in seen:
            continue
        manifest.record(
            name,
            tool=meta.get("tool", "unknown"),
            count=int(meta.get("count", 0) or 0),
            query=meta.get("query", ""),
            required=bool(meta.get("required", False)),
            note=meta.get("note", ""),
            diagnosis=meta.get("diagnosis", ""),
        )

    if "customer_accounts" not in {s["name"] for s in manifest.sources}:
        manifest.record(
            "customer_accounts",
            tool="unknown",
            count=len(raw["accounts"]),
            required=True,
            diagnosis="raw/accounts.json is missing or empty. The run skill never got a "
            "customer list back from the CRM — re-run /customer-health:setup to "
            "re-probe the connector and the customer filter.",
        )

    for warning in raw["_sources"].get("warnings") or []:
        manifest.warn(str(warning))
    return manifest


# --------------------------------------------------------------------------- shaping


def shape_accounts(raw: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = cfg["crm_fields"]
    monthly = bool(fields.get("account_arr_is_monthly"))
    excluded = {str(x) for x in (cfg.get("customer_definition") or {}).get("exclude_account_ids", [])}
    kickoff_by_id = {str(a.get("account_id")): a for a in cfg.get("accounts") or []}

    out: List[Dict[str, Any]] = []
    for rec in raw["accounts"]:
        acct_id = str(rec.get("Id") or rec.get("id") or rec.get("hs_object_id") or "")
        if not acct_id or acct_id in excluded:
            continue
        arr = to_number(pick(rec, fields["account_arr"])) or 0.0
        if monthly:
            arr *= 12.0
        out.append(
            {
                "id": acct_id,
                "name": str(pick(rec, fields["account_name"]) or acct_id),
                "arr": round(arr, 2),
                "owner": pick(rec, fields["account_owner"]),
                "segment": pick(rec, fields["account_segment"]),
                "renewal_date_account": pick(rec, fields["renewal_date"]),
                "contract_start": pick(rec, fields["contract_start"]),
                "auto_renew": truthy(pick(rec, fields["auto_renew"])),
                "champion_field": pick(rec, fields["champion_contact"]),
                "economic_buyer_field": pick(rec, fields["economic_buyer"]),
                "kickoff": kickoff_by_id.get(acct_id),
                "_raw": rec,
            }
        )
    return sorted(out, key=lambda a: -a["arr"])


def group_by_account(records: Sequence[Dict[str, Any]], key: str = None) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        acct = str(rec.get(key)) if key and rec.get(key) is not None else link_account(rec)
        if not acct:
            continue
        grouped.setdefault(acct, []).append(rec)
    return grouped


# ------------------------------------------------------------------ commercial risk


def renewal_state(
    account: Dict[str, Any],
    renewals: List[Dict[str, Any]],
    now: datetime,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Resolve the renewal instrument for one account across every shape this data
    takes in the wild: a renewal Opportunity, a Contract, a subscription record,
    or a date field sitting on the Account.
    """
    fields = cfg["crm_fields"]
    best: Optional[Dict[str, Any]] = None
    for rec in renewals:
        kind = str(rec.get("record_kind") or rec.get("RecordKind") or "opportunity").lower()
        date_value = pick(rec, list(fields["renewal_date"]) + ["CloseDate", "closedate", "EndDate", "end_date"])
        parsed = parse_dt(date_value)
        if parsed is None:
            continue
        is_won = truthy(rec.get("IsWon") or rec.get("is_won")) or str(
            rec.get("StageName") or rec.get("dealstage") or rec.get("Status") or ""
        ).strip().lower() in ("closed won", "closedwon", "signed", "renewed", "activated")
        is_closed_lost = str(
            rec.get("StageName") or rec.get("dealstage") or rec.get("Status") or ""
        ).strip().lower() in ("closed lost", "closedlost", "churned", "cancelled", "canceled")
        candidate = {
            "kind": kind,
            "id": str(rec.get("Id") or rec.get("id") or ""),
            "name": rec.get("Name") or rec.get("dealname") or rec.get("ContractNumber") or "",
            "date": parsed,
            "stage": rec.get("StageName") or rec.get("dealstage") or rec.get("Status") or "",
            "amount": to_number(pick(rec, ["Amount", "amount", "ContractValue", "TotalValue"])),
            "is_signed": bool(is_won),
            "is_lost": bool(is_closed_lost),
        }
        # Prefer the nearest-dated instrument that is still open; a signed one only
        # counts if nothing open is closer.
        if best is None:
            best = candidate
        else:
            better = (not candidate["is_signed"], -abs((candidate["date"] - now).days))
            current = (not best["is_signed"], -abs((best["date"] - now).days))
            if better > current:
                best = candidate

    if best is None:
        parsed = parse_dt(account.get("renewal_date_account"))
        if parsed is not None:
            best = {
                "kind": "account_field",
                "id": account["id"],
                "name": "Renewal date on the Account record",
                "date": parsed,
                "stage": "",
                "amount": account["arr"],
                "is_signed": False,
                "is_lost": False,
            }

    if best is None:
        return {"known": False, "days_to_renewal": None, "is_signed": None, "instrument": None}

    return {
        "known": True,
        "days_to_renewal": (best["date"] - now).days,
        "renewal_date": best["date"].strftime("%Y-%m-%d"),
        "is_signed": best["is_signed"],
        "is_lost": best["is_lost"],
        "instrument": best["kind"],
        "instrument_id": best["id"],
        "instrument_name": best["name"],
        "stage": best["stage"],
        "amount": best["amount"],
        "has_open_opportunity": best["kind"] in ("opportunity", "deal") and not best["is_signed"],
    }


def score_unsigned_renewal(state: Dict[str, Any], account: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[float]:
    """
    The heaviest signal in the plugin.

        signed                                   -> 0
        > 2x notice window out, unsigned         -> 0
        between 2x and 1x the window             -> ramps 0 -> 40
        inside the notice window                 -> ramps 40 -> 95 as the date closes
        past the renewal date, still unsigned    -> 100
        inside the window with no renewal record -> floored at 90 (nobody has even started)

    Auto-renewing paper is discounted, not exempted: evergreen contracts still
    have a notice deadline, and missing it quietly is how a downsell happens.
    """
    if not state.get("known"):
        return None
    if state.get("is_signed"):
        return 0.0
    window = float(cfg["notice_window_days"])
    days = float(state["days_to_renewal"])

    if days < 0:
        base = 100.0
    elif days <= window:
        base = 40.0 + 55.0 * (1.0 - days / max(1.0, window))
    elif days <= 2 * window:
        base = 40.0 * (1.0 - (days - window) / max(1.0, window))
    else:
        base = 0.0

    if days <= window and not state.get("has_open_opportunity") and state.get("instrument") in ("account_field", None):
        base = max(base, 90.0)  # inside the window with no renewal record at all

    if account.get("auto_renew"):
        base *= float(cfg["thresholds"]["auto_renew_discount"])
    return round(_clamp(base), 1)


def score_champion(
    account: Dict[str, Any],
    contacts: List[Dict[str, Any]],
    interactions: List[Dict[str, Any]],
    sentiment_doc: Optional[Dict[str, Any]],
    research: Optional[Dict[str, Any]],
    now: datetime,
    cfg: Dict[str, Any],
) -> Tuple[Optional[float], Dict[str, Any]]:
    fields = cfg["crm_fields"]
    silence_days = float(cfg["thresholds"]["champion_silence_days"])
    kickoff = account.get("kickoff") or {}
    champ_cfg = kickoff.get("champion") or {}
    champ_name = (champ_cfg.get("name") or "").strip() or str(account.get("champion_field") or "").strip()
    champ_email = (champ_cfg.get("email") or "").strip().lower()

    detail: Dict[str, Any] = {"champion": champ_name or None, "email": champ_email or None, "evidence": []}

    if not champ_name and not champ_email:
        detail["state"] = "none_named"
        detail["evidence"].append("No champion recorded in the CRM or the kickoff baseline.")
        return 30.0, detail

    champ_contact = None
    for contact in contacts:
        email = str(contact.get("Email") or contact.get("email") or "").strip().lower()
        name = str(contact.get("Name") or contact.get("name") or "").strip()
        # Salesforce gives FirstName/LastName; HubSpot gives firstname/lastname.
        full = (
            f"{contact.get('FirstName', contact.get('firstname', ''))} "
            f"{contact.get('LastName', contact.get('lastname', ''))}"
        ).strip()
        if (champ_email and email == champ_email) or (
            champ_name and champ_name.lower() in (name.lower(), full.lower())
        ):
            champ_contact = contact
            break

    if champ_contact is not None:
        bounced = pick(champ_contact, fields["contact_bounced_at"])
        active_raw = pick(champ_contact, fields["contact_is_active"])
        inactive = active_raw is not None and not truthy(active_raw)
        if not is_blank(bounced):
            when = parse_dt(bounced)
            detail["state"] = "email_bouncing"
            detail["evidence"].append(
                "Email to the champion bounced on "
                f"{when.strftime('%Y-%m-%d') if when else str(bounced)}."
            )
            return 100.0, detail
        if inactive:
            detail["state"] = "marked_inactive"
            detail["evidence"].append("The champion's contact record is flagged inactive in the CRM.")
            return 100.0, detail

    for event in (research or {}).get("events", []) or []:
        if str(event.get("type")) == "champion_departure_public":
            detail["state"] = "departed_public"
            detail["evidence"].append(
                f"{event.get('headline', 'Public departure signal')} ({event.get('date', 'date unknown')})."
            )
            return 100.0, detail

    signal = str((sentiment_doc or {}).get("champion_signal") or "unknown").lower()
    last_seen = None
    for item in interactions:
        for person in item.get("customer_participants") or []:
            matched = (
                champ_email and str(person.get("email", "")).lower() == champ_email
            ) or (champ_name and champ_name.lower() == str(person.get("name", "")).lower())
            if matched or person.get("is_champion"):
                when = parse_dt(item.get("occurred_at"))
                if when and (last_seen is None or when > last_seen):
                    last_seen = when

    if last_seen is not None:
        gap = (now - last_seen).days
        detail["last_seen"] = last_seen.strftime("%Y-%m-%d")
        detail["days_since_seen"] = gap
        if gap >= silence_days:
            detail["state"] = "silent"
            detail["evidence"].append(
                f"The champion has not appeared on a call or in a message thread for {gap} days."
            )
            return 70.0, detail
    elif interactions:
        detail["state"] = "absent_from_all_conversations"
        detail["evidence"].append(
            "The champion appears in none of the conversations inside the window, "
            "though the account is otherwise active."
        )
        return 70.0, detail

    if signal == "departed":
        detail["state"] = "departed"
        detail["evidence"].append("Claude read the conversations as a champion handoff or departure.")
        return 100.0, detail
    if signal == "fading":
        detail["state"] = "fading"
        detail["evidence"].append("Claude read the champion as disengaging across the window.")
        return 40.0, detail

    if not interactions:
        # No calls, no threads, no bounce, no CRM flag. We know nothing about this
        # champion — which is not the same as knowing they are fine. Return None so
        # the weight redistributes and the account lands in the "unmeasured" bucket.
        detail["state"] = "unmeasured"
        detail["evidence"].append(
            "No conversation source covers this account, and the CRM shows no departure signal, "
            "so champion status is unknown rather than healthy."
        )
        return None, detail

    detail["state"] = "engaged"
    return 0.0, detail


def score_silence(arr: float, days_since_touch: Optional[int], cfg: Dict[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
    """Silence weighted by ARR — a $400k account is allowed to go quiet for far less time."""
    thresholds = cfg["thresholds"]
    tier = arr_tier(arr, thresholds)
    tolerated = float(thresholds["silence_tolerance_days"][tier])
    full = float(thresholds["silence_full_risk_days"][tier])
    if days_since_touch is None:
        return 100.0, {"tier": tier, "tolerated_days": tolerated, "days_since_touch": None,
                       "note": "No touch of any kind inside the window."}
    return round(_lerp_score(float(days_since_touch), tolerated, full), 1), {
        "tier": tier,
        "tolerated_days": tolerated,
        "full_risk_days": full,
        "days_since_touch": days_since_touch,
    }


def score_single_threading(engaged: int) -> float:
    return {0: 100.0, 1: 100.0, 2: 55.0, 3: 25.0}.get(engaged, 0.0)


def score_exec_touch(days_since_exec: Optional[int], cfg: Dict[str, Any]) -> float:
    target = float(cfg["thresholds"]["exec_touch_target_days"])
    if days_since_exec is None:
        return 100.0
    return round(_lerp_score(float(days_since_exec), target, target * 2.0), 1)


def score_contract_trend(account: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
    kickoff = account.get("kickoff") or {}
    baseline_arr = to_number(kickoff.get("kickoff_arr"))
    current = account["arr"]
    if not baseline_arr:
        return None, {"note": "No kickoff ARR captured — contract movement is unprovable for this account."}
    change = pct(current - baseline_arr, baseline_arr)
    material = float(cfg["thresholds"]["downsell_material_pct"])
    if change >= 0:
        score = 0.0 if change > 0 else 10.0
    elif abs(change) >= 25:
        score = 100.0
    elif abs(change) >= material:
        score = 60.0
    else:
        score = 30.0
    return score, {
        "kickoff_arr": baseline_arr,
        "current_arr": current,
        "change_pct": change,
        "kickoff_date": kickoff.get("kickoff_date"),
    }


def score_external(research: Optional[Dict[str, Any]], now: datetime, cfg: Dict[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
    """
    A champion's departure or a down round is a churn signal that no amount of
    sentiment analysis in your own call transcripts will ever surface.
    """
    if not research:
        return None, {"note": "No company research on file for this account."}
    points_table = cfg["external_event_points"]
    sev_mult = cfg["external_severity_multiplier"]
    decay = float(cfg["external_event_decay_days"])

    total = 0.0
    counted: List[Dict[str, Any]] = []
    for event in research.get("events", []) or []:
        base = float(points_table.get(str(event.get("type")), 0))
        if base == 0:
            continue
        mult = float(sev_mult.get(str(event.get("severity", "medium")).lower(), 0.6))
        age = days_between(event.get("date"), now)
        fade = 1.0 if age is None else max(0.0, 1.0 - (max(0, age) / max(1.0, decay)))
        contribution = base * mult * fade
        total += contribution
        counted.append(
            {
                "type": event.get("type"),
                "date": event.get("date"),
                "severity": event.get("severity"),
                "headline": event.get("headline"),
                "source": event.get("source_url") or event.get("source"),
                "points": round(contribution, 1),
            }
        )
    counted.sort(key=lambda e: -abs(e["points"]))
    return round(_clamp(total), 1), {
        "funding_stage": research.get("funding_stage"),
        "months_since_last_raise": research.get("months_since_last_raise"),
        "events": counted,
        "researched_at": research.get("researched_at"),
    }


def score_support(weighted_per_100k: Optional[float], population: Sequence[Optional[float]], open_critical: int) -> Optional[float]:
    if weighted_per_100k is None:
        return None
    rank = pct_rank(weighted_per_100k, population)
    score = rank if rank is not None else 0.0
    if open_critical:
        score = max(score, 60.0 + 10.0 * min(4, open_critical))
    return round(_clamp(score), 1)


def score_usage(usage_rows: List[Dict[str, Any]]) -> Tuple[Optional[float], Dict[str, Any]]:
    if not usage_rows:
        return None, {}
    row = usage_rows[-1]
    recent = to_number(row.get("recent_value"))
    baseline_value = to_number(row.get("baseline_value"))
    if recent is None or not baseline_value:
        return None, {}
    change = pct(recent - baseline_value, baseline_value)
    if change <= -40:
        score = 100.0
    elif change <= -20:
        score = 65.0
    elif change <= -5:
        score = 35.0
    else:
        score = 0.0
    return score, {"metric": row.get("metric"), "recent": recent, "baseline": baseline_value, "change_pct": change}


# ---------------------------------------------------------------------- sentiment


def score_sentiment(
    readings: List[Dict[str, Any]],
    interactions: List[Dict[str, Any]],
    champion_component: Optional[float],
    now: datetime,
    window_start: datetime,
    cfg: Dict[str, Any],
) -> Tuple[Optional[float], Dict[str, Any], Dict[str, Any]]:
    """
    Compose sentiment from Claude's qualitative readings plus behavioural facts.

    Nothing here inspects the words in a quote. `tone` and `is_escalation` were
    decided by Claude while reading the actual transcript; this function only
    weights them by recency and blends them with attendance and latency.
    """
    half_life = float(cfg["thresholds"]["recency_half_life_days"])
    components: Dict[str, Optional[float]] = {}
    detail: Dict[str, Any] = {}

    # --- interaction tone -------------------------------------------------
    tone_weight_sum = 0.0
    tone_value_sum = 0.0
    for reading in readings:
        tone = to_number(reading.get("tone"))
        if tone is None:
            continue
        weight = recency_weight(reading.get("occurred_at"), now, half_life)
        tone_weight_sum += weight
        tone_value_sum += weight * _clamp((float(tone) + 2.0) / 4.0 * 100.0)
    components["interaction_tone"] = round(tone_value_sum / tone_weight_sum, 1) if tone_weight_sum > 0 else None

    # --- escalation load --------------------------------------------------
    if readings:
        esc_weight = sum(
            recency_weight(r.get("occurred_at"), now, half_life) for r in readings if r.get("is_escalation")
        )
        all_weight = sum(recency_weight(r.get("occurred_at"), now, half_life) for r in readings)
        share = (esc_weight / all_weight) if all_weight > 0 else 0.0
        components["escalation_load"] = round(_clamp(100.0 - share * 100.0 * 1.6), 1)
        detail["escalation_share_pct"] = round(share * 100, 1)
    else:
        components["escalation_load"] = None

    # --- champion engagement (invert the commercial champion sub-score) ----
    components["champion_engagement"] = None if champion_component is None else round(100.0 - champion_component, 1)

    # --- senior attendance decay -----------------------------------------
    meetings = [i for i in interactions if str(i.get("type", "")).lower() == "meeting"]
    if meetings:
        midpoint = window_start + (now - window_start) / 2
        recent = [m for m in meetings if (parse_dt(m.get("occurred_at")) or window_start) >= midpoint]
        earlier = [m for m in meetings if (parse_dt(m.get("occurred_at")) or window_start) < midpoint]

        def senior_share(group: List[Dict[str, Any]]) -> Optional[float]:
            if not group:
                return None
            hits = sum(
                1
                for m in group
                if any(
                    str(p.get("seniority", "")).lower() in ("exec", "senior")
                    for p in (m.get("customer_participants") or [])
                )
            )
            return 100.0 * hits / len(group)

        recent_share = senior_share(recent)
        earlier_share = senior_share(earlier)
        components["senior_attendance"] = None if recent_share is None else round(recent_share, 1)
        detail["senior_attendance"] = {
            "recent_half_pct": None if recent_share is None else round(recent_share, 1),
            "earlier_half_pct": None if earlier_share is None else round(earlier_share, 1),
            "meetings_recent": len(recent),
            "meetings_earlier": len(earlier),
        }
    else:
        components["senior_attendance"] = None

    # --- responsiveness ---------------------------------------------------
    latencies: List[float] = []
    by_id = {str(i.get("id")): i for i in interactions if i.get("id")}
    for item in interactions:
        if str(item.get("direction", "")).lower() != "inbound":
            continue
        stated = to_number(item.get("response_latency_hours"))
        if stated is not None:
            latencies.append(stated)
            continue
        parent = by_id.get(str(item.get("response_to_id") or ""))
        if parent:
            a, b = parse_dt(parent.get("occurred_at")), parse_dt(item.get("occurred_at"))
            if a and b and b >= a:
                latencies.append((b - a).total_seconds() / 3600.0)
    med = median(latencies)
    if med is None:
        components["responsiveness"] = None
    else:
        curve = ((4, 100.0), (24, 80.0), (48, 60.0), (96, 35.0), (168, 10.0))
        value = 0.0
        previous_hours, previous_score = 0.0, 100.0
        for hours, score in curve:
            if med <= hours:
                span = hours - previous_hours
                value = previous_score + (score - previous_score) * ((med - previous_hours) / span if span else 0)
                break
            previous_hours, previous_score = float(hours), score
        components["responsiveness"] = round(_clamp(value), 1)
        detail["median_response_hours"] = round(med, 1)

    # Sentiment must come from a conversation. champion_engagement can be derived
    # from a CRM bounce flag alone, and an account scored on that one component
    # would read as "unhappy" when the truth is that nobody listened to it. If no
    # conversation-derived component survived, sentiment is unavailable, full stop.
    conversational = ("interaction_tone", "escalation_load", "senior_attendance", "responsiveness")
    if all(components.get(key) is None for key in conversational):
        detail["components"] = {}
        detail["unmeasured_components"] = sorted(k for k, v in components.items() if v is None)
        detail["reading_count"] = len(readings)
        detail["confidence"] = "unavailable"
        detail["why_unavailable"] = (
            "No call transcript and no message thread covers this account inside the window. "
            "Champion status alone is a CRM fact, not a sentiment reading."
        )
        return None, detail, {"recent": None, "earlier": None, "delta": None}

    score, blend_detail, dropped = weighted_blend(components, cfg["sentiment_weights"])
    detail["components"] = blend_detail
    detail["unmeasured_components"] = dropped
    detail["reading_count"] = len(readings)
    detail["confidence"] = (
        "unavailable"
        if score is None
        else ("low" if len(readings) < int(cfg["thresholds"]["min_readings_for_confident_sentiment"]) else "normal")
    )

    # --- trend: recent half vs earlier half of the window -----------------
    trend: Dict[str, Any] = {"recent": None, "earlier": None, "delta": None}
    if readings:
        midpoint = window_start + (now - window_start) / 2

        def tone_mean(group: List[Dict[str, Any]]) -> Optional[float]:
            vals = [to_number(r.get("tone")) for r in group]
            vals = [v for v in vals if v is not None]
            if not vals:
                return None
            return round(sum((v + 2.0) / 4.0 * 100.0 for v in vals) / len(vals), 1)

        recent = [r for r in readings if (parse_dt(r.get("occurred_at")) or window_start) >= midpoint]
        earlier = [r for r in readings if (parse_dt(r.get("occurred_at")) or window_start) < midpoint]
        trend["recent"] = tone_mean(recent)
        trend["earlier"] = tone_mean(earlier)
        if trend["recent"] is not None and trend["earlier"] is not None:
            trend["delta"] = round(trend["recent"] - trend["earlier"], 1)
        trend["readings_recent"] = len(recent)
        trend["readings_earlier"] = len(earlier)
    return score, detail, trend


# ------------------------------------------------------------------------ quadrant


def quadrant_of(sentiment: Optional[float], risk: Optional[float], cfg: Dict[str, Any]) -> str:
    floor = float(cfg["quadrant"]["sentiment_floor"])
    threshold = float(cfg["quadrant"]["risk_threshold"])
    if sentiment is None:
        return "commercial_only"
    at_risk = risk is not None and risk >= threshold
    happy = sentiment >= floor
    if happy and at_risk:
        return "happy_but_exposed"
    if not happy and at_risk:
        return "burning"
    if not happy and not at_risk:
        return "grumbling_but_safe"
    return "healthy"


# ------------------------------------------------------------------------ pipeline


def analyze(raw: Dict[str, Any], cfg: Dict[str, Any], profile: Dict[str, Any], now: datetime,
            window: Dict[str, str]) -> Tuple[FindingsDoc, Dict[str, Any]]:
    window_start = parse_dt(window["start"]) or (now - timedelta(days=int(cfg["window_days"])))
    redact = bool(profile.get("redact_pii_in_reports"))

    def person(name: Any) -> str:
        if is_blank(name):
            return "unknown"
        return redact_name(name) if redact else str(name)

    accounts = shape_accounts(raw, cfg)
    renewals_by = group_by_account(raw["renewals"])
    expansion_by = group_by_account(raw["expansion"])
    contacts_by = group_by_account(raw["contacts"])
    activities_by = group_by_account(raw["activities"])
    interactions_by = group_by_account(raw["interactions"], key="account_id")
    tickets_by = group_by_account(raw["tickets"], key="account_id")
    usage_by = group_by_account(raw["usage"], key="account_id")
    sentiment_by = {str(d.get("account_id")): d for d in raw["sentiment"] if d.get("account_id")}
    research_by = {str(d.get("account_id")): d for d in raw["company_research"] if d.get("account_id")}

    have_tickets = bool(raw["tickets"])
    have_usage = bool(raw["usage"])
    have_conversations = bool(raw["interactions"]) or bool(raw["sentiment"])

    # Support burden needs a book-wide population before any account can be ranked.
    support_population: Dict[str, Optional[float]] = {}
    severity_weight = {"critical": 4.0, "urgent": 4.0, "p1": 4.0, "high": 2.0, "p2": 2.0,
                       "normal": 1.0, "medium": 1.0, "p3": 1.0, "low": 0.5, "p4": 0.5}
    for account in accounts:
        rows = tickets_by.get(account["id"], [])
        if not have_tickets:
            support_population[account["id"]] = None
            continue
        weighted = sum(severity_weight.get(str(t.get("severity", "normal")).lower(), 1.0) for t in rows)
        per_100k = weighted / max(1.0, account["arr"] / 100000.0)
        support_population[account["id"]] = round(per_100k, 2)

    rows: List[Dict[str, Any]] = []
    detail_by_account: Dict[str, Any] = {}
    dropped_signals_global: List[str] = []

    for account in accounts:
        acct_id = account["id"]
        interactions = sorted(
            interactions_by.get(acct_id, []), key=lambda i: parse_dt(i.get("occurred_at")) or window_start
        )
        contacts = contacts_by.get(acct_id, [])
        activities = activities_by.get(acct_id, [])
        sentiment_doc = sentiment_by.get(acct_id)
        readings = (sentiment_doc or {}).get("readings", []) or []
        research = research_by.get(acct_id)

        # ---- shared facts ------------------------------------------------
        touch_dates = [parse_dt(i.get("occurred_at")) for i in interactions]
        touch_dates += [parse_dt(pick(a, ACTIVITY_DATE_FIELDS)) for a in activities]
        touch_dates = [d for d in touch_dates if d is not None]
        last_touch = max(touch_dates) if touch_dates else None
        days_since_touch = (now - last_touch).days if last_touch else None

        exec_dates: List[datetime] = []
        for item in interactions:
            people = (item.get("customer_participants") or []) + (item.get("our_participants") or [])
            if any(str(p.get("seniority", "")).lower() == "exec" for p in people):
                when = parse_dt(item.get("occurred_at"))
                if when:
                    exec_dates.append(when)
        for act in activities:
            if str(act.get("seniority", "")).lower() == "exec":
                when = parse_dt(pick(act, ACTIVITY_DATE_FIELDS))
                if when:
                    exec_dates.append(when)
        days_since_exec = (now - max(exec_dates)).days if exec_dates else None

        engaged: set = set()
        for item in interactions:
            for p in item.get("customer_participants") or []:
                key = str(p.get("email") or p.get("name") or "").strip().lower()
                if key:
                    engaged.add(key)
        if not engaged:
            for contact in contacts:
                if not is_blank(pick(contact, ["LastActivityDate", "last_activity_date", "notes_last_contacted"])):
                    key = str(contact.get("Email") or contact.get("email") or contact.get("Id") or "").lower()
                    if key:
                        engaged.add(key)

        # ---- commercial risk components ---------------------------------
        rstate = renewal_state(account, renewals_by.get(acct_id, []), now, cfg)
        champion_score, champion_detail = score_champion(
            account, contacts, interactions, sentiment_doc, research, now, cfg
        )
        silence_score, silence_detail = score_silence(account["arr"], days_since_touch, cfg)
        trend_score, trend_detail = score_contract_trend(account, cfg)
        external_score, external_detail = score_external(research, now, cfg)
        usage_score, usage_detail = score_usage(usage_by.get(acct_id, []) if have_usage else [])
        open_critical = sum(
            1
            for t in tickets_by.get(acct_id, [])
            if str(t.get("severity", "")).lower() in ("critical", "urgent", "p1")
            and str(t.get("status", "")).lower() not in ("closed", "resolved", "done")
        )
        support_score = score_support(
            support_population.get(acct_id), list(support_population.values()), open_critical
        ) if have_tickets else None

        expansion_rows = [
            r for r in expansion_by.get(acct_id, [])
            if not truthy(r.get("IsClosed") or r.get("is_closed"))
            and (to_number(pick(r, ["Amount", "amount"])) or 0) > 0
        ]

        commercial_components: Dict[str, Optional[float]] = {
            "unsigned_renewal": score_unsigned_renewal(rstate, account, cfg),
            "champion_departure": champion_score,
            "external_company_risk": external_score,
            "silence": silence_score,
            "contract_value_trend": trend_score,
            "single_threading": score_single_threading(len(engaged)),
            "exec_touch_gap": score_exec_touch(days_since_exec, cfg),
            "support_burden": support_score,
            "usage_decline": usage_score,
            "expansion_absent": 0.0 if expansion_rows else 100.0,
        }
        risk, risk_detail, risk_dropped = weighted_blend(commercial_components, cfg["commercial_weights"])
        dropped_signals_global = sorted(set(dropped_signals_global) | set(risk_dropped))

        # --- tripwire floors -------------------------------------------------
        # Some signals are not "one input among ten" — they are the thing itself.
        # A 30% weight cannot on its own carry an account over the risk line, so
        # an unsigned renewal inside the notice window and a departed champion
        # each set a floor under the composite. Weights rank accounts; floors
        # make sure the ones that matter cannot be averaged into the middle.
        floor_rules: List[Tuple[float, str]] = []
        notice = float(cfg["notice_window_days"])
        days_out = rstate.get("days_to_renewal")
        if not rstate.get("is_signed") and days_out is not None:
            if days_out < 0:
                floor_rules.append((float(cfg["risk_floors"]["unsigned_renewal_critical"]),
                                    "Renewal date has passed with no signed instrument"))
            elif days_out <= notice / 2:
                floor_rules.append((float(cfg["risk_floors"]["unsigned_renewal_critical"]),
                                    f"Unsigned renewal {int(days_out)} days out — inside half the "
                                    f"{int(notice)}-day notice window"))
            elif days_out <= notice:
                floor_rules.append((float(cfg["risk_floors"]["unsigned_renewal_inside_window"]),
                                    f"Unsigned renewal {int(days_out)} days out — inside the "
                                    f"{int(notice)}-day notice window"))
        if champion_score == 100.0:
            floor_rules.append((float(cfg["risk_floors"]["champion_departed"]),
                                "Champion has departed, gone inactive, or their email is bouncing"))

        risk_floor: Dict[str, Any] = {"weighted_score": risk, "floored_to": None,
                                      "rules_triggered": [{"floor": v, "rule": t} for v, t in floor_rules]}
        if risk is not None and floor_rules:
            highest = max(v for v, _ in floor_rules)
            if highest > risk:
                risk_floor["floored_to"] = highest
                risk = highest

        # ---- sentiment ---------------------------------------------------
        sentiment, sentiment_detail, sentiment_trend = score_sentiment(
            readings, interactions, champion_score, now, window_start, cfg
        )

        quad = quadrant_of(sentiment, risk, cfg)
        kickoff = account.get("kickoff") or {}
        kickoff_sentiment = to_number(kickoff.get("kickoff_sentiment"))

        # The headline quote: strongest complaint if there is one, otherwise the
        # strongest praise. Ties break toward the most recent, because a customer's
        # most recent word is the one that matters. Python's sort is stable, so
        # ordering by recency first and then by tone gives exactly that.
        top_quote = None
        by_recency = sorted(readings, key=lambda r: str(r.get("occurred_at") or ""), reverse=True)
        negative = sorted(
            [r for r in by_recency if (to_number(r.get("tone")) or 0) < 0],
            key=lambda r: to_number(r.get("tone")) or 0,
        )
        positive = sorted(
            [r for r in by_recency if (to_number(r.get("tone")) or 0) > 0],
            key=lambda r: -(to_number(r.get("tone")) or 0),
        )
        pool = negative or positive
        if pool:
            chosen = pool[0]
            top_quote = {
                "quote": chosen.get("quote"),
                "speaker": person(chosen.get("speaker")),
                "speaker_role": chosen.get("speaker_role"),
                "date": chosen.get("occurred_at"),
                "source": chosen.get("source"),
                "tone": chosen.get("tone"),
            }

        detail_by_account[acct_id] = {
            "account_id": acct_id,
            "name": account["name"],
            "arr": account["arr"],
            "owner": person(account["owner"]) if redact else account["owner"],
            "segment": account["segment"],
            "sentiment": sentiment,
            "sentiment_detail": sentiment_detail,
            "sentiment_trend": sentiment_trend,
            "commercial_risk": risk,
            "risk_band": band(risk),
            "risk_components": risk_detail,
            "risk_floor": risk_floor,
            "risk_unmeasured": risk_dropped,
            "quadrant": quad,
            "quadrant_label": QUADRANTS[quad],
            "renewal": rstate,
            "champion": {**champion_detail, "champion": person(champion_detail.get("champion"))},
            "silence": silence_detail,
            "contract_trend": trend_detail,
            "external": external_detail,
            "usage": usage_detail,
            "support": {"weighted_per_100k_arr": support_population.get(acct_id), "open_critical": open_critical},
            "engaged_contacts": len(engaged),
            "days_since_touch": days_since_touch,
            "days_since_exec_touch": days_since_exec,
            "open_expansion_amount": round(sum(to_number(pick(r, ["Amount", "amount"])) or 0 for r in expansion_rows), 2),
            "top_quote": top_quote,
            "kickoff": {
                "captured": bool(kickoff),
                "date": kickoff.get("kickoff_date"),
                "arr": to_number(kickoff.get("kickoff_arr")),
                "sentiment": kickoff_sentiment,
                "sentiment_delta": (
                    None if (sentiment is None or kickoff_sentiment is None) else round(sentiment - kickoff_sentiment, 1)
                ),
                "arr_delta_pct": trend_detail.get("change_pct"),
                "engaged_contacts_at_kickoff": kickoff.get("kickoff_engaged_contacts"),
                "success_criteria": kickoff.get("success_criteria") or [],
            },
        }

        rows.append(
            {
                "Account": account["name"],
                "ARR": f"${account['arr']:,.0f}",
                "Sentiment": "n/a" if sentiment is None else f"{sentiment:.0f}",
                "Risk": "n/a" if risk is None else f"{risk:.0f}",
                "Quadrant": QUADRANTS[quad],
                "Renewal": rstate.get("renewal_date") or "unknown",
                "Days out": "n/a" if rstate.get("days_to_renewal") is None else str(rstate["days_to_renewal"]),
                "Signed": "unknown" if rstate.get("is_signed") is None else ("yes" if rstate["is_signed"] else "NO"),
                "Owner": detail_by_account[acct_id]["owner"] or "unassigned",
            }
        )

    # ------------------------------------------------------------------ scores
    threshold = float(cfg["quadrant"]["risk_threshold"])
    at_risk = [a for a in detail_by_account.values() if (a["commercial_risk"] or 0) >= threshold]
    arr_at_risk = sum(a["arr"] for a in at_risk)
    sentiments = [a["sentiment"] for a in detail_by_account.values() if a["sentiment"] is not None]
    unsigned_in_window = [
        a for a in detail_by_account.values()
        if a["renewal"].get("known")
        and not a["renewal"].get("is_signed")
        and a["renewal"].get("days_to_renewal") is not None
        and a["renewal"]["days_to_renewal"] <= int(cfg["notice_window_days"])
    ]
    exposed = [a for a in detail_by_account.values() if a["quadrant"] == "happy_but_exposed"]

    doc = FindingsDoc(
        plugin=PLUGIN,
        window=window,
        org_name=profile.get("org_name", ""),
        is_baseline_run=True,  # report.py corrects this once it has checked for a prior snapshot
    )
    doc.add_score(Score(
        key="accounts_at_commercial_risk", label="Accounts at commercial risk",
        value=len(at_risk), unit="count", direction_good="down",
        context=f"of {len(accounts)} customers · risk score ≥ {threshold:.0f}",
    ))
    doc.add_score(Score(
        key="arr_at_risk", label="ARR at risk", value=round(arr_at_risk),
        unit="currency", direction_good="down",
        context=f"{pct(arr_at_risk, sum(a['arr'] for a in detail_by_account.values())):.0f}% of book ARR",
    ))
    doc.add_score(Score(
        # Deliberately the string "n/a" rather than 0 when nothing is measurable.
        # A big zero in a KPI tile reads as "our customers hate us"; the truth is
        # "nobody was listening", and those are opposite problems.
        key="mean_sentiment", label="Mean sentiment",
        value=round(sum(sentiments) / len(sentiments), 1) if sentiments else "n/a",
        unit="score_0_100", direction_good="up",
        context=(f"across {len(sentiments)} of {len(accounts)} accounts with a conversation source"
                 if sentiments else "no conversation source connected — unavailable, not zero"),
    ))
    doc.add_score(Score(
        key="unsigned_renewals_in_window", label="Unsigned renewals in window",
        value=len(unsigned_in_window), unit="count", direction_good="down",
        context=f"inside the {int(cfg['notice_window_days'])}-day notice window",
    ))
    doc.add_score(Score(
        key="happy_but_exposed", label="Happy but exposed",
        value=len(exposed), unit="count", direction_good="down",
        context="high sentiment, high commercial risk — the quadrant that churns",
    ))

    # --------------------------------------------------------------- findings
    queries = {s.get("name"): s.get("query", "") for s in (raw["_sources"].get("sources") or [])}
    add_findings(doc, detail_by_account, cfg, queries, accounts, have_conversations, now)

    # ------------------------------------------------------------- sections
    quadrant_counts = {key: 0 for key in QUADRANTS}
    for account in detail_by_account.values():
        quadrant_counts[account["quadrant"]] += 1

    doc.sections = {
        "quadrant": {
            "definition": {
                "sentiment_floor": cfg["quadrant"]["sentiment_floor"],
                "risk_threshold": cfg["quadrant"]["risk_threshold"],
                "labels": QUADRANTS,
                "why": (
                    "Sentiment and commercial risk are never blended. A single health score "
                    "averages the happy account with the unsigned renewal into the middle of "
                    "the pack, which is precisely the account you needed to see."
                ),
            },
            "counts": quadrant_counts,
            "arr": {
                key: round(sum(a["arr"] for a in detail_by_account.values() if a["quadrant"] == key))
                for key in QUADRANTS
            },
            "accounts": {
                key: [
                    {
                        "account": a["name"],
                        "arr": a["arr"],
                        "sentiment": a["sentiment"],
                        "risk": a["commercial_risk"],
                        "renewal": a["renewal"].get("renewal_date"),
                        "days_out": a["renewal"].get("days_to_renewal"),
                        "signed": a["renewal"].get("is_signed"),
                    }
                    for a in sorted(detail_by_account.values(), key=lambda x: -x["arr"])
                    if a["quadrant"] == key
                ]
                for key in QUADRANTS
            },
        },
        "accounts": list(detail_by_account.values()),
        "model": {
            "commercial_weights": cfg["commercial_weights"],
            "sentiment_weights": cfg["sentiment_weights"],
            "risk_floors": cfg["risk_floors"],
            "floor_rule": (
                "Two signals act as tripwires rather than weights: an unsigned renewal inside the "
                "notice window, and a departed champion. Each sets a floor under the composite risk "
                "score so a single strong signal cannot be averaged into the middle of the pack."
            ),
            "signals_with_no_data_anywhere": sorted(
                set(dropped_signals_global) & {
                    k for k in cfg["commercial_weights"]
                    if all(k in a["risk_unmeasured"] for a in detail_by_account.values())
                }
            ),
            "notice_window_days": cfg["notice_window_days"],
            "thresholds": cfg["thresholds"],
            "redistribution_rule": (
                "A signal with no data has its weight redistributed proportionally across the "
                "signals that do have data. A missing feed never lowers a risk score."
            ),
        },
        "kickoff_baseline": {
            "captured": [a["name"] for a in detail_by_account.values() if a["kickoff"]["captured"]],
            "missing": [a["name"] for a in detail_by_account.values() if not a["kickoff"]["captured"]],
            "movement": [
                {
                    "account": a["name"],
                    "kickoff_date": a["kickoff"]["date"],
                    "sentiment_at_kickoff": a["kickoff"]["sentiment"],
                    "sentiment_now": a["sentiment"],
                    "sentiment_delta": a["kickoff"]["sentiment_delta"],
                    "arr_at_kickoff": a["kickoff"]["arr"],
                    "arr_now": a["arr"],
                    "arr_delta_pct": a["kickoff"]["arr_delta_pct"],
                }
                for a in sorted(detail_by_account.values(), key=lambda x: -x["arr"])
                if a["kickoff"]["captured"]
            ],
        },
        "book": {
            "accounts": len(accounts),
            "total_arr": round(sum(a["arr"] for a in detail_by_account.values())),
            "table": rows,
        },
    }
    return doc, detail_by_account


# ----------------------------------------------------------------------- findings


def _fmt_money(value: Optional[float]) -> str:
    return "unknown" if value is None else f"${value:,.0f}"


def _phrase(count: int, singular: str, plural: str) -> str:
    """"1 account is single-threaded" reads like a product; "1 accounts are" reads like a script."""
    return f"{count} {singular if count == 1 else plural}"


def add_findings(
    doc: FindingsDoc,
    accounts: Dict[str, Any],
    cfg: Dict[str, Any],
    queries: Dict[str, str],
    account_list: List[Dict[str, Any]],
    have_conversations: bool,
    now: datetime,
) -> None:
    notice = int(cfg["notice_window_days"])
    ordered = sorted(accounts.values(), key=lambda a: -a["arr"])

    def evidence(subset: List[Dict[str, Any]], rows: List[Dict[str, Any]], source: str) -> Dict[str, Any]:
        return {
            "count": len(subset),
            "sample_ids": [a["account_id"] for a in subset[:12]],
            "rows": rows,
            "query": queries.get(source, ""),
        }

    # 1 — unsigned renewals inside the notice window (the heaviest signal)
    subset = [
        a for a in ordered
        if a["renewal"].get("known") and not a["renewal"].get("is_signed")
        and a["renewal"].get("days_to_renewal") is not None
        and a["renewal"]["days_to_renewal"] <= notice
    ]
    if subset:
        doc.add(Finding(
            id="unsigned-renewals-in-notice-window",
            severity="critical",
            title=_phrase(len(subset), "renewal is", "renewals are")
                  + f" unsigned inside the {notice}-day notice window "
                    f"({_fmt_money(sum(a['arr'] for a in subset))} ARR)",
            what=(
                "These accounts are inside their contractual notice period with no signed renewal "
                "instrument. Some of them look perfectly happy on the calls. An unsigned renewal "
                "inside the window sets a floor under the commercial-risk score on its own — it is "
                "a tripwire, not one input among ten."
            ),
            why_it_matters=(
                "This is the highest-weighted signal in the model because it is the one that "
                "actually ends the contract. Sentiment can be excellent right up to the day the "
                "notice deadline passes and the account rolls into a downsell or a non-renewal."
            ),
            recommended_fix=(
                "Work this list top-down by ARR today. For each: confirm the renewal record exists "
                "and has the correct close date, name the signer, and get a written commitment to a "
                "signature date that lands before the notice deadline."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Renewal": a["renewal"].get("renewal_date"),
                    "Days out": a["renewal"].get("days_to_renewal"),
                    "Instrument": a["renewal"].get("instrument"),
                    "Stage": a["renewal"].get("stage") or "— none created —",
                    "Risk": "n/a" if a["commercial_risk"] is None else f"{a['commercial_risk']:.0f}",
                    "Sentiment": "n/a" if a["sentiment"] is None else f"{a['sentiment']:.0f}",
                    "Quadrant": a["quadrant_label"],
                    "Owner": a["owner"] or "unassigned",
                } for a in subset
            ], "renewals"),
            effort="quick",
            owner_hint="Customer Success leader",
        ))

    # 2 — the quadrant trap
    subset = [a for a in ordered if a["quadrant"] == "happy_but_exposed"]
    if subset:
        rows = []
        for a in subset:
            quote = a.get("top_quote") or {}
            rows.append({
                "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                "Sentiment": f"{a['sentiment']:.0f}", "Risk": f"{a['commercial_risk']:.0f}",
                "Renewal": a["renewal"].get("renewal_date") or "unknown",
                "Days out": a["renewal"].get("days_to_renewal"),
                "What they said": (quote.get("quote") or "—"),
                "Said by": f"{quote.get('speaker', '—')} · {quote.get('date', '')}",
            })
        doc.add(Finding(
            id="happy-but-exposed",
            severity="critical",
            title=_phrase(len(subset), "account is", "accounts are")
                  + f" happy and still at commercial risk "
                    f"({_fmt_money(sum(a['arr'] for a in subset))} ARR)",
            what=(
                "High sentiment, high commercial risk. Every one of these would look fine on a "
                "blended health score, because the good relationship averages out the bad paper."
            ),
            why_it_matters=(
                "This is the quadrant that churns. Nobody escalates a happy account, so nothing "
                "gets done until the renewal date arrives and the answer is a procurement process "
                "nobody started. The praise below is real — it is also not a renewal."
            ),
            recommended_fix=(
                "Treat these as commercial, not relationship, problems. Get the signer named, the "
                "paper moving and a signature date on the calendar. Use the goodwill in the quotes "
                "as the reason the conversation is easy — not as evidence it isn't needed."
            ),
            evidence=evidence(subset, rows, "renewals"),
            effort="quick",
            owner_hint="Customer Success leader",
        ))

    # 3 — champion departure
    subset = [
        a for a in ordered
        if a["champion"].get("state") in ("email_bouncing", "marked_inactive", "departed", "departed_public")
    ]
    if subset:
        doc.add(Finding(
            id="champion-departed",
            severity="critical",
            title=_phrase(len(subset), "account has lost its champion", "accounts have lost their champion")
                  + f" ({_fmt_money(sum(a['arr'] for a in subset))} ARR)",
            what="The named champion has left, gone inactive in the CRM, or their email is bouncing.",
            why_it_matters=(
                "Champion departure is the single most reliable leading indicator of churn, and it "
                "is invisible to sentiment analysis — the remaining contacts are usually perfectly "
                "pleasant. The person who spent political capital to keep you is gone, and the "
                "renewal now sits with someone who never chose you."
            ),
            recommended_fix=(
                "Within a week: confirm the departure, identify the replacement, and run a fresh "
                "value conversation with them as if it were a new sale. Re-baseline the account — "
                "the new champion has no memory of what you were hired to fix."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Champion": a["champion"].get("champion") or "—",
                    "Signal": a["champion"].get("state"),
                    "Evidence": "; ".join(a["champion"].get("evidence") or []) or "—",
                    "Renewal": a["renewal"].get("renewal_date") or "unknown",
                    "Risk": "n/a" if a["commercial_risk"] is None else f"{a['commercial_risk']:.0f}",
                } for a in subset
            ], "contacts"),
            effort="medium",
            owner_hint="Account owner",
        ))

    # 4 — champion fading / silent
    subset = [a for a in ordered if a["champion"].get("state") in ("silent", "fading", "absent_from_all_conversations")]
    if subset:
        doc.add(Finding(
            id="champion-disengaging",
            severity="high",
            title=_phrase(len(subset), "champion has", "champions have") + " gone quiet without leaving",
            what=(
                "The champion is still employed and still on the account, but has stopped appearing "
                "on calls and in threads."
            ),
            why_it_matters=(
                "A champion who stops showing up has usually been reassigned, overruled, or has "
                "quietly stopped defending the spend. It reads as calm right up until renewal."
            ),
            recommended_fix=(
                "Ask directly, in a 1:1, whether they are still the right person and whether "
                "anything changed internally. Do not accept a rescheduled meeting as an answer."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Champion": a["champion"].get("champion") or "—",
                    "Last seen": a["champion"].get("last_seen") or "not in window",
                    "Days quiet": a["champion"].get("days_since_seen", "—"),
                    "Renewal": a["renewal"].get("renewal_date") or "unknown",
                } for a in subset
            ], "interactions"),
            effort="quick",
            owner_hint="Account owner",
        ))

    # 5 — single-threaded accounts
    subset = [a for a in ordered if a["engaged_contacts"] <= 1]
    if subset:
        doc.add(Finding(
            id="single-threaded-accounts",
            severity="high",
            title=_phrase(len(subset), "account is", "accounts are")
                  + f" single-threaded "
                    f"({_fmt_money(sum(a['arr'] for a in subset))} ARR resting on one relationship)",
            what="One or zero people at the customer have engaged with you across the whole window.",
            why_it_matters=(
                "One person is one resignation, one reorg or one bad quarter away from an account "
                "with nobody in it. Single-threading is also why a champion departure lands as a "
                "surprise instead of a handover."
            ),
            recommended_fix=(
                "Set a floor of three engaged contacts per account: the champion, an economic buyer "
                "and one day-to-day user. Book the second and third relationships this month, with a "
                "reason to meet that is theirs, not yours."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Engaged contacts": a["engaged_contacts"],
                    "At kickoff": a["kickoff"].get("engaged_contacts_at_kickoff", "—"),
                    "Renewal": a["renewal"].get("renewal_date") or "unknown",
                    "Risk": "n/a" if a["commercial_risk"] is None else f"{a['commercial_risk']:.0f}",
                } for a in subset
            ], "interactions"),
            effort="medium",
            owner_hint="Account owner",
        ))

    # 6 — silence, weighted by ARR
    subset = [a for a in ordered if (a["silence"].get("days_since_touch") is None
                                     or a["silence"]["days_since_touch"] > a["silence"].get("tolerated_days", 30))]
    if subset:
        doc.add(Finding(
            id="silent-accounts",
            severity="high",
            title=_phrase(len(subset), "account has gone quiet past its", "accounts have gone quiet past their")
                  + f" ARR-weighted silence tolerance "
                    f"({_fmt_money(sum(a['arr'] for a in subset))} ARR)",
            what=(
                "No call, message or logged activity for longer than an account of this size should "
                "go without one. Tolerance scales with ARR — a large account is allowed far less silence."
            ),
            why_it_matters=(
                "Silence is not neutral. It is where a competitor evaluation, a budget review or a "
                "quiet decision to not renew happens without you in the room."
            ),
            recommended_fix=(
                "Re-establish contact this week with something worth their time — a result, a "
                "benchmark, a risk you spotted. Then set a cadence commitment per ARR tier and hold it."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Days silent": a["silence"].get("days_since_touch", "no touch in window"),
                    "Tolerance": f"{a['silence'].get('tolerated_days')}d ({a['silence'].get('tier')} tier)",
                    "Renewal": a["renewal"].get("renewal_date") or "unknown",
                    "Owner": a["owner"] or "unassigned",
                } for a in subset
            ], "crm_activities"),
            effort="quick",
            owner_hint="Account owner",
        ))

    # 7 — external company risk
    subset = [a for a in ordered if (a["external"].get("events") or []) and (a["commercial_risk"] or 0) > 0
              and any(e["points"] > 0 for e in a["external"]["events"])]
    if subset:
        rows = []
        for a in subset:
            worst = max((e for e in a["external"]["events"] if e["points"] > 0), key=lambda e: e["points"])
            rows.append({
                "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                "Signal": worst.get("type"), "Date": worst.get("date"),
                "Headline": worst.get("headline"), "Source": worst.get("source") or "—",
                "Funding stage": a["external"].get("funding_stage") or "—",
                "Months since raise": a["external"].get("months_since_last_raise", "—"),
            })
        doc.add(Finding(
            id="external-company-risk",
            severity="high",
            title=_phrase(len(subset), "customer shows", "customers show")
                  + " churn signals coming from their own business, not yours",
            what=(
                "Layoffs, down rounds, acquisitions, exec turnover or funding pressure at the "
                "customer, found by researching the company rather than reading your own calls."
            ),
            why_it_matters=(
                "A budget freeze after a down round will end a contract that has perfect sentiment "
                "and a perfect delivery record. No amount of transcript analysis surfaces it, "
                "because your customer will not raise it on a status call."
            ),
            recommended_fix=(
                "For each: find out what changed and who now owns the budget. Pre-empt the "
                "procurement review with a defensible ROI case before it is requested, and know "
                "which line item you are on when the spend review happens."
            ),
            evidence=evidence(subset, rows, "company_research"),
            effort="medium",
            owner_hint="Account owner",
        ))

    # 8 — sentiment decline
    subset = [a for a in ordered if (a["sentiment_trend"].get("delta") is not None
                                     and a["sentiment_trend"]["delta"] <= -10)]
    if subset:
        rows = []
        for a in subset:
            quote = a.get("top_quote") or {}
            rows.append({
                "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                "Sentiment now": f"{a['sentiment']:.0f}" if a["sentiment"] is not None else "n/a",
                "Earlier half": a["sentiment_trend"].get("earlier"),
                "Recent half": a["sentiment_trend"].get("recent"),
                "Move": a["sentiment_trend"].get("delta"),
                "What they said": quote.get("quote") or "—",
                "Said by": f"{quote.get('speaker', '—')} ({quote.get('speaker_role') or 'role unknown'})",
                "When / where": f"{quote.get('date', '')} · {quote.get('source', '')}",
            })
        doc.add(Finding(
            id="sentiment-declining",
            severity="high",
            title=_phrase(len(subset), "account got", "accounts got") + " measurably less happy across the window",
            what=(
                "Sentiment in the recent half of the window is at least 10 points below the earlier "
                "half, based on Claude's reading of the actual conversations."
            ),
            why_it_matters=(
                "A trend is information; a point reading is not. An account at 68 and falling is a "
                "worse position than an account at 55 and rising, and only one of them is on most "
                "health dashboards."
            ),
            recommended_fix=(
                "Take the verbatim quote to the account owner and ask what changed between the two "
                "halves. If it maps to a delivery event, fix that. If it maps to a person change, "
                "you have a champion problem, not a satisfaction problem."
            ),
            evidence=evidence(subset, rows, "sentiment_readings"),
            effort="medium",
            owner_hint="Customer Success leader",
        ))

    # 9 — contract value decline
    subset = [a for a in ordered if (a["contract_trend"].get("change_pct") is not None
                                     and a["contract_trend"]["change_pct"] <= -float(cfg["thresholds"]["downsell_material_pct"]))]
    if subset:
        doc.add(Finding(
            id="contract-value-declining",
            severity="high",
            title=_phrase(len(subset), "account is", "accounts are") + " worth materially less than at kickoff",
            what="Current ARR is below the ARR captured in the kickoff baseline.",
            why_it_matters=(
                "A downsell is a partial churn that nobody logs as churn. It is also the most common "
                "shape a renewal takes when the notice window passes without a conversation."
            ),
            recommended_fix=(
                "Find the specific thing that was removed and whether it was removed for cost or for "
                "value. Cost is a packaging conversation; value is a delivery conversation."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"],
                    "ARR at kickoff": _fmt_money(a["contract_trend"].get("kickoff_arr")),
                    "ARR now": _fmt_money(a["arr"]),
                    "Change": f"{a['contract_trend'].get('change_pct')}%",
                    "Since": a["contract_trend"].get("kickoff_date") or "—",
                } for a in subset
            ], "customer_accounts"),
            effort="medium",
            owner_hint="Account owner",
        ))

    # 10 — executive touch gap
    target = int(cfg["thresholds"]["exec_touch_target_days"])
    subset = [a for a in ordered if a["days_since_exec_touch"] is None or a["days_since_exec_touch"] > target]
    if subset:
        doc.add(Finding(
            id="executive-touch-gap",
            severity="medium",
            title=_phrase(len(subset), "account has", "accounts have")
                  + f" had no executive-level contact in {target}+ days",
            what="No interaction inside the window included anyone at executive level on either side.",
            why_it_matters=(
                "Executive relationships are what survive a champion leaving and what get you into "
                "the room when the budget review happens. They cannot be built during the notice window."
            ),
            recommended_fix=(
                "Schedule one executive-to-executive conversation per quarter on the accounts above, "
                "starting with the largest ARR and the nearest renewal date."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Days since exec touch": a["days_since_exec_touch"] if a["days_since_exec_touch"] is not None
                    else "none in window",
                    "Renewal": a["renewal"].get("renewal_date") or "unknown",
                } for a in subset
            ], "interactions"),
            effort="medium",
            owner_hint="Executive sponsor",
        ))

    # 11 — support burden
    subset = [a for a in ordered if a["support"]["open_critical"] > 0]
    if subset:
        doc.add(Finding(
            id="open-critical-tickets",
            severity="high",
            title=_phrase(len(subset), "account has", "accounts have") + " open critical support tickets",
            what="At least one ticket at critical/P1 severity is still open on these accounts.",
            why_it_matters=(
                "An open P1 at renewal time is a negotiating position for the customer and a reason "
                "for procurement to slow the paper down."
            ),
            recommended_fix=(
                "Close or credibly re-scope every open critical ticket on any account renewing "
                "inside the notice window, and say so in writing to the champion."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Open critical": a["support"]["open_critical"],
                    "Weighted tickets / $100k ARR": a["support"]["weighted_per_100k_arr"],
                    "Renewal": a["renewal"].get("renewal_date") or "unknown",
                } for a in subset
            ], "support_tickets"),
            effort="medium",
            owner_hint="Support lead",
        ))

    # 12 — usage decline
    subset = [a for a in ordered if (a["usage"].get("change_pct") is not None and a["usage"]["change_pct"] <= -20)]
    if subset:
        doc.add(Finding(
            id="product-usage-declining",
            severity="high",
            title=_phrase(len(subset), "account is using the product materially less than its baseline",
                          "accounts are using the product materially less than their baseline"),
            what="The tracked usage metric has fallen 20% or more against its baseline.",
            why_it_matters=(
                "Usage decline usually precedes the sentiment decline by a quarter — people stop "
                "logging in long before they say anything on a call."
            ),
            recommended_fix=(
                "Identify which team stopped using it and why. A usage cliff on one team is a "
                "reorg; a slow slide across all teams is a value problem."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Metric": a["usage"].get("metric"),
                    "Baseline": a["usage"].get("baseline"), "Recent": a["usage"].get("recent"),
                    "Change": f"{a['usage'].get('change_pct')}%",
                } for a in subset
            ], "product_usage"),
            effort="medium",
            owner_hint="Account owner",
        ))

    # 13 — renewal date unknown (an evidence gap, never a pass)
    subset = [a for a in ordered if not a["renewal"].get("known")]
    if subset:
        doc.add(Finding(
            id="renewal-date-unknown",
            severity="high",
            title=_phrase(len(subset), "customer has", "customers have")
                  + f" no discoverable renewal date "
                    f"({_fmt_money(sum(a['arr'] for a in subset))} ARR unmeasurable)",
            what=(
                "No renewal opportunity, contract, subscription record or account field carries a "
                "renewal date for these accounts."
            ),
            why_it_matters=(
                "The heaviest signal in this model cannot be computed for them. They are not low "
                "risk — they are unmeasured, and they will not appear on any renewal forecast either."
            ),
            recommended_fix=(
                "Backfill the renewal date on these accounts from the signed paper, and make the "
                "field required at close so the gap does not reappear next quarter."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Owner": a["owner"] or "unassigned",
                    "Sentiment": "n/a" if a["sentiment"] is None else f"{a['sentiment']:.0f}",
                } for a in subset
            ], "renewals"),
            effort="quick",
            owner_hint="RevOps",
        ))

    # 14 — no conversation coverage
    subset = [a for a in ordered if a["sentiment"] is None]
    if subset:
        doc.add(Finding(
            id="no-sentiment-coverage",
            severity="medium" if have_conversations else "high",
            title=_phrase(len(subset), "customer has", "customers have")
                  + f" no sentiment reading at all "
                    f"({_fmt_money(sum(a['arr'] for a in subset))} ARR scored on paper alone)",
            what=(
                "No call transcripts and no shared message threads were available for these "
                "accounts, so only the commercial-risk half of the model ran."
            ),
            why_it_matters=(
                "Their absence from the sentiment findings is not a clean bill of health — it is a "
                "blind spot. The most common way an account surprises you is that nobody was "
                "listening to it in the first place."
            ),
            recommended_fix=(
                "Either connect a conversation source for these accounts (recorder, shared channel "
                "or an exported transcript folder) or accept that they are managed on paper only and "
                "review them manually every month."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Risk": "n/a" if a["commercial_risk"] is None else f"{a['commercial_risk']:.0f}",
                    "Renewal": a["renewal"].get("renewal_date") or "unknown",
                    "Owner": a["owner"] or "unassigned",
                } for a in subset
            ], "interactions"),
            effort="project",
            owner_hint="RevOps",
        ))

    # 15 — kickoff baseline missing
    subset = [a for a in ordered if not a["kickoff"]["captured"]]
    if subset:
        doc.add(Finding(
            id="kickoff-baseline-missing",
            severity="medium",
            title=_phrase(len(subset), "customer has no kickoff baseline, so its progress is unprovable",
                          "customers have no kickoff baseline, so their progress is unprovable"),
            what=(
                "No starting sentiment, starting ARR or starting contact count was captured for "
                "these accounts, so nothing in this report can show movement for them."
            ),
            why_it_matters=(
                "A health score without a baseline is a vibe. At renewal you will be asked what "
                "changed since you started, and 'they are at 64' is not an answer — 'they started "
                "at 41 and they are at 64' is."
            ),
            recommended_fix=(
                "Re-run /customer-health:setup and fill in the kickoff block for each account "
                "below. Reconstruct it honestly from the earliest call in the window and mark it "
                "as reconstructed in the notes — an approximate baseline beats none."
            ),
            evidence=evidence(subset, [
                {
                    "Account": a["name"], "ARR": _fmt_money(a["arr"]),
                    "Sentiment now": "n/a" if a["sentiment"] is None else f"{a['sentiment']:.0f}",
                    "Risk now": "n/a" if a["commercial_risk"] is None else f"{a['commercial_risk']:.0f}",
                    "Owner": a["owner"] or "unassigned",
                } for a in subset
            ], "customer_accounts"),
            effort="quick",
            owner_hint="Customer Success leader",
        ))


# ---------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="customer-health: raw/*.json -> findings.json")
    parser.add_argument("--run-dir", required=True, help="Run directory, e.g. ./gtm-agents/customer-health/2026-08-10-0900")
    parser.add_argument("--raw", default=None, help="Override the raw/ directory (used for the offline fixture run)")
    parser.add_argument("--window-days", type=int, default=None, help="Override window_days from config")
    parser.add_argument("--as-of", default=None, help="Treat this ISO date as 'today'. Fixtures and replays only.")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser().resolve()
    raw_dir = Path(args.raw).expanduser().resolve() if args.raw else run_dir / "raw"
    if not raw_dir.exists():
        print(f"No raw directory at {raw_dir}. The run skill writes it before analyze.py runs.", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        profile = load_profile(required=True)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cfg = deep_merge(DEFAULTS, load_plugin_config(PLUGIN, defaults={}))
    if args.window_days:
        cfg["window_days"] = args.window_days

    raw = load_raw(raw_dir)

    now = parse_dt(args.as_of) or parse_dt((raw["_sources"].get("window") or {}).get("end")) or datetime.now(timezone.utc)
    declared_window = raw["_sources"].get("window") or {}
    window = {
        "start": declared_window.get("start")
        or (now - timedelta(days=int(cfg["window_days"]))).strftime("%Y-%m-%d"),
        "end": declared_window.get("end") or now.strftime("%Y-%m-%d"),
    }

    manifest = build_manifest(raw, run_dir, window)
    manifest.finalize()  # raises SourceEmptyError if a required source came back empty

    doc, _ = analyze(raw, cfg, profile, now, window)
    doc.unavailable = build_unavailable(manifest, raw, doc)
    path = doc.write(run_dir)

    print(f"findings.json  -> {path}")
    print(f"manifest.json  -> {run_dir / 'manifest.json'}")
    print(
        f"{doc.sections['book']['accounts']} customers · "
        f"{len(doc.findings)} findings · "
        f"{doc.sections['quadrant']['counts']['happy_but_exposed']} happy-but-exposed"
    )
    return 0


def build_unavailable(manifest: RunManifest, raw: Dict[str, Any], doc: FindingsDoc) -> List[str]:
    """Everything the run could not see, phrased so it never reads as a clean result."""
    labels = {
        "interactions": "Call transcripts and shared message threads",
        "sentiment_readings": "Sentiment readings",
        "company_research": "Company research (funding, layoffs, exec changes)",
        "support_tickets": "Support tickets",
        "product_usage": "Product usage",
        "expansion_pipeline": "Open expansion pipeline",
        "crm_activities": "CRM activity history",
        "contacts": "Contact records",
    }
    out = [labels.get(name, name) for name in manifest.unavailable_optional()]
    if not raw["interactions"] and not raw["sentiment"]:
        out.append("Sentiment scoring for the entire book — no conversation source is connected")
    else:
        blind = [a["name"] for a in doc.sections["accounts"] if a["sentiment"] is None]
        if blind:
            out.append(f"Sentiment for {len(blind)} of {len(doc.sections['accounts'])} accounts")
    dropped = doc.sections["model"]["signals_with_no_data_anywhere"]
    if dropped:
        out.append("Commercial signals with no data anywhere in the book: " + ", ".join(dropped))
    return out


if __name__ == "__main__":
    sys.exit(main())
