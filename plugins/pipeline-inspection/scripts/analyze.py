#!/usr/bin/env python3
"""
pipeline-inspection / analyze.py

Layer 2 of the plugin. Pure stdlib, offline, deterministic. It reads the raw
JSON that the :run skill fetched out of the CRM and turns it into findings.json.

This is deliberately NOT a forecast. It answers one question per deal:
"which of your own rules is this deal breaking, right now?" Every rule is
checkable by a human in their own CRM in under a minute, which is why every
finding carries a row table and the exact query that reproduces it.

Nothing here hardcodes an industry benchmark. Stage medians, cycle times and
push distributions are MEASURED from the customer's own closed history; the
thresholds are multiples of their own numbers, confirmed during setup.

Input  (written by skills/run/SKILL.md):
    <run>/raw/meta.json                 crm, as_of, per-source provenance
    <run>/raw/open_opportunities.json   REQUIRED
    <run>/raw/closed_opportunities.json REQUIRED (source of the measured medians)
    <run>/raw/stage_history.json        optional
    <run>/raw/field_history.json        optional
    <run>/raw/contact_roles.json        optional
    <run>/raw/activities.json           optional
    <run>/raw/open_tasks.json           optional
    <run>/raw/stage_metadata.json       optional
    <run>/raw/quota.json                optional

Output:
    <run>/findings.json
    <run>/manifest.json

Usage:
    python3 analyze.py --run-dir ./gtm-agents/pipeline-inspection/2026-08-10-0900
    python3 analyze.py --raw-dir ./fixtures/raw --out-dir /tmp/demo \
                       --config ./config.example.json --as-of 2026-08-10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    ConfigError,
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    load_plugin_config,
    load_profile,
    median,
    normalize_records,
    parse_dt,
    percentile,
)
from lib.config import GTM_HOME, fiscal_period, plugin_config_path, profile_path  # noqa: E402
from lib.crmutil import is_blank, to_number  # noqa: E402

PLUGIN = "pipeline-inspection"

# --------------------------------------------------------------------------- defaults

DEFAULTS: Dict[str, Any] = {
    "crm": None,
    "stage_order": [],
    "commit_stages": [],
    "closed_won_stages": [],
    "closed_lost_stages": [],
    "expected_days_in_stage": {},
    "stagnation_basis": "measured_median",
    "stagnation_multiple": 2.0,
    "severe_stagnation_multiple": 4.0,
    "min_closed_deals_for_median": 8,
    "fallback_days_in_stage": 60,
    "next_step_mode": "field",
    "next_step_field": "NextStep",
    "next_step_staleness_days": 14,
    "push_threshold": 3,
    "push_watch_threshold": 1,
    "history_lookback_months": 24,
    "single_thread_thresholds": [
        {"min_amount": 0, "min_contacts": 1},
        {"min_amount": 25000, "min_contacts": 2},
        {"min_amount": 100000, "min_contacts": 3},
        {"min_amount": 250000, "min_contacts": 5},
    ],
    "activity_silence_days": 21,
    "activity_silence_days_by_stage": {},
    "deal_size_bands": [
        {"label": "Small", "min_amount": 0},
        {"label": "Mid-Market", "min_amount": 50000},
        {"label": "Enterprise", "min_amount": 250000},
    ],
    "clustering_flag_pct": 30.0,
    "amount_change_tolerance_pct": 10.0,
    "same_day_close_max_days": 1,
    "close_date_realism_multiple": 0.5,
    "material_deal_floor": None,
    "require_closed_history": True,
    "escalate_amount_share_pct": 20.0,
    "max_rows_per_finding": 50,
    "call_list_size": 30,
    "inspection_cadence": "weekly",
    "field_map": {},
    "owner_scope": [],
}

SF_FIELD_MAP = {
    "id": "Id",
    "name": "Name",
    "amount": "Amount",
    "stage": "StageName",
    "close_date": "CloseDate",
    "created": "CreatedDate",
    "last_modified": "LastModifiedDate",
    "last_activity": "LastActivityDate",
    "last_stage_change": "LastStageChangeDate",
    "next_step": "NextStep",
    "owner": "Owner.Name",
    "owner_id": "OwnerId",
    "account": "Account.Name",
    "account_id": "AccountId",
    "is_closed": "IsClosed",
    "is_won": "IsWon",
    "type": "Type",
    "forecast_category": "ForecastCategoryName",
    "currency": "CurrencyIsoCode",
}

HS_FIELD_MAP = {
    "id": "Id",
    "name": "dealname",
    "amount": "amount",
    "stage": "dealstage",
    "close_date": "closedate",
    "created": "createdate",
    "last_modified": "hs_lastmodifieddate",
    "last_activity": "notes_last_updated",
    "last_stage_change": "hs_v2_date_entered_current_stage",
    "next_step": "hs_next_step",
    "owner": "hubspot_owner_name",
    "owner_id": "hubspot_owner_id",
    "account": "associated_company_name",
    "account_id": "associatedcompanyid",
    "is_closed": "hs_is_closed",
    "is_won": "hs_is_closed_won",
    "type": "dealtype",
    "forecast_category": "hs_manual_forecast_category",
    "currency": "deal_currency_code",
}

SEV_ORDER = ["low", "medium", "high", "critical"]
RULE_WEIGHT = {"critical": 8, "high": 5, "medium": 3, "low": 1}


def escalate(sev: str, steps: int = 1) -> str:
    i = SEV_ORDER.index(sev) if sev in SEV_ORDER else 1
    return SEV_ORDER[min(len(SEV_ORDER) - 1, i + steps)]


# ------------------------------------------------------------------------- raw loading


def _records_from(payload: Any) -> List[Dict[str, Any]]:
    """Accept every shape these MCP servers hand back without complaining."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("records", "results", "data", "rows", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # A single record handed back bare.
        if any(k in payload for k in ("Id", "id", "properties")):
            return [payload]
    return []


def read_raw(raw_dir: Path, name: str) -> List[Dict[str, Any]]:
    path = raw_dir / f"{name}.json"
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            return _records_from(json.load(fh))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON ({exc}). The :run skill writes this file — "
            f"re-run /pipeline-inspection:run, or delete the file and re-fetch that source."
        ) from exc


def read_meta(raw_dir: Path) -> Dict[str, Any]:
    path = raw_dir / "meta.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def detect_crm(meta: Dict[str, Any], profile: Dict[str, Any], sample: Sequence[Dict[str, Any]]) -> str:
    for candidate in (meta.get("crm"), (profile.get("crm") or {}).get("system")):
        if candidate in ("salesforce", "hubspot"):
            return candidate
    for record in sample[:25]:
        if "properties" in record or "dealstage" in record or "hs_object_id" in record:
            return "hubspot"
        if "StageName" in record or str(record.get("Id", "")).startswith("006"):
            return "salesforce"
    return "salesforce"


# ------------------------------------------------------------------------ normalizing


def _get(record: Dict[str, Any], key: str) -> Any:
    if key in record:
        return record[key]
    lowered = key.lower()
    for k, v in record.items():
        if k.lower() == lowered:
            return v
    return None


def _hs_stage_entry(record: Dict[str, Any], stage: Any, as_of: datetime) -> Tuple[Optional[datetime], str]:
    """HubSpot spreads stage-entry timestamps across per-stage property names."""
    if stage:
        for prefix in ("hs_v2_date_entered_", "hs_date_entered_"):
            value = _get(record, f"{prefix}{stage}")
            parsed = parse_dt(value)
            if parsed:
                return parsed, prefix + str(stage)
    parsed = parse_dt(_get(record, "hs_v2_date_entered_current_stage"))
    if parsed:
        return parsed, "hs_v2_date_entered_current_stage"
    duration_ms = to_number(_get(record, "hs_v2_latest_time_in_current_stage"))
    if duration_ms and duration_ms > 0:
        return as_of - timedelta(milliseconds=duration_ms), "hs_v2_latest_time_in_current_stage"
    return None, ""


def normalize_deals(
    records: Sequence[Dict[str, Any]], fmap: Dict[str, str], crm: str, as_of: datetime
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for record in normalize_records(records):
        deal_id = _get(record, fmap["id"]) or _get(record, "Id") or _get(record, "id")
        if is_blank(deal_id):
            continue
        stage = _get(record, fmap["stage"])
        stage_entry, stage_entry_src = None, ""
        if crm == "hubspot":
            stage_entry, stage_entry_src = _hs_stage_entry(record, stage, as_of)
        else:
            stage_entry = parse_dt(_get(record, fmap["last_stage_change"]))
            stage_entry_src = fmap["last_stage_change"] if stage_entry else ""
        deal = {
            "id": str(deal_id),
            "name": _get(record, fmap["name"]) or f"(unnamed {deal_id})",
            "amount": to_number(_get(record, fmap["amount"])),
            "stage": str(stage) if stage is not None else "",
            "close_date": parse_dt(_get(record, fmap["close_date"])),
            "created": parse_dt(_get(record, fmap["created"])),
            "last_modified": parse_dt(_get(record, fmap["last_modified"])),
            "last_activity": parse_dt(_get(record, fmap["last_activity"])),
            "stage_entry": stage_entry,
            "stage_entry_source": stage_entry_src,
            "next_step": _get(record, fmap["next_step"]),
            "owner": _get(record, fmap["owner"]) or _get(record, fmap["owner_id"]) or "(unassigned)",
            "owner_id": _get(record, fmap["owner_id"]),
            "account": _get(record, fmap["account"]) or "(no account)",
            "account_id": _get(record, fmap["account_id"]),
            "type": _get(record, fmap["type"]),
            "forecast_category": _get(record, fmap["forecast_category"]),
            "currency": _get(record, fmap["currency"]),
            "is_closed": _truthy(_get(record, fmap["is_closed"])),
            "is_won": _truthy(_get(record, fmap["is_won"])),
            "raw": record,
        }
        out.append(deal)
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "y", "won")


# ------------------------------------------------------------------------ stage model


def build_stage_model(records: Sequence[Dict[str, Any]], crm: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize Salesforce OpportunityStage rows and HubSpot pipeline payloads into
    one shape: ordered stages with won/closed flags and a display label.
    """
    stages: List[Dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if "stages" in record and isinstance(record["stages"], list):  # HubSpot pipeline
            pipeline_label = record.get("label") or record.get("id")
            for stage in record["stages"]:
                meta = stage.get("metadata") or {}
                probability = to_number(meta.get("probability"))
                stages.append(
                    {
                        "key": str(stage.get("id")),
                        "label": stage.get("label") or str(stage.get("id")),
                        "order": to_number(stage.get("displayOrder")) or 0.0,
                        "is_closed": str(meta.get("isClosed", "")).lower() == "true",
                        "is_won": probability == 1.0,
                        "pipeline": pipeline_label,
                    }
                )
        elif "MasterLabel" in record or "ApiName" in record:  # Salesforce OpportunityStage
            key = record.get("ApiName") or record.get("MasterLabel")
            stages.append(
                {
                    "key": str(key),
                    "label": str(record.get("MasterLabel") or key),
                    "order": to_number(record.get("SortOrder")) or 0.0,
                    "is_closed": _truthy(record.get("IsClosed")),
                    "is_won": _truthy(record.get("IsWon")),
                    "pipeline": "",
                }
            )
    stages.sort(key=lambda s: s["order"])

    order = [s["key"] for s in stages] or list(cfg.get("stage_order") or [])
    labels = {s["key"]: s["label"] for s in stages}
    won = {s["key"] for s in stages if s["is_won"]}
    lost = {s["key"] for s in stages if s["is_closed"] and not s["is_won"]}
    won |= set(cfg.get("closed_won_stages") or [])
    lost |= set(cfg.get("closed_lost_stages") or [])
    return {
        "order": order,
        "index": {key: i for i, key in enumerate(order)},
        "labels": labels,
        "won": won,
        "lost": lost,
        "detail": stages,
        "source": "CRM stage metadata" if stages else "config stage_order",
    }


# ---------------------------------------------------------------------------- history


def _hs_history_events(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """HubSpot batch-read with propertiesWithHistory -> flat change events."""
    out: List[Dict[str, Any]] = []
    bag = record.get("propertiesWithHistory") or record.get("properties_with_history") or {}
    if not isinstance(bag, dict):
        return out
    deal_id = str(record.get("id") or record.get("Id") or "")
    for prop, versions in bag.items():
        if not isinstance(versions, list):
            continue
        ordered = []
        for version in versions:
            if not isinstance(version, dict):
                continue
            at = parse_dt(version.get("timestamp") or version.get("updatedAt"))
            if at is None:
                continue
            ordered.append({"at": at, "value": version.get("value")})
        ordered.sort(key=lambda v: v["at"])
        previous = None
        for i, version in enumerate(ordered):
            out.append(
                {
                    "opp_id": deal_id,
                    "at": version["at"],
                    "field": prop,
                    "old": previous,
                    "new": version["value"],
                    "is_first": i == 0,
                }
            )
            previous = version["value"]
    return out


def load_change_events(
    field_history: Sequence[Dict[str, Any]], stage_history: Sequence[Dict[str, Any]], fmap: Dict[str, str]
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """
    Returns (stage_events, close_date_changes, amount_changes, coverage).

    Three input shapes are handled, because customers have all three:
      · Salesforce OpportunityFieldHistory  {OpportunityId, Field, OldValue, NewValue, CreatedDate}
      · Salesforce OpportunityHistory       {OpportunityId, CreatedDate, StageName, Amount, CloseDate}
      · HubSpot batch-read propertiesWithHistory
    """
    stage_events: Dict[str, List[Dict[str, Any]]] = {}
    close_changes: Dict[str, List[Dict[str, Any]]] = {}
    amount_changes: Dict[str, List[Dict[str, Any]]] = {}
    seen_at: List[datetime] = []
    shapes: Dict[str, int] = {}

    def bump(shape: str) -> None:
        shapes[shape] = shapes.get(shape, 0) + 1

    # ---- explicit field-history rows (SF) and HubSpot property history
    for record in list(field_history or []) + list(stage_history or []):
        if not isinstance(record, dict):
            continue
        if "propertiesWithHistory" in record or "properties_with_history" in record:
            bump("hubspot_property_history")
            for event in _hs_history_events(record):
                _file_change(event, stage_events, close_changes, amount_changes, seen_at)
            continue

        flat = normalize_records([record])[0]
        opp_id = str(_get(flat, "OpportunityId") or _get(flat, "opp_id") or _get(flat, "Id") or "")
        at = parse_dt(_get(flat, "CreatedDate") or _get(flat, "at") or _get(flat, "timestamp"))
        if not opp_id or at is None:
            continue
        field = _get(flat, "Field") or _get(flat, "field")
        if field:  # OpportunityFieldHistory
            bump("salesforce_field_history")
            _file_change(
                {
                    "opp_id": opp_id,
                    "at": at,
                    "field": str(field),
                    "old": _get(flat, "OldValue"),
                    "new": _get(flat, "NewValue"),
                    "is_first": False,
                },
                stage_events,
                close_changes,
                amount_changes,
                seen_at,
            )
        else:  # OpportunityHistory snapshot row
            bump("salesforce_stage_history")
            seen_at.append(at)
            stage_value = _get(flat, "StageName")
            if stage_value:
                stage_events.setdefault(opp_id, []).append({"at": at, "stage": str(stage_value)})
            snap = stage_events.setdefault("__snap__" + opp_id, [])
            snap.append(
                {
                    "at": at,
                    "close_date": parse_dt(_get(flat, "CloseDate")),
                    "amount": to_number(_get(flat, "Amount")),
                }
            )

    # ---- derive close-date / amount changes from OpportunityHistory snapshots where
    #      explicit field history was not available for that deal.
    for key in [k for k in stage_events if k.startswith("__snap__")]:
        opp_id = key[len("__snap__"):]
        snaps = sorted(stage_events.pop(key), key=lambda s: s["at"])
        if opp_id in close_changes and close_changes[opp_id]:
            derived_close = False
        else:
            derived_close = True
        if opp_id in amount_changes and amount_changes[opp_id]:
            derived_amount = False
        else:
            derived_amount = True
        prev_close, prev_amount = None, None
        for i, snap in enumerate(snaps):
            if derived_close and snap["close_date"] is not None:
                if prev_close is not None and snap["close_date"] != prev_close:
                    close_changes.setdefault(opp_id, []).append(
                        {"at": snap["at"], "old": prev_close, "new": snap["close_date"], "derived": True}
                    )
                prev_close = snap["close_date"]
            if derived_amount and snap["amount"] is not None:
                if prev_amount is not None and snap["amount"] != prev_amount:
                    amount_changes.setdefault(opp_id, []).append(
                        {"at": snap["at"], "old": prev_amount, "new": snap["amount"], "derived": True}
                    )
                prev_amount = snap["amount"]
            del i

    for events in stage_events.values():
        events.sort(key=lambda e: e["at"])
    for changes in close_changes.values():
        changes.sort(key=lambda c: c["at"])
    for changes in amount_changes.values():
        changes.sort(key=lambda c: c["at"])

    coverage = {
        "shapes": shapes,
        "oldest_change_seen": min(seen_at).strftime("%Y-%m-%d") if seen_at else None,
        "newest_change_seen": max(seen_at).strftime("%Y-%m-%d") if seen_at else None,
        "deals_with_stage_events": len(stage_events),
        "deals_with_close_date_changes": len(close_changes),
        "deals_with_amount_changes": len(amount_changes),
    }
    del fmap
    return stage_events, close_changes, amount_changes, coverage


def _file_change(
    event: Dict[str, Any],
    stage_events: Dict[str, List[Dict[str, Any]]],
    close_changes: Dict[str, List[Dict[str, Any]]],
    amount_changes: Dict[str, List[Dict[str, Any]]],
    seen_at: List[datetime],
) -> None:
    field = str(event.get("field") or "").lower()
    opp_id, at = event["opp_id"], event["at"]
    seen_at.append(at)
    if field in ("closedate", "close_date"):
        old, new = parse_dt(event.get("old")), parse_dt(event.get("new"))
        if new is not None and old is not None and new != old:
            close_changes.setdefault(opp_id, []).append({"at": at, "old": old, "new": new, "derived": False})
    elif field == "amount":
        old, new = to_number(event.get("old")), to_number(event.get("new"))
        if new is not None and old is not None and new != old:
            amount_changes.setdefault(opp_id, []).append({"at": at, "old": old, "new": new, "derived": False})
    elif field in ("stagename", "dealstage", "stage"):
        new = event.get("new")
        if not is_blank(new):
            stage_events.setdefault(opp_id, []).append({"at": at, "stage": str(new)})
        old = event.get("old")
        if event.get("is_first") and not is_blank(old):
            stage_events.setdefault(opp_id, []).insert(0, {"at": at - timedelta(seconds=1), "stage": str(old)})


def collapse_runs(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for event in events:
        if not out or out[-1]["stage"] != event["stage"]:
            out.append(dict(event))
    return out


# ------------------------------------------------------------- measured stage medians


def measure_stage_durations(
    closed: Sequence[Dict[str, Any]], stage_events: Dict[str, List[Dict[str, Any]]], crm: str
) -> Tuple[Dict[str, List[float]], Dict[str, Any]]:
    """
    Days spent in each stage, measured from the customer's own closed deals.
    Only COMPLETED intervals count — a stage you entered and left.
    """
    durations: Dict[str, List[float]] = {}
    inferred, total = 0, 0

    for deal in closed:
        events = collapse_runs(stage_events.get(deal["id"], []))
        if events:
            created = deal.get("created")
            if created and (events[0]["at"] - created).total_seconds() > 86400:
                events = [{"at": created, "stage": events[0]["stage"], "inferred": True}] + events
                events = collapse_runs(events)
            for i in range(len(events) - 1):
                days = (events[i + 1]["at"] - events[i]["at"]).total_seconds() / 86400.0
                if days < 0:
                    continue
                durations.setdefault(events[i]["stage"], []).append(days)
                total += 1
                if events[i].get("inferred"):
                    inferred += 1
        elif crm == "hubspot":
            # HubSpot exposes per-stage dwell time directly; use it when history is absent.
            for key, value in (deal.get("raw") or {}).items():
                match = re.match(r"^hs_(?:v2_)?time_in_(.+)$", str(key))
                if not match:
                    continue
                millis = to_number(value)
                if millis and millis > 0:
                    durations.setdefault(match.group(1), []).append(millis / 86400000.0)
                    total += 1

    meta = {
        "intervals_measured": total,
        "intervals_inferred_from_create_date": inferred,
        "inferred_share_pct": round(100.0 * inferred / total, 1) if total else 0.0,
    }
    return durations, meta


def measure_remaining_cycle(
    closed_won: Sequence[Dict[str, Any]], stage_events: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, List[float]]:
    """For each stage, how many days it historically took to get from there to closed-won."""
    remaining: Dict[str, List[float]] = {}
    for deal in closed_won:
        close = deal.get("close_date")
        if close is None:
            continue
        for event in collapse_runs(stage_events.get(deal["id"], [])):
            days = (close - event["at"]).total_seconds() / 86400.0
            if days >= 0:
                remaining.setdefault(event["stage"], []).append(days)
    return remaining


# ------------------------------------------------------------------------- date logic


def last_day_of_month(when: datetime) -> datetime:
    first_next = (when.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_next - timedelta(days=1)


def fiscal_quarter_bounds(as_of: datetime, fy_start_month: int) -> Tuple[datetime, datetime]:
    month, year = as_of.month, as_of.year
    q_index = ((month - fy_start_month) % 12) // 3
    start_year = year if month >= fy_start_month else year - 1
    shifted = (fy_start_month - 1) + q_index * 3
    start = datetime(start_year + shifted // 12, shifted % 12 + 1, 1, tzinfo=timezone.utc)
    shifted_end = shifted + 3
    end = datetime(start_year + shifted_end // 12, shifted_end % 12 + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return start, end


def is_fiscal_quarter_end_month(month: int, fy_start_month: int) -> bool:
    return ((month - fy_start_month) % 12) % 3 == 2


# ------------------------------------------------------------------------ presentation


def money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return "${:,.0f}".format(value)


def ymd(when: Optional[datetime]) -> str:
    return when.strftime("%Y-%m-%d") if when else "—"


def band_for(amount: Optional[float], bands: Sequence[Dict[str, Any]]) -> str:
    ordered = sorted(bands, key=lambda b: to_number(b.get("min_amount")) or 0.0)
    label = ordered[0]["label"] if ordered else "unknown"
    for band in ordered:
        if amount is not None and amount >= (to_number(band.get("min_amount")) or 0.0):
            label = band["label"]
    return label if amount is not None else "no amount"


def required_contacts(amount: Optional[float], rules: Sequence[Dict[str, Any]]) -> int:
    ordered = sorted(rules, key=lambda r: to_number(r.get("min_amount")) or 0.0)
    needed = 1
    for rule in ordered:
        floor = to_number(rule.get("min_amount")) or 0.0
        if (amount or 0.0) >= floor:
            needed = int(to_number(rule.get("min_contacts")) or 1)
    return needed


# ------------------------------------------------------------------- verification SQL

VERIFY: Dict[str, Dict[str, str]] = {
    "past-due-open-deals": {
        "salesforce": (
            "SELECT Id, Name, Account.Name, Owner.Name, Amount, StageName, CloseDate\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false AND CloseDate < TODAY\n"
            "ORDER BY Amount DESC NULLS LAST"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            "{\n"
            '  "filterGroups": [{"filters": [\n'
            '    {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"},\n'
            '    {"propertyName": "closedate", "operator": "LT", "value": "<today as epoch ms>"}\n'
            "  ]}],\n"
            '  "properties": ["dealname","amount","dealstage","closedate","hubspot_owner_id"],\n'
            '  "sorts": [{"propertyName":"amount","direction":"DESCENDING"}], "limit": 100\n'
            "}"
        ),
    },
    "close-date-serial-pushes": {
        "salesforce": (
            "SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate\n"
            "FROM OpportunityFieldHistory\n"
            "WHERE Field = 'CloseDate' AND CreatedDate = LAST_N_MONTHS:{lookback}\n"
            "  AND OpportunityId IN ({ids})\n"
            "ORDER BY OpportunityId, CreatedDate\n"
            "-- count the rows per OpportunityId where NewValue > OldValue"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/batch/read\n"
            "{\n"
            '  "propertiesWithHistory": ["closedate"],\n'
            '  "properties": ["dealname","amount","dealstage"],\n'
            '  "inputs": [{"id":"<dealId>"}]\n'
            "}\n"
            "-- count versions of closedate where the value moved later in time"
        ),
    },
    "stage-stagnation": {
        "salesforce": (
            "SELECT Id, Name, Owner.Name, Amount, StageName, LastStageChangeDate, CloseDate\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false AND StageName = '{stage}'\n"
            "  AND LastStageChangeDate < LAST_N_DAYS:{days}\n"
            "ORDER BY LastStageChangeDate ASC"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            "{\n"
            '  "filterGroups": [{"filters": [\n'
            '    {"propertyName": "dealstage", "operator": "EQ", "value": "{stage}"},\n'
            '    {"propertyName": "hs_v2_date_entered_current_stage", "operator": "LT",\n'
            '     "value": "<now minus {days} days, epoch ms>"}\n'
            "  ]}],\n"
            '  "properties": ["dealname","amount","dealstage","closedate","hs_v2_latest_time_in_current_stage"]\n'
            "}"
        ),
    },
    "no-next-step": {
        "salesforce": (
            "SELECT Id, Name, Owner.Name, Amount, StageName, NextStep, CloseDate\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false AND (NextStep = null OR NextStep = '')\n"
            "ORDER BY Amount DESC NULLS LAST"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            "{\n"
            '  "filterGroups": [{"filters": [\n'
            '    {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"},\n'
            '    {"propertyName": "hs_next_step", "operator": "NOT_HAS_PROPERTY"}\n'
            "  ]}],\n"
            '  "properties": ["dealname","amount","dealstage","hs_next_step","closedate"]\n'
            "}"
        ),
    },
    "single-threaded-deals": {
        "salesforce": (
            "SELECT OpportunityId, COUNT(Id) contacts\n"
            "FROM OpportunityContactRole\n"
            "WHERE Opportunity.IsClosed = false\n"
            "GROUP BY OpportunityId\n"
            "HAVING COUNT(Id) < {min_contacts}"
        ),
        "hubspot": (
            "POST /crm/v4/associations/deals/contacts/batch/read\n"
            '{ "inputs": [{"id": "<dealId>"}] }\n'
            "-- count results[].to[] per deal"
        ),
    },
    "activity-silence": {
        "salesforce": (
            "SELECT Id, Name, Owner.Name, Amount, StageName, LastActivityDate, CloseDate\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false\n"
            "  AND (LastActivityDate < LAST_N_DAYS:{days} OR LastActivityDate = null)\n"
            "ORDER BY Amount DESC NULLS LAST"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            "{\n"
            '  "filterGroups": [{"filters": [\n'
            '    {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"},\n'
            '    {"propertyName": "notes_last_updated", "operator": "LT",\n'
            '     "value": "<now minus {days} days, epoch ms>"}\n'
            "  ]}],\n"
            '  "properties": ["dealname","amount","dealstage","notes_last_updated","closedate"]\n'
            "}"
        ),
    },
    "post-commit-amount-change": {
        "salesforce": (
            "SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate\n"
            "FROM OpportunityFieldHistory\n"
            "WHERE Field = 'Amount' AND OpportunityId IN ({ids})\n"
            "ORDER BY OpportunityId, CreatedDate"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/batch/read\n"
            '{ "propertiesWithHistory": ["amount","dealstage"], "inputs": [{"id":"<dealId>"}] }'
        ),
    },
    "stage-regression": {
        "salesforce": (
            "SELECT OpportunityId, CreatedDate, StageName\n"
            "FROM OpportunityHistory\n"
            "WHERE OpportunityId IN ({ids})\n"
            "ORDER BY OpportunityId, CreatedDate\n"
            "-- compare against SELECT ApiName, SortOrder FROM OpportunityStage ORDER BY SortOrder"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/batch/read\n"
            '{ "propertiesWithHistory": ["dealstage"], "inputs": [{"id":"<dealId>"}] }\n'
            "-- compare against GET /crm/v3/pipelines/deals (stages[].displayOrder)"
        ),
    },
    "quarter-end-clustering": {
        "salesforce": (
            "SELECT CloseDate, COUNT(Id) deals, SUM(Amount) amount\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false\n"
            "GROUP BY CloseDate\n"
            "ORDER BY COUNT(Id) DESC"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            '{ "filterGroups": [{"filters":[{"propertyName":"hs_is_closed","operator":"EQ","value":"false"}]}],\n'
            '  "properties": ["dealname","amount","closedate"], "limit": 100 }\n'
            "-- bucket closedate by day-of-month"
        ),
    },
    "missing-or-zero-amount": {
        "salesforce": (
            "SELECT Id, Name, Owner.Name, StageName, Amount, CloseDate\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false AND (Amount = null OR Amount = 0)\n"
            "ORDER BY CreatedDate DESC"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            "{\n"
            '  "filterGroups": [\n'
            '    {"filters": [{"propertyName":"hs_is_closed","operator":"EQ","value":"false"},\n'
            '                 {"propertyName":"amount","operator":"NOT_HAS_PROPERTY"}]},\n'
            '    {"filters": [{"propertyName":"hs_is_closed","operator":"EQ","value":"false"},\n'
            '                 {"propertyName":"amount","operator":"EQ","value":"0"}]}\n'
            "  ],\n"
            '  "properties": ["dealname","amount","dealstage","closedate"]\n'
            "}"
        ),
    },
    "same-day-create-and-close": {
        "salesforce": (
            "SELECT Id, Name, Owner.Name, Amount, StageName, CreatedDate, CloseDate\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = true AND CloseDate = LAST_N_MONTHS:{lookback}\n"
            "ORDER BY CreatedDate DESC\n"
            "-- keep rows where CloseDate - DAY_ONLY(CreatedDate) <= {days}"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            '{ "filterGroups": [{"filters":[{"propertyName":"hs_is_closed","operator":"EQ","value":"true"}]}],\n'
            '  "properties": ["dealname","createdate","closedate","amount","dealstage"] }\n'
            "-- keep rows where closedate - createdate <= {days} days"
        ),
    },
    "close-date-faster-than-history": {
        "salesforce": (
            "SELECT Id, Name, Owner.Name, Amount, StageName, LastStageChangeDate, CloseDate\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false AND StageName = '{stage}' AND CloseDate < NEXT_N_DAYS:{days}\n"
            "ORDER BY Amount DESC NULLS LAST"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            '{ "filterGroups": [{"filters":[\n'
            '    {"propertyName":"dealstage","operator":"EQ","value":"{stage}"},\n'
            '    {"propertyName":"closedate","operator":"LT","value":"<now plus {days} days, epoch ms>"}]}],\n'
            '  "properties": ["dealname","amount","dealstage","closedate"] }'
        ),
    },
    "possible-double-counted-pipeline": {
        "salesforce": (
            "SELECT AccountId, Account.Name, COUNT(Id) open_deals, SUM(Amount) amount\n"
            "FROM Opportunity\n"
            "WHERE IsClosed = false\n"
            "GROUP BY AccountId, Account.Name\n"
            "HAVING COUNT(Id) > 1\n"
            "ORDER BY SUM(Amount) DESC"
        ),
        "hubspot": (
            "POST /crm/v3/objects/deals/search\n"
            '{ "filterGroups": [{"filters":[{"propertyName":"hs_is_closed","operator":"EQ","value":"false"}]}],\n'
            '  "properties": ["dealname","amount","closedate","associatedcompanyid"] }\n'
            "-- group by associatedcompanyid, keep companies with >1 open deal at the same amount"
        ),
    },
}

VERIFY["stage-stagnation-severe"] = VERIFY["stage-stagnation"]
VERIFY["close-date-pushes-emerging"] = VERIFY["close-date-serial-pushes"]
VERIFY["no-contact-roles"] = VERIFY["single-threaded-deals"]
VERIFY["stale-next-step"] = VERIFY["no-next-step"]


def verify_query(rule: str, crm: str, **kwargs: Any) -> str:
    template = (VERIFY.get(rule) or {}).get(crm, "")
    if not template:
        return ""
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


# ------------------------------------------------------------------------- the engine


class Inspection:
    def __init__(self, cfg: Dict[str, Any], profile: Dict[str, Any], as_of: datetime, crm: str):
        self.cfg = cfg
        self.profile = profile
        self.as_of = as_of
        self.crm = crm
        self.flags: Dict[str, List[Dict[str, Any]]] = {}
        self.notes: List[str] = []
        self.unavailable: List[str] = []

    # -- helpers ---------------------------------------------------------------
    def flag(self, deal: Dict[str, Any], rule: str, severity: str, detail: str) -> None:
        self.flags.setdefault(deal["id"], []).append(
            {"rule": rule, "severity": severity, "detail": detail, "weight": RULE_WEIGHT.get(severity, 1)}
        )

    def label(self, stage: str) -> str:
        return self.stage_model["labels"].get(stage, stage) if getattr(self, "stage_model", None) else stage


def build(
    raw_dir: Path,
    cfg: Dict[str, Any],
    profile: Dict[str, Any],
    as_of: datetime,
    manifest: RunManifest,
) -> FindingsDoc:
    meta = read_meta(raw_dir)

    open_raw = read_raw(raw_dir, "open_opportunities")
    closed_raw = read_raw(raw_dir, "closed_opportunities")
    crm = cfg.get("crm") or detect_crm(meta, profile, open_raw)

    base_map = dict(SF_FIELD_MAP if crm == "salesforce" else HS_FIELD_MAP)
    base_map.update({k: v for k, v in (cfg.get("field_map") or {}).items() if v})
    if cfg.get("next_step_field"):
        base_map["next_step"] = cfg["next_step_field"]

    stage_raw = read_raw(raw_dir, "stage_history")
    field_raw = read_raw(raw_dir, "field_history")
    roles_raw = read_raw(raw_dir, "contact_roles")
    activity_raw = read_raw(raw_dir, "activities")
    tasks_raw = read_raw(raw_dir, "open_tasks")
    stagemeta_raw = read_raw(raw_dir, "stage_metadata")
    quota_raw = read_raw(raw_dir, "quota")

    # ---- provenance: named by the skill in meta.json, counted here.
    declared = {s.get("name"): s for s in (meta.get("sources") or []) if isinstance(s, dict)}
    lookback = int(cfg.get("history_lookback_months") or 24)

    def record_source(name: str, rows: Sequence[Any], required: bool, diagnosis: str) -> None:
        info = declared.get(name, {})
        manifest.record(
            name,
            tool=info.get("tool") or "unspecified (meta.json did not name the tool)",
            query=info.get("query", ""),
            count=len(rows),
            required=required,
            note=info.get("note", ""),
            diagnosis=diagnosis,
        )

    record_source(
        "open_opportunities",
        open_raw,
        True,
        "the connected CRM identity may lack read access to Opportunity/Deal, the filter may "
        "have excluded every record, or the query hit a permission-scoped sharing rule. A real "
        "pipeline with zero open deals is not a hygiene finding — it is a broken connection.",
    )
    record_source(
        "closed_opportunities",
        closed_raw,
        bool(cfg.get("require_closed_history", True)),
        f"no closed deals came back for the last {lookback} months. Without closed history the "
        "stage medians cannot be measured and every threshold falls back to a guess. If this org "
        "genuinely has no closed deals yet, set require_closed_history=false in "
        "~/.leanscale-gtm/pipeline-inspection.json and re-run.",
    )
    record_source("stage_history", stage_raw, False, "")
    record_source("field_history", field_raw, False, "")
    record_source("contact_roles", roles_raw, False, "")
    record_source("activities", activity_raw, False, "")
    record_source("open_tasks", tasks_raw, False, "")
    record_source("stage_metadata", stagemeta_raw, False, "")
    record_source("quota", quota_raw, False, "")

    manifest.finalize()  # aborts loudly if a required source came back empty

    # ---- normalize -----------------------------------------------------------
    stage_model = build_stage_model(stagemeta_raw, crm, cfg)
    open_deals = normalize_deals(open_raw, base_map, crm, as_of)
    closed_deals = normalize_deals(closed_raw, base_map, crm, as_of)

    floor = cfg.get("material_deal_floor")
    if floor is None:
        floor = profile.get("material_deal_floor") or 0
    floor = float(floor or 0)
    below_floor = [d for d in open_deals if (d["amount"] or 0) < floor and d["amount"] is not None and floor > 0]
    scoped = [d for d in open_deals if d not in below_floor]

    owner_scope = [str(o).lower() for o in (cfg.get("owner_scope") or [])]
    if owner_scope:
        scoped = [d for d in scoped if str(d.get("owner", "")).lower() in owner_scope]

    stage_events, close_changes, amount_changes, history_coverage = load_change_events(
        field_raw, stage_raw, base_map
    )

    ins = Inspection(cfg, profile, as_of, crm)
    ins.stage_model = stage_model

    # ---- measured medians ----------------------------------------------------
    durations, duration_meta = measure_stage_durations(closed_deals, stage_events, crm)
    min_n = int(cfg.get("min_closed_deals_for_median") or 8)
    multiple = float(cfg.get("stagnation_multiple") or 2.0)
    severe_multiple = float(cfg.get("severe_stagnation_multiple") or 4.0)
    basis_mode = cfg.get("stagnation_basis") or "measured_median"
    expected = {str(k): to_number(v) for k, v in (cfg.get("expected_days_in_stage") or {}).items()}

    all_measured = [v for values in durations.values() for v in values]
    global_median = median(all_measured)

    stage_stats: Dict[str, Dict[str, Any]] = {}
    for stage in sorted(set(list(durations.keys()) + [d["stage"] for d in scoped] + list(expected.keys()))):
        values = durations.get(stage, [])
        measured = median(values) if len(values) >= min_n else None
        exp = expected.get(stage)
        if basis_mode == "expected" and exp:
            basis, basis_label = exp, "expected (you set this in setup)"
        elif basis_mode == "max_of_both" and (measured or exp):
            basis = max([v for v in (measured, exp) if v is not None])
            basis_label = "max of measured median and your expected days"
        elif measured is not None:
            basis, basis_label = measured, "measured median from your own closed deals"
        elif exp:
            basis, basis_label = exp, "expected (you set this in setup — too few closed deals to measure)"
        elif global_median:
            basis, basis_label = global_median, "all-stage measured median (this stage had too few samples)"
        else:
            basis, basis_label = float(cfg.get("fallback_days_in_stage") or 60), "fallback default — no history at all"
        stage_stats[stage] = {
            "stage": stage,
            "label": stage_model["labels"].get(stage, stage),
            "closed_samples": len(values),
            "median_days": round(measured, 1) if measured is not None else None,
            "p75_days": round(percentile(values, 75), 1) if len(values) >= min_n else None,
            "p90_days": round(percentile(values, 90), 1) if len(values) >= min_n else None,
            "expected_days": exp,
            "basis_days": round(basis, 1),
            "basis_label": basis_label,
            "threshold_days": round(basis * multiple, 1),
            "severe_threshold_days": round(basis * severe_multiple, 1),
        }

    remaining_cycle = measure_remaining_cycle([d for d in closed_deals if d["is_won"]], stage_events)
    remaining_median = {
        stage: round(median(values), 1)
        for stage, values in remaining_cycle.items()
        if len(values) >= min_n and median(values) is not None
    }

    # ---- per-deal derived facts ---------------------------------------------
    contacts_by_deal = count_contacts(roles_raw, crm)
    activity_by_deal = last_activity_from_records(activity_raw, crm)
    open_task_by_deal = open_tasks_by_deal(tasks_raw, crm, as_of)

    for deal in scoped:
        deal["days_in_stage"], deal["stage_age_confident"] = days_in_stage(deal, stage_events, as_of)
        deal["contacts"] = contacts_by_deal.get(deal["id"])
        if activity_by_deal.get(deal["id"]) and (
            deal["last_activity"] is None or activity_by_deal[deal["id"]] > deal["last_activity"]
        ):
            deal["last_activity"] = activity_by_deal[deal["id"]]
        deal["open_tasks"] = open_task_by_deal.get(deal["id"], [])
        deal["pushes"] = [c for c in close_changes.get(deal["id"], []) if c["new"] > c["old"]]
        deal["pull_ins"] = [c for c in close_changes.get(deal["id"], []) if c["new"] < c["old"]]
        deal["days_pushed"] = sum(int((c["new"] - c["old"]).days) for c in deal["pushes"])
        deal["amount_changes"] = amount_changes.get(deal["id"], [])
        deal["stage_path"] = collapse_runs(stage_events.get(deal["id"], []))
        deal["band"] = band_for(deal["amount"], cfg.get("deal_size_bands") or [])

    total_open_amount = sum(d["amount"] or 0 for d in scoped)
    doc = FindingsDoc(
        plugin=PLUGIN,
        window={
            "start": (as_of - timedelta(days=30 * lookback)).strftime("%Y-%m-%d"),
            "end": as_of.strftime("%Y-%m-%d"),
        },
        org_name=profile.get("org_name", "") or meta.get("org_name", ""),
    )

    # ---- what we could not check --------------------------------------------
    history_available = bool(close_changes) or bool(field_raw) or bool(stage_raw)
    if not history_available:
        doc.unavailable.append(
            "Close-date push history — no field-history rows came back. In Salesforce this "
            "means field history tracking is off for CloseDate, or the changes predate the "
            "18-month retention window (24 months with Field Audit Trail). In HubSpot it means "
            "the batch read was not run with propertiesWithHistory."
        )
        doc.unavailable.append("Stage regressions and post-commit amount changes (same missing history).")
    if not roles_raw:
        doc.unavailable.append(
            "Single-threading — no contact roles came back. Salesforce: OpportunityContactRole "
            "may be empty or unreadable by this identity. HubSpot: the deal-to-contact "
            "association batch read was not run."
        )
    if (cfg.get("next_step_mode") or "field") == "none":
        doc.unavailable.append(
            "Next step — you told setup that next steps do not live anywhere in the CRM, so "
            "this check is switched off. It is the cheapest signal in the plugin; consider "
            "turning on the NextStep field or a due-dated task convention."
        )
    if not durations:
        doc.unavailable.append(
            "Measured stage medians — no stage transitions were readable, so stagnation "
            "thresholds fell back to your configured expectations or a generic default."
        )

    # ---- rules ---------------------------------------------------------------
    ctx = {
        "cfg": cfg,
        "crm": crm,
        "as_of": as_of,
        "open": scoped,
        "closed": closed_deals,
        "stage_stats": stage_stats,
        "stage_model": stage_model,
        "remaining_median": remaining_median,
        "total_open_amount": total_open_amount,
        "profile": profile,
        "history_available": history_available,
        "roles_available": bool(roles_raw),
        "lookback": lookback,
    }

    for rule in (
        rule_past_due,
        rule_serial_pushes,
        rule_emerging_pushes,
        rule_stagnation,
        rule_no_contact_roles,
        rule_single_threaded,
        rule_next_step,
        rule_stale_next_step,
        rule_activity_silence,
        rule_post_commit_amount_change,
        rule_stage_regression,
        rule_close_date_realism,
        rule_missing_amount,
        rule_quarter_end_clustering,
        rule_same_day_close,
        rule_double_counted,
    ):
        for finding in rule(ins, ctx) or []:
            doc.add(finding)

    # ---- scores --------------------------------------------------------------
    # The same-day-close rule flags CLOSED records; they must never count towards the
    # open-pipeline totals, or the "x of y open deals" line is quietly wrong.
    open_ids = {d["id"] for d in scoped}
    flagged_ids = set(ins.flags.keys()) & open_ids
    flagged_amount = sum(d["amount"] or 0 for d in scoped if d["id"] in flagged_ids)
    at_risk_ids = {
        deal_id
        for deal_id, flags in ins.flags.items()
        if deal_id in open_ids and any(f["severity"] in ("critical", "high") for f in flags)
    }
    at_risk_amount = sum(d["amount"] or 0 for d in scoped if d["id"] in at_risk_ids)
    past_due_amount = sum(
        d["amount"] or 0 for d in scoped if d["close_date"] and d["close_date"] < as_of
    )

    clean_amount = total_open_amount - flagged_amount
    inspection_score = round(100.0 * clean_amount / total_open_amount, 1) if total_open_amount else 100.0

    doc.add_score(
        Score(
            key="inspection_score",
            label="Inspection Score",
            value=inspection_score,
            unit="score_0_100",
            direction_good="up",
            context=f"Share of open pipeline dollars breaking none of your {len(doc.findings)} active rules.",
        )
    )
    doc.add_score(
        Score(
            key="at_risk_amount",
            label="At-Risk Pipeline",
            value=round(at_risk_amount),
            unit="currency",
            direction_good="down",
            context=f"{len(at_risk_ids):,} deals breaking at least one critical or high rule.",
        )
    )
    doc.add_score(
        Score(
            key="flagged_pct",
            label="Pipeline Flagged",
            value=round(100.0 * flagged_amount / total_open_amount, 1) if total_open_amount else 0.0,
            unit="percent",
            direction_good="down",
            context=f"{len(flagged_ids):,} of {len(scoped):,} open deals broke at least one rule.",
        )
    )
    doc.add_score(
        Score(
            key="past_due_amount",
            label="Past-Due Pipeline",
            value=round(past_due_amount),
            unit="currency",
            direction_good="down",
            context="Open deals whose close date has already been and gone.",
        )
    )

    coverage = compute_coverage(quota_raw, scoped, profile, as_of)
    if coverage.get("coverage_ratio") is not None:
        doc.add_score(
            Score(
                key="coverage_ratio",
                label="Coverage Ratio",
                value=coverage["coverage_ratio"],
                unit="ratio",
                direction_good="up",
                context=(
                    f"{money(coverage['pipeline_in_period'])} of pipeline closing in "
                    f"{coverage['period_label']} against {money(coverage['quota'])} of quota."
                ),
            )
        )

    # ---- sections ------------------------------------------------------------
    doc.sections = build_sections(ins, ctx, {
        "stage_stats": stage_stats,
        "duration_meta": duration_meta,
        "history_coverage": history_coverage,
        "coverage": coverage,
        "below_floor": below_floor,
        "floor": floor,
        "remaining_median": remaining_median,
        "flagged_ids": flagged_ids,
        "at_risk_ids": at_risk_ids,
    })

    for note in ins.notes:
        manifest.warn(note)
    manifest.write()
    return doc


# --------------------------------------------------------------------- derived facts


def days_in_stage(
    deal: Dict[str, Any], stage_events: Dict[str, List[Dict[str, Any]]], as_of: datetime
) -> Tuple[Optional[int], bool]:
    """
    Days in the CURRENT stage, and whether we actually know it.

    "Confident" means a real stage-change signal exists. When all we have is the
    create date, the number is only trustworthy for deals that have never moved
    at all — so it is reported but not used to flag stagnation.
    """
    if deal.get("stage_entry"):
        return max(0, (as_of - deal["stage_entry"]).days), True
    events = collapse_runs(stage_events.get(deal["id"], []))
    for event in reversed(events):
        if event["stage"] == deal["stage"]:
            return max(0, (as_of - event["at"]).days), True
    if deal.get("created"):
        never_moved = len(events) <= 1
        return max(0, (as_of - deal["created"]).days), never_moved
    return None, False


def count_contacts(records: Sequence[Dict[str, Any]], crm: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        # HubSpot v4 association batch read: {"from": {"id": "..."}, "to": [{...}, ...]}
        if "from" in record and isinstance(record.get("from"), dict):
            deal_id = str(record["from"].get("id") or "")
            if deal_id:
                counts[deal_id] = counts.get(deal_id, 0) + len(record.get("to") or [])
            continue
        # HubSpot object payload carrying associations inline
        assoc = record.get("associations") or {}
        if isinstance(assoc, dict) and ("contacts" in assoc):
            deal_id = str(record.get("id") or record.get("Id") or "")
            results = (assoc.get("contacts") or {}).get("results") or []
            if deal_id:
                counts[deal_id] = counts.get(deal_id, 0) + len(results)
            continue
        flat = normalize_records([record])[0]
        deal_id = str(_get(flat, "OpportunityId") or _get(flat, "dealId") or _get(flat, "deal_id") or "")
        if not deal_id:
            continue
        explicit = to_number(_get(flat, "contacts") or _get(flat, "contact_count"))
        counts[deal_id] = counts.get(deal_id, 0) + (int(explicit) if explicit is not None else 1)
    del crm
    return counts


def last_activity_from_records(records: Sequence[Dict[str, Any]], crm: str) -> Dict[str, datetime]:
    latest: Dict[str, datetime] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        flat = normalize_records([record])[0]
        deal_id = str(
            _get(flat, "WhatId")
            or _get(flat, "OpportunityId")
            or _get(flat, "dealId")
            or _get(flat, "deal_id")
            or ""
        )
        when = parse_dt(
            _get(flat, "ActivityDate")
            or _get(flat, "ActivityDateTime")
            or _get(flat, "hs_timestamp")
            or _get(flat, "hs_lastmodifieddate")
            or _get(flat, "CreatedDate")
            or _get(flat, "createdate")
        )
        if deal_id and when and (deal_id not in latest or when > latest[deal_id]):
            latest[deal_id] = when
    del crm
    return latest


def open_tasks_by_deal(records: Sequence[Dict[str, Any]], crm: str, as_of: datetime) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        flat = normalize_records([record])[0]
        deal_id = str(
            _get(flat, "WhatId") or _get(flat, "OpportunityId") or _get(flat, "dealId") or _get(flat, "deal_id") or ""
        )
        if not deal_id:
            continue
        status = str(_get(flat, "Status") or _get(flat, "hs_task_status") or "").lower()
        closed = _truthy(_get(flat, "IsClosed")) or status in ("completed", "deferred")
        due = parse_dt(_get(flat, "ActivityDate") or _get(flat, "hs_timestamp") or _get(flat, "hs_task_due_date"))
        out.setdefault(deal_id, []).append(
            {
                "subject": _get(flat, "Subject") or _get(flat, "hs_task_subject") or "(no subject)",
                "due": due,
                "closed": closed,
                "overdue": bool(due and due < as_of and not closed),
            }
        )
    del crm
    return out


def compute_coverage(
    quota_raw: Sequence[Dict[str, Any]], deals: Sequence[Dict[str, Any]], profile: Dict[str, Any], as_of: datetime
) -> Dict[str, Any]:
    fy_start = int(profile.get("fiscal_year_start_month") or 1)
    start, end = fiscal_quarter_bounds(as_of, fy_start)
    label = fiscal_period(profile, as_of.year, as_of.month)
    in_period = [d for d in deals if d["close_date"] and start <= d["close_date"] <= end]
    pipeline = sum(d["amount"] or 0 for d in in_period)
    quota_total = 0.0
    for record in quota_raw or []:
        if not isinstance(record, dict):
            continue
        value = to_number(record.get("quota") or record.get("Quota") or record.get("amount"))
        period = str(record.get("period") or record.get("Period") or "")
        if value and (not period or period in (label, f"{as_of.year}-Q{(as_of.month - 1) // 3 + 1}")):
            quota_total += value
    result = {
        "period_label": label,
        "period_start": start.strftime("%Y-%m-%d"),
        "period_end": end.strftime("%Y-%m-%d"),
        "pipeline_in_period": round(pipeline),
        "deals_in_period": len(in_period),
        "quota": round(quota_total) if quota_total else None,
        "coverage_ratio": round(pipeline / quota_total, 2) if quota_total else None,
    }
    return result


# ---------------------------------------------------------------------------- rules


def _emit(
    ins: Inspection,
    ctx: Dict[str, Any],
    *,
    rule: str,
    severity: str,
    title: str,
    what: str,
    why: str,
    fix: str,
    deals: Sequence[Dict[str, Any]],
    rows: Sequence[Dict[str, Any]],
    query: str = "",
    effort: str = "quick",
    owner_hint: str = "Sales management",
    extra_evidence: Optional[Dict[str, Any]] = None,
) -> List[Finding]:
    if not deals and not rows:
        return []
    amount = sum(d.get("amount") or 0 for d in deals)
    share = 100.0 * amount / ctx["total_open_amount"] if ctx["total_open_amount"] else 0.0
    # Materiality can promote a finding one level, but never INTO critical. Critical means
    # what the spec says it means — revenue leaking now — and is set by the rule itself.
    # Without this ceiling a concentrated pipeline turns every finding red and the severity
    # column stops carrying information.
    ceiling = SEV_ORDER.index("high")
    if share >= float(ctx["cfg"].get("escalate_amount_share_pct") or 20.0) and SEV_ORDER.index(severity) < ceiling:
        severity = escalate(severity)
    for deal in deals:
        ins.flag(deal, rule, severity, title)
    limit = int(ctx["cfg"].get("max_rows_per_finding") or 50)
    evidence: Dict[str, Any] = {
        "count": len(deals) if deals else len(rows),
        "amount_at_risk": round(amount),
        "share_of_open_pipeline_pct": round(share, 1),
        "sample_ids": [d["id"] for d in deals[:10]],
        "rows": list(rows)[:limit],
        "rows_shown": min(len(rows), limit),
        "rows_total": len(rows),
    }
    if query:
        evidence["query"] = query
    if extra_evidence:
        evidence.update(extra_evidence)
    return [
        Finding(
            id=rule,
            severity=severity,
            title=title,
            what=what,
            why_it_matters=why,
            recommended_fix=fix,
            evidence=evidence,
            effort=effort,
            owner_hint=owner_hint,
        )
    ]


def _base_row(deal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "Deal": deal["name"],
        "Account": deal["account"],
        "Owner": deal["owner"],
        "Amount": money(deal["amount"]),
        "Stage": deal["stage"],
    }


def rule_past_due(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    as_of = ctx["as_of"]
    hits = sorted(
        [d for d in ctx["open"] if d["close_date"] and d["close_date"] < as_of],
        key=lambda d: (d["amount"] or 0),
        reverse=True,
    )
    rows = [
        dict(_base_row(d), **{
            "Close date": ymd(d["close_date"]),
            "Days past due": (as_of - d["close_date"]).days,
            "Rule broken": "Close date is in the past and the deal is still open",
        })
        for d in hits
    ]
    total = sum(d["amount"] or 0 for d in hits)
    return _emit(
        ins,
        ctx,
        rule="past-due-open-deals",
        severity="critical",
        title=f"{len(hits)} open deals worth {money(total)} have a close date in the past",
        what=(
            f"{len(hits)} deals are still open with a close date before {as_of.strftime('%Y-%m-%d')}. "
            f"The oldest is {max((as_of - d['close_date']).days for d in hits) if hits else 0} days past due."
        ),
        why=(
            "Every roll-up, coverage ratio and quarter-to-date number silently includes these. A deal "
            "with a close date in the past is not a forecast — it is an unanswered question, and it is "
            "the single fastest thing to clean up before a pipeline review."
        ),
        fix=(
            "Work the list top-down by amount. Each deal gets one of three outcomes in the same "
            "session: a new close date the rep will defend, a stage change, or closed-lost. Do not "
            "let a rep leave the meeting with a past-due date still on the board."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("past-due-open-deals", ctx["crm"]),
        effort="quick",
    )


def rule_serial_pushes(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    if not ctx["history_available"]:
        return []
    threshold = int(ctx["cfg"].get("push_threshold") or 3)
    hits = sorted(
        [d for d in ctx["open"] if len(d["pushes"]) >= threshold],
        key=lambda d: (len(d["pushes"]), d["amount"] or 0),
        reverse=True,
    )
    rows = [
        dict(_base_row(d), **{
            "Pushes": len(d["pushes"]),
            "Days pushed": d["days_pushed"],
            "First date": ymd(d["pushes"][0]["old"]) if d["pushes"] else "—",
            "Current date": ymd(d["close_date"]),
            "Rule broken": f"Close date moved later {len(d['pushes'])}x (limit {threshold})",
        })
        for d in hits
    ]
    ids = ", ".join(f"'{d['id']}'" for d in hits[:20]) or "'<opportunity ids>'"
    total = sum(d["amount"] or 0 for d in hits)
    max_pushes = max((len(d["pushes"]) for d in hits), default=0)
    return _emit(
        ins,
        ctx,
        rule="close-date-serial-pushes",
        severity="critical",
        title=f"{len(hits)} deals have pushed their close date {threshold}+ times ({money(total)})",
        what=(
            f"{len(hits)} open deals moved their close date later at least {threshold} times. The worst "
            f"has pushed {max_pushes} times, and the group has slipped "
            f"{sum(d['days_pushed'] for d in hits):,} days in total."
        ),
        why=(
            "A deal that has pushed three times is a categorically different animal from one that has "
            "pushed zero times — the push count is the highest-signal, lowest-effort predictor in your "
            "CRM, and it is the one number nobody looks at. Serial pushers are where forecast credibility "
            "goes to die: each push is individually defensible and collectively fatal."
        ),
        fix=(
            "Put every deal on this list through a different conversation than a normal pipeline review. "
            "Do not ask 'when will it close' — ask what specifically changed since the last date, who "
            "moved it, and what has to be true for this date to hold. Deals that push a fourth time "
            "should leave the committed forecast entirely until a buyer-verifiable event moves them back."
        ),
        deals=hits,
        rows=rows,
        query=verify_query(
            "close-date-serial-pushes", ctx["crm"], lookback=ctx["lookback"], ids=ids
        ),
        effort="medium",
        owner_hint="Sales leadership",
    )


def rule_emerging_pushes(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    if not ctx["history_available"]:
        return []
    threshold = int(ctx["cfg"].get("push_threshold") or 3)
    watch = int(ctx["cfg"].get("push_watch_threshold") or 1)
    hits = sorted(
        [d for d in ctx["open"] if watch <= len(d["pushes"]) < threshold],
        key=lambda d: (len(d["pushes"]), d["amount"] or 0),
        reverse=True,
    )
    rows = [
        dict(_base_row(d), **{
            "Pushes": len(d["pushes"]),
            "Days pushed": d["days_pushed"],
            "Current date": ymd(d["close_date"]),
            "Rule broken": f"Close date moved later {len(d['pushes'])}x (watch list, limit {threshold})",
        })
        for d in hits
    ]
    return _emit(
        ins,
        ctx,
        rule="close-date-pushes-emerging",
        severity="low",
        title=f"{len(hits)} deals are on their way to becoming serial pushers",
        what=(
            f"{len(hits)} open deals have pushed their close date between {watch} and {threshold - 1} "
            "times. They are not yet over your limit."
        ),
        why=(
            "The distribution matters more than the cutoff. These are the deals that become next "
            "quarter's serial pushers, and catching the second push is far cheaper than catching the fourth."
        ),
        fix=(
            "No action today. Watch them: if any of these pushes again this cycle, it graduates to the "
            "serial-pusher conversation rather than a routine date change."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("close-date-pushes-emerging", ctx["crm"], lookback=ctx["lookback"], ids="'<ids>'"),
        effort="quick",
    )


def _stagnation_rows(deals: Sequence[Dict[str, Any]], stats: Dict[str, Any], as_of: datetime) -> List[Dict[str, Any]]:
    rows = []
    for deal in deals:
        stat = stats.get(deal["stage"], {})
        rows.append(
            dict(_base_row(deal), **{
                "Days in stage": deal["days_in_stage"],
                "Your median": stat.get("median_days") if stat.get("median_days") is not None else "—",
                "Threshold": stat.get("threshold_days"),
                "Close date": ymd(deal["close_date"]),
                "Rule broken": (
                    f"{deal['days_in_stage']}d in {deal['stage']} vs a "
                    f"{stat.get('basis_days')}d basis ({stat.get('basis_label', '')})"
                ),
            })
        )
    del as_of
    return rows


def rule_stagnation(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    stats, out = ctx["stage_stats"], []
    severe, stalled = [], []
    for deal in ctx["open"]:
        if deal["days_in_stage"] is None or not deal["stage_age_confident"]:
            continue
        stat = stats.get(deal["stage"])
        if not stat:
            continue
        if deal["days_in_stage"] >= stat["severe_threshold_days"]:
            severe.append(deal)
        elif deal["days_in_stage"] >= stat["threshold_days"]:
            stalled.append(deal)

    severe.sort(key=lambda d: (d["days_in_stage"], d["amount"] or 0), reverse=True)
    stalled.sort(key=lambda d: (d["days_in_stage"], d["amount"] or 0), reverse=True)

    multiple = float(ctx["cfg"].get("stagnation_multiple") or 2.0)
    severe_multiple = float(ctx["cfg"].get("severe_stagnation_multiple") or 4.0)
    worst_stage = ""
    if severe or stalled:
        counts: Dict[str, int] = {}
        for deal in severe + stalled:
            counts[deal["stage"]] = counts.get(deal["stage"], 0) + 1
        worst_stage = max(counts, key=lambda k: counts[k])
        worst_days = stats.get(worst_stage, {}).get("threshold_days")
        query_stage, query_days = worst_stage, int(worst_days or 0)
    else:
        query_stage, query_days = "", 0

    out += _emit(
        ins,
        ctx,
        rule="stage-stagnation-severe",
        severity="critical",
        title=f"{len(severe)} deals have been frozen in stage for more than {severe_multiple:g}x your own median",
        what=(
            f"{len(severe)} open deals have sat in their current stage longer than "
            f"{severe_multiple:g} times the median time your own closed deals spent there."
        ),
        why=(
            "This is not a slow deal, it is an abandoned one that is still counted. At four times your "
            "measured median the historical odds of it closing in the current stage are close to zero, "
            "yet it inflates coverage, distorts every stage conversion rate you compute, and gives the "
            "rep somewhere to hide."
        ),
        fix=(
            "Close-lost or explicitly park them this week. If a deal is genuinely alive, it needs a new "
            "close date and a documented buyer-side event; if it is not, taking it out makes every other "
            "number in the pipeline honest."
        ),
        deals=severe,
        rows=_stagnation_rows(severe, stats, ctx["as_of"]),
        query=verify_query("stage-stagnation-severe", ctx["crm"], stage=query_stage, days=query_days),
        effort="quick",
    )
    out += _emit(
        ins,
        ctx,
        rule="stage-stagnation",
        severity="high",
        title=f"{len(stalled)} deals are stalled at more than {multiple:g}x your measured stage median",
        what=(
            f"{len(stalled)} open deals have been in their current stage longer than {multiple:g}x the "
            "median your own closed deals took to get through that stage. The medians are measured from "
            "your history, not a benchmark."
        ),
        why=(
            "Most teams have never seen their own stage medians, so 'this one is just taking a while' is "
            "unfalsifiable. Once the median exists, a deal at twice it is a specific, arguable claim a "
            "manager can put to a rep — and stage time is the earliest warning you get before a slip."
        ),
        fix=(
            "Work these in the weekly review. For each: what is the single buyer-side event that would "
            "move it to the next stage, and what date is that event on? No event, no date — no forecast."
        ),
        deals=stalled,
        rows=_stagnation_rows(stalled, stats, ctx["as_of"]),
        query=verify_query("stage-stagnation", ctx["crm"], stage=query_stage, days=query_days),
        effort="medium",
    )
    return out


def rule_no_contact_roles(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    if not ctx["roles_available"]:
        return []
    hits = sorted(
        [d for d in ctx["open"] if not d.get("contacts")],
        key=lambda d: (d["amount"] or 0),
        reverse=True,
    )
    rows = [
        dict(_base_row(d), **{
            "Contacts": 0,
            "Close date": ymd(d["close_date"]),
            "Rule broken": "No contact is attached to this deal at all",
        })
        for d in hits
    ]
    total = sum(d["amount"] or 0 for d in hits)
    return _emit(
        ins,
        ctx,
        rule="no-contact-roles",
        severity="critical",
        title=f"{len(hits)} open deals worth {money(total)} have zero contacts attached",
        what=f"{len(hits)} open deals have no contact associated with them in the CRM.",
        why=(
            "Nobody but the rep can touch these deals. If that rep leaves, goes on holiday, or gets "
            "reassigned, there is literally no person to call — the relationship exists only in their "
            "inbox. It also means marketing cannot nurture the account and legal cannot route paper."
        ),
        fix=(
            "Make at least one contact a hard requirement to advance past your first qualified stage. "
            "Backfill the current list from the rep's calendar and email in one sitting."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("no-contact-roles", ctx["crm"], min_contacts=1),
        effort="quick",
    )


def rule_single_threaded(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    if not ctx["roles_available"]:
        return []
    rules = ctx["cfg"].get("single_thread_thresholds") or []
    hits = []
    for deal in ctx["open"]:
        contacts = deal.get("contacts") or 0
        if contacts == 0:
            continue  # covered by the zero-contact rule; don't double-count
        needed = required_contacts(deal["amount"], rules)
        if contacts < needed:
            deal["_needed_contacts"] = needed
            hits.append(deal)
    hits.sort(key=lambda d: (d["amount"] or 0), reverse=True)
    rows = [
        dict(_base_row(d), **{
            "Band": d["band"],
            "Contacts": d.get("contacts"),
            "Required": d["_needed_contacts"],
            "Close date": ymd(d["close_date"]),
            "Rule broken": f"{d.get('contacts')} contact(s) on a {money(d['amount'])} deal, {d['_needed_contacts']} required",
        })
        for d in hits
    ]
    total = sum(d["amount"] or 0 for d in hits)
    big = [d for d in hits if (d["amount"] or 0) >= 100000]
    return _emit(
        ins,
        ctx,
        rule="single-threaded-deals",
        severity="high",
        title=f"{len(hits)} deals are single-threaded below your size-based bar ({money(total)})",
        what=(
            f"{len(hits)} open deals have fewer contacts than your threshold for their deal size"
            + (f", including {len(big)} above $100k." if big else ".")
        ),
        why=(
            "One contact on a six-figure deal is not a relationship, it is a single point of failure. "
            "Champions change jobs, get reorganised, and go quiet — and a single-threaded deal gives you "
            "no way to find out you have been dropped until the close date passes."
        ),
        fix=(
            "For each deal above your top band, name the second and third stakeholder and the specific "
            "reason each one cares. If the rep cannot name them, that is the next step — not a demo."
        ),
        deals=hits,
        rows=rows,
        query=verify_query(
            "single-threaded-deals",
            ctx["crm"],
            min_contacts=max((required_contacts(d["amount"], rules) for d in hits), default=2),
        ),
        effort="medium",
        owner_hint="Sales management",
    )


def rule_next_step(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    mode = ctx["cfg"].get("next_step_mode") or "field"
    if mode == "none":
        return []
    hits = []
    for deal in ctx["open"]:
        has_field = not is_blank(deal.get("next_step"))
        has_task = any(not t["closed"] for t in deal.get("open_tasks") or [])
        if mode == "field":
            missing = not has_field
        elif mode == "task":
            missing = not has_task
        else:  # both -> either satisfies
            missing = not (has_field or has_task)
        if missing:
            hits.append(deal)
    hits.sort(key=lambda d: (d["amount"] or 0), reverse=True)
    where = {
        "field": "the next-step field",
        "task": "an open task",
        "both": "either the next-step field or an open task",
    }[mode]
    rows = [
        dict(_base_row(d), **{
            "Days in stage": d["days_in_stage"],
            "Close date": ymd(d["close_date"]),
            "Rule broken": f"No next step recorded in {where}",
        })
        for d in hits
    ]
    total = sum(d["amount"] or 0 for d in hits)
    return _emit(
        ins,
        ctx,
        rule="no-next-step",
        severity="high",
        title=f"{len(hits)} open deals worth {money(total)} have no next step",
        what=f"{len(hits)} open deals carry no next step in {where}.",
        why=(
            "A deal with no scheduled next action is not being worked, whatever the stage says. This is "
            "the cheapest signal in the whole inspection and the one most likely to be ignored, because "
            "it looks like an admin problem rather than a pipeline problem. It is a pipeline problem: "
            "every one of these is a deal whose next movement depends on the buyer remembering."
        ),
        fix=(
            "Require a next step with a date to keep a deal in an active stage. Enforce it in the weekly "
            "review rather than with a validation rule first — reps will type 'follow up' into any field "
            "you make required, and a bad next step is worse than a blank one."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("no-next-step", ctx["crm"]),
        effort="quick",
    )


def rule_stale_next_step(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    mode = ctx["cfg"].get("next_step_mode") or "field"
    if mode == "none":
        return []
    days = int(ctx["cfg"].get("next_step_staleness_days") or 14)
    as_of = ctx["as_of"]
    hits = []
    for deal in ctx["open"]:
        overdue_task = any(t["overdue"] for t in deal.get("open_tasks") or [])
        stale_field = (
            not is_blank(deal.get("next_step"))
            and deal.get("last_modified") is not None
            and (as_of - deal["last_modified"]).days > days
        )
        if overdue_task or (mode in ("field", "both") and stale_field):
            deal["_stale_reason"] = (
                "next-step task is past its due date" if overdue_task else f"record untouched for {days}+ days"
            )
            hits.append(deal)
    hits.sort(key=lambda d: (d["amount"] or 0), reverse=True)
    rows = [
        dict(_base_row(d), **{
            "Next step": str(d.get("next_step") or "(task)")[:70],
            "Last touched": ymd(d.get("last_modified")),
            "Close date": ymd(d["close_date"]),
            "Rule broken": f"Next step is stale — {d['_stale_reason']}",
        })
        for d in hits
    ]
    return _emit(
        ins,
        ctx,
        rule="stale-next-step",
        severity="medium",
        title=f"{len(hits)} deals have a next step nobody has touched in {days}+ days",
        what=(
            f"{len(hits)} open deals have a next step on record, but the task is past due or the record "
            f"has not been modified in over {days} days."
        ),
        why=(
            "A stale next step is worse than a blank one: it makes the deal look worked in every report "
            "and every roll-up, so nobody asks about it. This is how a dead deal survives four pipeline "
            "reviews."
        ),
        fix=(
            "In the review, read the next step out loud and ask when it happened. If the answer is 'we "
            "were going to', the deal moves back a stage or out of the forecast."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("stale-next-step", ctx["crm"]),
        effort="quick",
    )


def rule_activity_silence(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    default_days = int(ctx["cfg"].get("activity_silence_days") or 21)
    per_stage = {str(k): int(to_number(v) or default_days) for k, v in (ctx["cfg"].get("activity_silence_days_by_stage") or {}).items()}
    as_of = ctx["as_of"]
    hits = []
    for deal in ctx["open"]:
        limit = per_stage.get(deal["stage"], default_days)
        last = deal.get("last_activity")
        silent_days = (as_of - last).days if last else None
        if last is None or silent_days > limit:
            deal["_silent_days"] = silent_days
            deal["_silence_limit"] = limit
            hits.append(deal)
    hits.sort(key=lambda d: ((d["amount"] or 0), d.get("_silent_days") or 9999), reverse=True)
    rows = [
        dict(_base_row(d), **{
            "Band": d["band"],
            "Last activity": ymd(d.get("last_activity")),
            "Days silent": d.get("_silent_days") if d.get("_silent_days") is not None else "never logged",
            "Close date": ymd(d["close_date"]),
            "Rule broken": f"No logged activity in {d['_silence_limit']}+ days",
        })
        for d in hits
    ]
    total = sum(d["amount"] or 0 for d in hits)
    never = len([d for d in hits if d.get("last_activity") is None])
    return _emit(
        ins,
        ctx,
        rule="activity-silence",
        severity="high",
        title=f"{len(hits)} deals worth {money(total)} have gone quiet for {default_days}+ days",
        what=(
            f"{len(hits)} open deals have no logged activity inside their silence window"
            + (f"; {never} have never had a single activity logged." if never else ".")
        ),
        why=(
            "Silence on an open deal is either a dead deal or an unlogged one, and both are expensive. "
            "Dead deals inflate coverage; unlogged deals mean the account has no history when the rep "
            "changes, which is the moment renewals and expansions get lost."
        ),
        fix=(
            "Split the list in two. Deals the rep says are active get their activity backfilled today; "
            "the rest get closed-lost. Then agree a logging standard — the point is not surveillance, "
            "it is that the next person can pick the account up."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("activity-silence", ctx["crm"], days=default_days),
        effort="medium",
    )


def rule_post_commit_amount_change(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    if not ctx["history_available"]:
        return []
    commit_stages = set(ctx["cfg"].get("commit_stages") or [])
    if not commit_stages:
        ins.notes.append(
            "commit_stages is empty in the plugin config, so post-commit amount changes were not "
            "checked. Re-run /pipeline-inspection:setup and answer the 'what does commit mean to you' "
            "question."
        )
        return []
    tolerance = float(ctx["cfg"].get("amount_change_tolerance_pct") or 10.0)
    hits = []
    for deal in ctx["open"]:
        entered = None
        for event in deal.get("stage_path") or []:
            if event["stage"] in commit_stages:
                entered = event["at"]
                break
        if entered is None and deal["stage"] in commit_stages and deal.get("stage_entry"):
            entered = deal["stage_entry"]
        if entered is None:
            continue
        after = [c for c in deal.get("amount_changes") or [] if c["at"] >= entered]
        material = [
            c for c in after
            if c["old"] and abs(c["new"] - c["old"]) / abs(c["old"]) * 100.0 >= tolerance
        ]
        if material:
            worst = max(material, key=lambda c: abs(c["new"] - c["old"]))
            deal["_amount_move"] = worst
            deal["_commit_entered"] = entered
            hits.append(deal)
    hits.sort(key=lambda d: abs(d["_amount_move"]["new"] - d["_amount_move"]["old"]), reverse=True)
    rows = []
    for deal in hits:
        move = deal["_amount_move"]
        delta_pct = (move["new"] - move["old"]) / abs(move["old"]) * 100.0 if move["old"] else 0.0
        rows.append(
            dict(_base_row(deal), **{
                "Was": money(move["old"]),
                "Now": money(move["new"]),
                "Change": f"{delta_pct:+.0f}%",
                "Changed on": ymd(move["at"]),
                "Rule broken": f"Amount moved {delta_pct:+.0f}% after entering a commit stage",
            })
        )
    shrunk = [d for d in hits if d["_amount_move"]["new"] < d["_amount_move"]["old"]]
    ids = ", ".join(f"'{d['id']}'" for d in hits[:20]) or "'<opportunity ids>'"
    return _emit(
        ins,
        ctx,
        rule="post-commit-amount-change",
        severity="high",
        title=f"{len(hits)} committed deals changed amount by more than {tolerance:g}% after commit",
        what=(
            f"{len(hits)} deals had their amount edited by more than {tolerance:g}% after they entered a "
            f"commit stage; {len(shrunk)} of them shrank."
        ),
        why=(
            "Commit is supposed to mean the number is settled. An amount that moves after commit means "
            "either the deal was committed before it was scoped, or the number is being managed to hit a "
            "target. Both destroy the one thing a commit stage exists to provide."
        ),
        fix=(
            "Review each change with the rep and the deal desk. Then decide the policy: either amount is "
            "locked at commit and changes require a re-commit, or your commit stage is earlier in the "
            "cycle than you think and should be renamed."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("post-commit-amount-change", ctx["crm"], ids=ids),
        effort="medium",
        owner_hint="Sales leadership",
    )


def rule_stage_regression(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    if not ctx["history_available"]:
        return []
    index = ctx["stage_model"]["index"]
    if not index:
        return []
    hits = []
    for deal in ctx["open"]:
        path = deal.get("stage_path") or []
        worst = None
        for i in range(len(path) - 1):
            a, b = path[i]["stage"], path[i + 1]["stage"]
            if a in index and b in index and index[b] < index[a]:
                drop = index[a] - index[b]
                if worst is None or drop > worst["drop"]:
                    worst = {"from": a, "to": b, "at": path[i + 1]["at"], "drop": drop}
        if worst:
            deal["_regression"] = worst
            hits.append(deal)
    hits.sort(key=lambda d: (d["_regression"]["drop"], d["amount"] or 0), reverse=True)
    rows = [
        dict(_base_row(d), **{
            "Moved back": f"{d['_regression']['from']} → {d['_regression']['to']}",
            "When": ymd(d["_regression"]["at"]),
            "Stages dropped": d["_regression"]["drop"],
            "Rule broken": "Deal moved backwards through the stage order",
        })
        for d in hits
    ]
    ids = ", ".join(f"'{d['id']}'" for d in hits[:20]) or "'<opportunity ids>'"
    return _emit(
        ins,
        ctx,
        rule="stage-regression",
        severity="medium",
        title=f"{len(hits)} open deals have moved backwards through your stages",
        what=f"{len(hits)} open deals went from a later stage to an earlier one at least once.",
        why=(
            "A regression is honest reporting and should not be punished — but it does mean the earlier "
            "stage exit criteria were not really met, and it silently corrupts every stage conversion "
            "rate you compute, because the deal now counts twice in the same stage."
        ),
        fix=(
            "Read the regressions as a stage-definition problem, not a rep problem. If the same "
            "transition keeps reversing, the exit criteria for the earlier stage are not buyer-verifiable."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("stage-regression", ctx["crm"], ids=ids),
        effort="project",
        owner_hint="RevOps",
    )


def rule_close_date_realism(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    remaining = ctx["remaining_median"]
    if not remaining:
        return []
    factor = float(ctx["cfg"].get("close_date_realism_multiple") or 0.5)
    as_of = ctx["as_of"]
    hits = []
    for deal in ctx["open"]:
        expected_days = remaining.get(deal["stage"])
        if not expected_days or not deal["close_date"]:
            continue
        days_out = (deal["close_date"] - as_of).days
        if days_out < 0:
            continue  # already covered by past-due
        if days_out < expected_days * factor:
            deal["_expected_days"] = expected_days
            deal["_days_out"] = days_out
            hits.append(deal)
    hits.sort(key=lambda d: (d["amount"] or 0), reverse=True)
    rows = [
        dict(_base_row(d), **{
            "Close date": ymd(d["close_date"]),
            "Days out": d["_days_out"],
            "Your history says": f"{d['_expected_days']:.0f}d from this stage",
            "Rule broken": (
                f"Close date is {d['_days_out']}d out; deals won from {d['stage']} historically "
                f"took {d['_expected_days']:.0f}d"
            ),
        })
        for d in hits
    ]
    total = sum(d["amount"] or 0 for d in hits)
    worst_stage = max(set(d["stage"] for d in hits), key=lambda s: len([d for d in hits if d["stage"] == s])) if hits else ""
    return _emit(
        ins,
        ctx,
        rule="close-date-faster-than-history",
        severity="high",
        title=f"{len(hits)} deals worth {money(total)} are forecast to close faster than any deal ever has from that stage",
        what=(
            f"{len(hits)} open deals have a close date less than {factor:g}x the median time your own "
            "won deals took to get from their current stage to signature."
        ),
        why=(
            "This is the arithmetic version of hope. The deal is not lying about the stage or the amount "
            "— it is lying about time, which is the hardest thing to argue with in a review because "
            "nobody has the history to hand. Now you do."
        ),
        fix=(
            "Ask the rep what is compressed: is the buyer running an unusually fast process, or is the "
            "date set to the end of the quarter? If it is genuinely fast, name the accelerator. If not, "
            "move the date to the historical median and let the coverage number tell the truth."
        ),
        deals=hits,
        rows=rows,
        query=verify_query(
            "close-date-faster-than-history",
            ctx["crm"],
            stage=worst_stage,
            days=int(remaining.get(worst_stage, 30) * factor) if worst_stage else 30,
        ),
        effort="medium",
    )


def rule_missing_amount(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    hits = sorted(
        [d for d in ctx["open"] if d["amount"] is None or d["amount"] == 0],
        key=lambda d: (d["created"] or ctx["as_of"]),
        reverse=True,
    )
    rows = [
        dict(_base_row(d), **{
            "Created": ymd(d["created"]),
            "Days in stage": d["days_in_stage"],
            "Close date": ymd(d["close_date"]),
            "Rule broken": "Open deal with no amount (or a zero amount)",
        })
        for d in hits
    ]
    return _emit(
        ins,
        ctx,
        rule="missing-or-zero-amount",
        severity="high",
        title=f"{len(hits)} open deals carry no amount at all",
        what=f"{len(hits)} open deals have a null or zero amount.",
        why=(
            "These are invisible to every dollar-based report you run — coverage, forecast, at-risk, "
            "win rate by size. They are not zero-dollar deals; they are unmeasured ones, and they make "
            "your pipeline look smaller and cleaner than it is."
        ),
        fix=(
            "Set a rule that amount is required to leave your first qualified stage, and give reps a "
            "default package price so an early estimate is easy. Backfill the current list from the "
            "rep's own estimate — an approximate number beats a blank one."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("missing-or-zero-amount", ctx["crm"]),
        effort="quick",
        owner_hint="RevOps",
    )


def rule_quarter_end_clustering(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    deals = [d for d in ctx["open"] if d["close_date"]]
    if not deals:
        return []
    fy_start = int(ctx["profile"].get("fiscal_year_start_month") or 1)
    month_end = [d for d in deals if d["close_date"].date() == last_day_of_month(d["close_date"]).date()]
    quarter_end = [
        d for d in month_end if is_fiscal_quarter_end_month(d["close_date"].month, fy_start)
    ]
    share = 100.0 * len(month_end) / len(deals)
    threshold = float(ctx["cfg"].get("clustering_flag_pct") or 30.0)
    if share < threshold:
        return []
    ranked = sorted(month_end, key=lambda d: (d["amount"] or 0), reverse=True)
    rows = [
        dict(_base_row(d), **{
            "Close date": ymd(d["close_date"]),
            "Days in stage": d["days_in_stage"],
            "Quarter end?": "yes" if d in quarter_end else "month end",
            "Rule broken": "Close date sits on the last day of the period",
        })
        for d in ranked
    ]
    total = sum(d["amount"] or 0 for d in month_end)
    return _emit(
        ins,
        ctx,
        rule="quarter-end-clustering",
        severity="medium",
        title=f"{share:.0f}% of open deals close on the last day of a month ({money(total)})",
        what=(
            f"{len(month_end)} of {len(deals)} open deals with a close date land on the final day of a "
            f"calendar month, and {len(quarter_end)} of those land on your fiscal quarter end."
        ),
        why=(
            "Buyers do not sign on the last day of your month at that rate — this is a placeholder, not a "
            "commitment. It is the clearest tell that close dates are being set to the period boundary "
            "rather than to a buyer's decision process, which makes in-quarter linearity, coverage and "
            "every weighted forecast unusable."
        ),
        fix=(
            "Ask for the buyer's date, not yours: whose signature, on what day, after what internal step. "
            "Then track the share of period-end dates as a metric in its own right — it should fall every "
            "quarter you inspect."
        ),
        deals=month_end,
        rows=rows,
        query=verify_query("quarter-end-clustering", ctx["crm"]),
        effort="medium",
        owner_hint="Sales leadership",
        extra_evidence={
            "last_day_of_month_share_pct": round(share, 1),
            "fiscal_quarter_end_deals": len(quarter_end),
        },
    )


def rule_same_day_close(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    max_days = int(ctx["cfg"].get("same_day_close_max_days") or 1)
    hits = []
    for deal in ctx["closed"]:
        if not deal["created"] or not deal["close_date"]:
            continue
        age = (deal["close_date"] - deal["created"]).days
        if age <= max_days:
            deal["_cycle_days"] = age
            hits.append(deal)
    hits.sort(key=lambda d: (d["amount"] or 0), reverse=True)
    rows = [
        dict(_base_row(d), **{
            "Created": ymd(d["created"]),
            "Closed": ymd(d["close_date"]),
            "Cycle days": d["_cycle_days"],
            "Won?": "won" if d["is_won"] else "lost",
            "Rule broken": f"Created and closed within {max_days} day(s)",
        })
        for d in hits
    ]
    share = 100.0 * len(hits) / len(ctx["closed"]) if ctx["closed"] else 0.0
    return _emit(
        ins,
        ctx,
        rule="same-day-create-and-close",
        severity="medium",
        title=f"{len(hits)} closed deals were created and closed within {max_days} day(s) ({share:.0f}% of closed history)",
        what=(
            f"{len(hits)} deals in your closed history were created and closed inside {max_days} day(s) — "
            "backfilled records rather than deals that were actually run through the pipeline."
        ),
        why=(
            "These are the reason your average sales cycle looks shorter than it is. They also corrupt "
            "the stage medians this very report depends on, every conversion rate, and any ramp or "
            "capacity model built on cycle time. They are usually legitimate — an inbound renewal, a "
            "migration — but they must not be counted as deals that were sold."
        ),
        fix=(
            "Give backfilled and auto-renewed deals their own record type or deal type, and exclude that "
            "type from cycle-time and conversion reporting. Do not delete them — they are real revenue."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("same-day-create-and-close", ctx["crm"], lookback=ctx["lookback"], days=max_days),
        effort="medium",
        owner_hint="RevOps",
    )


def rule_double_counted(ins: Inspection, ctx: Dict[str, Any]) -> List[Finding]:
    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for deal in ctx["open"]:
        key = str(deal.get("account_id") or deal.get("account") or "")
        if key:
            by_account.setdefault(key, []).append(deal)
    hits, rows = [], []
    for key, deals in by_account.items():
        if len(deals) < 2:
            continue
        for i in range(len(deals)):
            for j in range(i + 1, len(deals)):
                a, b = deals[i], deals[j]
                same_amount = a["amount"] and b["amount"] and abs(a["amount"] - b["amount"]) < 1
                same_close = a["close_date"] and b["close_date"] and a["close_date"] == b["close_date"]
                if same_amount and same_close:
                    for deal in (a, b):
                        if deal not in hits:
                            hits.append(deal)
                    rows.append(
                        {
                            "Account": a["account"],
                            "Deal A": a["name"],
                            "Deal B": b["name"],
                            "Amount": money(a["amount"]),
                            "Close date": ymd(a["close_date"]),
                            "Owner": f"{a['owner']} / {b['owner']}",
                            "Rule broken": "Two open deals on one account with the same amount and close date",
                        }
                    )
        del key
    total = sum(d["amount"] or 0 for d in hits)
    return _emit(
        ins,
        ctx,
        rule="possible-double-counted-pipeline",
        severity="medium",
        title=f"{len(hits)} open deals across {len(rows)} account(s) look like duplicates ({money(total)})",
        what=(
            f"{len(hits)} open deals form {len(rows)} pairs on the same account with an identical amount "
            "and an identical close date."
        ),
        why=(
            "Identical amount and identical date on one account is almost never two real deals. It is "
            "usually a duplicate created by an integration or a rep, and it double-counts real pipeline "
            "— which means coverage looks better than it is at exactly the moment you rely on it."
        ),
        fix=(
            "Have the owning reps confirm which record is live, close the other as a duplicate, and check "
            "whether an integration is creating them. If both are real, make the names distinguish them."
        ),
        deals=hits,
        rows=rows,
        query=verify_query("possible-double-counted-pipeline", ctx["crm"]),
        effort="quick",
        owner_hint="RevOps",
    )


# --------------------------------------------------------------------------- sections


def build_sections(ins: Inspection, ctx: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    cfg, deals, as_of = ctx["cfg"], ctx["open"], ctx["as_of"]
    stats = extra["stage_stats"]
    flags = ins.flags

    # ---- call list: the actual deliverable
    ranked = []
    for deal in deals:
        deal_flags = flags.get(deal["id"]) or []
        if not deal_flags:
            continue
        ranked.append(
            {
                "risk_score": sum(f["weight"] for f in deal_flags),
                "deal": deal,
                "rules": [f["rule"] for f in deal_flags],
                "worst": min(deal_flags, key=lambda f: SEV_ORDER[::-1].index(f["severity"]))["severity"],
            }
        )
    ranked.sort(key=lambda r: (r["risk_score"], r["deal"]["amount"] or 0), reverse=True)
    call_list = [
        {
            "Rank": i,
            "Deal": r["deal"]["name"],
            "Account": r["deal"]["account"],
            "Owner": r["deal"]["owner"],
            "Amount": money(r["deal"]["amount"]),
            "Stage": r["deal"]["stage"],
            "Days in stage": r["deal"]["days_in_stage"],
            "Close date": ymd(r["deal"]["close_date"]),
            "Risk score": r["risk_score"],
            "Rules broken": ", ".join(sorted(set(r["rules"]))),
        }
        for i, r in enumerate(ranked, 1)
    ]

    # ---- stage medians
    open_by_stage: Dict[str, List[Dict[str, Any]]] = {}
    for deal in deals:
        open_by_stage.setdefault(deal["stage"], []).append(deal)
    median_rows = []
    for stage in sorted(stats, key=lambda s: ctx["stage_model"]["index"].get(s, 999)):
        stat = stats[stage]
        stage_deals = open_by_stage.get(stage, [])
        if not stage_deals and not stat["closed_samples"]:
            continue
        median_rows.append(
            {
                "Stage": stat["label"],
                "Closed samples": stat["closed_samples"],
                "Median days": stat["median_days"] if stat["median_days"] is not None else "too few",
                "75th pct": stat["p75_days"] if stat["p75_days"] is not None else "—",
                "90th pct": stat["p90_days"] if stat["p90_days"] is not None else "—",
                "You expected": stat["expected_days"] if stat["expected_days"] else "—",
                "Flag above": stat["threshold_days"],
                "Open deals": len(stage_deals),
                "Open amount": money(sum(d["amount"] or 0 for d in stage_deals)),
                "Basis": stat["basis_label"],
            }
        )

    # ---- push distribution
    push_buckets: Dict[str, Dict[str, Any]] = {}
    for deal in deals:
        n = len(deal["pushes"])
        key = "3+" if n >= 3 else str(n)
        entry = push_buckets.setdefault(key, {"Pushes": key, "Deals": 0, "Amount": 0.0, "Days slipped": 0})
        entry["Deals"] += 1
        entry["Amount"] += deal["amount"] or 0
        entry["Days slipped"] += deal["days_pushed"]
    push_rows = [
        {**row, "Amount": money(row["Amount"])}
        for row in sorted(push_buckets.values(), key=lambda r: (r["Pushes"] == "3+", r["Pushes"]))
    ]
    pusher_by_owner: Dict[str, Dict[str, Any]] = {}
    for deal in deals:
        entry = pusher_by_owner.setdefault(
            str(deal["owner"]), {"Owner": str(deal["owner"]), "Open deals": 0, "Total pushes": 0, "Days slipped": 0}
        )
        entry["Open deals"] += 1
        entry["Total pushes"] += len(deal["pushes"])
        entry["Days slipped"] += deal["days_pushed"]
    push_by_owner = sorted(
        (dict(v, **{"Pushes per deal": round(v["Total pushes"] / v["Open deals"], 2) if v["Open deals"] else 0})
         for v in pusher_by_owner.values()),
        key=lambda r: r["Pushes per deal"],
        reverse=True,
    )

    # ---- close date clustering
    dated = [d for d in deals if d["close_date"]]
    day_buckets: Dict[str, Dict[str, Any]] = {}
    for deal in dated:
        day = deal["close_date"].day
        if deal["close_date"].date() == last_day_of_month(deal["close_date"]).date():
            key = "Last day of month"
        elif day <= 10:
            key = "Days 1-10"
        elif day <= 20:
            key = "Days 11-20"
        else:
            key = "Days 21 to penultimate"
        entry = day_buckets.setdefault(key, {"Close date lands on": key, "Deals": 0, "Amount": 0.0})
        entry["Deals"] += 1
        entry["Amount"] += deal["amount"] or 0
    cluster_rows = [
        {**row, "Amount": money(row["Amount"]), "Share": f"{100.0 * row['Deals'] / len(dated):.0f}%"}
        for row in sorted(day_buckets.values(), key=lambda r: r["Deals"], reverse=True)
    ] if dated else []

    # ---- contact roles
    contact_buckets: Dict[str, Dict[str, Any]] = {}
    for deal in deals:
        contacts = deal.get("contacts")
        key = "unknown" if contacts is None else ("5+" if contacts >= 5 else str(contacts))
        entry = contact_buckets.setdefault(key, {"Contacts on deal": key, "Deals": 0, "Amount": 0.0})
        entry["Deals"] += 1
        entry["Amount"] += deal["amount"] or 0
    contact_rows = [{**row, "Amount": money(row["Amount"])} for row in
                    sorted(contact_buckets.values(), key=lambda r: str(r["Contacts on deal"]))]

    band_rows = []
    for band in sorted(cfg.get("deal_size_bands") or [], key=lambda b: to_number(b.get("min_amount")) or 0):
        in_band = [d for d in deals if d["band"] == band["label"]]
        if not in_band:
            continue
        with_contacts = [d for d in in_band if d.get("contacts") is not None]
        band_rows.append(
            {
                "Band": band["label"],
                "From": money(to_number(band.get("min_amount")) or 0),
                "Open deals": len(in_band),
                "Open amount": money(sum(d["amount"] or 0 for d in in_band)),
                "Median contacts": round(median([d["contacts"] for d in with_contacts]) or 0, 1) if with_contacts else "—",
                "Contacts required": required_contacts(
                    to_number(band.get("min_amount")) or 0, cfg.get("single_thread_thresholds") or []
                ),
            }
        )

    # ---- owner scorecard
    owner_rows: Dict[str, Dict[str, Any]] = {}
    for deal in deals:
        owner = str(deal["owner"])
        entry = owner_rows.setdefault(
            owner,
            {"Owner": owner, "Open deals": 0, "Open amount": 0.0, "Flagged deals": 0, "Flagged amount": 0.0,
             "_rules": {}},
        )
        entry["Open deals"] += 1
        entry["Open amount"] += deal["amount"] or 0
        deal_flags = flags.get(deal["id"]) or []
        if deal_flags:
            entry["Flagged deals"] += 1
            entry["Flagged amount"] += deal["amount"] or 0
            for flag in deal_flags:
                entry["_rules"][flag["rule"]] = entry["_rules"].get(flag["rule"], 0) + 1
    scorecard = []
    for entry in owner_rows.values():
        worst = max(entry["_rules"], key=lambda k: entry["_rules"][k]) if entry["_rules"] else "—"
        scorecard.append(
            {
                "Owner": entry["Owner"],
                "Open deals": entry["Open deals"],
                "Open amount": money(entry["Open amount"]),
                "Flagged deals": entry["Flagged deals"],
                "Flagged amount": money(entry["Flagged amount"]),
                "% flagged": f"{100.0 * entry['Flagged amount'] / entry['Open amount']:.0f}%" if entry["Open amount"] else "—",
                "Most common issue": worst,
            }
        )
    scorecard.sort(key=lambda r: r["Flagged deals"], reverse=True)

    # ---- cycle time
    cycle_rows = [
        {"Stage": ctx["stage_model"]["labels"].get(stage, stage), "Median days from here to closed-won": value}
        for stage, value in sorted(
            extra["remaining_median"].items(), key=lambda kv: ctx["stage_model"]["index"].get(kv[0], 999)
        )
    ]
    won_cycles = [
        (d["close_date"] - d["created"]).days
        for d in ctx["closed"]
        if d["is_won"] and d["created"] and d["close_date"] and (d["close_date"] - d["created"]).days > 1
    ]

    unknown_age = [d for d in deals if not d["stage_age_confident"]]

    return {
        "call_list": call_list[: int(cfg.get("call_list_size") or 30)],
        "call_list_full": call_list,
        "stage_medians": {
            "rows": median_rows,
            "measurement": extra["duration_meta"],
            "note": (
                "Medians are measured from your own closed deals over the lookback window — no industry "
                "benchmark is used anywhere in this report. Where a stage had fewer than "
                f"{cfg.get('min_closed_deals_for_median', 8)} completed intervals, the basis falls back "
                "to the number you confirmed during setup, then to the all-stage median."
            ),
        },
        "push_distribution": {
            "rows": push_rows,
            "by_owner": push_by_owner[:25],
            "coverage": extra["history_coverage"],
        },
        "close_date_clustering": {"rows": cluster_rows, "dated_deals": len(dated)},
        "contact_roles": {"rows": contact_rows, "by_band": band_rows},
        "owner_scorecard": scorecard,
        "cycle_time": {
            "rows": cycle_rows,
            "median_days_create_to_won": round(median(won_cycles), 1) if won_cycles else None,
            "won_deals_measured": len(won_cycles),
        },
        "coverage": extra["coverage"],
        "totals": {
            "open_deals": len(deals),
            "open_amount": round(ctx["total_open_amount"]),
            "flagged_deals": len(extra["flagged_ids"]),
            "at_risk_deals": len(extra["at_risk_ids"]),
            "closed_deals_analyzed": len(ctx["closed"]),
            "excluded_below_material_floor": len(extra["below_floor"]),
            "material_deal_floor": extra["floor"],
            "deals_with_unknown_stage_age": len(unknown_age),
            "as_of": as_of.strftime("%Y-%m-%d"),
            "crm": ctx["crm"],
        },
        "thresholds_used": {
            "stagnation_basis": cfg.get("stagnation_basis"),
            "stagnation_multiple": cfg.get("stagnation_multiple"),
            "severe_stagnation_multiple": cfg.get("severe_stagnation_multiple"),
            "push_threshold": cfg.get("push_threshold"),
            "next_step_mode": cfg.get("next_step_mode"),
            "next_step_staleness_days": cfg.get("next_step_staleness_days"),
            "activity_silence_days": cfg.get("activity_silence_days"),
            "single_thread_thresholds": cfg.get("single_thread_thresholds"),
            "amount_change_tolerance_pct": cfg.get("amount_change_tolerance_pct"),
            "clustering_flag_pct": cfg.get("clustering_flag_pct"),
            "close_date_realism_multiple": cfg.get("close_date_realism_multiple"),
            "commit_stages": cfg.get("commit_stages"),
            "material_deal_floor": extra["floor"],
            "inspection_cadence": cfg.get("inspection_cadence"),
        },
        "notes": ins.notes,
    }


# ------------------------------------------------------------------------------- main


def load_config(explicit: Optional[Path]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    profile = load_profile(required=False)
    if explicit:
        with Path(explicit).open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})
        return cfg, profile
    if not profile and not plugin_config_path(PLUGIN).exists():
        raise ConfigError(
            f"No configuration found. Expected {profile_path()} or {plugin_config_path(PLUGIN)}.\n"
            f"Run /pipeline-inspection:setup first — it discovers your stages, measures your own stage "
            f"medians, asks the handful of questions the CRM cannot answer, and writes both files.\n"
            f"To try the analyzer offline instead, pass --config with the bundled config.example.json."
        )
    return load_plugin_config(PLUGIN, defaults=dict(DEFAULTS)), profile


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Turn raw CRM pipeline data into findings.json.")
    parser.add_argument("--run-dir", help="Run directory containing raw/ ; output is written here too.")
    parser.add_argument("--raw-dir", help="Override the input directory (defaults to <run-dir>/raw).")
    parser.add_argument("--out-dir", help="Override the output directory (defaults to <run-dir>).")
    parser.add_argument("--config", help="Read plugin config from this file instead of ~/.leanscale-gtm/.")
    parser.add_argument("--as-of", help="Treat this date as today (YYYY-MM-DD). Defaults to raw/meta.json or now.")
    args = parser.parse_args(argv)

    if not args.run_dir and not (args.raw_dir and args.out_dir):
        parser.error("give --run-dir, or both --raw-dir and --out-dir")

    run_dir = Path(args.run_dir).expanduser() if args.run_dir else None
    raw_dir = Path(args.raw_dir).expanduser() if args.raw_dir else (run_dir / "raw")
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else run_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if not raw_dir.exists():
        print(f"error: {raw_dir} does not exist. The :run skill writes the raw JSON there.", file=sys.stderr)
        return 2

    try:
        cfg, profile = load_config(Path(args.config).expanduser() if args.config else None)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    meta = read_meta(raw_dir)
    as_of_text = args.as_of or meta.get("as_of")
    as_of = parse_dt(as_of_text) or datetime.now(timezone.utc)
    as_of = as_of.replace(hour=0, minute=0, second=0, microsecond=0)

    manifest = RunManifest(PLUGIN, out_dir, window={"end": as_of.strftime("%Y-%m-%d")})
    try:
        doc = build(raw_dir, cfg, profile, as_of, manifest)
    except Exception as exc:  # noqa: BLE001 - fail loud, with the reason
        print(f"\n{type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    path = doc.write(out_dir)
    counts = doc.counts_by_severity()
    totals = doc.sections.get("totals", {})
    print(f"pipeline-inspection · {totals.get('open_deals', 0):,} open deals · {money(totals.get('open_amount'))}")
    print(f"  {len(doc.findings)} findings — " + " · ".join(f"{v} {k}" for k, v in counts.items() if v))
    print(f"  {totals.get('flagged_deals', 0):,} deals on the call list")
    print(f"  wrote {path}")
    if doc.unavailable:
        print(f"  {len(doc.unavailable)} check(s) unavailable — see the report's 'not covered' banner")
    print("  next: python3 report.py --run-dir " + str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
