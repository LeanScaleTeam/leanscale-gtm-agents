#!/usr/bin/env python3
"""
forecast-agent — Layer 2. Pure, offline transform of raw/*.json into findings.json.

Two modes, and audit is the default:

  --mode audit      Can this CRM support a forecast at all? Scores it 0-100 and
                    names every reason the number is fiction.
  --mode forecast   Three numbers (worst / likely / best), every step shown, and
                    the delta between the rep-called commit and the evidence.
                    Refuses to run below the integrity threshold unless forced,
                    because a precise number on top of invented close dates is
                    worse than no number.

Reads only local files. No network, no MCP, Python 3.9+ stdlib only.
Claude fetches the data (see skills/run/SKILL.md) and writes it to raw/.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    ConfigError,
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    SourceEmptyError,
    apply_deltas,
    load_plugin_config,
    load_profile,
    median,
    normalize_records,
    parse_dt,
    pct,
    percentile,
    redact_name,
    save_baseline,
)
from lib.config import fiscal_period  # noqa: E402

PLUGIN = "forecast-agent"

# --------------------------------------------------------------------------- defaults
# Mirrors config.example.json minus the _help keys. Per-plugin config layers over this.
DEFAULTS: Dict[str, Any] = {
    "methodology": "category",
    "category_map": {
        "commit": ["Commit"],
        "best_case": ["Best Case", "Upside"],
        "pipeline": ["Pipeline"],
        "omitted": ["Omitted"],
        "closed": ["Closed"],
    },
    "commit_buckets": ["commit"],
    "forecast_measure": "bookings",
    "amount_field_by_measure": {"bookings": "Amount", "arr": "ARR__c", "revenue": "Recognized_Revenue__c"},
    "count_types": {"new": True, "expansion": True, "renewal": False},
    "type_field": "Type",
    "type_map": {
        "new": ["New Business", "New Logo", "New", "newbusiness"],
        "expansion": ["Existing Business - Upsell", "Expansion", "Upsell", "Cross-sell", "existingbusiness"],
        "renewal": ["Renewal", "Existing Business - Renewal"],
    },
    "history_quarters": 8,
    "min_cohort_n": 25,
    "run_forecast_below_threshold": False,
    "forecast_threshold": 60,
    "push_days_threshold": 7,
    "serial_push_count": 3,
    "single_thread_max_contacts": 1,
    "stale_activity_days": 21,
    "next_step_field": "NextStep",
    "quota": {"source": "manual", "period_quota_by_owner": {}, "org_quota": None},
    "field_map": {
        "id": "Id", "name": "Name", "amount": "Amount", "amount_converted": "ConvertedAmount",
        "close_date": "CloseDate", "created_date": "CreatedDate", "stage": "StageName",
        "forecast_category": "ForecastCategoryName", "is_closed": "IsClosed", "is_won": "IsWon",
        "owner_id": "OwnerId", "owner_name": "Owner.Name", "next_step": "NextStep", "type": "Type",
        "probability": "Probability", "last_activity": "LastActivityDate",
        "currency": "CurrencyIsoCode", "contact_role_count": "ContactRoleCount",
    },
    "stage_order": [],
    "closed_won_stages": ["Closed Won", "closedwon"],
    "closed_lost_stages": ["Closed Lost", "Closed Lost - No Decision", "closedlost"],
    "redact_reps": False,
}

HUBSPOT_FIELD_MAP = {
    "id": "Id", "name": "dealname", "amount": "amount", "amount_converted": "amount_in_home_currency",
    "close_date": "closedate", "created_date": "createdate", "stage": "dealstage",
    "forecast_category": "forecast_category", "is_closed": "hs_is_closed", "is_won": "hs_is_closed_won",
    "owner_id": "hubspot_owner_id", "owner_name": "owner_name", "next_step": "hs_next_step",
    "type": "dealtype", "probability": "hs_deal_stage_probability",
    "last_activity": "notes_last_updated", "currency": "deal_currency_code",
    "contact_role_count": "num_associated_contacts",
}

# The integrity score. Weights sum to 100 and are documented in the README.
COMPONENTS: List[Tuple[str, str, int, str]] = [
    ("date_integrity", "Date integrity", 22,
     "Have the close dates on the deals you are counting already moved?"),
    ("deal_evidence", "Deal evidence", 18,
     "Does a committed deal carry a next step and recent activity, or is it a name and a number?"),
    ("buying_group", "Buying-group coverage", 12,
     "Is there more than one human between you and the signature?"),
    ("history_depth", "History depth", 15,
     "Is there enough closed history for a conversion rate to mean anything?"),
    ("calibration", "Calibration", 20,
     "When this team said Commit before, how much of it actually landed, and how steady was that?"),
    ("date_realism", "Date realism", 13,
     "Do the close dates look like a sales cycle, or like a quarter-end wish?"),
]


# --------------------------------------------------------------------------- small math

def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def quarter_bounds(d: date, start_month: int) -> Tuple[date, date]:
    """First and last day of the fiscal quarter containing `d`."""
    start_month = int(start_month or 1)
    fy_start = date(d.year, start_month, 1) if d.month >= start_month else date(d.year - 1, start_month, 1)
    idx = ((d.year - fy_start.year) * 12 + d.month - start_month) // 3
    qs = add_months(fy_start, idx * 3)
    return qs, add_months(qs, 3) - timedelta(days=1)


def wilson(k: float, n: float, z: float = 1.96) -> Tuple[float, float]:
    """
    Wilson score interval. Used deliberately: it is n-aware, so thin history
    widens the worst/best spread on its own instead of us asserting a caveat.
    """
    if not n:
        return 0.0, 1.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def ecdf(sorted_values: Sequence[float], x: float) -> float:
    """Share of observations <= x. Empirical, no distribution assumed."""
    if not sorted_values:
        return 0.5
    lo, hi = 0, len(sorted_values)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo / float(len(sorted_values))


def as_date(value: Any) -> Optional[date]:
    dt = parse_dt(value)
    return dt.date() if dt else None


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def money(v: float) -> str:
    return "${:,.0f}".format(v or 0)


def nv(n: int, noun: str, verb_singular: str, verb_plural: str) -> str:
    """'1 deal has' / '4 deals have'. Small thing — a report that says '1 deals' reads unserious."""
    return f"{n} {noun}{'' if n == 1 else 's'} {verb_singular if n == 1 else verb_plural}"


def truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y")


# --------------------------------------------------------------------------- raw loading

REQUIRED_SOURCES = ("open_deals", "closed_deals")
OPTIONAL_SOURCES = ("stage_history", "field_history", "contact_roles", "users",
                    "stage_meta", "activities", "quota")
# Optional sources whose absence genuinely removes a measurement, so it belongs in the
# report's "unavailable, not clean" banner. activities/quota have documented alternates.
MEASURED_SOURCES = ("stage_history", "field_history", "contact_roles", "users", "stage_meta")

DIAGNOSIS = {
    "open_deals": "the integration user may lack read access to Opportunity/Deal, the pipeline "
                  "filter in the query may exclude everything, or the run skill wrote the file "
                  "before the query returned. Re-run /forecast-agent:setup to test the connector.",
    "closed_deals": "the history window may predate this CRM instance, the closed-stage names in "
                    "config may not match the real picklist, or field-level security is hiding "
                    "CloseDate from the integration user.",
}


def load_raw(raw_dir: Path, name: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = raw_dir / f"{name}.json"
    if not path.exists():
        return [], {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}). Re-run the :run skill's fetch step.")
    if isinstance(payload, list):
        return payload, {"source": name, "tool": "unknown", "query": ""}
    records = payload.get("records")
    if records is None:
        records = payload.get("results") or payload.get("data") or []
    meta = {k: payload.get(k, "") for k in ("source", "tool", "query", "note", "fetched_at")}
    meta.setdefault("source", name)
    return list(records), meta


# --------------------------------------------------------------------------- normalisation

class Ctx:
    """Everything the analysis needs, resolved once."""

    def __init__(self, cfg: Dict[str, Any], profile: Dict[str, Any], meta: Dict[str, Any]):
        self.cfg = cfg
        self.profile = profile
        self.meta = meta
        self.crm = (meta.get("crm") or (profile.get("crm") or {}).get("system") or "salesforce").lower()

        fm = dict(DEFAULTS["field_map"])
        if self.crm == "hubspot":
            fm.update(HUBSPOT_FIELD_MAP)
        # Only the customer's OWN field_map may override the CRM map. cfg["field_map"]
        # is not that: load_plugin_config layers the config file over DEFAULTS, so it
        # carries the complete Salesforce map whenever the customer never set one —
        # and re-applying it here silently replaced every HubSpot property name,
        # which is how a healthy HubSpot org reported zero pipeline and zero win rate.
        fm.update({k: v for k, v in (cfg.get("_user_field_map") or {}).items() if v})
        # Two convenience keys that live at the top level of the config because customers
        # look for them there. They win over field_map when set.
        if cfg.get("type_field"):
            fm["type"] = cfg["type_field"]
        self.has_next_step_field = "next_step_field" not in cfg or bool(cfg.get("next_step_field"))
        if cfg.get("next_step_field"):
            fm["next_step"] = cfg["next_step_field"]
        self.fm = fm

        self.fy_start = int(meta.get("fiscal_year_start_month")
                            or profile.get("fiscal_year_start_month") or 1)
        self.fy_naming = meta.get("fiscal_year_naming") or profile.get("fiscal_year_naming", "ends_in")
        self.multi_currency = bool(meta.get("multi_currency",
                                            (profile.get("currency") or {}).get("multi_currency", False)))
        self.floor = float(profile.get("material_deal_floor") or 0)
        self.redact = bool(cfg.get("redact_reps") or profile.get("redact_pii_in_reports"))
        self.warnings: List[str] = []
        self.amount_field_used = ""

        self.cat_lookup: Dict[str, str] = {}
        for bucket, values in (cfg.get("category_map") or {}).items():
            for v in values:
                self.cat_lookup[str(v).strip().lower()] = bucket
        self.type_lookup: Dict[str, str] = {}
        for canon, values in (cfg.get("type_map") or {}).items():
            for v in values:
                self.type_lookup[str(v).strip().lower()] = canon

        self.won_stages = {str(s).strip().lower() for s in cfg.get("closed_won_stages") or []}
        self.lost_stages = {str(s).strip().lower() for s in cfg.get("closed_lost_stages") or []}

    def category(self, value: Any) -> str:
        return self.cat_lookup.get(str(value or "").strip().lower(), "unmapped")

    def deal_type(self, value: Any) -> str:
        return self.type_lookup.get(str(value or "").strip().lower(), "unmapped")

    def label(self, name: Any) -> str:
        return redact_name(name) if self.redact else str(name or "Unassigned")


def resolve_amount_field(ctx: Ctx, sample: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    """Pick the amount field for the chosen measure. Returns (field, disclosure sentence)."""
    cfg = ctx.cfg
    measure = str(cfg.get("forecast_measure") or "bookings").lower()
    field = (cfg.get("amount_field_by_measure") or {}).get(measure) or ctx.fm["amount"]
    present = any(field in r for r in sample[:50])
    note = ""
    if not present:
        note = (f"Configured {measure} field '{field}' is absent from the returned records; "
                f"fell back to '{ctx.fm['amount']}'. The report is in {ctx.fm['amount']} units, "
                f"not {measure}.")
        field = ctx.fm["amount"]
    if ctx.multi_currency:
        conv = ctx.fm.get("amount_converted")
        if measure == "bookings" and conv and any(conv in r for r in sample[:50]):
            note = (note + " " if note else "") + (
                f"Multi-currency org: summed the corporate-currency field '{conv}', not '{field}'.")
            field = conv
        else:
            note = (note + " " if note else "") + (
                f"Multi-currency org, but no converted-amount field was available for measure "
                f"'{measure}'. Amounts are summed in each deal's own currency and are therefore "
                f"NOT comparable. Add a converted-amount field before trusting the totals.")
    else:
        note = (note + " " if note else "") + f"Single-currency org: summed '{field}' directly."
    return field, note.strip()


def build_deals(records: Sequence[Dict[str, Any]], ctx: Ctx, amount_field: str,
                owner_names: Dict[str, str]) -> List[Dict[str, Any]]:
    fm = ctx.fm
    out: List[Dict[str, Any]] = []
    for r in normalize_records(records):
        stage = str(r.get(fm["stage"]) or "")
        raw_closed = r.get(fm["is_closed"])
        is_closed = truthy(raw_closed) if raw_closed is not None else (
            stage.strip().lower() in ctx.won_stages | ctx.lost_stages)
        raw_won = r.get(fm["is_won"])
        is_won = truthy(raw_won) if raw_won is not None else (stage.strip().lower() in ctx.won_stages)
        amt = r.get(amount_field)
        try:
            amount = float(str(amt).replace(",", "").replace("$", "")) if amt not in (None, "") else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        prob = r.get(fm["probability"])
        try:
            probability = float(prob) if prob not in (None, "") else None
        except (TypeError, ValueError):
            probability = None
        if probability is not None and probability <= 1.0 and ctx.crm == "hubspot":
            probability *= 100.0
        owner_id = str(r.get(fm["owner_id"]) or "")
        out.append({
            "id": str(r.get(fm["id"]) or r.get("Id") or ""),
            "name": r.get(fm["name"]) or "",
            "account": r.get("Account.Name") or r.get("account_name") or "",
            "amount": amount,
            "currency": r.get(fm["currency"]) or "",
            "close_date": as_date(r.get(fm["close_date"])),
            "created_date": as_date(r.get(fm["created_date"])),
            "stage": stage,
            "category": ctx.category(r.get(fm["forecast_category"])),
            "category_raw": r.get(fm["forecast_category"]) or "",
            "is_closed": is_closed,
            "is_won": is_won,
            "owner_id": owner_id,
            "owner_name": r.get(fm["owner_name"]) or owner_names.get(owner_id) or "",
            "next_step": r.get(fm["next_step"]) or "",
            "type": ctx.deal_type(r.get(fm["type"])),
            "type_raw": r.get(fm["type"]) or "",
            "probability": probability,
            "last_activity": as_date(r.get(fm["last_activity"])),
            "contact_roles": r.get(fm["contact_role_count"]),
        })
    return out


# --------------------------------------------------------------------------- history

CANON_FIELD = {
    "closedate": "close_date", "close_date": "close_date",
    "forecastcategoryname": "forecast_category", "forecast_category": "forecast_category",
    "forecastcategory": "forecast_category",
    "amount": "amount",
}


def build_field_history(records: Sequence[Dict[str, Any]], crm: str) -> Dict[str, Dict[str, List[Tuple[date, Any, Any]]]]:
    """deal_id -> canonical field -> [(changed_on, old, new)] sorted ascending."""
    staged: Dict[str, Dict[str, List[Tuple[date, Any, Any]]]] = defaultdict(lambda: defaultdict(list))
    if crm == "hubspot":
        tmp: Dict[str, Dict[str, List[Tuple[date, Any]]]] = defaultdict(lambda: defaultdict(list))
        for r in normalize_records(records):
            did = str(r.get("dealId") or r.get("objectId") or r.get("Id") or "")
            field = CANON_FIELD.get(str(r.get("property") or "").lower())
            when = as_date(r.get("timestamp") or r.get("CreatedDate"))
            if not did or not field or not when:
                continue
            tmp[did][field].append((when, r.get("value")))
        for did, fields in tmp.items():
            for field, seq in fields.items():
                seq.sort(key=lambda x: x[0])
                prev = None
                for when, val in seq:
                    staged[did][field].append((when, prev, val))
                    prev = val
    else:
        for r in normalize_records(records):
            did = str(r.get("OpportunityId") or r.get("ParentId") or "")
            field = CANON_FIELD.get(str(r.get("Field") or "").lower())
            when = as_date(r.get("CreatedDate"))
            if not did or not field or not when:
                continue
            staged[did][field].append((when, r.get("OldValue"), r.get("NewValue")))
        for fields in staged.values():
            for seq in fields.values():
                seq.sort(key=lambda x: x[0])
    return {k: dict(v) for k, v in staged.items()}


def build_stage_history(records: Sequence[Dict[str, Any]], crm: str) -> Dict[str, List[Tuple[date, str]]]:
    hist: Dict[str, List[Tuple[date, str]]] = defaultdict(list)
    for r in normalize_records(records):
        if crm == "hubspot":
            did = str(r.get("dealId") or r.get("objectId") or r.get("Id") or "")
            when = as_date(r.get("timestamp") or r.get("CreatedDate"))
            stage = r.get("value") or r.get("dealstage")
        else:
            did = str(r.get("OpportunityId") or "")
            when = as_date(r.get("CreatedDate"))
            stage = r.get("StageName")
        if did and when and stage:
            hist[did].append((when, str(stage)))
    for seq in hist.values():
        seq.sort(key=lambda x: x[0])
    return dict(hist)


def value_at(changes: List[Tuple[date, Any, Any]], when: date, current: Any) -> Any:
    """The value a field held at a point in time, reconstructed from its change log."""
    if not changes:
        return current
    # Before the first recorded change: Salesforce gives us the genuine prior value in
    # OldValue; HubSpot's history has no OldValue, so its first entry IS the initial value.
    result = changes[0][1] if changes[0][1] not in (None, "") else changes[0][2]
    for changed_on, _old, new in changes:
        if changed_on <= when:
            result = new
        else:
            break
    return result


def count_pushes(changes: List[Tuple[date, Any, Any]], min_days: int) -> Tuple[int, int]:
    """(number of pushes, total days pushed) for a close-date change log."""
    n, total = 0, 0
    for _when, old, new in changes:
        o, d = as_date(old), as_date(new)
        if not o or not d:
            continue
        delta = (d - o).days
        if delta >= min_days:
            n += 1
            total += delta
    return n, total


# --------------------------------------------------------------------------- the analysis

def scope_filter(deals: Sequence[Dict[str, Any]], ctx: Ctx) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Apply the material-deal floor and the new/expansion/renewal counting rules."""
    counts = {"below_floor": 0, "excluded_type": 0, "unmapped_type": 0}
    wanted = {k for k, v in (ctx.cfg.get("count_types") or {}).items() if v}
    kept = []
    for d in deals:
        if ctx.floor and d["amount"] and d["amount"] < ctx.floor:
            counts["below_floor"] += 1
            continue
        t = d["type"]
        if t == "unmapped":
            counts["unmapped_type"] += 1
            t = "new"
        if t not in wanted:
            counts["excluded_type"] += 1
            continue
        kept.append(d)
    return kept, counts


def cohort_rates(closed: Sequence[Dict[str, Any]], membership) -> Dict[str, Dict[str, Any]]:
    """
    Conversion measured on the cohort that ENTERED a stage/category, not on the
    deals currently sitting in it. Current-stage rates are survivorship: the deals
    that fell out are gone from the denominator, which inflates every rate.
    """
    agg: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0.0, "won": 0.0, "won_amt": 0.0, "amt": 0.0})
    for d in closed:
        for key in membership(d):
            a = agg[key]
            a["n"] += 1
            a["amt"] += d["amount"]
            if d["is_won"]:
                a["won"] += 1
                a["won_amt"] += d["amount"]
    out = {}
    for key, a in agg.items():
        lo, hi = wilson(a["won"], a["n"])
        out[key] = {"n": int(a["n"]), "won": int(a["won"]),
                    "rate": (a["won"] / a["n"]) if a["n"] else 0.0,
                    "lo": lo, "hi": hi,
                    "amount_rate": (a["won_amt"] / a["amt"]) if a["amt"] else 0.0}
    return out


def analyse(raw_dir: Path, run_dir: Path, cfg: Dict[str, Any], profile: Dict[str, Any],
            mode: str, as_of_override: Optional[str], force: bool,
            keep_baseline: bool = True) -> Dict[str, Any]:
    meta_records, _ = load_raw(raw_dir, "meta")
    meta = meta_records[0] if isinstance(meta_records, list) and meta_records else {}
    if not isinstance(meta, dict):
        meta = {}
    meta_path = raw_dir / "meta.json"
    if meta_path.exists():
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "records" not in payload:
            meta = payload

    ctx = Ctx(cfg, profile, meta)
    as_of = as_date(as_of_override) or as_date(meta.get("as_of")) or date.today()
    period_start = as_date(meta.get("period_start"))
    period_end = as_date(meta.get("period_end"))
    if not (period_start and period_end):
        period_start, period_end = quarter_bounds(as_of, ctx.fy_start)
    period_label = meta.get("period_label") or fiscal_period(
        {"fiscal_year_start_month": ctx.fy_start, "fiscal_year_naming": ctx.fy_naming},
        period_start.year, period_start.month)

    hist_quarters = int(meta.get("history_quarters") or cfg.get("history_quarters") or 8)
    history_start = add_months(period_start, -3 * hist_quarters)

    manifest = RunManifest(PLUGIN, run_dir,
                           window={"start": history_start.isoformat(), "end": period_end.isoformat()})

    loaded: Dict[str, List[Dict[str, Any]]] = {}
    for name in REQUIRED_SOURCES + OPTIONAL_SOURCES:
        records, src_meta = load_raw(raw_dir, name)
        loaded[name] = records
        required = name in REQUIRED_SOURCES
        if not required and not (raw_dir / f"{name}.json").exists():
            continue
        manifest.record(name, tool=src_meta.get("tool") or "unknown", count=len(records),
                        query=str(src_meta.get("query") or ""), required=required,
                        diagnosis=DIAGNOSIS.get(name, ""),
                        note=str(src_meta.get("note") or ""))
    manifest.finalize()  # aborts loudly if open_deals or closed_deals came back empty

    owner_names: Dict[str, str] = {}
    managers: Dict[str, str] = {}
    active: Dict[str, bool] = {}
    for u in normalize_records(loaded.get("users") or []):
        uid = str(u.get("Id") or u.get("id") or "")
        if not uid:
            continue
        name = u.get("Name") or " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x)
        owner_names[uid] = name or uid
        managers[uid] = str(u.get("ManagerId") or "") or ""
        active[uid] = truthy(u.get("IsActive", True))

    amount_field, currency_note = resolve_amount_field(ctx, normalize_records(loaded["open_deals"]))
    ctx.amount_field_used = amount_field

    open_all = build_deals(loaded["open_deals"], ctx, amount_field, owner_names)
    closed_all = build_deals(loaded["closed_deals"], ctx, amount_field, owner_names)

    field_hist = build_field_history(loaded.get("field_history") or [], ctx.crm)
    stage_hist = build_stage_history(loaded.get("stage_history") or [], ctx.crm)

    roles_by_deal: Dict[str, int] = defaultdict(int)
    for r in normalize_records(loaded.get("contact_roles") or []):
        did = str(r.get("OpportunityId") or r.get("dealId") or r.get("associatedDealId") or "")
        if did:
            roles_by_deal[did] += 1
    have_roles = bool(roles_by_deal) or any(d["contact_roles"] not in (None, "") for d in open_all)

    stage_meta = {}
    for s in normalize_records(loaded.get("stage_meta") or []):
        key = str(s.get("value") or s.get("MasterLabel") or s.get("label") or "")
        if key:
            stage_meta[key] = s
    stage_order = list(cfg.get("stage_order") or [])
    if not stage_order and stage_meta:
        stage_order = [k for k, v in sorted(stage_meta.items(), key=lambda kv: kv[1].get("order", 99))
                       if not truthy(v.get("is_closed"))]

    # -- scope ---------------------------------------------------------------
    open_scoped, open_excl = scope_filter(open_all, ctx)
    closed_scoped, closed_excl = scope_filter(closed_all, ctx)
    closed_hist = [d for d in closed_scoped
                   if d["close_date"] and history_start <= d["close_date"] < period_start]
    banked = [d for d in closed_scoped
              if d["is_won"] and d["close_date"] and period_start <= d["close_date"] <= as_of]
    banked_amount = sum(d["amount"] for d in banked)

    in_period = [d for d in open_scoped if d["close_date"] and d["close_date"] <= period_end]
    commit_buckets = set(cfg.get("commit_buckets") or ["commit"])
    commit_deals = [d for d in in_period if d["category"] in commit_buckets]
    forecast_deals = [d for d in in_period if d["category"] in (commit_buckets | {"best_case"})]

    # -- per-deal derived facts ----------------------------------------------
    push_min = int(cfg.get("push_days_threshold") or 7)
    for d in open_all + closed_all:
        ch = field_hist.get(d["id"], {})
        d["_pushes"], d["_push_days"] = count_pushes(ch.get("close_date", []), push_min)
        d["_stages_entered"] = [s for _w, s in stage_hist.get(d["id"], [])
                                if s.strip().lower() not in ctx.won_stages | ctx.lost_stages]
        if roles_by_deal:
            d["contact_roles"] = roles_by_deal.get(d["id"], 0)
        elif d["contact_roles"] not in (None, ""):
            try:
                d["contact_roles"] = int(d["contact_roles"])
            except (TypeError, ValueError):
                d["contact_roles"] = None

    # -- measured conversion (cohort-controlled by ENTERED) -------------------
    def entered_stages(d):
        seen = list(dict.fromkeys(d["_stages_entered"]))
        return seen or ([d["stage"]] if d["stage"] else [])

    def entered_categories(d):
        ch = field_hist.get(d["id"], {}).get("forecast_category", [])
        # For a closed deal, ignore the transition that happens AT close — every lost deal
        # gets dumped into Omitted on its last day, and counting that as "entered Omitted"
        # would poison the Omitted cohort with the entire loss book.
        if d["is_closed"] and d["close_date"]:
            ch = [c for c in ch if c[0] < d["close_date"]]
        vals = []
        for _w, old, new in ch:
            for v in (old, new):
                c = ctx.category(v)
                if c not in ("unmapped", "closed") and c not in vals:
                    vals.append(c)
        if not vals:
            c = ctx.category(d["category_raw"])
            vals = [c] if c not in ("unmapped", "closed") else []
        return vals

    stage_rates = cohort_rates(closed_hist, entered_stages)
    category_rates = cohort_rates(closed_hist, entered_categories)
    survivorship = cohort_rates(closed_hist, lambda d: [d["_stages_entered"][-1]] if d["_stages_entered"] else [])

    won_hist = [d for d in closed_hist if d["is_won"]]
    global_win = (len(won_hist) / len(closed_hist)) if closed_hist else 0.0

    # push penalty: measured, not assumed
    pushed = [d for d in closed_hist if d["_pushes"] >= 2]
    unpushed = [d for d in closed_hist if d["_pushes"] < 2]
    wr_pushed = (sum(1 for d in pushed if d["is_won"]) / len(pushed)) if pushed else None
    wr_unpushed = (sum(1 for d in unpushed if d["is_won"]) / len(unpushed)) if unpushed else None
    push_penalty = 1.0
    if wr_pushed is not None and wr_unpushed:
        push_penalty = clamp(wr_pushed / wr_unpushed, 0.05, 1.0)

    # -- slip distribution ----------------------------------------------------
    # "One month out, how wrong was the stated close date?" — the question a
    # forecast actually asks. Reconstructed from the close-date change log.
    slips: List[int] = []
    slip_rows: List[Dict[str, Any]] = []
    for d in closed_hist:
        ch = field_hist.get(d["id"], {}).get("close_date", [])
        if not ch or not d["close_date"]:
            continue
        stated = as_date(value_at(ch, d["close_date"] - timedelta(days=30), d["close_date"]))
        if not stated:
            continue
        slips.append((d["close_date"] - stated).days)
    slips.sort()
    slip_p25 = percentile(slips, 25) if slips else None
    slip_p50 = median(slips) if slips else None
    slip_p75 = percentile(slips, 75) if slips else None
    slip_p90 = percentile(slips, 90) if slips else None
    late = [s for s in slips if s > 0]
    slip_step = int(max(7, (slip_p75 - slip_p50))) if (slip_p75 is not None and slip_p50 is not None) else 14
    for lbl, val in (("p25", slip_p25), ("p50 (median)", slip_p50), ("p75", slip_p75), ("p90", slip_p90)):
        if val is not None:
            slip_rows.append({"Quantile": lbl, "Days late vs the date stated 30 days out": round(val, 1)})

    cycles = [(d["close_date"] - d["created_date"]).days for d in won_hist
              if d["close_date"] and d["created_date"]]
    cycle_p25 = percentile(cycles, 25) if cycles else None
    cycle_p50 = median(cycles) if cycles else None

    # -- quarter-end clustering ----------------------------------------------
    def in_last_days(d: date, n: int = 5) -> bool:
        _qs, qe = quarter_bounds(d, ctx.fy_start)
        return (qe - d).days < n

    hist_cluster = (sum(1 for d in closed_hist if d["is_won"] and d["close_date"] and in_last_days(d["close_date"]))
                    / max(len(won_hist), 1))
    open_cluster = (sum(1 for d in forecast_deals if in_last_days(d["close_date"]))
                    / max(len(forecast_deals), 1))
    excess_cluster = max(0.0, open_cluster - hist_cluster)

    # -- calibration: what did Commit actually deliver, quarter by quarter ----
    attainment_rows: List[Dict[str, Any]] = []
    attainments: List[float] = []
    q = history_start
    while q < period_start:
        qs, qe = quarter_bounds(q, ctx.fy_start)
        # "Everything that was ever called Commit FOR this quarter" — sampled weekly through
        # the quarter rather than from a single snapshot, because a deal committed in week 11
        # was still committed. This is the question a CRO is actually asking.
        samples = [qs + timedelta(days=7 * i) for i in range(((qe - qs).days // 7) + 1)]
        cohort_amt = 0.0
        landed_amt = 0.0
        n = 0
        for d in closed_scoped + open_scoped:
            ch = field_hist.get(d["id"], {})
            cats = ch.get("forecast_category", [])
            dates = ch.get("close_date", [])
            committed_for_q = False
            for t in samples:
                if d["created_date"] and d["created_date"] > t:
                    continue
                if d["is_closed"] and d["close_date"] and d["close_date"] < t:
                    break
                if ctx.category(value_at(cats, t, d["category_raw"])) not in commit_buckets:
                    continue
                stated = as_date(value_at(dates, t, d["close_date"]))
                if stated and qs <= stated <= qe:
                    committed_for_q = True
                    break
            if not committed_for_q:
                continue
            cohort_amt += d["amount"]
            n += 1
            if d["is_won"] and d["close_date"] and qs <= d["close_date"] <= qe:
                landed_amt += d["amount"]
        if cohort_amt > 0 and n >= 3:
            att = landed_amt / cohort_amt
            attainments.append(att)
            attainment_rows.append({
                "Quarter": fiscal_period({"fiscal_year_start_month": ctx.fy_start,
                                          "fiscal_year_naming": ctx.fy_naming}, qs.year, qs.month),
                "Deals committed for it": n,
                "Committed": money(cohort_amt),
                "Landed in quarter": money(landed_amt),
                "Attainment": f"{att * 100:.0f}%"})
        q = add_months(qs, 3)
    commit_attainment = (sum(attainments) / len(attainments)) if attainments else None
    commit_volatility = statistics.pstdev(attainments) if len(attainments) > 1 else None

    # repeat commits: the same deal called in two consecutive quarters
    repeat_commit = []
    for d in commit_deals:
        ch = field_hist.get(d["id"], {})
        quarters = set()
        for _w, old, _new in ch.get("close_date", []):
            od = as_date(old)
            if od and od < period_start:
                cat_then = ctx.category(value_at(ch.get("forecast_category", []), od, d["category_raw"]))
                if cat_then in commit_buckets:
                    quarters.add(quarter_bounds(od, ctx.fy_start)[0])
        if quarters:
            d["_repeat_quarters"] = sorted(x.isoformat() for x in quarters)
            repeat_commit.append(d)

    # ================================================================= scoring
    comp: Dict[str, Dict[str, Any]] = {}

    def set_comp(key: str, sub: Optional[float], detail: str) -> None:
        comp[key] = {"subscore": None if sub is None else round(clamp(sub, 0, 100), 1), "detail": detail}

    # A. date integrity
    if field_hist:
        n_f = max(len(forecast_deals), 1)
        pushed_1 = sum(1 for d in forecast_deals if d["_pushes"] >= 1) / n_f
        pushed_3 = sum(1 for d in forecast_deals if d["_pushes"] >= int(cfg.get("serial_push_count") or 3)) / n_f
        set_comp("date_integrity", 100 * (1 - 0.7 * pushed_1 - 0.3 * min(1.0, pushed_3 * 2)),
                 f"{pushed_1 * 100:.0f}% of forecast deals have been pushed at least once; "
                 f"{pushed_3 * 100:.0f}% three times or more.")
    else:
        set_comp("date_integrity", None, "No close-date change history was available, so pushes "
                                         "cannot be measured at all.")

    # B. deal evidence
    n_c = max(len(commit_deals), 1)
    has_ns = sum(1 for d in commit_deals if str(d["next_step"]).strip()) / n_c
    stale_days = int(cfg.get("stale_activity_days") or 21)
    fresh_act = sum(1 for d in commit_deals if d["last_activity"]
                    and (as_of - d["last_activity"]).days <= stale_days) / n_c
    generic = {"following up", "follow up", "check in next week", "check in", "tbd", "n/a", "call"}
    specific = sum(1 for d in commit_deals
                   if str(d["next_step"]).strip() and str(d["next_step"]).strip().lower() not in generic) / n_c
    if ctx.has_next_step_field:
        set_comp("deal_evidence", 100 * (0.45 * has_ns + 0.2 * specific + 0.35 * fresh_act),
                 f"{has_ns * 100:.0f}% of commit deals carry a next step "
                 f"({specific * 100:.0f}% a specific one); "
                 f"{fresh_act * 100:.0f}% had activity in the last {stale_days} days.")
    else:
        # No next-step field configured — the config says this team uses open tasks instead.
        # Score activity alone rather than penalising them for a field they do not have.
        set_comp("deal_evidence", 100 * fresh_act,
                 f"No next-step field is configured, so this is activity recency only: "
                 f"{fresh_act * 100:.0f}% of commit deals had activity in the last {stale_days} days.")

    # C. buying-group coverage
    if have_roles:
        thin = int(cfg.get("single_thread_max_contacts") or 1)
        known = [d for d in commit_deals if d["contact_roles"] is not None]
        multi = sum(1 for d in known if (d["contact_roles"] or 0) > thin) / max(len(known), 1)
        set_comp("buying_group", 100 * multi,
                 f"{multi * 100:.0f}% of commit deals have more than {thin} contact role(s).")
    else:
        set_comp("buying_group", None, "No contact-role or contact-association data was supplied, so "
                                       "single-threading cannot be measured.")

    # D. history depth
    depth_n = clamp(len(closed_hist) / 100.0)
    depth_q = clamp(len(attainment_rows) / 6.0)
    has_stage_hist = 1.0 if stage_hist else 0.0
    sub_d = 100 * (0.45 * depth_n + 0.3 * depth_q + 0.25 * has_stage_hist)
    set_comp("history_depth", sub_d,
             f"{len(closed_hist)} closed deals across {len(attainment_rows)} comparable quarters; "
             f"stage-transition history {'present' if stage_hist else 'ABSENT'}.")

    # E. calibration
    if commit_attainment is not None:
        acc = 1 - min(1.0, abs(1 - commit_attainment) / 0.4)
        stab = 1 - min(1.0, (commit_volatility or 0) / 0.25)
        rpt = 1 - min(1.0, (len(repeat_commit) / max(len(commit_deals), 1)) / 0.2)
        set_comp("calibration", 100 * (0.5 * acc + 0.3 * stab + 0.2 * rpt),
                 f"Commit landed at {commit_attainment * 100:.0f}% on average across "
                 f"{len(attainments)} quarters (swing ±{(commit_volatility or 0) * 100:.0f} pts); "
                 f"{len(repeat_commit)} of {len(commit_deals)} current commit deals were already "
                 f"committed in an earlier quarter.")
    else:
        set_comp("calibration", None, "Not enough forecast-category history to reconstruct what "
                                      "Commit delivered in past quarters.")

    # F. date realism
    young = [d for d in forecast_deals if d["created_date"]
             and cycle_p25 is not None and (as_of - d["created_date"]).days < cycle_p25]
    fdollars = sum(d["amount"] for d in forecast_deals) or 1.0
    young_share = sum(d["amount"] for d in young) / fdollars
    set_comp("date_realism",
             100 * (1 - 0.5 * min(1.0, excess_cluster / 0.35) - 0.5 * min(1.0, young_share / 0.4)),
             f"{open_cluster * 100:.0f}% of forecast deals close in the final 5 days of the quarter "
             f"(history: {hist_cluster * 100:.0f}%); {young_share * 100:.0f}% of forecast dollars sit on "
             f"deals younger than the 25th-percentile sales cycle"
             + (f" of {cycle_p25:.0f} days." if cycle_p25 is not None else "."))

    available_weight = sum(w for k, _l, w, _d in COMPONENTS if comp[k]["subscore"] is not None)
    raw = sum(comp[k]["subscore"] * w for k, _l, w, _d in COMPONENTS if comp[k]["subscore"] is not None)
    raw_score = (raw / available_weight) if available_weight else 0.0
    measurable_share = available_weight / 100.0
    # The multiplier is the point: a CRM you cannot measure can never score as well as one
    # you can. Dropping an unmeasurable component must never raise the score.
    integrity = round(raw_score * (0.6 + 0.4 * measurable_share), 1)

    band = ("Not forecastable" if integrity < 40 else
            "Directional only" if integrity < 60 else
            "Forecastable with named caveats" if integrity < 80 else "Board-grade")

    score_rows = [{
        "Component": lbl, "Weight": w,
        "Subscore": "n/a — not measurable" if comp[k]["subscore"] is None else f"{comp[k]['subscore']:.0f}/100",
        "Contribution": "0" if comp[k]["subscore"] is None
                        else f"{comp[k]['subscore'] * w / max(available_weight, 1):.1f}",
        "What it measures": desc, "Measured": comp[k]["detail"],
    } for k, lbl, w, desc in COMPONENTS]

    # ================================================================ findings
    doc = FindingsDoc(plugin=PLUGIN, org_name=meta.get("org_name") or profile.get("org_name") or "",
                      window={"start": history_start.isoformat(), "end": period_end.isoformat()})
    unavailable = [f"{lbl} — {comp[k]['detail']}" for k, lbl, _w, _d in COMPONENTS
                   if comp[k]["subscore"] is None]
    unavailable += [f"{name}.json was not supplied by this run"
                    for name in MEASURED_SOURCES if not (raw_dir / f"{name}.json").exists()]

    def sample(deals: Sequence[Dict[str, Any]], n: int = 8) -> List[str]:
        return [d["id"] for d in deals[:n]]

    def rows_for(deals: Sequence[Dict[str, Any]], extra=None, n: int = 12) -> List[Dict[str, Any]]:
        out = []
        for d in sorted(deals, key=lambda x: -x["amount"])[:n]:
            row = {"Deal": d["name"] or d["id"], "Owner": ctx.label(d["owner_name"]),
                   "Stage": d["stage"], "Amount": money(d["amount"]),
                   "Close date": d["close_date"].isoformat() if d["close_date"] else "—"}
            if extra:
                row.update(extra(d))
            out.append(row)
        return out

    SOQL_PUSH = ("SELECT OpportunityId, OldValue, NewValue, CreatedDate FROM OpportunityFieldHistory\n"
                 "WHERE Field = 'CloseDate' AND CreatedDate = LAST_N_DAYS:180\n"
                 "ORDER BY OpportunityId, CreatedDate")

    # 1. pushed commit deals
    pushed_commit = [d for d in forecast_deals if d["_pushes"] >= 1]
    if pushed_commit and field_hist:
        share = pct(len(pushed_commit), len(forecast_deals))
        dollars = sum(d["amount"] for d in pushed_commit)
        doc.add(Finding(
            id="commit-close-date-already-pushed",
            severity="critical" if share >= 45 else "high" if share >= 25 else "medium",
            title=f"{share:.0f}% of the forecast number sits on close dates that have already moved",
            what=f"{len(pushed_commit)} of {len(forecast_deals)} Commit/Best-Case deals in {period_label} "
                 f"({money(dollars)}) have had their close date pushed later at least once, by a median of "
                 f"{median([d['_push_days'] for d in pushed_commit]) or 0:.0f} days in total.",
            why_it_matters="A close date that has already moved is a rep's hope, not a buyer's commitment. "
                           "Every one of these dates was wrong once. The forecast treats them as if they "
                           "were right this time.",
            recommended_fix="Require a buyer-verifiable reason on every close-date change — a dated event "
                            "the buyer owns (signature date, board meeting, budget release), not 'they said "
                            "next month'. Deals pushed twice drop out of Commit automatically until the "
                            "reason is logged.",
            evidence={"count": len(pushed_commit), "value": money(dollars),
                      "sample_ids": sample(pushed_commit),
                      "rows": rows_for(pushed_commit, lambda d: {"Pushes": d["_pushes"],
                                                                 "Days pushed": d["_push_days"]}),
                      "query": SOQL_PUSH},
            effort="medium", owner_hint="Sales leadership"))

    serial_n = int(cfg.get("serial_push_count") or 3)
    serial = [d for d in forecast_deals if d["_pushes"] >= serial_n]
    if serial:
        doc.add(Finding(
            id="commit-serial-pushers",
            severity="critical",
            title=f"{nv(len(serial), 'deal', 'has', 'have')} been pushed {serial_n}+ times "
                  f"and {'is' if len(serial) == 1 else 'are'} still in the forecast",
            what=f"These {len(serial)} deals ({money(sum(d['amount'] for d in serial))}) have moved their "
                 f"close date {serial_n} or more times and are still being counted this quarter.",
            why_it_matters="A deal that has slipped three times is not a slow deal, it is a deal with no "
                           "date. Historically these convert at "
                           f"{(wr_pushed or 0) * 100:.0f}% versus {(wr_unpushed or 0) * 100:.0f}% for "
                           "deals that never moved — the forecast is counting them the same.",
            recommended_fix="Move every deal at or above the serial-push threshold out of Commit into "
                            "Pipeline and make re-entry a manager decision with a documented buyer event.",
            evidence={"count": len(serial), "sample_ids": sample(serial),
                      "rows": rows_for(serial, lambda d: {"Pushes": d["_pushes"],
                                                          "Days pushed": d["_push_days"]}),
                      "query": SOQL_PUSH},
            effort="quick", owner_hint="Sales leadership"))

    # 2. no next step
    no_ns = [d for d in commit_deals if not str(d["next_step"]).strip()] if ctx.has_next_step_field else []
    if no_ns:
        share = pct(len(no_ns), len(commit_deals))
        doc.add(Finding(
            id="commit-no-next-step",
            severity="high" if share >= 25 else "medium" if share >= 10 else "low",
            title=f"{share:.0f}% of commit deals have no next step recorded",
            what=f"{len(no_ns)} of {len(commit_deals)} Commit deals ({money(sum(d['amount'] for d in no_ns))}) "
                 f"have a blank next-step field.",
            why_it_matters="A commit with no next step is a number without a plan. There is nothing for a "
                           "manager to inspect and nothing that would tell you the deal has stopped moving.",
            recommended_fix="Make next step required to sit in Commit — validation rule, not a training "
                            "reminder — and inspect the wording, not the presence, in the weekly review.",
            evidence={"count": len(no_ns), "sample_ids": sample(no_ns), "rows": rows_for(no_ns),
                      "query": "SELECT Id, Name, Amount, CloseDate, NextStep FROM Opportunity\n"
                               "WHERE IsClosed = false AND ForecastCategoryName = 'Commit'\n"
                               "AND (NextStep = null OR NextStep = '')"},
            effort="quick", owner_hint="RevOps"))

    stale = [d for d in commit_deals if not d["last_activity"]
             or (as_of - d["last_activity"]).days > stale_days]
    if stale:
        doc.add(Finding(
            id="commit-stale-activity",
            severity="high" if pct(len(stale), max(len(commit_deals), 1)) >= 25 else "medium",
            title=f"{nv(len(stale), 'commit deal', 'has', 'have')} gone quiet for more than "
                  f"{stale_days} days",
            what=f"{money(sum(d['amount'] for d in stale))} of the commit number has had no logged "
                 f"activity in over {stale_days} days.",
            why_it_matters="Deals in the last mile are the most active deals in the pipeline. Silence at "
                           "the top of the funnel is normal; silence in Commit means the buyer stopped "
                           "and nobody told the forecast.",
            recommended_fix="Add a silence trigger to the weekly inspection: any Commit deal with no "
                            "activity past the threshold gets a named action or comes out of Commit.",
            evidence={"count": len(stale), "sample_ids": sample(stale),
                      "rows": rows_for(stale, lambda d: {"Last activity": d["last_activity"].isoformat()
                                                         if d["last_activity"] else "never"}),
                      "query": "SELECT Id, Name, Amount, LastActivityDate FROM Opportunity\n"
                               f"WHERE IsClosed = false AND ForecastCategoryName = 'Commit'\n"
                               f"AND (LastActivityDate < LAST_N_DAYS:{stale_days} OR LastActivityDate = null)"},
            effort="quick", owner_hint="Sales leadership"))

    # 3. single-threaded
    if have_roles and commit_deals:
        thin = int(cfg.get("single_thread_max_contacts") or 1)
        single = [d for d in commit_deals if d["contact_roles"] is not None and d["contact_roles"] <= thin]
        if single:
            doc.add(Finding(
                id="commit-single-threaded",
                severity="high" if pct(len(single), len(commit_deals)) >= 35 else "medium",
                title=f"{pct(len(single), len(commit_deals)):.0f}% of the commit number rests on one contact",
                what=f"{len(single)} Commit deals ({money(sum(d['amount'] for d in single))}) have "
                     f"{thin} or fewer contact roles attached.",
                why_it_matters="Single-threaded deals die when one person changes jobs, and enterprise "
                               "purchases are never one signature. This is the most common reason a "
                               "committed deal slips a full quarter.",
                recommended_fix="Set a multi-threading floor for Commit (economic buyer plus one) and "
                                "inspect contact roles, not contact counts — a second name at the same "
                                "level is not a second thread.",
                evidence={"count": len(single), "sample_ids": sample(single),
                          "rows": rows_for(single, lambda d: {"Contact roles": d["contact_roles"]}),
                          "query": "SELECT Id, Name, Amount, (SELECT ContactId, Role FROM "
                                   "OpportunityContactRoles) FROM Opportunity\n"
                                   "WHERE IsClosed = false AND ForecastCategoryName = 'Commit'"},
                effort="medium", owner_hint="Sales leadership"))

    # 4. same-quarter creation concentration
    if young and cycle_p25 is not None:
        doc.add(Finding(
            id="forecast-rests-on-young-deals",
            severity="high" if young_share >= 0.25 else "medium",
            title=f"{young_share * 100:.0f}% of the forecast is on deals younger than your fastest quartile",
            what=f"{money(sum(d['amount'] for d in young))} of the {period_label} forecast sits on "
                 f"{len(young)} deals created less than {cycle_p25:.0f} days ago — the 25th percentile of "
                 f"your measured sales cycle. Your median won deal takes {cycle_p50:.0f} days.",
            why_it_matters="These deals are being asked to close faster than 75% of everything this team "
                           "has ever won. Some will. Counting all of them is how a quarter misses.",
            recommended_fix="Hold new-in-quarter deals at Best Case until they clear the p25 cycle mark or "
                            "produce a buyer-driven reason for the compressed timeline (expiring contract, "
                            "budget deadline, outage).",
            evidence={"count": len(young), "value": money(sum(d["amount"] for d in young)),
                      "sample_ids": sample(young),
                      "rows": rows_for(young, lambda d: {"Age (days)": (as_of - d["created_date"]).days}),
                      "query": "SELECT Id, Name, Amount, CreatedDate, CloseDate FROM Opportunity\n"
                               "WHERE IsClosed = false AND ForecastCategoryName IN ('Commit','Best Case')\n"
                               "AND CreatedDate = THIS_FISCAL_QUARTER"},
            effort="medium", owner_hint="Sales leadership"))

    # 5. quarter-end clustering
    cluster_deals = [d for d in forecast_deals if in_last_days(d["close_date"])]
    if cluster_deals:
        doc.add(Finding(
            id="close-dates-cluster-on-quarter-end",
            severity="high" if excess_cluster >= 0.2 else "medium" if excess_cluster >= 0.08 else "low",
            title=f"{open_cluster * 100:.0f}% of forecast deals close in the last 5 days of the quarter",
            what=f"{len(cluster_deals)} deals ({money(sum(d['amount'] for d in cluster_deals))}) are dated "
                 f"in the final 5 days of {period_label}. Historically only {hist_cluster * 100:.0f}% of "
                 f"won deals actually closed in that window.",
            why_it_matters="Quarter-end dates are a default, not a forecast. When the stated dates cluster "
                           f"{excess_cluster * 100:.0f} points harder than reality did, the close-date "
                           "field is recording the deadline rather than the deal.",
            recommended_fix="Ask for the buyer event behind each quarter-end date. Any date that exists "
                            "because the quarter ends — rather than because something happens on the "
                            "buyer's side — moves to the first plausible date in the next period.",
            evidence={"count": len(cluster_deals), "sample_ids": sample(cluster_deals),
                      "rows": rows_for(cluster_deals),
                      "query": "SELECT Id, Name, Amount, CloseDate FROM Opportunity\n"
                               "WHERE IsClosed = false AND CloseDate = THIS_FISCAL_QUARTER\n"
                               "ORDER BY CloseDate DESC"},
            effort="medium", owner_hint="Sales leadership"))

    # 6. measured vs assigned probability / implied rate
    if stage_rates:
        rate_rows = []
        for st in (stage_order or sorted(stage_rates)):
            r = stage_rates.get(st)
            if not r:
                continue
            assigned = stage_meta.get(st, {}).get("probability")
            surv = survivorship.get(st, {}).get("rate")
            rate_rows.append({
                "Stage": st, "Closed deals that entered it": r["n"],
                "Measured win rate (entered cohort)": f"{r['rate'] * 100:.0f}%",
                "95% band": f"{r['lo'] * 100:.0f}–{r['hi'] * 100:.0f}%",
                "Survivorship rate (misleading)": f"{surv * 100:.0f}%" if surv is not None else "—",
                "Probability assigned in CRM": f"{assigned:.0f}%" if isinstance(assigned, (int, float)) else "—",
                "Gap": (f"{assigned - r['rate'] * 100:+.0f} pts"
                        if isinstance(assigned, (int, float)) else "—"),
            })
        gaps = [stage_meta.get(st, {}).get("probability", 0) - stage_rates[st]["rate"] * 100
                for st in stage_rates if isinstance(stage_meta.get(st, {}).get("probability"), (int, float))]
        worst_gap = max(gaps) if gaps else 0
        if rate_rows:
            doc.add(Finding(
                id="stage-probability-vs-measured",
                severity="critical" if worst_gap >= 30 else "high" if worst_gap >= 15 else "low",
                title=(f"Stage probabilities run up to {worst_gap:.0f} points above what your history "
                       f"measures" if worst_gap >= 5 else
                       "Measured stage conversion, cohort-controlled by stage entered"),
                what="Measured against the cohort that ENTERED each stage — not the deals sitting in it "
                     "today — conversion is materially below the probability the CRM assigns. "
                     f"Based on {len(closed_hist)} closed deals over {len(attainment_rows)} quarters.",
                why_it_matters="A weighted forecast multiplies every deal by these numbers. If the "
                               "multiplier is 30 points high, the weighted number is inflated before a "
                               "single rep exaggerates anything. The survivorship column shows the rate "
                               "you get if you measure by current stage instead — it is higher, and it "
                               "is wrong, because the deals that fell out have left the denominator.",
                recommended_fix="Reset stage probabilities to the measured entered-cohort rates and "
                                "re-measure every two quarters. If a stage's rate is not stable enough to "
                                "set a probability, that stage has no exit criteria.",
                evidence={"count": len(closed_hist), "rows": rate_rows,
                          "query": "SELECT OpportunityId, StageName, CreatedDate FROM OpportunityHistory\n"
                                   "WHERE CreatedDate = LAST_N_FISCAL_QUARTERS:8\n"
                                   "-- join to Opportunity.IsWon; count DISTINCT deals per stage ENTERED"},
                effort="project", owner_hint="RevOps"))

    # 7. thin history
    if len(closed_hist) < 100 or len(attainment_rows) < 4:
        doc.add(Finding(
            id="history-too-thin-for-rates",
            severity="high",
            title=f"Only {len(closed_hist)} closed deals across {len(attainment_rows)} comparable quarters",
            what="Conversion rates computed on this volume carry wide confidence bands — see the 95% band "
                 "column in the stage-rate table. Several stage cohorts are under the "
                 f"{cfg.get('min_cohort_n', 25)}-deal threshold where a rate is worth quoting.",
            why_it_matters="Thin history does not make a forecast impossible, it makes the range wide. "
                           "The worst/best spread in this report is widened automatically to match — "
                           "which is honest, but it means the number cannot be presented as a point.",
            recommended_fix="Extend the history window if the stage model has not changed, or accept the "
                            "wider band and forecast in ranges until four more quarters accumulate.",
            evidence={"count": len(closed_hist),
                      "rows": [{"Stage": k, "Closed deals that entered": v["n"],
                                "Rate": f"{v['rate'] * 100:.0f}%",
                                "95% band": f"{v['lo'] * 100:.0f}–{v['hi'] * 100:.0f}%",
                                "Thin?": "yes" if v["n"] < int(cfg.get("min_cohort_n") or 25) else ""}
                               for k, v in sorted(stage_rates.items(), key=lambda kv: kv[1]["n"])],
                      "query": "SELECT COUNT(Id), FISCAL_QUARTER FROM Opportunity WHERE IsClosed = true\n"
                               "GROUP BY FISCAL_QUARTER ORDER BY FISCAL_QUARTER"},
            effort="project", owner_hint="RevOps"))

    # 8. calibration / commit attainment
    if commit_attainment is not None:
        doc.add(Finding(
            id="commit-attainment-history",
            severity="critical" if abs(1 - commit_attainment) >= 0.25 else
                     "high" if abs(1 - commit_attainment) >= 0.12 else "low",
            title=f"Commit has landed at {commit_attainment * 100:.0f}% of what was called, "
                  f"not 100%",
            what=f"Across {len(attainments)} completed quarters, dollars sitting in Commit 30 days before "
                 f"quarter end converted to closed-won in that same quarter "
                 f"{commit_attainment * 100:.0f}% of the time, with a quarter-to-quarter swing of "
                 f"±{(commit_volatility or 0) * 100:.0f} points.",
            why_it_matters="This is the single number that tells you what the word 'commit' means in this "
                           "company. If it is 70%, then a $4M commit is a $2.8M expectation, and the board "
                           "has been told $4M.",
            recommended_fix=f"Either publish the commit number with the measured "
                            f"{commit_attainment * 100:.0f}% haircut applied, or change what qualifies for "
                            "Commit until the measured rate reaches the rate leadership believes it is.",
            evidence={"count": len(attainments), "rows": attainment_rows,
                      "query": "SELECT OpportunityId, OldValue, NewValue, CreatedDate FROM "
                               "OpportunityFieldHistory\nWHERE Field = 'ForecastCategoryName' "
                               "AND CreatedDate = LAST_N_FISCAL_QUARTERS:8"},
            effort="quick", owner_hint="Sales leadership"))

    if repeat_commit:
        doc.add(Finding(
            id="repeat-commit-deals",
            severity="high",
            title=f"{nv(len(repeat_commit), 'deal', 'is', 'are')} being committed for at least the "
                  f"second quarter running",
            what=f"{money(sum(d['amount'] for d in repeat_commit))} of this quarter's commit was also "
                 f"called as commit in an earlier quarter and did not land.",
            why_it_matters="A deal committed twice is evidence that the commit definition is not being "
                           "enforced. It also double-counts optimism: the same dollars have now been "
                           "promised to the board twice.",
            recommended_fix="Flag re-committed deals in the weekly review and require a different, "
                            "verifiable reason from the one given last quarter before they re-enter Commit.",
            evidence={"count": len(repeat_commit), "sample_ids": sample(repeat_commit),
                      "rows": rows_for(repeat_commit,
                                       lambda d: {"Previously committed for": ", ".join(d["_repeat_quarters"])}),
                      "query": "SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate FROM "
                               "OpportunityFieldHistory\nWHERE Field IN "
                               "('ForecastCategoryName','CloseDate') AND CreatedDate = "
                               "LAST_N_FISCAL_QUARTERS:4 ORDER BY OpportunityId, CreatedDate"},
            effort="quick", owner_hint="Sales leadership"))

    # 9. overdue open deals
    overdue = [d for d in open_scoped if d["close_date"] and d["close_date"] < as_of]
    if overdue:
        doc.add(Finding(
            id="open-deals-past-close-date",
            severity="high" if len(overdue) >= max(5, 0.05 * len(open_scoped)) else "medium",
            title=f"{nv(len(overdue), 'open deal', 'has', 'have')} a close date in the past",
            what=f"{money(sum(d['amount'] for d in overdue))} of open pipeline is dated before "
                 f"{as_of.isoformat()} and has not been updated.",
            why_it_matters="Past-dated open deals are the cheapest possible tell that the close-date field "
                           "is not maintained. They also silently inflate any 'this quarter' rollup that "
                           "filters on date range rather than on status.",
            recommended_fix="Zero-tolerance rule: an open deal may not carry a past close date. Automate a "
                            "daily reminder to the owner and escalate at 7 days.",
            evidence={"count": len(overdue), "sample_ids": sample(overdue), "rows": rows_for(overdue),
                      "query": "SELECT Id, Name, Amount, CloseDate, Owner.Name FROM Opportunity\n"
                               "WHERE IsClosed = false AND CloseDate < TODAY ORDER BY CloseDate"},
            effort="quick", owner_hint="RevOps"))

    # 10. inactive owners
    inactive_owned = [d for d in in_period if d["owner_id"] and active.get(d["owner_id"]) is False]
    if inactive_owned:
        doc.add(Finding(
            id="forecast-deals-owned-by-inactive-users",
            severity="high",
            title=f"{nv(len(inactive_owned), 'forecast deal', 'is', 'are')} owned by "
                  f"{'a deactivated user' if len(inactive_owned) == 1 else 'deactivated users'}",
            what=f"{money(sum(d['amount'] for d in inactive_owned))} in {period_label} is owned by users "
                 f"marked inactive in the CRM.",
            why_it_matters="Nobody is calling these deals. They roll up into a manager's number and into "
                           "the org total, and no human is accountable for them in the forecast call.",
            recommended_fix="Reassign on deactivation as part of offboarding, and block the forecast "
                            "submission until the orphan count is zero.",
            evidence={"count": len(inactive_owned), "sample_ids": sample(inactive_owned),
                      "rows": rows_for(inactive_owned, lambda d: {"Owner ID": d["owner_id"]}),
                      "query": "SELECT Id, Name, Amount, OwnerId, Owner.Name FROM Opportunity\n"
                               "WHERE IsClosed = false AND Owner.IsActive = false"},
            effort="quick", owner_hint="RevOps"))

    # 11. amount hygiene
    zero_amt = [d for d in in_period if not d["amount"]]
    if zero_amt:
        doc.add(Finding(
            id="forecast-deals-without-an-amount",
            severity="high",
            title=f"{nv(len(zero_amt), 'deal', 'in the forecast period has', 'in the forecast period have')} "
                  f"no amount",
            what=f"{len(zero_amt)} open deals dated inside {period_label} carry a blank or zero "
                 f"{ctx.amount_field_used}.",
            why_it_matters="They contribute nothing to the number but they appear in deal counts and "
                           "conversion rates, so every per-deal average in the business is quietly wrong.",
            recommended_fix="Require an amount to leave the first qualified stage, and backfill these "
                            "before the next submission.",
            evidence={"count": len(zero_amt), "sample_ids": sample(zero_amt), "rows": rows_for(zero_amt),
                      "query": "SELECT Id, Name, Amount, StageName FROM Opportunity\n"
                               "WHERE IsClosed = false AND (Amount = null OR Amount = 0)"},
            effort="quick", owner_hint="RevOps"))

    unmapped_cat = [d for d in in_period if d["category"] == "unmapped"]
    if unmapped_cat and str(cfg.get("methodology")).lower() in ("category", "hybrid"):
        doc.add(Finding(
            id="unmapped-forecast-category",
            severity="medium",
            title=f"{nv(len(unmapped_cat), 'in-period deal', 'carries', 'carry')} a forecast category "
                  f"this config does not map",
            what="Their category value is not listed in category_map, so they fall outside Commit, Best "
                 "Case, Pipeline and Omitted entirely.",
            why_it_matters="Anything unmapped is invisible to the roll-up. Deals disappear from the "
                           "forecast without anyone deciding they should.",
            recommended_fix="Add the missing picklist values to category_map in "
                            "~/.leanscale-gtm/forecast-agent.json, or retire the values in the CRM.",
            evidence={"count": len(unmapped_cat), "sample_ids": sample(unmapped_cat),
                      "rows": rows_for(unmapped_cat, lambda d: {"Category value": d["category_raw"]}),
                      "query": "SELECT ForecastCategoryName, COUNT(Id) FROM Opportunity\n"
                               "WHERE IsClosed = false GROUP BY ForecastCategoryName"},
            effort="quick", owner_hint="RevOps"))

    if closed_excl["unmapped_type"] or open_excl["unmapped_type"]:
        doc.add(Finding(
            id="unmapped-deal-types",
            severity="medium",
            title=f"{nv(open_excl['unmapped_type'] + closed_excl['unmapped_type'], 'deal', 'has', 'have')} "
                  f"a type this config does not recognise",
            what="Their Type value is not in type_map, so the new / expansion / renewal counting rules "
                 "could not be applied. They were counted as new business.",
            why_it_matters="How renewals and expansions count is the difference between a bookings number "
                           "and a growth number. Silently treating an unknown type as new business "
                           "overstates new business.",
            recommended_fix="Add the missing values to type_map, then re-run. Two minutes of config here "
                            "removes a systematic bias from every number in this report.",
            evidence={"count": open_excl["unmapped_type"] + closed_excl["unmapped_type"],
                      "query": "SELECT Type, COUNT(Id) FROM Opportunity GROUP BY Type"},
            effort="quick", owner_hint="RevOps"))

    if ctx.multi_currency and "not comparable" in currency_note.lower():
        doc.add(Finding(
            id="multi-currency-without-conversion",
            severity="critical",
            title="Multi-currency org with no converted-amount field for the chosen measure",
            what=currency_note,
            why_it_matters="Summing EUR and USD in one total produces a number that is not a number. "
                           "Every roll-up in this report inherits the error.",
            recommended_fix="Enable advanced currency management (or add a corporate-currency roll-up "
                            "field) and point field_map.amount_converted at it.",
            evidence={"count": len({d["currency"] for d in in_period if d["currency"]}),
                      "value": ", ".join(sorted({d["currency"] for d in in_period if d["currency"]})),
                      "query": "SELECT CurrencyIsoCode, COUNT(Id), SUM(Amount) FROM Opportunity\n"
                               "WHERE IsClosed = false GROUP BY CurrencyIsoCode"},
            effort="project", owner_hint="RevOps"))

    # ============================================================ the call
    quota_cfg = cfg.get("quota") or {}
    quota_by_owner = {str(k): float(v) for k, v in (quota_cfg.get("period_quota_by_owner") or {}).items()}
    quota_origin = "manual entry in forecast-agent.json"
    # quota.source == "crm" means the run fetched a quota object (Salesforce ForecastingQuota).
    # CRM-supplied quota wins over the manual entry, because it is the one the reps see.
    if str(quota_cfg.get("source", "")).lower() == "crm" and loaded.get("quota"):
        crm_quota: Dict[str, float] = {}
        for q in normalize_records(loaded["quota"]):
            owner = str(q.get("QuotaOwnerId") or q.get("ownerId") or q.get("Id") or "")
            amount = q.get("QuotaAmount", q.get("amount"))
            try:
                value = float(str(amount).replace(",", "").replace("$", ""))
            except (TypeError, ValueError):
                continue
            start = as_date(q.get("StartDate") or q.get("startDate"))
            if start and not (period_start <= start <= period_end):
                continue
            if owner:
                crm_quota[owner] = crm_quota.get(owner, 0.0) + value
        if crm_quota:
            quota_by_owner = crm_quota
            quota_origin = "the CRM's quota object"
    org_quota = quota_cfg.get("org_quota")
    org_quota = float(org_quota) if org_quota not in (None, "", 0) else (
        sum(quota_by_owner.values()) if quota_by_owner else None)
    if org_quota is None:
        quota_origin = "not configured"

    forecast_section: Dict[str, Any] = {}
    call_ok = mode == "forecast" and (force or integrity >= float(cfg.get("forecast_threshold") or 60)
                                      or bool(cfg.get("run_forecast_below_threshold")))

    methodology = str(cfg.get("methodology") or "category").lower()

    def p_win_for(d: Dict[str, Any]) -> Tuple[float, float, float, int, str]:
        """(point, low, high, cohort n, basis) — cohort-controlled, by their methodology."""
        use_category = methodology == "category" or (methodology == "hybrid"
                                                     and d["category"] in ("commit", "best_case"))
        if use_category and category_rates.get(d["category"]):
            r = category_rates[d["category"]]
            return r["rate"], r["lo"], r["hi"], r["n"], f"category entered: {d['category']}"
        if stage_rates.get(d["stage"]):
            r = stage_rates[d["stage"]]
            return r["rate"], r["lo"], r["hi"], r["n"], f"stage entered: {d['stage']}"
        lo, hi = wilson(len(won_hist), max(len(closed_hist), 1))
        return global_win, lo, hi, len(closed_hist), "org-wide win rate (no cohort match)"

    if mode == "forecast":
        rows: List[Dict[str, Any]] = []
        totals = {"worst": 0.0, "likely": 0.0, "best": 0.0, "raw": 0.0, "called": 0.0}
        by_owner: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"called": 0.0, "likely": 0.0, "worst": 0.0, "best": 0.0, "deals": 0})
        by_manager: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"called": 0.0, "likely": 0.0, "worst": 0.0, "best": 0.0, "deals": 0})

        for d in in_period:
            p, lo, hi, n, basis = p_win_for(d)
            penalty = push_penalty if d["_pushes"] >= 2 else 1.0
            room = (period_end - d["close_date"]).days if d["close_date"] else 0
            t_likely = clamp(ecdf(slips, room), 0.02, 0.99) if slips else 0.8
            t_worst = clamp(ecdf(slips, room - slip_step), 0.0, 0.99) if slips else 0.6
            t_best = clamp(ecdf(slips, room + slip_step), 0.05, 1.0) if slips else 0.95
            w = d["amount"] * lo * penalty * t_worst
            l = d["amount"] * p * penalty * t_likely
            b = d["amount"] * hi * t_best
            totals["worst"] += w
            totals["likely"] += l
            totals["best"] += b
            totals["raw"] += d["amount"]
            if methodology == "weighted":
                called = d["amount"] * ((d["probability"] or 0) / 100.0)
            else:
                called = d["amount"] if d["category"] in commit_buckets else 0.0
            totals["called"] += called
            key = ctx.label(d["owner_name"]) or d["owner_id"]
            mgr_id = managers.get(d["owner_id"], "")
            for bucket in (by_owner[key], by_manager[ctx.label(owner_names.get(mgr_id, "")) if mgr_id
                                                     else "Unassigned manager"]):
                bucket["called"] += called
                bucket["likely"] += l
                bucket["worst"] += w
                bucket["best"] += b
                bucket["deals"] += 1
            d["_call"] = {"p": p, "lo": lo, "hi": hi, "n": n, "basis": basis, "penalty": penalty,
                          "t_likely": t_likely, "worst": w, "likely": l, "best": b, "called": called}

        worst = banked_amount + totals["worst"]
        likely = banked_amount + totals["likely"]
        best = banked_amount + totals["best"]
        rep_called = banked_amount + totals["called"]
        delta = rep_called - likely

        open_pipeline = sum(d["amount"] for d in in_period)
        coverage_ratio = None
        if org_quota:
            remaining = max(org_quota - banked_amount, 1.0)
            coverage_ratio = round(open_pipeline / remaining, 2)

        def rollup_rows(bucket: Dict[str, Dict[str, float]], label: str) -> List[Dict[str, Any]]:
            return [{label: k, "Deals": v["deals"], "Called": money(v["called"]),
                     "Evidence-based likely": money(v["likely"]),
                     "Called minus evidence": money(v["called"] - v["likely"]),
                     "Range": f"{money(v['worst'])} – {money(v['best'])}"}
                    for k, v in sorted(bucket.items(), key=lambda kv: -kv[1]["called"])]

        owner_rows = rollup_rows(by_owner, "Rep")
        manager_rows = rollup_rows(by_manager, "Manager") if managers else []
        manager_rows.append({"Manager": "ORG TOTAL", "Deals": len(in_period),
                             "Called": money(rep_called), "Evidence-based likely": money(likely),
                             "Called minus evidence": money(delta),
                             "Range": f"{money(worst)} – {money(best)}"})

        for d in sorted([x for x in in_period if x.get("_call")],
                        key=lambda x: -x["_call"]["likely"])[:20]:
            c = d["_call"]
            rows.append({
                "Deal": d["name"] or d["id"], "Owner": ctx.label(d["owner_name"]),
                "Amount": money(d["amount"]), "Category": d["category"],
                "Cohort basis": c["basis"], "n": c["n"],
                "Win % (measured)": f"{c['p'] * 100:.0f}%",
                "Push penalty": f"×{c['penalty']:.2f}",
                "In-period %": f"{c['t_likely'] * 100:.0f}%",
                "Likely $": money(c["likely"]),
            })

        forecast_section = {
            "period": period_label,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "as_of": as_of.isoformat(),
            "methodology": methodology,
            "measure": cfg.get("forecast_measure"),
            "counting": {k: bool(v) for k, v in (cfg.get("count_types") or {}).items()},
            "produced": call_ok,
            "banked": round(banked_amount, 2),
            "worst": round(worst, 2),
            "likely": round(likely, 2),
            "best": round(best, 2),
            "rep_called": round(rep_called, 2),
            "delta": round(delta, 2),
            "delta_pct": round(pct(delta, max(rep_called, 1)), 1),
            "open_pipeline_in_period": round(open_pipeline, 2),
            "coverage_ratio": coverage_ratio,
            "quota": org_quota,
            "quota_source": quota_origin,
            "push_penalty": round(push_penalty, 3),
            "slip_step_days": slip_step,
            "deal_rows": rows,
            "owner_rows": owner_rows,
            "manager_rows": manager_rows,
            "roll_up_field": (profile.get("team_map") or {}).get("roll_up_field")
                             or "not configured — rep level only",
            "slip_rows": slip_rows,
            "assumptions": [
                f"Win probability per deal is the measured win rate of the cohort that ENTERED that "
                f"{'forecast category' if methodology != 'weighted' else 'stage'}, taken from "
                f"{len(closed_hist)} closed deals between {history_start.isoformat()} and "
                f"{period_start.isoformat()}. Not the deals currently sitting in it — that is "
                f"survivorship and it inflates every rate.",
                "Worst case uses the LOWER bound of the 95% Wilson interval on each measured rate; best "
                "case uses the upper bound. Thin cohorts therefore widen the range by themselves — "
                "no caveat has to be asserted.",
                f"Timing uses your own slip distribution ({len(slips)} closed deals): for each deal we "
                f"take the days of room between its stated close date and {period_end.isoformat()}, then "
                f"read off the share of historical closes that landed within that many days of their "
                f"stated date. Worst case shifts that lookup {slip_step} days more pessimistic (the "
                f"p75–p50 gap in your own data); best case shifts it {slip_step} days more optimistic.",
                f"Deals pushed twice or more are multiplied by {push_penalty:.2f}, which is the measured "
                f"ratio of the win rate of pushed deals to un-pushed deals in this history — not an "
                f"assumed haircut.",
                f"Closed-won already banked in {period_label} ({money(banked_amount)}) is added to all "
                f"three numbers unchanged.",
                f"Counting rules: {', '.join(k for k, v in (cfg.get('count_types') or {}).items() if v)} "
                f"count toward the number; "
                f"{', '.join(k for k, v in (cfg.get('count_types') or {}).items() if not v) or 'nothing'} "
                f"excluded. Measure = {cfg.get('forecast_measure')}. {currency_note}",
            ],
        }

        if not call_ok:
            doc.add(Finding(
                id="forecast-call-withheld",
                severity="critical",
                title=f"The call was withheld: Forecast Integrity Score is {integrity:.0f}, "
                      f"below the {cfg.get('forecast_threshold')} threshold",
                what=f"This CRM scores {integrity:.0f}/100 ({band}). A three-number call was computed but "
                     f"is not published, because it would rest on close dates and stage definitions the "
                     f"audit shows are not load-bearing.",
                why_it_matters="A precise number derived from fiction is more dangerous than no number: it "
                               "gets repeated on a board call. The findings above are the work that has to "
                               "happen before a forecast means anything.",
                recommended_fix="Fix the critical and high findings above, re-run the audit, and run the "
                                "call once the score clears the threshold. To publish anyway, pass --force "
                                "or set run_forecast_below_threshold to true in "
                                "~/.leanscale-gtm/forecast-agent.json — and put the score on the slide.",
                evidence={"value": f"{integrity:.0f}/100",
                          "count": len([f for f in doc.findings if f.severity in ("critical", "high")]),
                          "query": "-- see the audit findings above; every one carries its own query"},
                effort="project", owner_hint="RevOps"))
        else:
            gap_pct = abs(pct(delta, max(rep_called, 1)))
            doc.add(Finding(
                id="called-commit-vs-evidence",
                severity="critical" if gap_pct >= 20 else "high" if gap_pct >= 10 else "low",
                title=(f"The called number is {money(abs(delta))} "
                       f"({gap_pct:.0f}%) {'above' if delta > 0 else 'below'} the evidence-based number"),
                what=f"Reps and managers are calling {money(rep_called)} for {period_label}. Measured "
                     f"against this company's own closed history — entered-cohort conversion, its own "
                     f"slip distribution, its own penalty for pushed deals — the likely number is "
                     f"{money(likely)}, inside a range of {money(worst)} to {money(best)}.",
                why_it_matters="This delta is the deliverable. It is not an opinion about any individual "
                               "deal; it is what happens when this team's stated call is scored against "
                               "what this team has actually done. Present the range, not the point.",
                recommended_fix=("Take the delta into the forecast call deal by deal, starting with the "
                                 "rows below. Anything that cannot survive the question 'what has the "
                                 "buyer done that makes this date real?' comes out of Commit."
                                 if delta > 0 else
                                 "The called number is conservative against history. Find out whether "
                                 "that is discipline or sandbagging — the per-rep table below shows who."),
                evidence={"count": len(in_period), "value": money(delta), "rows": rows[:15],
                          "query": "SELECT Id, Name, Amount, StageName, ForecastCategoryName, CloseDate,\n"
                                   "  Owner.Name FROM Opportunity\n"
                                   "WHERE IsClosed = false AND CloseDate <= "
                                   f"{period_end.isoformat()}\nORDER BY Amount DESC"},
                effort="quick", owner_hint="Sales leadership"))

        if coverage_ratio is None:
            doc.add(Finding(
                id="no-quota-no-coverage",
                severity="low",
                title="No quota is available, so no coverage ratio was produced",
                what="Neither the plugin config nor the CRM supplied a quota for this period, so pipeline "
                     "coverage was skipped rather than computed against an invented denominator.",
                why_it_matters="Coverage is the fastest read on whether the gap is closeable at all. "
                               "Without it, the delta above tells you the number is wrong but not whether "
                               "there is enough pipeline to fix it.",
                recommended_fix="Add quota.org_quota (or per-owner quotas) to "
                                "~/.leanscale-gtm/forecast-agent.json. One line, one period, and every "
                                "future run gets coverage.",
                evidence={"count": 0, "value": "quota not configured",
                          "query": "-- Salesforce: SELECT QuotaAmount, StartDate, QuotaOwnerId FROM "
                                   "ForecastingQuota (requires Collaborative Forecasting enabled)"},
                effort="quick", owner_hint="RevOps"))

    # ================================================================== scores
    if mode == "forecast" and forecast_section.get("produced"):
        doc.add_score(Score(key="forecast_likely", label=f"Likely — {period_label}",
                            value=round(forecast_section["likely"]), unit="currency", direction_good="up",
                            context=f"Range {money(forecast_section['worst'])} – "
                                    f"{money(forecast_section['best'])}"))
        doc.add_score(Score(key="call_delta", label="Called minus evidence", value=round(delta),
                            unit="currency", direction_good="down",
                            context=f"Reps called {money(rep_called)}; history says {money(likely)}"))
        doc.add_score(Score(key="forecast_integrity", label="Forecast Integrity Score", value=integrity,
                            unit="score_0_100", direction_good="up", context=band))
        doc.add_score(Score(key="commit_at_risk", label="Commit at risk", unit="currency",
                            direction_good="down",
                            value=round(sum(d["amount"] for d in commit_deals
                                            if d["_pushes"] >= 1 or not str(d["next_step"]).strip()
                                            or (d["contact_roles"] or 99) <= 1)),
                            context="Commit dollars on pushed, next-step-less or single-threaded deals"))
        if forecast_section.get("coverage_ratio"):
            doc.add_score(Score(key="coverage_ratio", label="Pipeline coverage",
                                value=forecast_section["coverage_ratio"], unit="count",
                                direction_good="up",
                                context=f"{money(forecast_section['open_pipeline_in_period'])} open against "
                                        f"{money(max((org_quota or 0) - banked_amount, 0))} still to find"))
    else:
        doc.add_score(Score(key="forecast_integrity", label="Forecast Integrity Score", value=integrity,
                            unit="score_0_100", direction_good="up", context=band))
        doc.add_score(Score(key="commit_at_risk", label="Commit at risk", unit="currency",
                            direction_good="down",
                            value=round(sum(d["amount"] for d in commit_deals
                                            if d["_pushes"] >= 1 or not str(d["next_step"]).strip()
                                            or (d["contact_roles"] or 99) <= 1)),
                            context=f"of {money(sum(d['amount'] for d in commit_deals))} total commit"))
        doc.add_score(Score(key="measured_win_rate", label="Measured win rate",
                            value=round(global_win * 100, 1), unit="percent", direction_good="up",
                            context=f"{len(won_hist)} won of {len(closed_hist)} closed, "
                                    f"{len(attainment_rows)} quarters"))
        if commit_attainment is not None:
            doc.add_score(Score(key="commit_attainment", label="Commit actually lands at",
                                value=round(commit_attainment * 100, 1), unit="percent",
                                direction_good="up",
                                context=f"±{(commit_volatility or 0) * 100:.0f} pts quarter to quarter"))
        if org_quota:
            remaining = max(org_quota - banked_amount, 1.0)
            doc.add_score(Score(key="coverage_ratio", label="Pipeline coverage",
                                value=round(sum(d["amount"] for d in in_period) / remaining, 2),
                                unit="count", direction_good="up",
                                context=f"open in-period pipeline against {money(remaining)} still to find"))

    # Rep -> manager -> org roll-up of the commit book and what is wrong with it. Present in
    # both modes: in an audit the useful question is not "how much" but "whose".
    risk_by_rep: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"deals": 0, "dollars": 0.0, "pushed": 0, "no_next_step": 0,
                 "single_threaded": 0, "stale": 0, "at_risk": 0.0, "manager": ""})
    for d in commit_deals:
        key = ctx.label(d["owner_name"]) or d["owner_id"] or "Unassigned"
        r = risk_by_rep[key]
        r["deals"] += 1
        r["dollars"] += d["amount"]
        r["manager"] = ctx.label(owner_names.get(managers.get(d["owner_id"], ""), "")) \
            if managers.get(d["owner_id"]) else "—"
        flawed = False
        if d["_pushes"] >= 1:
            r["pushed"] += 1
            flawed = True
        if not str(d["next_step"]).strip():
            r["no_next_step"] += 1
            flawed = True
        if d["contact_roles"] is not None and d["contact_roles"] <= int(
                cfg.get("single_thread_max_contacts") or 1):
            r["single_threaded"] += 1
            flawed = True
        if not d["last_activity"] or (as_of - d["last_activity"]).days > stale_days:
            r["stale"] += 1
            flawed = True
        if flawed:
            r["at_risk"] += d["amount"]
    rollup_rows_audit = [{
        "Rep": k, "Manager": v["manager"], "Commit deals": v["deals"],
        "Commit $": money(v["dollars"]), "At risk $": money(v["at_risk"]),
        "At risk %": f"{pct(v['at_risk'], v['dollars']):.0f}%",
        "Pushed": v["pushed"], "No next step": v["no_next_step"],
        "Single-threaded": v["single_threaded"], "Quiet": v["stale"],
    } for k, v in sorted(risk_by_rep.items(), key=lambda kv: -kv[1]["at_risk"])]

    if rollup_rows_audit:
        worst_rep = rollup_rows_audit[0]
        doc.add(Finding(
            id="commit-risk-concentration",
            severity="high" if len(risk_by_rep) > 1 and (
                list(risk_by_rep.values())[0]["at_risk"] > 0) else "medium",
            title=f"The commit book by rep: {worst_rep['Rep']} carries the most at-risk dollars",
            what=f"Across {len(risk_by_rep)} reps holding commit deals, "
                 f"{money(sum(v['at_risk'] for v in risk_by_rep.values()))} of "
                 f"{money(sum(v['dollars'] for v in risk_by_rep.values()))} sits on deals with at least "
                 f"one defect — a pushed date, a missing next step, a single thread, or silence.",
            why_it_matters="Forecast problems are rarely evenly distributed. Coaching the whole team on "
                           "hygiene wastes the tenured reps' time; the table below names who to sit with "
                           "and what to ask about.",
            recommended_fix="Take the two reps at the top of this table into a deal-by-deal review before "
                            "the next submission. Everyone else gets the summary.",
            evidence={"count": len(risk_by_rep), "rows": rollup_rows_audit,
                      "query": "SELECT Owner.Name, COUNT(Id), SUM(Amount) FROM Opportunity\n"
                               "WHERE IsClosed = false AND ForecastCategoryName = 'Commit'\n"
                               "GROUP BY Owner.Name ORDER BY SUM(Amount) DESC"},
            effort="quick", owner_hint="Sales leadership"))

    doc.unavailable = unavailable
    doc.sections = {
        "mode": mode,
        "rollup": {"rows": rollup_rows_audit,
                   "roll_up_field": (profile.get("team_map") or {}).get("roll_up_field")
                                    or "not configured — rep level only"},
        "period": {"label": period_label, "start": period_start.isoformat(),
                   "end": period_end.isoformat(), "as_of": as_of.isoformat(),
                   "fiscal_year_start_month": ctx.fy_start, "naming": ctx.fy_naming},
        "integrity": {
            "score": integrity, "band": band, "raw_score": round(raw_score, 1),
            "measurable_weight": available_weight,
            "coverage_multiplier": round(0.6 + 0.4 * measurable_share, 3),
            "formula": "score = (Σ subscore×weight ÷ Σ weight of MEASURABLE components) × "
                       "(0.6 + 0.4 × measurable share of weight). The multiplier means missing "
                       "measurement can never raise the score.",
            "rows": score_rows,
            "bands": ["0–39 Not forecastable", "40–59 Directional only",
                      "60–79 Forecastable with named caveats", "80–100 Board-grade"],
        },
        "measured": {
            "win_rate": round(global_win, 4),
            "closed_deals": len(closed_hist),
            "quarters": len(attainment_rows),
            "commit_attainment": None if commit_attainment is None else round(commit_attainment, 4),
            "commit_volatility": None if commit_volatility is None else round(commit_volatility, 4),
            "push_penalty": round(push_penalty, 3),
            "win_rate_pushed": None if wr_pushed is None else round(wr_pushed, 4),
            "win_rate_unpushed": None if wr_unpushed is None else round(wr_unpushed, 4),
            "cycle_days_p25": cycle_p25, "cycle_days_median": cycle_p50,
            "slip_days": {"p25": slip_p25, "p50": slip_p50, "p75": slip_p75, "p90": slip_p90,
                          "n": len(slips), "share_late": round(len(late) / max(len(slips), 1), 3)},
            "stage_rates": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                                for kk, vv in v.items()} for k, v in stage_rates.items()},
            "category_rates": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                                   for kk, vv in v.items()} for k, v in category_rates.items()},
            "attainment_by_quarter": attainment_rows,
        },
        "scope": {
            "amount_field": ctx.amount_field_used, "currency_note": currency_note,
            "methodology": methodology, "commit_buckets": sorted(commit_buckets),
            "open_in_scope": len(open_scoped), "open_excluded": open_excl,
            "closed_in_scope": len(closed_hist), "closed_excluded": closed_excl,
            "banked_in_period": round(banked_amount, 2),
            "commit_deals": len(commit_deals),
            "commit_dollars": round(sum(d["amount"] for d in commit_deals), 2),
        },
        "forecast": forecast_section,
    }

    payload = doc.to_dict()
    payload = apply_deltas(payload, PLUGIN)
    (run_dir / "findings.json").write_text(json.dumps(payload, indent=2, default=str) + "\n",
                                           encoding="utf-8")
    # Setup's discovery and smoke-test runs pass --no-baseline so they never land in the
    # customer's evidence trail — a partial slice would corrupt the first real comparison.
    if keep_baseline:
        save_baseline(PLUGIN, payload)
    return payload


# --------------------------------------------------------------------------- cli

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="forecast-agent analysis (offline, stdlib only)")
    ap.add_argument("--raw", required=True, help="directory holding the raw/*.json this run fetched")
    ap.add_argument("--out", required=True, help="run directory to write findings.json + manifest.json into")
    ap.add_argument("--mode", choices=["audit", "forecast"], default="audit",
                    help="audit (default) scores whether a forecast is possible; forecast produces the call")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; defaults to raw/meta.json as_of, else today")
    ap.add_argument("--config", default=None,
                    help="path to a forecast-agent config JSON; defaults to ~/.leanscale-gtm/forecast-agent.json")
    ap.add_argument("--profile", default=None,
                    help="path to a profile JSON; defaults to ~/.leanscale-gtm/profile.json")
    ap.add_argument("--force", action="store_true",
                    help="produce the call even when the integrity score is below threshold")
    ap.add_argument("--no-baseline", action="store_true",
                    help="do not save a baseline snapshot (use for setup discovery and smoke tests)")
    args = ap.parse_args(argv)

    raw_dir, run_dir = Path(args.raw), Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)

    # `_user_field_map` records what the CUSTOMER actually set, as opposed to the
    # defaults layered underneath it. Ctx needs the difference: the CRM field map
    # must win over the Salesforce defaults, but lose to a deliberate override.
    if args.config:
        user_cfg = {k: v for k, v
                    in json.loads(Path(args.config).read_text(encoding="utf-8")).items()
                    if not k.startswith("_")}
        cfg = dict(DEFAULTS)
        cfg.update(user_cfg)
    else:
        user_cfg = load_plugin_config(PLUGIN)
        cfg = load_plugin_config(PLUGIN, defaults=DEFAULTS)
    cfg["_user_field_map"] = user_cfg.get("field_map") or {}

    if args.profile:
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    else:
        try:
            profile = load_profile(required=False)
        except ConfigError:
            profile = {}

    try:
        payload = analyse(raw_dir, run_dir, cfg, profile, args.mode, args.as_of, args.force,
                          keep_baseline=not args.no_baseline)
    except SourceEmptyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    scores = " · ".join(f"{s['label']}: {s['value']}" for s in payload.get("scores", []))
    print(f"mode={args.mode}  findings={len(payload.get('findings', []))}  {scores}")
    if payload.get("is_baseline_run"):
        print("Baseline run — the comparison starts on your next run.")
    print(f"wrote {run_dir / 'findings.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
