#!/usr/bin/env python3
"""
analyze.py — raw/*.json  ->  findings.json  for the lead-source plugin.

Layer 2 of the three-layer split. Pure stdlib, offline, deterministic. Claude
fetches the records through MCP and writes them to raw/; this file only
transforms local JSON. It knows nothing about Salesforce or HubSpot beyond the
field names in the config, which is why the same code audits both.

WHAT THIS MEASURES (and, just as importantly, what it does not)
--------------------------------------------------------------
This is a LEAD SOURCE OF TRUTH audit: whether the source data underneath your
channel report is capable of supporting the claims made on it. It is NOT
multi-touch attribution. It never models influence, never distributes credit,
never touches an ad platform. It answers a narrower and more useful question:
if you present a channel mix to the board on Thursday, is that mix real?

THE SOURCE INTEGRITY SCORE — 0 to 100, higher is better
-------------------------------------------------------
A weighted mean of up to five components, each itself on a 0-100 scale:

  component   weight  definition
  ---------   ------  ----------------------------------------------------------
  coverage     0.30   100 - unattributed_rate on the primary reported source
                      field. Unattributed = blank OR a placeholder value
                      ('Other', 'Unknown', 'N/A', ...).
  survival     0.25   the Lead -> Opportunity source survival rate (below).
  taxonomy     0.20   100 - the share of ATTRIBUTED records sitting on a value
                      that is either off your intended taxonomy or a
                      non-canonical member of a duplicate cluster.
  agreement    0.15   the mean of every agreement rate we could measure
                      (100 - disagreement): UTM-vs-source, plus each configured
                      field pair such as self-reported vs tracked.
  stability    0.10   the share of records whose declared first-touch source was
                      never overwritten after creation. Needs field history.

    score = round( sum(weight_i * component_i) / sum(weight_i) )   over
    AVAILABLE components only.

Components that cannot be measured are DROPPED and the remaining weights are
rescaled to sum to 1. The report always names which components were included,
because a score built from three of five components is a different number and
pretending otherwise is how a dashboard starts lying.

Bands:  85-100 trustworthy · 70-84 usable with caveats ·
        50-69 directional at best · 0-49 the channel report is fiction.

THE SURVIVAL RATE — stated precisely, because everyone measures it differently
-----------------------------------------------------------------------------
Denominator: leads that (a) converted, (b) carried an ATTRIBUTED source at the
lead level, and (c) actually produced a record at the target hop. Converted
leads with no opportunity are excluded from the Lead->Opportunity denominator
and reported separately — a contact-only conversion is not a leak.
Numerator: the target record whose source value matches the lead's on the
normalised key, so a pure casing difference counts as survival (it is a
different defect and gets its own finding). Failures split into LOST (target
blank) and CHANGED (target carries a different value).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
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
    SourceEmptyError,
    load_plugin_config,
    load_profile,
    normalize_records,
    parse_dt,
    pct,
    redact_name,
)
from lib.config import fiscal_period  # noqa: E402
from lib.crmutil import is_blank  # noqa: E402

import taxonomy as tx  # noqa: E402

PLUGIN = "lead-source"

# --------------------------------------------------------------------------- defaults

DEFAULTS: Dict[str, Any] = {
    "crm": "",
    "window_days": 540,
    "trend_bucket": "quarter",
    "min_bucket_records": 25,
    "max_evidence_rows": 50,
    "objects": {
        "primary": {"raw_file": "leads.json", "label": "Lead", "id_field": "Id",
                    "created_field": "CreatedDate"},
        "intermediate": {"raw_file": "contacts.json", "label": "Contact", "id_field": "Id",
                         "created_field": "CreatedDate"},
        "deal": {"raw_file": "opportunities.json", "label": "Opportunity", "id_field": "Id",
                 "created_field": "CreatedDate"},
    },
    "fields": {
        "reported_source": {"primary": "LeadSource", "intermediate": "LeadSource",
                            "deal": "LeadSource"},
        "first_touch": {"primary": "LeadSource"},
        "last_touch": {"primary": ""},
        "self_reported": {"primary": ""},
        "utm_source": {"primary": ""},
        "utm_medium": {"primary": ""},
        "utm_campaign": {"primary": ""},
    },
    "extra_source_fields": [],
    "intended_taxonomy": [],
    "placeholder_values": [],
    "extra_synonym_groups": {},
    "similarity_threshold": 0.88,
    "subset_min_records": 10,
    "creation_route": {
        "fields": ["Record_Creation_Route__c", "hs_object_source_label", "hs_object_source",
                   "CreatedBy.Name"],
        "rules": [
            {"route": "web_form", "matches": ["form", "web to lead", "web-to-lead",
                                              "marketing site", "landing page", "website"]},
            {"route": "import", "matches": ["import", "data loader", "dataloader", "batch",
                                            "csv", "list upload"]},
            {"route": "api", "matches": ["api", "sync", "integration", "connector",
                                         "enrichment", "workflow", "automation"]},
            {"route": "manual", "matches": ["crm ui", "crm_ui", "manual", "salesforce ui",
                                            "rep", "user"]},
        ],
        "default": "manual",
    },
    "web_routes": ["web_form"],
    "conversion": {
        "enabled": True,
        "converted_flag_field": "IsConverted",
        "converted_date_field": "ConvertedDate",
        "to_intermediate_id_field": "ConvertedContactId",
        "to_deal_id_field": "ConvertedOpportunityId",
        "converted_by_field": "ConvertedBy.Name",
        "lifecycle_progressed_field": "",
        "association_file": "",
    },
    "won": {"boolean_field": "IsWon", "stage_field": "StageName", "won_stages": ["Closed Won"]},
    "agreement_pairs": [],
    "history_file": "lead_history.json",
    "field_definitions_file": "field_definitions.json",
    "thresholds": {
        "unattributed_report_floor_pct": 2.0,
        "unattributed_critical_pct": 20.0,
        "unattributed_high_pct": 12.0,
        "unattributed_medium_pct": 6.0,
        "survival_critical_pct": 70.0,
        "survival_high_pct": 85.0,
        "utm_capture_floor_pct": 80.0,
        "disagreement_high_pct": 25.0,
        "zero_conversion_min_leads": 25,
        "route_concentration_multiple": 1.75,
        "route_min_volume_pct": 5.0,
        "duplicate_volume_high_pct": 5.0,
        "collapsed_touch_agreement_pct": 98.0,
        "overwrite_material_pct": 5.0,
        "trend_worsening_points": 3.0,
    },
    "integrity_weights": {
        "coverage": 0.30, "survival": 0.25, "taxonomy": 0.20,
        "agreement": 0.15, "stability": 0.10,
    },
}

ROLE_ORDER = ("primary", "intermediate", "deal")


# --------------------------------------------------------------------------- small helpers


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Nested merge. load_plugin_config merges shallowly, which would silently drop
    sibling keys when a customer overrides one threshold — so we merge deeply here."""
    out = dict(base)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path: Path) -> Any:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        # A truncated fetch writes valid-looking JSON that stops mid-object. The bare
        # decoder error named no file and ended twelve frames deep in the standard
        # library, so the customer could not tell which extract to re-fetch.
        raise ConfigError(
            f"{p.name} is not valid JSON — {exc.msg} at line {exc.lineno}, column "
            f"{exc.colno}.\nThat usually means the fetch was interrupted and the file "
            f"was written half-complete.\nDelete {p} and re-run the run skill's fetch "
            f"step for that source."
        ) from exc


def extract_list(payload: Any) -> List[Dict[str, Any]]:
    """Unwrap whatever envelope the MCP tool returned: a bare list, a SOQL
    {records: []}, a HubSpot {results: []}, or a list of paged envelopes."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        for key in ("records", "results", "data", "rows"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        for key in ("result", "response"):
            if isinstance(payload.get(key), dict):
                return extract_list(payload[key])
        return []
    if isinstance(payload, list):
        if payload and all(
            isinstance(p, dict) and any(k in p for k in ("records", "results", "data"))
            for p in payload
        ):
            out: List[Dict[str, Any]] = []
            for page in payload:
                out.extend(extract_list(page))
            return out
        return [r for r in payload if isinstance(r, dict)]
    return []


def load_records(raw_dir: Path, filename: str) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Reads raw/<filename>, or — for extracts too big to hold in one write —
    every raw/<stem>.partNN.json in name order. Paging a 200k-lead org into one
    JSON blob is not a thing anyone should have to do.
    """
    if not filename:
        return [], False
    path = raw_dir / filename
    if path.exists():
        return normalize_records(extract_list(read_json(path))), True
    parts = sorted(raw_dir.glob(f"{Path(filename).stem}.part*.json"))
    if not parts:
        return [], False
    rows: List[Dict[str, Any]] = []
    for part in parts:
        rows.extend(extract_list(read_json(part)))
    return normalize_records(rows), True


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "y", "t", "won")


def rows_cap(rows: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    return rows[: int(cfg.get("max_evidence_rows", 50))]


def article(word: str) -> str:
    """'a lead' but 'an opportunity'. Small thing; a report that fumbles it reads as generated."""
    return "an" if str(word)[:1].lower() in "aeiou" else "a"


def qty(n: int, singular: str, plural: str = "") -> str:
    """'1 source value' / '3 source values' — a report that cannot count is not credible."""
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


def sev_above(value: float, critical: float, high: float, medium: float) -> str:
    if value >= critical:
        return "critical"
    if value >= high:
        return "high"
    if value >= medium:
        return "medium"
    return "low"


def sev_below(value: float, critical: float, high: float, medium: float) -> str:
    if value <= critical:
        return "critical"
    if value <= high:
        return "high"
    if value <= medium:
        return "medium"
    return "low"


# --------------------------------------------------------------------------- context


class Ctx:
    """Everything the analysis functions need, assembled once."""

    def __init__(self, cfg: Dict[str, Any], profile: Dict[str, Any], raw_dir: Path):
        self.cfg = cfg
        self.profile = profile
        self.raw_dir = raw_dir
        self.thr = cfg["thresholds"]
        self.redact = bool(profile.get("redact_pii_in_reports"))
        self.crm = (cfg.get("crm") or (profile.get("crm") or {}).get("system") or "salesforce").lower()
        self.taxonomy: List[str] = [str(t) for t in cfg.get("intended_taxonomy") or []]
        self.ph_keys = tx.placeholder_keys(cfg.get("placeholder_values") or [])
        self.lexicon = tx.build_lexicon(cfg.get("extra_synonym_groups") or {})
        self.records: Dict[str, List[Dict[str, Any]]] = {}
        self.present: Dict[str, bool] = {}
        self.queries: Dict[str, Dict[str, str]] = {}
        self.history: List[Dict[str, Any]] = []
        self.picklists: Dict[str, List[str]] = {}
        self.assoc: Dict[str, str] = {}
        self.dropped_out_of_window = 0

    # -- field access ------------------------------------------------------
    def field(self, kind: str, role: str) -> str:
        return str(((self.cfg.get("fields") or {}).get(kind) or {}).get(role) or "")

    def obj(self, role: str) -> Dict[str, Any]:
        return (self.cfg.get("objects") or {}).get(role) or {}

    def label(self, role: str) -> str:
        return str(self.obj(role).get("label") or role.title())

    def plural(self, role: str) -> str:
        label = self.label(role)
        if label.endswith("y") and not label.endswith(("ay", "ey", "oy", "uy")):
            return label[:-1] + "ies"
        return label + ("es" if label.endswith(("s", "x", "ch", "sh")) else "s")

    def recs(self, role: str) -> List[Dict[str, Any]]:
        return self.records.get(role, [])

    def source_of(self, rec: Dict[str, Any], role: str) -> Any:
        return rec.get(self.field("reported_source", role))

    def attributed(self, value: Any) -> bool:
        return tx.is_attributed(value, self.ph_keys)

    def placeholder(self, value: Any) -> bool:
        return tx.is_placeholder(value, self.ph_keys)

    def name(self, value: Any) -> str:
        if is_blank(value):
            return "unknown"
        return redact_name(value) if self.redact else str(value)

    def period(self, value: Any) -> str:
        dt = parse_dt(value)
        if dt is None:
            return "unknown"
        if str(self.cfg.get("trend_bucket", "quarter")).startswith("m"):
            return dt.strftime("%Y-%m")
        return fiscal_period(self.profile or {}, dt.year, dt.month)


# --------------------------------------------------------------------------- loading


def build_ctx(args: argparse.Namespace) -> Ctx:
    if args.config:
        user_cfg = read_json(Path(args.config))
        profile = read_json(Path(args.profile)) if args.profile else load_profile(required=False)
    else:
        user_cfg = load_plugin_config(PLUGIN, defaults={})
        profile = read_json(Path(args.profile)) if args.profile else load_profile(required=True)
    cfg = deep_merge(DEFAULTS, user_cfg)
    if args.window_days:
        cfg["window_days"] = args.window_days

    raw_dir = Path(args.raw) if args.raw else Path(args.run_dir) / "raw"
    if not raw_dir.exists():
        raise ConfigError(
            f"No raw directory at {raw_dir}.\n"
            f"The :run skill writes the CRM extracts there before this script is called. "
            f"Run /lead-source:run, or pass --raw at a directory that already has them."
        )

    ctx = Ctx(cfg, profile, raw_dir)

    for role in ROLE_ORDER:
        filename = str(ctx.obj(role).get("raw_file") or "")
        records, present = load_records(raw_dir, filename)
        ctx.records[role] = records
        ctx.present[role] = present

    qpath = Path(args.queries) if args.queries else raw_dir / "_queries.json"
    if qpath.exists():
        try:
            payload = read_json(qpath)
            if isinstance(payload, dict):
                ctx.queries = {k: (v if isinstance(v, dict) else {"query": str(v)})
                               for k, v in payload.items()}
        except (OSError, json.JSONDecodeError):
            pass

    ctx.history = load_history(raw_dir, cfg, ctx)
    ctx.picklists = load_picklists(raw_dir, cfg)
    ctx.assoc = load_associations(raw_dir, cfg, ctx)
    ctx.dropped_out_of_window = apply_window(ctx)
    return ctx


def apply_window(ctx: Ctx) -> int:
    """
    Scope the primary object to records created inside the window. The :run skill
    already filters in the query; this makes the window honest when someone hands
    over a wider extract. Records with an unparseable creation date are KEPT —
    dropping records because we could not read a date would be the wrong failure.
    Downstream objects are never filtered: a lead inside the window may well have
    converted to an opportunity created outside it.
    """
    days = int(ctx.cfg.get("window_days") or 0)
    created = str(ctx.obj("primary").get("created_field") or "")
    if days <= 0 or not created:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept, dropped = [], 0
    for rec in ctx.recs("primary"):
        dt = parse_dt(rec.get(created))
        if dt is not None and dt < cutoff:
            dropped += 1
            continue
        kept.append(rec)
    ctx.records["primary"] = kept
    return dropped


def load_history(raw_dir: Path, cfg: Dict[str, Any], ctx: Ctx) -> List[Dict[str, Any]]:
    """
    Normalise the three shapes we actually see into
    {record_id, field, old_value, new_value, changed_at, changed_by}.

      Salesforce  LeadHistory / ContactHistory rows: Field, OldValue, NewValue,
                  CreatedDate, LeadId|ContactId|OpportunityId, CreatedBy.Name
      HubSpot     a flat unroll of propertiesWithHistory: objectId, property,
                  value, timestamp, sourceType/sourceId
      HubSpot     records carrying propertiesWithHistory inline (what the API
                  actually returns when you ask for history)
    """
    out: List[Dict[str, Any]] = []

    def push(record_id, field, old, new, when, who):
        if not field:
            return
        out.append({
            "record_id": str(record_id or ""), "field": str(field),
            "old_value": old, "new_value": new, "changed_at": when, "changed_by": who,
        })

    filename = str(cfg.get("history_file") or "")
    if filename and (raw_dir / filename).exists():
        for row in normalize_records(extract_list(read_json(raw_dir / filename))):
            if "Field" in row or "NewValue" in row:  # Salesforce
                rid = (row.get("LeadId") or row.get("ContactId") or row.get("OpportunityId")
                       or row.get("ParentId") or row.get("Id"))
                push(rid, row.get("Field"), row.get("OldValue"), row.get("NewValue"),
                     row.get("CreatedDate"), row.get("CreatedBy.Name") or row.get("CreatedById"))
            elif "property" in row:  # HubSpot flat unroll
                push(row.get("objectId") or row.get("object_id"), row.get("property"),
                     None, row.get("value"), row.get("timestamp"),
                     row.get("sourceId") or row.get("sourceType"))
            elif "field" in row:  # already normalised
                push(row.get("record_id"), row.get("field"), row.get("old_value"),
                     row.get("new_value"), row.get("changed_at"), row.get("changed_by"))

    # HubSpot inline propertiesWithHistory on the primary object
    for role in ROLE_ORDER:
        filename = str(ctx.obj(role).get("raw_file") or "")
        if not filename or not (raw_dir / filename).exists():
            continue
        id_field = str(ctx.obj(role).get("id_field") or "Id")
        for raw in extract_list(read_json(raw_dir / filename)):
            history = raw.get("propertiesWithHistory") or raw.get("properties_with_history")
            if not isinstance(history, dict):
                continue
            rid = raw.get("id") or raw.get(id_field)
            for prop, entries in history.items():
                for entry in entries or []:
                    if isinstance(entry, dict):
                        push(rid, prop, None, entry.get("value"), entry.get("timestamp"),
                             entry.get("sourceId") or entry.get("sourceType"))
    return out


def load_picklists(raw_dir: Path, cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    field API name -> allowed values, from whatever describe/metadata payload the
    setup skill captured. Handles the Salesforce FieldDefinition / describe shape
    and the HubSpot property `options` shape.
    """
    filename = str(cfg.get("field_definitions_file") or "")
    if not filename or not (raw_dir / filename).exists():
        return {}
    out: Dict[str, List[str]] = {}
    raw_rows = extract_list(read_json(raw_dir / filename))

    # Salesforce PicklistValueInfo comes back one row PER VALUE, keyed by
    # EntityParticleId ('Lead.LeadSource'). Fold those into field -> values first.
    particle: Dict[str, List[str]] = defaultdict(list)
    for row in raw_rows:
        pid, value = row.get("EntityParticleId"), row.get("Value")
        if pid and value is not None and row.get("IsActive", True) is not False:
            particle[str(pid).split(".")[-1]].append(str(value))
    for field, values in particle.items():
        out[field] = sorted(dict.fromkeys(values))

    for row in raw_rows:
        api = (row.get("QualifiedApiName") or row.get("DeveloperName") or row.get("name")
               or row.get("fullName") or row.get("field"))
        if not api or str(api) in out:
            continue
        values: List[str] = []
        for container in (row.get("picklistValues"), row.get("options"), row.get("values"),
                          row.get("Metadata", {}).get("valueSet", {}) if isinstance(
                              row.get("Metadata"), dict) else None):
            if isinstance(container, list):
                for item in container:
                    if isinstance(item, dict):
                        val = item.get("value") or item.get("label") or item.get("fullName")
                        if val is not None and item.get("active", True) is not False:
                            values.append(str(val))
                    elif item is not None:
                        values.append(str(item))
        if values:
            out[str(api)] = sorted(dict.fromkeys(values))
    return out


def load_associations(raw_dir: Path, cfg: Dict[str, Any], ctx: Ctx) -> Dict[str, str]:
    """
    primary record id -> deal id, for CRMs where the join is not a field on the
    record (HubSpot). Accepts the v4 batch-read shape and a flat [{from,to}] list.
    """
    filename = str((cfg.get("conversion") or {}).get("association_file") or "")
    if not filename or not (raw_dir / filename).exists():
        return {}
    payload = read_json(raw_dir / filename)
    rows = payload.get("results") if isinstance(payload, dict) else payload
    out: Dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        src = row.get("from")
        src_id = src.get("id") if isinstance(src, dict) else src
        targets = row.get("to")
        if isinstance(targets, list):
            for target in targets:
                tid = (target.get("toObjectId") or target.get("id")) if isinstance(target, dict) else target
                if src_id and tid:
                    out.setdefault(str(src_id), str(tid))
        elif targets is not None:
            tid = targets.get("id") if isinstance(targets, dict) else targets
            if src_id and tid:
                out.setdefault(str(src_id), str(tid))
    return out


# --------------------------------------------------------------------------- field inventory


def source_field_specs(ctx: Ctx) -> List[Dict[str, str]]:
    """Every field this run treats as source-bearing, deduped, with its declared role."""
    specs: List[Dict[str, str]] = []
    seen = set()

    def add(role: str, field: str, kind: str, touch: str = ""):
        if not field:
            return
        marker = (role, field)
        if marker in seen:
            for spec in specs:
                if (spec["object"], spec["field"]) == marker and kind not in spec["kinds"]:
                    spec["kinds"] += f", {kind}"
            return
        seen.add(marker)
        specs.append({
            "object": role, "field": field, "kinds": kind, "touch": touch,
            "label": f"{ctx.label(role)}.{field}",
        })

    for role in ROLE_ORDER:
        add(role, ctx.field("reported_source", role), "reported", "declared")
    add("primary", ctx.field("first_touch", "primary"), "first_touch", "first")
    add("primary", ctx.field("last_touch", "primary"), "last_touch", "last")
    add("primary", ctx.field("self_reported", "primary"), "self_reported", "self")
    for extra in ctx.cfg.get("extra_source_fields") or []:
        add(str(extra.get("object") or "primary"), str(extra.get("field") or ""), "extra")
    return [s for s in specs if ctx.recs(s["object"])]


def field_inventory(ctx: Ctx) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for spec in source_field_specs(ctx):
        records = ctx.recs(spec["object"])
        total = len(records)
        blank = placeholder = 0
        distinct = set()
        for rec in records:
            value = rec.get(spec["field"])
            if tx.is_blank_value(value):
                blank += 1
            elif ctx.placeholder(value):
                placeholder += 1
            else:
                distinct.add(tx.key(value))
        out.append({
            "object": spec["object"], "field": spec["field"], "label": spec["label"],
            "kinds": spec["kinds"], "touch": spec["touch"], "records": total,
            "blank": blank, "placeholder": placeholder,
            "unattributed": blank + placeholder,
            "unattributed_pct": pct(blank + placeholder, total),
            "distinct_values": len(distinct),
        })
    out.sort(key=lambda r: (ROLE_ORDER.index(r["object"]) if r["object"] in ROLE_ORDER else 9,
                            -r["records"]))
    return out


# --------------------------------------------------------------------------- routes / trend


def creation_route(rec: Dict[str, Any], route_cfg: Dict[str, Any]) -> str:
    saw_value = False
    for field in route_cfg.get("fields") or []:
        value = rec.get(field)
        if is_blank(value):
            continue
        saw_value = True
        text = str(value).lower()
        for rule in route_cfg.get("rules") or []:
            if any(str(m).lower() in text for m in rule.get("matches") or []):
                return str(rule.get("route") or "unknown")
    return str(route_cfg.get("default") or "manual") if saw_value else "unknown"


def route_breakdown(ctx: Ctx) -> List[Dict[str, Any]]:
    field = ctx.field("reported_source", "primary")
    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"records": 0, "blank": 0, "placeholder": 0, "distinct": set()})
    for rec in ctx.recs("primary"):
        route = creation_route(rec, ctx.cfg.get("creation_route") or {})
        bucket = buckets[route]
        bucket["records"] += 1
        value = rec.get(field)
        if tx.is_blank_value(value):
            bucket["blank"] += 1
        elif ctx.placeholder(value):
            bucket["placeholder"] += 1
        else:
            bucket["distinct"].add(tx.key(value))
    rows = []
    for route, bucket in buckets.items():
        unattributed = bucket["blank"] + bucket["placeholder"]
        rows.append({
            "route": route, "records": bucket["records"], "blank": bucket["blank"],
            "placeholder": bucket["placeholder"], "unattributed": unattributed,
            "unattributed_pct": pct(unattributed, bucket["records"]),
            "distinct_values": len(bucket["distinct"]),
        })
    rows.sort(key=lambda r: -r["records"])
    return rows


def trend(ctx: Ctx) -> List[Dict[str, Any]]:
    field = ctx.field("reported_source", "primary")
    created = str(ctx.obj("primary").get("created_field") or "CreatedDate")
    buckets: Dict[str, Dict[str, int]] = defaultdict(lambda: {"records": 0, "unattributed": 0})
    for rec in ctx.recs("primary"):
        label = ctx.period(rec.get(created))
        bucket = buckets[label]
        bucket["records"] += 1
        if not ctx.attributed(rec.get(field)):
            bucket["unattributed"] += 1
    rows = [
        {"period": label, "records": b["records"], "unattributed": b["unattributed"],
         "unattributed_pct": pct(b["unattributed"], b["records"])}
        for label, b in buckets.items() if label != "unknown"
    ]
    rows.sort(key=lambda r: _period_sort(r["period"]))
    return rows


def _period_sort(label: str) -> Tuple:
    if label.startswith("FY"):
        try:
            year, quarter = label[2:].split("-Q")
            return (int(year), int(quarter))
        except ValueError:
            return (9999, 9)
    parts = label.split("-")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except ValueError:
        return (9999, 9)


# --------------------------------------------------------------------------- conversion


def deal_id_for(rec: Dict[str, Any], ctx: Ctx) -> Optional[str]:
    conv = ctx.cfg.get("conversion") or {}
    field = str(conv.get("to_deal_id_field") or "")
    if field and not is_blank(rec.get(field)):
        return str(rec.get(field))
    rid = rec.get(str(ctx.obj("primary").get("id_field") or "Id"))
    return ctx.assoc.get(str(rid)) if rid is not None else None


def is_converted(rec: Dict[str, Any], ctx: Ctx) -> bool:
    conv = ctx.cfg.get("conversion") or {}
    flag = str(conv.get("converted_flag_field") or "")
    if flag and flag in rec and not is_blank(rec.get(flag)):
        return truthy(rec.get(flag))
    lifecycle = str(conv.get("lifecycle_progressed_field") or "")
    if lifecycle and not is_blank(rec.get(lifecycle)):
        return True
    return deal_id_for(rec, ctx) is not None


def survival(ctx: Ctx) -> Dict[str, Any]:
    """
    The hop-by-hop survival of the source value through conversion.
    See the module docstring for the exact denominator; it is the part every
    other tool gets wrong.
    """
    conv = ctx.cfg.get("conversion") or {}
    if not conv.get("enabled", True):
        return {"available": False, "reason": "conversion analysis disabled in config"}

    primary_source = ctx.field("reported_source", "primary")
    id_field = str(ctx.obj("primary").get("id_field") or "Id")
    converted_by_field = str(conv.get("converted_by_field") or "")

    targets: Dict[str, Dict[str, Any]] = {}
    for role in ("intermediate", "deal"):
        index = {}
        role_id = str(ctx.obj(role).get("id_field") or "Id")
        for rec in ctx.recs(role):
            rid = rec.get(role_id)
            if rid is not None:
                index[str(rid)] = rec
        targets[role] = index

    converted = [r for r in ctx.recs("primary") if is_converted(r, ctx)]
    eligible = [r for r in converted if ctx.attributed(r.get(primary_source))]

    hops: List[Dict[str, Any]] = []
    detail: Dict[str, Any] = {}
    for role, id_key in (("intermediate", str(conv.get("to_intermediate_id_field") or "")),
                         ("deal", "")):
        if not ctx.recs(role):
            continue
        target_source = ctx.field("reported_source", role)
        if not target_source:
            continue
        survived = survived_case_only = lost = changed = missing = no_target = 0
        changed_to: Counter = Counter()
        changed_by: Counter = Counter()
        samples: List[str] = []
        for rec in eligible:
            tid = str(rec.get(id_key)) if (id_key and not is_blank(rec.get(id_key))) else None
            if role == "deal":
                tid = deal_id_for(rec, ctx)
            if not tid:
                no_target += 1
                continue
            target = targets[role].get(str(tid))
            if target is None:
                missing += 1
                continue
            lead_value = rec.get(primary_source)
            target_value = target.get(target_source)
            if tx.is_blank_value(target_value) or ctx.placeholder(target_value):
                lost += 1
                if len(samples) < 12:
                    samples.append(str(rec.get(id_field)))
            elif tx.key(target_value) == tx.key(lead_value):
                survived += 1
                if str(target_value).strip() != str(lead_value).strip():
                    survived_case_only += 1
            else:
                changed += 1
                changed_to[str(target_value)] += 1
                if converted_by_field:
                    # Salesforce has no ConvertedBy on Lead; the user who created the
                    # opportunity IS the converting user, so look at the target first.
                    who = target.get(converted_by_field)
                    changed_by[ctx.name(who if not is_blank(who) else rec.get(converted_by_field))] += 1
                if len(samples) < 12:
                    samples.append(str(rec.get(id_field)))
        denom = survived + lost + changed
        hops.append({
            "hop": f"{ctx.label('primary')} → {ctx.label(role)}",
            "role": role,
            "eligible": denom,
            "survived": survived,
            "survived_after_normalisation_only": survived_case_only,
            "lost_blank_or_placeholder": lost,
            "changed_to_different_value": changed,
            "no_target_record": no_target,
            "target_id_not_in_extract": missing,
            "survival_pct": pct(survived, denom),
            "sample_ids": samples,
        })
        detail[role] = {
            "changed_to": changed_to.most_common(12),
            "changed_by": changed_by.most_common(12),
        }

    return {
        "available": bool(hops),
        "converted_records": len(converted),
        "converted_with_attributed_source": len(eligible),
        "converted_without_source": len(converted) - len(eligible),
        "hops": hops,
        "detail": detail,
    }


def conversion_by_value(ctx: Ctx) -> List[Dict[str, Any]]:
    """Per source value: leads, converted, opportunities, closed-won. Feeds the
    'volume but never wins' finding, which is almost always a broken mapping."""
    primary_source = ctx.field("reported_source", "primary")
    won_cfg = ctx.cfg.get("won") or {}
    deal_id_field = str(ctx.obj("deal").get("id_field") or "Id")
    deals = {str(d.get(deal_id_field)): d for d in ctx.recs("deal") if d.get(deal_id_field) is not None}

    stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"leads": 0, "converted": 0, "opportunities": 0, "won": 0})
    for rec in ctx.recs("primary"):
        value = rec.get(primary_source)
        if not ctx.attributed(value):
            continue
        row = stats[str(value)]
        row["leads"] += 1
        if is_converted(rec, ctx):
            row["converted"] += 1
        tid = deal_id_for(rec, ctx)
        deal = deals.get(str(tid)) if tid else None
        if deal is not None:
            row["opportunities"] += 1
            if is_won(deal, won_cfg):
                row["won"] += 1

    rows = [
        {"value": value, **counts,
         "lead_to_won_pct": pct(counts["won"], counts["leads"])}
        for value, counts in stats.items()
    ]
    rows.sort(key=lambda r: -r["leads"])
    return rows


def is_won(deal: Dict[str, Any], won_cfg: Dict[str, Any]) -> bool:
    boolean_field = str(won_cfg.get("boolean_field") or "")
    if boolean_field and boolean_field in deal and not is_blank(deal.get(boolean_field)):
        return truthy(deal.get(boolean_field))
    stage_field = str(won_cfg.get("stage_field") or "")
    stage = deal.get(stage_field) if stage_field else None
    if not is_blank(stage):
        return tx.key(stage) in {tx.key(s) for s in won_cfg.get("won_stages") or []}
    return False


# --------------------------------------------------------------------------- utm / agreement


def utm_analysis(ctx: Ctx) -> Dict[str, Any]:
    utm_source_field = ctx.field("utm_source", "primary")
    utm_medium_field = ctx.field("utm_medium", "primary")
    utm_campaign_field = ctx.field("utm_campaign", "primary")
    if not (utm_source_field or utm_medium_field):
        return {"available": False, "reason": "no UTM fields configured"}

    source_field = ctx.field("reported_source", "primary")
    web_routes = {str(r) for r in ctx.cfg.get("web_routes") or []}
    route_cfg = ctx.cfg.get("creation_route") or {}

    web = web_with_utm = 0
    total_with_utm = 0
    utm_present_source_blank = 0
    comparable = disagree = 0
    pairs: Counter = Counter()
    samples: List[str] = []
    id_field = str(ctx.obj("primary").get("id_field") or "Id")

    for rec in ctx.recs("primary"):
        utm_source = rec.get(utm_source_field) if utm_source_field else None
        utm_medium = rec.get(utm_medium_field) if utm_medium_field else None
        has_utm = not (tx.is_blank_value(utm_source) and tx.is_blank_value(utm_medium))
        if has_utm:
            total_with_utm += 1
        if creation_route(rec, route_cfg) in web_routes:
            web += 1
            if has_utm:
                web_with_utm += 1
        source_value = rec.get(source_field)
        if has_utm and not ctx.attributed(source_value):
            utm_present_source_blank += 1
        if not has_utm or not ctx.attributed(source_value):
            continue
        utm_group = tx.resolve_channel([utm_medium, utm_source], ctx.lexicon)
        source_group = tx.resolve_channel([source_value], ctx.lexicon)
        if not utm_group or not source_group:
            continue
        comparable += 1
        if utm_group != source_group:
            disagree += 1
            pairs[(str(source_value), f"{utm_source or ''}/{utm_medium or ''}".strip("/"))] += 1
            if len(samples) < 12:
                samples.append(str(rec.get(id_field)))

    return {
        "available": True,
        "utm_source_field": utm_source_field,
        "utm_medium_field": utm_medium_field,
        "utm_campaign_field": utm_campaign_field,
        "web_created_records": web,
        "web_created_with_utm": web_with_utm,
        "web_capture_pct": pct(web_with_utm, web),
        "records_with_utm": total_with_utm,
        "utm_present_source_unattributed": utm_present_source_blank,
        "comparable_records": comparable,
        "disagreements": disagree,
        "disagreement_pct": pct(disagree, comparable),
        "top_pairs": [{"Source field says": a, "UTM says": b, "Records": n}
                      for (a, b), n in pairs.most_common(20)],
        "sample_ids": samples,
    }


def agreement_pairs(ctx: Ctx) -> List[Dict[str, Any]]:
    """
    Generic two-field comparison. Covers self-reported vs tracked, and on HubSpot
    the automatic hs_analytics_source vs the manually-set original-source
    property — the same arithmetic, a different story.
    """
    configured = list(ctx.cfg.get("agreement_pairs") or [])
    self_field = ctx.field("self_reported", "primary")
    if self_field and not any(str(p.get("a")) == self_field or str(p.get("b")) == self_field
                              for p in configured):
        configured.append({
            "object": "primary", "a": ctx.field("reported_source", "primary"), "b": self_field,
            "kind": "self_reported",
            "label": f"Tracked source vs self-reported ({self_field})",
        })

    out: List[Dict[str, Any]] = []
    for spec in configured:
        role = str(spec.get("object") or "primary")
        a_field, b_field = str(spec.get("a") or ""), str(spec.get("b") or "")
        if not a_field or not b_field or not ctx.recs(role):
            continue
        both = exact_disagree = channel_comparable = channel_disagree = 0
        pairs: Counter = Counter()
        samples: List[str] = []
        id_field = str(ctx.obj(role).get("id_field") or "Id")
        for rec in ctx.recs(role):
            a_value, b_value = rec.get(a_field), rec.get(b_field)
            if not ctx.attributed(a_value) or not ctx.attributed(b_value):
                continue
            both += 1
            if tx.key(a_value) != tx.key(b_value):
                exact_disagree += 1
            a_group = tx.resolve_channel([a_value], ctx.lexicon)
            b_group = tx.resolve_channel([b_value], ctx.lexicon)
            if a_group and b_group:
                channel_comparable += 1
                if a_group != b_group:
                    channel_disagree += 1
                    pairs[(str(a_value), str(b_value))] += 1
                    if len(samples) < 12:
                        samples.append(str(rec.get(id_field)))
        if not both:
            continue
        out.append({
            "object": role, "a": a_field, "b": b_field,
            "kind": str(spec.get("kind") or "generic"),
            "label": str(spec.get("label") or f"{a_field} vs {b_field}"),
            "records_with_both": both,
            "exact_disagreements": exact_disagree,
            "exact_disagreement_pct": pct(exact_disagree, both),
            "channel_comparable": channel_comparable,
            "channel_disagreements": channel_disagree,
            "channel_disagreement_pct": pct(channel_disagree, channel_comparable),
            "top_pairs": [{"Field A": a, "Field B": b, "Records": n} for (a, b), n in pairs.most_common(20)],
            "sample_ids": samples,
        })
    return out


# --------------------------------------------------------------------------- touch / history


def overwrites(ctx: Ctx, fields: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """
    Per field: how many records had the value changed after creation.

    Field matching is case-insensitive on purpose. Salesforce returns
    LeadHistory.Field for standard fields with a lowercase initial ('leadSource',
    not 'LeadSource'), and silently matching nothing here would report a clean
    first-touch field that is in fact being overwritten daily.
    """
    wanted = {f.lower(): f for f in fields if f}
    if not wanted or not ctx.history:
        return {}
    seen: Dict[str, Dict[str, List[Any]]] = defaultdict(lambda: defaultdict(list))
    for entry in ctx.history:
        canonical = wanted.get(str(entry["field"]).lower())
        if canonical:
            seen[canonical][entry["record_id"]].append(entry)
    out: Dict[str, Dict[str, Any]] = {}
    for field, by_record in seen.items():
        changed_records = []
        transitions: Counter = Counter()
        for rid, entries in by_record.items():
            values = [e["new_value"] for e in entries if not tx.is_blank_value(e["new_value"])]
            distinct = list(dict.fromkeys(tx.key(v) for v in values))
            if len(entries) > 1 or len(distinct) > 1 or any(
                    not tx.is_blank_value(e["old_value"]) for e in entries):
                changed_records.append(rid)
                ordered = sorted(entries, key=lambda e: str(e.get("changed_at") or ""))
                first, last = ordered[0], ordered[-1]
                origin = first["old_value"] if not tx.is_blank_value(first["old_value"]) else first["new_value"]
                if tx.key(origin) != tx.key(last["new_value"]):
                    transitions[(str(origin), str(last["new_value"]))] += 1
        out[field] = {
            "records_with_history": len(by_record),
            "records_overwritten": len(changed_records),
            "sample_ids": changed_records[:12],
            "top_transitions": [{"From": a, "To": b, "Records": n}
                                for (a, b), n in transitions.most_common(15)],
        }
    return out


def touch_analysis(ctx: Ctx) -> Dict[str, Any]:
    first_field = ctx.field("first_touch", "primary")
    last_field = ctx.field("last_touch", "primary")
    reported = ctx.field("reported_source", "primary")
    records = ctx.recs("primary")

    result: Dict[str, Any] = {
        "first_touch_field": first_field,
        "last_touch_field": last_field,
        "reported_field": reported,
        "same_field_for_both": bool(first_field and first_field == last_field),
        "reported_is_first_touch": bool(first_field and reported == first_field),
    }

    if first_field and last_field and first_field != last_field:
        both = agree = 0
        for rec in records:
            a, b = rec.get(first_field), rec.get(last_field)
            if not ctx.attributed(a) or not ctx.attributed(b):
                continue
            both += 1
            if tx.key(a) == tx.key(b):
                agree += 1
        result["pair_records"] = both
        result["pair_agreements"] = agree
        result["pair_agreement_pct"] = pct(agree, both)

    watched = list(dict.fromkeys(f for f in (first_field, reported) if f))
    result["overwrites"] = overwrites(ctx, watched)
    result["history_record_coverage"] = len({e["record_id"] for e in ctx.history})
    if first_field and first_field in result["overwrites"]:
        stats = result["overwrites"][first_field]
        result["first_touch_overwritten"] = stats["records_overwritten"]
        result["first_touch_overwritten_pct"] = pct(stats["records_overwritten"], len(records))
    return result


def utm_overwrites(ctx: Ctx) -> Dict[str, Any]:
    fields = [ctx.field(k, "primary") for k in ("utm_source", "utm_medium", "utm_campaign")]
    stats = overwrites(ctx, fields)
    total = sum(s["records_overwritten"] for s in stats.values())
    return {"by_field": stats, "records_overwritten": total,
            "records_overwritten_pct": pct(total, len(ctx.recs("primary")))}


# --------------------------------------------------------------------------- score


def integrity_score(ctx: Ctx, parts: Dict[str, Optional[float]]) -> Dict[str, Any]:
    weights = ctx.cfg.get("integrity_weights") or {}
    rows: List[Dict[str, Any]] = []
    available = {k: v for k, v in parts.items() if v is not None}
    total_weight = sum(float(weights.get(k, 0)) for k in available) or 1.0
    score = 0.0
    for key_name in ("coverage", "survival", "taxonomy", "agreement", "stability"):
        weight = float(weights.get(key_name, 0))
        value = parts.get(key_name)
        if value is None:
            rows.append({"Component": key_name, "Weight": f"{weight:.0%}", "Score": "not measured",
                         "Contribution": "—"})
            continue
        effective = weight / total_weight
        score += effective * float(value)
        rows.append({"Component": key_name, "Weight": f"{weight:.0%}",
                     "Score": f"{float(value):.1f}",
                     "Contribution": f"{effective * float(value):.1f} "
                                     f"(reweighted to {effective:.0%})"})
    value = int(round(score))
    if value >= 85:
        band = "Trustworthy — the channel mix you present is defensible."
    elif value >= 70:
        band = "Usable with caveats — directionally right, with known holes."
    elif value >= 50:
        band = "Directional at best — do not make budget decisions on this."
    else:
        band = "The channel report is fiction. Fix the capture layer before the next board deck."
    return {"score": value, "band": band, "components": rows,
            "components_measured": sorted(available), "components_missing":
                sorted(k for k, v in parts.items() if v is None)}


# --------------------------------------------------------------------------- findings


def build_findings(ctx: Ctx, sections: Dict[str, Any], window: Dict[str, str],
                   org_name: str) -> FindingsDoc:
    doc = FindingsDoc(plugin=PLUGIN, window=window, org_name=org_name)
    thr = ctx.thr
    cfg = ctx.cfg
    primary_label = ctx.label("primary")
    primary_field = ctx.field("reported_source", "primary")
    primary_total = len(ctx.recs("primary"))
    inventory = sections["field_inventory"]
    head = next((r for r in inventory if r["object"] == "primary" and r["field"] == primary_field), None)
    query = _query_for(ctx, "primary")

    # ---- 1. unattributed rate on the reported field
    if head and head["unattributed_pct"] >= thr["unattributed_report_floor_pct"]:
        rate = head["unattributed_pct"]
        doc.add(Finding(
            id="unattributed-source-rate",
            severity=sev_above(rate, thr["unattributed_critical_pct"], thr["unattributed_high_pct"],
                               thr["unattributed_medium_pct"]),
            title=f"{rate}% of {ctx.plural('primary').lower()} carry no usable source on {primary_field}",
            what=(f"{head['unattributed']:,} of {head['records']:,} {primary_label.lower()} records are "
                  f"unattributed on {primary_field}: {head['blank']:,} blank and "
                  f"{head['placeholder']:,} sitting on a placeholder value such as 'Other' or 'Unknown'. "
                  f"Every other source field on the objects in scope is broken out in the table below."),
            why_it_matters=(
                f"A channel report built on this field silently drops {rate}% of the volume, and that "
                f"missing slice is not random — it concentrates in whichever capture route is broken. "
                f"Any percentage mix you present is computed on the {100 - rate:.1f}% that happened to "
                f"get filled in."),
            recommended_fix=(
                f"Split the fix by cause: make {primary_field} required at the capture points that "
                f"produce blanks, and remove the placeholder values from the picklist entirely so the "
                f"field cannot be satisfied with a shrug. Backfill is a separate project — decide "
                f"whether the history is worth reconstructing before anyone starts."),
            evidence={"count": head["unattributed"], "rows": rows_cap(
                [{"Field": r["label"], "Role": r["kinds"], "Records": f"{r['records']:,}",
                  "Blank": f"{r['blank']:,}", "Placeholder": f"{r['placeholder']:,}",
                  "Unattributed %": r["unattributed_pct"], "Distinct values": r["distinct_values"]}
                 for r in inventory], cfg), "query": query},
            effort="project", owner_hint="Marketing Ops"))

    # ---- 2. orphans: no source at all
    if head and head["blank"] > 0 and pct(head["blank"], head["records"]) >= 2.0:
        orphan_ids = [str(r.get(str(ctx.obj("primary").get("id_field") or "Id")))
                      for r in ctx.recs("primary") if tx.is_blank_value(r.get(primary_field))][:12]
        blank_rate = pct(head["blank"], head["records"])
        doc.add(Finding(
            id="orphan-records-no-source",
            severity=sev_above(blank_rate, 15.0, 8.0, 3.0),
            title=(f"{head['blank']:,} {ctx.plural('primary').lower() if head['blank'] != 1 else ctx.label('primary').lower()} "
                   f"{'has' if head['blank'] == 1 else 'have'} no source value at all"),
            what=(f"{head['blank']:,} records ({blank_rate}%) have {primary_field} completely empty — "
                  f"not 'Other', not 'Unknown', empty. These records entered the system through a path "
                  f"that never set the field."),
            why_it_matters=("A blank is different from an 'Other' and needs a different fix. Blanks point "
                            "at a capture path — a form, an import, an integration — that was never wired "
                            "to write the field at all, which means the leak is ongoing, not historical."),
            recommended_fix=("Take the sample IDs, look at how each one was created, and you will find "
                             "two or three paths responsible for nearly all of them. Fix those paths "
                             "first; that stops the bleeding before anyone argues about backfill."),
            evidence={"count": head["blank"], "sample_ids": orphan_ids, "query": query},
            effort="medium", owner_hint="Marketing Ops"))

    # ---- 3. route concentration
    routes = sections["routes"]
    overall = head["unattributed_pct"] if head else 0.0
    material = [r for r in routes
                if r["records"] >= max(20, primary_total * thr["route_min_volume_pct"] / 100.0)]
    worst = max(material, key=lambda r: r["unattributed_pct"], default=None)
    if worst and overall > 0 and worst["unattributed_pct"] >= overall * thr["route_concentration_multiple"]:
        doc.add(Finding(
            id="unattributed-route-concentration",
            severity="high" if worst["unattributed_pct"] >= 40 else "medium",
            title=f"The missing source concentrates in one capture route: {worst['route']}",
            what=(f"Records created via '{worst['route']}' are {worst['unattributed_pct']}% unattributed "
                  f"against a {overall}% overall rate — {worst['unattributed']:,} of "
                  f"{worst['records']:,} records. The rot is not spread evenly across the database."),
            why_it_matters=("This is the difference between a data-quality programme and a two-hour fix. "
                            "One route is producing most of the hole; every other route is comparatively "
                            "healthy, so fixing that one path recovers most of the loss."),
            recommended_fix=(f"Trace how '{worst['route']}' records are created and make the source field "
                             f"non-optional on that path specifically. Do not roll out a company-wide "
                             f"data-quality initiative for a problem that lives in one integration."),
            evidence={"count": worst["unattributed"], "rows": rows_cap(
                [{"Creation route": r["route"], "Records": f"{r['records']:,}",
                  "Blank": f"{r['blank']:,}", "Placeholder": f"{r['placeholder']:,}",
                  "Unattributed %": r["unattributed_pct"], "Distinct values": r["distinct_values"]}
                 for r in routes], cfg), "query": query},
            effort="quick", owner_hint="Marketing Ops"))

    # ---- 4. trend
    trend_rows = sections["trend"]
    usable = [r for r in trend_rows if r["records"] >= cfg.get("min_bucket_records", 25)]
    if len(usable) >= 4:
        half = len(usable) // 2
        early = sum(r["unattributed"] for r in usable[:half]) / max(
            1, sum(r["records"] for r in usable[:half])) * 100
        late = sum(r["unattributed"] for r in usable[half:]) / max(
            1, sum(r["records"] for r in usable[half:])) * 100
        drift = round(late - early, 1)
        if drift >= thr["trend_worsening_points"]:
            doc.add(Finding(
                id="unattributed-trend-worsening",
                severity="high" if drift >= 10 else "medium",
                title=f"Source coverage is getting worse, not better: +{drift} points",
                what=(f"The unattributed rate on {primary_field} ran at {early:.1f}% across the earlier "
                      f"half of the window and {late:.1f}% across the recent half. Period by period, "
                      f"the table below shows the drift."),
                why_it_matters=("A worsening trend means whatever broke is still broken and is still "
                                "taking on new records. It also means year-over-year channel comparisons "
                                "are measuring your data collection, not your demand."),
                recommended_fix=("Find what changed at the point the rate started climbing — a new form, "
                                 "a new integration, a picklist edit, a routing change. The period column "
                                 "gives you the window to look in."),
                evidence={"count": sum(r["unattributed"] for r in usable),
                          "rows": rows_cap([{"Period": r["period"], "Records": f"{r['records']:,}",
                                             "Unattributed": f"{r['unattributed']:,}",
                                             "Unattributed %": r["unattributed_pct"]}
                                            for r in trend_rows], cfg),
                          "query": query},
                effort="medium", owner_hint="Marketing Ops"))

    # ---- 5. duplicate / near-duplicate values
    taxonomy_section = sections["taxonomy"]
    merge_clusters = [c for c in taxonomy_section["clusters"] if c["tier"] != "placeholder"]
    if merge_clusters:
        movable = sum(c["non_canonical_records"] for c in merge_clusters)
        share = pct(movable, taxonomy_section["attributed_records"])
        doc.add(Finding(
            id="duplicate-source-values",
            severity="high" if share >= thr["duplicate_volume_high_pct"] else "medium",
            title=(f"{qty(len(merge_clusters), 'group')} of source values look like the same "
                   f"channel spelled differently"),
            what=(f"{taxonomy_section['distinct_values']} distinct attributed values sit in "
                  f"{primary_field}. {movable:,} records ({share}% of attributed volume) are on a value "
                  f"that a candidate merge would move. Every group below is a PROPOSAL for a human to "
                  f"confirm or reject — nothing was merged, and the record count behind each member is "
                  f"shown so you can judge the blast radius."),
            why_it_matters=("Split values quietly halve a channel. Whichever spelling has the most volume "
                            "wins the report, and the rest of the channel's pipeline is scattered across "
                            "rows nobody looks at — which is how a channel gets defunded for having a bad "
                            "quarter it did not actually have."),
            recommended_fix=("Work the table top-down: high-confidence typographic groups are a picklist "
                             "cleanup plus a bulk update, and the low-confidence semantic ones need a "
                             "decision from whoever owns the channel definitions. Fix the picklist "
                             "before the backfill or the variants come straight back."),
            evidence={"count": movable,
                      "rows": rows_cap(taxonomy_section["proposed_mapping"], cfg),
                      "query": query},
            effort="medium", owner_hint="Marketing Ops"))

    # ---- 6. off-taxonomy values
    off = taxonomy_section["off_taxonomy"]
    if ctx.taxonomy and off:
        off_records = sum(r["records"] for r in off)
        variants = [r for r in off if r["variant_of"]]
        novel = [r for r in off if not r["variant_of"]]
        doc.add(Finding(
            id="off-taxonomy-values",
            severity="high" if pct(off_records, taxonomy_section["attributed_records"]) >= 15 else "medium",
            title=(f"{qty(len(off), 'value')} in {primary_field} "
                   f"{'is' if len(off) == 1 else 'are'} not in the taxonomy you told us you have"),
            what=(f"Your intended taxonomy has {len(ctx.taxonomy)} values. The field carries "
                  f"{taxonomy_section['distinct_values']} distinct attributed values, of which {len(off)} "
                  f"({off_records:,} records) are not on your list: {len(variants)} are a different "
                  f"spelling of a value you do have, and {len(novel)} are values you never defined."),
            why_it_matters=("Anything not in the taxonomy is invisible to a report grouped by the "
                            "taxonomy, or it lands in an 'other' bucket that grows every quarter. The "
                            "two kinds need different fixes, which is why they are separated here."),
            recommended_fix=("Spelling variants: fix at the picklist and validation layer, then bulk "
                             "update. Values you never defined: decide whether the taxonomy is out of "
                             "date — often it is, and the reps are describing something real."),
            evidence={"count": off_records, "rows": rows_cap(
                [{"Value": r["value"], "Records": f"{r['records']:,}",
                  "Spelling variant of": r["variant_of"] or "— not in your taxonomy at all —"}
                 for r in off], cfg), "query": query},
            effort="medium", owner_hint="Marketing Ops"))

    # ---- 7. off-picklist (free-text pollution)
    pollution = taxonomy_section.get("off_picklist") or []
    if pollution:
        polluted = sum(r["records"] for r in pollution)
        doc.add(Finding(
            id="off-picklist-values",
            severity="high" if len(pollution) >= 10 else "medium",
            title=(f"{qty(len(pollution), 'value')} in {primary_field} "
                   f"{'is' if len(pollution) == 1 else 'are'} not in the field's own picklist"),
            what=(f"{polluted:,} records carry a value that does not appear in the picklist metadata for "
                  f"{primary_field}. Salesforce and HubSpot both allow this through the API and through "
                  f"imports, so the picklist is a suggestion, not a constraint."),
            why_it_matters=("Off-picklist values are the entry point for the whole taxonomy problem: they "
                            "arrive from integrations and list uploads that nobody governs, and they will "
                            "keep arriving after you finish cleaning up."),
            recommended_fix=("Turn on strict picklist enforcement for this field, then audit the "
                             "integrations that were writing the off-list values and give each one an "
                             "explicit mapping. Restricted picklists are the only durable fix."),
            evidence={"count": polluted, "rows": rows_cap(
                [{"Off-picklist value": r["value"], "Records": f"{r['records']:,}"} for r in pollution],
                cfg), "query": query},
            effort="quick", owner_hint="RevOps"))

    # ---- 8/9. conversion survival
    surv = sections["survival"]
    if surv.get("available") and surv["hops"]:
        rows = [{"Hop": h["hop"], "Eligible": f"{h['eligible']:,}", "Survived": f"{h['survived']:,}",
                 "Lost (blank/placeholder)": f"{h['lost_blank_or_placeholder']:,}",
                 "Changed to another value": f"{h['changed_to_different_value']:,}",
                 "Survival %": h["survival_pct"]} for h in surv["hops"]]
        final = surv["hops"][-1]
        rate = final["survival_pct"]
    if surv.get("available") and surv["hops"] and not surv["hops"][-1]["eligible"]:
        # Nothing converted into the target object with a source to carry, so there is
        # no survival to measure. pct() correctly returns 0.0 on a zero denominator, but
        # rendering that as "Only 0.0% survive" and scoring it critical states a
        # catastrophe the data never showed — an unmeasured hop is not a failed one.
        final = surv["hops"][-1]
        doc.add(Finding(
            id="source-survival-unmeasurable",
            severity="low",
            title=f"Source survival to {final['hop'].split('→')[-1].strip()} cannot be measured yet",
            what=(f"No converted {ctx.plural('primary').lower()} carrying a real source produced "
                  f"{article(ctx.label(final['role']))} {ctx.label(final['role']).lower()} in this "
                  f"window, so the survival denominator is zero. "
                  f"{final['no_target_record']:,} converted without producing a target record."),
            why_it_matters=("This is an absence of evidence, not evidence of a problem. Reported as a "
                            "0% survival rate it would look like the worst finding in the audit while "
                            "actually meaning the window was too short or conversion is not running."),
            recommended_fix=("Widen the window until converted records with a source appear, or confirm "
                             "that conversion genuinely produces no downstream record — which is its own "
                             "finding, in a different report."),
            evidence={"count": 0, "rows": [{"hop": final["hop"], "eligible": 0,
                                            "converted_without_target": final["no_target_record"]}]},
            effort="quick",
            owner_hint="RevOps",
        ))
    elif surv.get("available") and surv["hops"]:
        doc.add(Finding(
            id="source-survival-conversion",
            severity=sev_below(rate, thr["survival_critical_pct"], thr["survival_high_pct"], 95.0),
            title=f"Only {rate}% of source values survive the trip to {final['hop'].split('→')[-1].strip()}",
            what=(f"Of {surv['converted_with_attributed_source']:,} converted {ctx.plural('primary').lower()} "
                  f"that carried a real source, {final['survived']:,} of {final['eligible']:,} arrive at "
                  f"the {ctx.label(final['role']).lower()} with the same source. "
                  f"{final['lost_blank_or_placeholder']:,} arrive blank or on a placeholder and "
                  f"{final['changed_to_different_value']:,} arrive carrying a different value. "
                  f"{final['no_target_record']:,} converted without producing "
                  f"{article(ctx.label(final['role']))} {ctx.label(final['role']).lower()} at all "
                  f"and are excluded from the denominator."
                  + (f" {final['survived_after_normalisation_only']:,} of the survivors only match once "
                     f"you ignore casing and spacing, so they are counted as survived here but they are "
                     f"a duplicate-value problem in their own right."
                     if final["survived_after_normalisation_only"] else "")),
            why_it_matters=("This is the finding most teams have never measured, and it is the one that "
                            "breaks the report that matters. Pipeline and revenue are reported at the "
                            "opportunity level; if source does not survive conversion, then every "
                            "revenue-by-channel number is built on whatever value happened to land on "
                            "the opportunity, not on where the buyer actually came from."),
            recommended_fix=("Make the source field on the downstream object read-only and populated by "
                             "automation at conversion, sourced from the lead. Then re-run this and watch "
                             "the number move — it is the cleanest before/after in the whole audit."),
            evidence={"count": final["lost_blank_or_placeholder"] + final["changed_to_different_value"],
                      "rows": rows, "sample_ids": final["sample_ids"],
                      "query": _query_for(ctx, "conversion") or query},
            effort="medium", owner_hint="RevOps"))

        detail = (surv.get("detail") or {}).get(final["role"], {})
        changed_to = detail.get("changed_to") or []
        changed_by = detail.get("changed_by") or []
        if final["changed_to_different_value"] >= 10 and changed_to:
            top_value, top_count = changed_to[0]
            dom = pct(top_count, final["changed_to_different_value"])
            user_line = ""
            if changed_by:
                top_user, user_count = changed_by[0]
                user_share = pct(user_count, final["changed_to_different_value"])
                if user_share >= 25:
                    user_line = (f" {user_share}% of the overwrites happened on conversions performed by "
                                 f"{top_user}, which is the signature of a per-user default rather than "
                                 f"a rule.")
            doc.add(Finding(
                id="converted-source-overwritten",
                severity="high" if dom >= 40 else "medium",
                title=f"Converted source is being overwritten, {dom}% of it to '{top_value}'",
                what=(f"{final['changed_to_different_value']:,} converted records arrive at the "
                      f"{ctx.label(final['role']).lower()} with a source that differs from the lead's. "
                      f"{top_count:,} of them ({dom}%) land on the single value '{top_value}'.{user_line}"),
                why_it_matters=("A value being lost is bad; a value being replaced by a specific default "
                                "is worse, because the report still looks complete. The channel that "
                                "receives the overwrite gets credit for pipeline it never generated."),
                recommended_fix=("Find the default: a field default on the downstream object, a "
                                 "conversion screen pre-fill, or an automation that stamps a value. "
                                 "Remove it and map the lead's value through instead."),
                evidence={"count": final["changed_to_different_value"],
                          "rows": [{"Overwritten to": v, "Records": f"{n:,}"} for v, n in changed_to],
                          "sample_ids": final["sample_ids"],
                          "query": _query_for(ctx, "conversion") or query},
                effort="quick", owner_hint="RevOps"))

    # ---- 10/11. UTM
    utm = sections["utm"]
    if utm.get("available"):
        if utm["web_created_records"] >= 20 and utm["web_capture_pct"] < thr["utm_capture_floor_pct"]:
            missing = utm["web_created_records"] - utm["web_created_with_utm"]
            doc.add(Finding(
                id="utm-capture-gap",
                severity=sev_below(utm["web_capture_pct"], 50.0, 70.0, thr["utm_capture_floor_pct"]),
                title=f"Only {utm['web_capture_pct']}% of web-created records carry a UTM",
                what=(f"{utm['web_created_with_utm']:,} of {utm['web_created_records']:,} records created "
                      f"through a web form have a value in {utm['utm_source_field'] or 'the UTM fields'}. "
                      f"{missing:,} arrived with the UTM fields empty. Separately, "
                      f"{utm['utm_present_source_unattributed']:,} records have a UTM but no usable "
                      f"source value — the answer was captured and then not used."),
                why_it_matters=("UTMs are the only capture mechanism here that does not depend on a human "
                                "choosing correctly. Every web record without one is a record whose "
                                "channel can only ever be guessed at afterwards."),
                recommended_fix=("Persist UTMs in a first-party cookie at first touch and write them on "
                                 "every form submission, not just the one on the landing page. Then map "
                                 f"UTM to the source field for the {utm['utm_present_source_unattributed']:,} "
                                 "records where the UTM is already sitting there unused — that is free "
                                 "coverage available today."),
                evidence={"count": missing, "query": query},
                effort="medium", owner_hint="Marketing Ops"))

        if utm["comparable_records"] >= 20 and utm["disagreement_pct"] > 0:
            doc.add(Finding(
                id="utm-source-disagreement",
                severity=sev_above(utm["disagreement_pct"], 40.0, thr["disagreement_high_pct"], 10.0),
                title=f"The source field and the UTM disagree on {utm['disagreement_pct']}% of records",
                what=(f"On {utm['comparable_records']:,} records both the source field and the UTM resolve "
                      f"to a known channel. They name a different channel on {utm['disagreements']:,} of "
                      f"them. Records where either side is ambiguous are excluded rather than guessed at."),
                why_it_matters=("Two systems of record for the same fact, disagreeing a third of the time, "
                                "means at least one report is wrong and nobody can say which. Marketing "
                                "usually reports on the UTM and sales reports on the field, which is why "
                                "the two teams' channel numbers never reconcile."),
                recommended_fix=("Pick which one is authoritative for the channel report and make the "
                                 "other derived from it. The common pairs below usually reveal a specific "
                                 "broken mapping rather than random noise."),
                evidence={"count": utm["disagreements"], "rows": rows_cap(utm["top_pairs"], cfg),
                          "sample_ids": utm["sample_ids"], "query": query},
                effort="medium", owner_hint="Marketing Ops"))

    utm_ow = sections["utm_overwrites"]
    if utm_ow["records_overwritten"] and \
            utm_ow["records_overwritten_pct"] >= thr["overwrite_material_pct"]:
        rows = [{"Field": f, "Records overwritten": f"{s['records_overwritten']:,}"}
                for f, s in utm_ow["by_field"].items()]
        doc.add(Finding(
            id="utm-overwritten-on-refill",
            severity="high",
            title=(f"UTMs are overwritten on later form fills for "
                   f"{qty(utm_ow['records_overwritten'], 'record')}"),
            what=(f"Field history shows the UTM fields changing after record creation on "
                  f"{utm_ow['records_overwritten']:,} records ({utm_ow['records_overwritten_pct']}%). "
                  f"The value you have today is the most recent form fill, not the first."),
            why_it_matters=("This turns your first-touch data into last-touch data without anyone "
                            "deciding to. A prospect who arrived from a paid ad and later returned via a "
                            "branded search now reads as organic, and the paid channel loses the credit "
                            "for a conversion it genuinely created."),
            recommended_fix=("Make the first-touch UTM fields write-once — populate only when empty — and "
                             "add a separate set of last-touch UTM fields if you want the latest value. "
                             "Never let one pair of fields try to hold both."),
            evidence={"count": utm_ow["records_overwritten"], "rows": rows, "query": query},
            effort="quick", owner_hint="Marketing Ops"))

    # ---- 12/13. first vs last touch
    touch = sections["touch"]
    if touch.get("first_touch_overwritten") and \
            touch.get("first_touch_overwritten_pct", 0) >= thr["overwrite_material_pct"]:
        field = touch["first_touch_field"]
        stats = touch["overwrites"].get(field, {})
        doc.add(Finding(
            id="first-touch-overwritten",
            severity="high",
            title=f"{field} is declared first-touch but behaves like last-touch",
            what=(f"You told us {field} holds first touch. Field history shows it being changed after "
                  f"record creation on {touch['first_touch_overwritten']:,} records "
                  f"({touch['first_touch_overwritten_pct']}%). The most common transitions are below."),
            why_it_matters=("A first-touch field that gets overwritten is the most expensive kind of bad "
                            "data, because it looks correct. Every historical cohort analysis you have "
                            "run against it silently used a mixture of first and last touch."),
            recommended_fix=("Make the field write-once at the automation layer, and if the team needs "
                             "the latest source, give them a separate last-touch field. Then decide "
                             "whether the overwritten history can be recovered from field history — "
                             "sometimes it can, and it is worth the afternoon."),
            evidence={"count": touch["first_touch_overwritten"],
                      "rows": rows_cap(stats.get("top_transitions") or [], cfg),
                      "sample_ids": stats.get("sample_ids") or [],
                      "query": _query_for(ctx, "history") or query},
            effort="medium", owner_hint="RevOps"))

    if touch.get("same_field_for_both"):
        doc.add(Finding(
            id="first-last-touch-same-field",
            severity="high",
            title="One field is being asked to hold both first touch and last touch",
            what=(f"{touch['first_touch_field']} is configured as both the first-touch and the last-touch "
                  f"source. A single field cannot hold two different facts; whichever write happens last "
                  f"is what you have."),
            why_it_matters=("Any report that describes this field as first-touch is mislabelled. This is "
                            "usually discovered in a board meeting when two teams present different "
                            "numbers for the same channel."),
            recommended_fix=("Add a second field. Decide explicitly which one the channel report uses and "
                             "write that decision down where the report is defined."),
            evidence={"count": len(ctx.recs("primary")), "value": touch["first_touch_field"]},
            effort="medium", owner_hint="RevOps"))
    elif touch.get("pair_records", 0) >= 50 and \
            touch.get("pair_agreement_pct", 0) >= thr["collapsed_touch_agreement_pct"]:
        doc.add(Finding(
            id="first-last-touch-collapsed",
            severity="medium",
            title=(f"Your first-touch and last-touch fields agree {touch['pair_agreement_pct']}% of the "
                   f"time — one is a copy"),
            what=(f"{touch['first_touch_field']} and {touch['last_touch_field']} carry the same channel on "
                  f"{touch['pair_agreements']:,} of {touch['pair_records']:,} records where both are "
                  f"populated. Real multi-touch journeys do not agree that often."),
            why_it_matters=("You have two fields and one fact. Any analysis that compares first touch to "
                            "last touch is comparing a field to itself, which will always show that the "
                            "first touch is also the last — and that conclusion is an artefact."),
            recommended_fix=("Check whether the last-touch field is populated by a copy of the first-touch "
                             "value at creation and never updated after. If so, either wire it to update "
                             "on each subsequent touch or retire it, because right now it is decorative."),
            evidence={"count": touch["pair_agreements"],
                      "value": f"{touch['pair_agreement_pct']}% agreement", "query": query},
            effort="quick", owner_hint="Marketing Ops"))

    # ---- 14. agreement pairs (self-reported vs tracked, auto vs manual)
    for i, pair in enumerate(sections["agreement"]):
        if pair["channel_comparable"] < 20:
            continue
        rate = pair["channel_disagreement_pct"]
        if rate < 10:
            continue
        if pair["kind"] == "self_reported":
            why = ("Buyers and tracking are describing two different journeys, and the self-reported "
                   "answer is usually the more honest one about influence — it is the channel the buyer "
                   "remembers, which is rarely the last click that carried the cookie. A large gap here "
                   "is not an error to correct; it is the strongest evidence you have that last-click "
                   "reporting understates the channels that create demand.")
            fix = ("Stop treating these as competing answers to the same question. Report them side by "
                   "side: tracked source for routing and operational reporting, self-reported for "
                   "budget conversations. The disagreement pairs below are where the two views diverge "
                   "most and are worth a look before the next planning cycle.")
        elif pair["kind"] == "auto_vs_manual":
            why = ("One of these is set by the platform and one is set by a person or an import. Where "
                   "they disagree, the manually-set value is overriding the tracked one and the report "
                   "built on it inherits whatever the person assumed.")
            fix = ("Decide which property is authoritative, and if it is the manual one, document why. "
                   "Otherwise stop writing to it and let the tracked property carry the channel.")
        else:
            why = ("Two fields on the same record describe the same channel differently, so any report "
                   "grouped by one of them tells a different story from a report grouped by the other.")
            fix = "Pick one as authoritative and derive the other, or retire it."
        # A big self-reported gap is evidence about influence, not a data defect, so it
        # is capped at 'high' — calling it critical would contradict what we just said.
        severity = (sev_above(rate, 999.0, 40.0, 15.0) if pair["kind"] == "self_reported"
                    else sev_above(rate, 55.0, thr["disagreement_high_pct"], 10.0))
        doc.add(Finding(
            id=f"field-disagreement-{pair['kind'].replace('_', '-')}-{i}",
            severity=severity,
            title=f"{pair['label']}: {rate}% disagree on the channel",
            what=(f"{pair['records_with_both']:,} records have both {pair['a']} and {pair['b']} populated. "
                  f"On the {pair['channel_comparable']:,} where both resolve to a known channel, they "
                  f"name a different one {pair['channel_disagreements']:,} times. On raw value alone the "
                  f"two fields differ on {pair['exact_disagreement_pct']}% of records."),
            why_it_matters=why,
            recommended_fix=fix,
            evidence={"count": pair["channel_disagreements"], "rows": rows_cap(pair["top_pairs"], cfg),
                      "sample_ids": pair["sample_ids"], "query": query},
            effort="medium", owner_hint="Marketing Ops"))

    # ---- 15. source values that never convert
    zero = [r for r in sections["conversion_by_value"]
            if r["leads"] >= thr["zero_conversion_min_leads"] and r["won"] == 0]
    if zero and ctx.recs("deal"):
        volume = sum(r["leads"] for r in zero)
        doc.add(Finding(
            id="zero-conversion-source-values",
            severity="high",
            title=(f"{qty(len(zero), 'source value')} "
                   f"{'carries' if len(zero) == 1 else 'carry'} real volume and "
                   f"{'has' if len(zero) == 1 else 'have'} never produced a win"),
            what=(f"{volume:,} records sit on source values that have produced zero closed-won "
                  f"opportunities across the window, each with at least "
                  f"{thr['zero_conversion_min_leads']} records behind it."),
            why_it_matters=("A channel with volume and no wins is usually not a bad channel — it is a "
                            "broken join. The value is a spelling variant, or the source is not "
                            "surviving to the opportunity, so the wins exist but are being counted "
                            "somewhere else. Check that before anyone proposes cutting the spend."),
            recommended_fix=("For each value below, check first whether it appears in the duplicate "
                             "clusters above, then whether its records survive conversion. Only once "
                             "both come back clean is 'this channel does not convert' a supportable "
                             "claim."),
            evidence={"count": volume, "rows": rows_cap(
                [{"Source value": r["value"], "Records": f"{r['leads']:,}",
                  "Converted": f"{r['converted']:,}", "Opportunities": f"{r['opportunities']:,}",
                  "Closed won": r["won"]} for r in zero], cfg), "query": query},
            effort="quick", owner_hint="Marketing Ops"))

    return doc


def _query_for(ctx: Ctx, name: str) -> str:
    entry = ctx.queries.get(name) or {}
    return str(entry.get("query") or "")


# --------------------------------------------------------------------------- main


def run(args: argparse.Namespace) -> int:
    ctx = build_ctx(args)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(ctx.cfg.get("window_days") or 540))
    window = {"start": start.isoformat(), "end": end.isoformat()}

    manifest = RunManifest(PLUGIN, run_dir, window=window)
    primary_file = str(ctx.obj("primary").get("raw_file") or "leads.json")
    manifest.record(
        "primary", tool=(ctx.queries.get("primary") or {}).get("tool", "crm.query"),
        count=len(ctx.recs("primary")), query=_query_for(ctx, "primary"), required=True,
        note=(f"{ctx.raw_dir / primary_file}"
              + (f" · {ctx.dropped_out_of_window:,} records dropped as older than the window"
                 if ctx.dropped_out_of_window else "")),
        diagnosis=(f"{primary_file} is missing or empty. Either the :run skill never wrote it, or the "
                   f"connected CRM identity cannot read {ctx.label('primary')} records — a profile or "
                   f"scope problem, not an empty database. Re-run /lead-source:setup to confirm which."))
    conversion_on = bool((ctx.cfg.get("conversion") or {}).get("enabled", True))
    manifest.record(
        "deal", tool=(ctx.queries.get("deal") or {}).get("tool", "crm.query"),
        count=len(ctx.recs("deal")), query=_query_for(ctx, "deal"), required=conversion_on,
        diagnosis=("No opportunity/deal records came back, so source survival and the closed-won join "
                   "cannot be computed. Check the object permissions for the connected identity, or set "
                   "conversion.enabled to false in ~/.leanscale-gtm/lead-source.json to run without it."))
    manifest.record(
        "intermediate", tool=(ctx.queries.get("intermediate") or {}).get("tool", "crm.query"),
        count=len(ctx.recs("intermediate")), query=_query_for(ctx, "intermediate"), required=False,
        note="Used for the middle conversion hop; absent on HubSpot, where there is no Lead object.")
    manifest.record("field_history", tool=(ctx.queries.get("history") or {}).get("tool", "crm.query"),
                    count=len(ctx.history), query=_query_for(ctx, "history"), required=False,
                    note="Without history, overwrite detection and the stability score are skipped.")
    manifest.record("field_definitions", tool="crm.describe", count=len(ctx.picklists), required=False,
                    note="Without picklist metadata, free-text pollution cannot be separated from "
                         "legitimate values.")
    if not ctx.taxonomy:
        manifest.warn("No intended_taxonomy in config — off-taxonomy detection is skipped. Re-run setup.")
    # Inline history (HubSpot propertiesWithHistory) covers only the records it was
    # pulled for. If that is a sample, overwrite RATES are understated and the user
    # needs to know before they quote one.
    if ctx.history and not str(ctx.cfg.get("history_file") or ""):
        covered = len({e["record_id"] for e in ctx.history})
        primary_n = len(ctx.recs("primary"))
        if primary_n and covered < primary_n * 0.9:
            manifest.warn(
                f"Property history was returned for {covered:,} of {primary_n:,} records. "
                f"Overwrite rates are computed against the full record count, so they are a FLOOR — "
                f"the true rate is likely higher. Pull history for the whole window to close this.")
    manifest.finalize()

    # ---------------- sections
    primary_field = ctx.field("reported_source", "primary")
    counts: Counter = Counter()
    for rec in ctx.recs("primary"):
        value = rec.get(primary_field)
        if not tx.is_blank_value(value):
            counts[str(value)] += 1

    clusters = tx.cluster_values(
        dict(counts),
        intended_taxonomy=ctx.taxonomy,
        similarity_threshold=float(ctx.cfg.get("similarity_threshold", 0.88)),
        subset_min_records=int(ctx.cfg.get("subset_min_records", 10)),
        extra_groups=ctx.cfg.get("extra_synonym_groups") or {},
        placeholder_values=ctx.cfg.get("placeholder_values") or [],
    )
    mapping = tx.proposed_mapping(clusters)
    attributed_records = sum(n for v, n in counts.items() if ctx.attributed(v))
    off_tax = tx.off_taxonomy_values(dict(counts), ctx.taxonomy,
                                     ctx.cfg.get("placeholder_values") or [])
    picklist = ctx.picklists.get(primary_field) or []
    picklist_keys = {tx.key(v) for v in picklist}
    off_picklist = ([{"value": v, "records": n} for v, n in
                     sorted(counts.items(), key=lambda kv: -kv[1])
                     if ctx.attributed(v) and tx.key(v) not in picklist_keys] if picklist else [])

    polluted_keys = {tx.key(r["Current value"]) for r in mapping}
    polluted_keys |= {tx.key(r["value"]) for r in off_tax}
    polluted_records = sum(n for v, n in counts.items()
                           if ctx.attributed(v) and tx.key(v) in polluted_keys)

    sections: Dict[str, Any] = {
        "crm": ctx.crm,
        "scope": {
            "primary_object": ctx.label("primary"),
            "primary_source_field": primary_field,
            "records": {role: len(ctx.recs(role)) for role in ROLE_ORDER},
            "history_entries": len(ctx.history),
            "intended_taxonomy": ctx.taxonomy,
        },
        "field_inventory": field_inventory(ctx),
        "routes": route_breakdown(ctx),
        "trend": trend(ctx),
        "taxonomy": {
            # Raw distinct values is the number a customer can count themselves in a
            # GROUP BY. The normalised count is how many channels those actually are;
            # the gap between the two IS the duplicate problem.
            "distinct_values": len([v for v in counts if ctx.attributed(v)]),
            "distinct_normalized": len({tx.key(v) for v in counts if ctx.attributed(v)}),
            "intended_size": len(ctx.taxonomy),
            "attributed_records": attributed_records,
            "clusters": [c.to_dict() for c in clusters],
            "proposed_mapping": mapping,
            "off_taxonomy": off_tax,
            "off_picklist": off_picklist,
            "picklist_values": picklist,
            "polluted_records": polluted_records,
            "value_counts": dict(counts.most_common()),
        },
        "survival": survival(ctx),
        "conversion_by_value": conversion_by_value(ctx),
        "utm": utm_analysis(ctx),
        "utm_overwrites": utm_overwrites(ctx),
        "touch": touch_analysis(ctx),
        "agreement": agreement_pairs(ctx),
    }

    # ---------------- score components
    head = next((r for r in sections["field_inventory"]
                 if r["object"] == "primary" and r["field"] == primary_field), None)
    coverage = (100.0 - head["unattributed_pct"]) if head else None

    surv = sections["survival"]
    # A hop with a zero denominator is unmeasured, not 0% — scoring it as 0 would drag
    # the index down on the strength of data that was never collected.
    survival_component = (surv["hops"][-1]["survival_pct"]
                          if surv.get("available") and surv.get("hops")
                          and surv["hops"][-1]["eligible"] else None)

    taxonomy_component = (100.0 - pct(polluted_records, attributed_records)
                          if attributed_records else None)

    agreements: List[float] = []
    if sections["utm"].get("available") and sections["utm"]["comparable_records"] >= 20:
        agreements.append(100.0 - sections["utm"]["disagreement_pct"])
    for pair in sections["agreement"]:
        if pair["channel_comparable"] >= 20:
            agreements.append(100.0 - pair["channel_disagreement_pct"])
    agreement_component = (sum(agreements) / len(agreements)) if agreements else None

    touch = sections["touch"]
    stability_component = None
    if ctx.history and touch.get("first_touch_field"):
        stability_component = 100.0 - float(touch.get("first_touch_overwritten_pct", 0.0))

    score_detail = integrity_score(ctx, {
        "coverage": coverage, "survival": survival_component, "taxonomy": taxonomy_component,
        "agreement": agreement_component, "stability": stability_component,
    })
    sections["integrity_score"] = score_detail

    # ---------------- findings + scores
    org_name = str(ctx.profile.get("org_name") or args.org_name or "Your organization")
    doc = build_findings(ctx, sections, window, org_name)
    doc.sections = sections

    measured = ", ".join(score_detail["components_measured"])
    caveat = ""
    if score_detail["components_missing"]:
        # Reweighting is honest arithmetic but it is NOT the same number: dropping a
        # component redistributes its weight onto the ones that remain. Say so, or the
        # delta against a run with different coverage will read as progress it isn't.
        caveat = (f" {', '.join(score_detail['components_missing'])} could not be measured, so "
                  f"their weight was redistributed — this score is not directly comparable to a "
                  f"run where all five components were available.")
    doc.add_score(Score(
        key="source_integrity_score", label="Source Integrity Score", value=score_detail["score"],
        unit="score_0_100", direction_good="up",
        context=f"{score_detail['band']} Built from: {measured}.{caveat}"))
    if head:
        doc.add_score(Score(
            key="unattributed_rate", label="Unattributed Source Rate",
            value=head["unattributed_pct"], unit="percent", direction_good="down",
            context=f"{head['blank']:,} blank + {head['placeholder']:,} placeholder "
                    f"of {head['records']:,} {ctx.plural('primary').lower()}."))
    if survival_component is not None:
        final = surv["hops"][-1]
        doc.add_score(Score(
            key="source_survival_rate", label=f"Source Survival · {final['hop']}",
            value=final["survival_pct"], unit="percent", direction_good="up",
            context=f"{final['survived']:,} of {final['eligible']:,} converted records keep their "
                    f"source. {final['no_target_record']:,} converted without a deal and are excluded."))
    elif surv.get("available") and surv.get("hops"):
        final = surv["hops"][-1]
        doc.add_score(Score(
            key="source_survival_rate", label=f"Source Survival · {final['hop']}",
            value="not measurable", unit="text", direction_good="up",
            context=f"No converted record carrying a source reached the "
                    f"{ctx.label(final['role']).lower()} in this window, so there is nothing to "
                    f"measure. Widen the window rather than reading this as a zero."))
    doc.add_score(Score(
        key="distinct_source_values", label="Distinct Source Values",
        value=sections["taxonomy"]["distinct_values"], unit="count", direction_good="down",
        context=((f"Against an intended taxonomy of {len(ctx.taxonomy)}. They reduce to "
                  f"{sections['taxonomy']['distinct_normalized']} once spelling is normalised.")
                 if ctx.taxonomy else
                 (f"They reduce to {sections['taxonomy']['distinct_normalized']} once spelling is "
                  f"normalised. No intended taxonomy captured — re-run setup to compare."))))

    doc.unavailable = unavailable_notes(ctx, sections, manifest)
    path = doc.write(run_dir)

    print(f"lead-source · {org_name} · CRM {ctx.crm}")
    print(f"  {len(ctx.recs('primary')):,} {ctx.plural('primary').lower()} · "
          f"{len(ctx.recs('deal')):,} {ctx.plural('deal').lower()} · "
          f"{len(ctx.history):,} history entries")
    print(f"  Source Integrity Score {score_detail['score']}/100 "
          f"({len(score_detail['components_measured'])} of 5 components measured)")
    print(f"  {len(doc.findings)} findings -> {path}")
    return 0


def unavailable_notes(ctx: Ctx, sections: Dict[str, Any], manifest: RunManifest) -> List[str]:
    out: List[str] = []
    if not ctx.history:
        out.append("Field-history checks (first-touch overwrite, UTM overwrite, stability score) — "
                   "no history records were supplied")
    if not sections["utm"].get("available"):
        out.append("UTM capture and UTM-vs-source agreement — no UTM fields configured")
    if not ctx.taxonomy:
        out.append("Off-taxonomy detection — no intended taxonomy captured during setup")
    if not ctx.picklists:
        out.append("Free-text pollution detection — no picklist metadata supplied")
    if not ctx.recs("deal"):
        out.append("Source survival and closed-won joins — no deal records supplied")
    if not sections["agreement"]:
        out.append("Self-reported vs tracked source — no self-reported field configured")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse lead source integrity from raw CRM extracts.")
    parser.add_argument("--run-dir", required=True, help="Run directory; findings.json is written here")
    parser.add_argument("--raw", default="", help="Directory of raw extracts (default: <run-dir>/raw)")
    parser.add_argument("--config", default="", help="Config file (default: ~/.leanscale-gtm/lead-source.json)")
    parser.add_argument("--profile", default="", help="Profile file (default: ~/.leanscale-gtm/profile.json)")
    parser.add_argument("--queries", default="", help="JSON of source -> {tool, query} for provenance")
    parser.add_argument("--window-days", type=int, default=0, help="Override the reporting window")
    parser.add_argument("--org-name", default="", help="Used only when no profile is available")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ConfigError as exc:
        print(f"\nConfiguration problem:\n{exc}\n", file=sys.stderr)
        return 2
    except SourceEmptyError as exc:
        # Fail loud, not silent. A clean-looking empty report is worse than a crash.
        print(f"\n{exc}\n", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
