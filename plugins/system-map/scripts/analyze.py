#!/usr/bin/env python3
"""
system-map — analyze.py

Layer 2 of the pipeline. Reads raw/*.json (written by the :run skill from MCP
calls) and emits findings.json. Pure standard library, no network, no MCP.
Everything here is deterministic and testable against fixtures/.

    python3 analyze.py --run-dir ./gtm-agents/system-map/2026-08-10-1422
    python3 analyze.py --raw ./fixtures/salesforce/raw --out /tmp/run-sf

WHAT IT COMPUTES
    · integration users and their write surface
    · connected apps / OAuth grants / installed packages, and which are orphaned
    · the automation inventory across every metadata surface that was readable
    · FIELD-WRITE CONFLICTS — the same field on the same object written by more
      than one automation, with the order they fire in where determinable
    · dormant automation, and automation changed in the last N days
    · object surface — which objects carry automation and which carry none
    · the stack map, and the gap between it and the tools the customer named

THE HONESTY RULE
    Every metadata surface this run could not read is recorded in `unavailable`
    with the exact permission that would fix it. A permissions failure must
    never render as "your org is clean."
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

try:
    from lib import (  # noqa: E402
        ConfigError,
        Finding,
        FindingsDoc,
        RunManifest,
        Score,
        SourceEmptyError,
        load_plugin_config,
        load_profile,
        parse_dt,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - packaging guard
    raise SystemExit(
        "The shared library is missing from this plugin install "
        f"(expected {Path(__file__).resolve().parent / 'lib'}). "
        "Reinstall the plugin — scripts/lib is vendored at package time and cannot be "
        f"resolved from anywhere else. Original error: {exc}"
    ) from exc

import fingerprints as fp  # noqa: E402

PLUGIN = "system-map"

# --------------------------------------------------------------------------- config


DEFAULTS: Dict[str, Any] = {
    "environment": "production",
    "org_label": "",
    "include_managed_packages": True,
    "include_integration_users": True,
    "include_connected_apps": True,
    "include_automation": True,
    "include_scheduled_jobs": True,
    "dormancy_days": 90,
    "recent_change_days": 14,
    "flag_orphaned_automation": True,
    "believed_tools": [],
    "conflict_ignore_fields": [],
    "ignore_automation_names": [],
    "ignore_namespaces": [],
    "extra_integration_user_patterns": [],
    "object_surface_min_records": 1000,
    "allow_empty_automation": False,
}


# ---------------------------------------------------------------- metadata surfaces
#
# The single most useful table in this plugin. Each entry names a metadata
# surface, the object/endpoint behind it, and THE EXACT PERMISSION the connected
# identity needs. When a surface is unreadable the report prints this verbatim
# so an admin can grant it without a support ticket.
#
# `required` marks the surfaces whose total emptiness means the run is blind
# rather than the org being clean.

SURFACES: Dict[str, Dict[str, Any]] = {
    # ---- Salesforce -------------------------------------------------------
    "salesforce_org": {
        "crm": "salesforce", "label": "Org and sandbox flag",
        "api": "Organization (standard SOQL)",
        "permission": "Read on Organization — available to any authenticated user with API Enabled.",
        "required": False,
    },
    "salesforce_users": {
        "crm": "salesforce", "label": "Users and integration users",
        "api": "User (standard SOQL)",
        "permission": "Read on User (standard for most profiles). Manager/CreatedBy joins need no extra grant.",
        "required": True,
    },
    "salesforce_login_history": {
        "crm": "salesforce", "label": "Login history and API client mix",
        "api": "LoginHistory (standard SOQL)",
        "permission": "'Manage Users' or 'View Setup and Configuration'.",
        "required": False,
    },
    "salesforce_oauth_tokens": {
        "crm": "salesforce", "label": "OAuth grants and last use",
        "api": "OauthToken (standard SOQL)",
        "permission": "'Manage Users' (without it you only see your own tokens, which reads as a clean org).",
        "required": False,
    },
    "salesforce_permset_assignments": {
        "crm": "salesforce", "label": "Permission set assignments",
        "api": "PermissionSetAssignment (standard SOQL)",
        "permission": "'View Setup and Configuration'.",
        "required": False,
    },
    "salesforce_object_permissions": {
        "crm": "salesforce", "label": "Object-level write permissions",
        "api": "ObjectPermissions (standard SOQL)",
        "permission": "'View Setup and Configuration'.",
        "required": False,
    },
    "salesforce_connected_apps": {
        "crm": "salesforce", "label": "Connected apps",
        "api": "ConnectedApplication (Tooling API)",
        "permission": "'Customize Application' + 'Manage Connected Apps', and the query must go to "
                      "/services/data/vXX.X/tooling/query — the standard query endpoint rejects this object.",
        "required": False,
    },
    "salesforce_installed_packages": {
        "crm": "salesforce", "label": "Installed managed packages",
        "api": "InstalledSubscriberPackage (Tooling API)",
        "permission": "'Download AppExchange Packages' or 'View Setup and Configuration', via the Tooling API. "
                      "Fallback with no Tooling access: PackageLicense on the standard API (licensed packages only).",
        "required": False,
    },
    "salesforce_flows": {
        "crm": "salesforce", "label": "Flows and Process Builder processes",
        "api": "FlowDefinitionView (standard SOQL — no Tooling API needed)",
        "permission": "'View Setup and Configuration' or 'Manage Flow'. FlowDefinitionView is exposed on the "
                      "STANDARD API, so this usually works even when Tooling access does not.",
        "required": False,
    },
    "salesforce_flow_versions": {
        "crm": "salesforce", "label": "Flow versions (inactive draft pile-up)",
        "api": "FlowVersionView (standard SOQL)",
        "permission": "'View Setup and Configuration' or 'Manage Flow'.",
        "required": False,
    },
    "salesforce_flow_metadata": {
        "crm": "salesforce", "label": "Flow definitions — which fields each flow writes",
        "api": "Flow.Metadata (Tooling API, one Id per query) or a Metadata API retrieve",
        "permission": "'Manage Flow' (or 'View All Data') + API Enabled, via the Tooling API. The Metadata field "
                      "can only be selected when the query filters on a single Id — batching it fails.",
        "required": False,
    },
    "salesforce_apex_triggers": {
        "crm": "salesforce", "label": "Apex triggers",
        "api": "ApexTrigger (Tooling API)",
        "permission": "'Author Apex' or 'View Setup and Configuration', via the Tooling API.",
        "required": False,
    },
    "salesforce_apex_classes": {
        "crm": "salesforce", "label": "Apex classes",
        "api": "ApexClass (Tooling API)",
        "permission": "'Author Apex' or 'View Setup and Configuration', via the Tooling API.",
        "required": False,
    },
    "salesforce_validation_rules": {
        "crm": "salesforce", "label": "Validation rules",
        "api": "ValidationRule (Tooling API)",
        "permission": "'View Setup and Configuration', via the Tooling API.",
        "required": False,
    },
    "salesforce_workflow_rules": {
        "crm": "salesforce", "label": "Workflow rules",
        "api": "WorkflowRule (Tooling API)",
        "permission": "'View Setup and Configuration', via the Tooling API.",
        "required": False,
    },
    "salesforce_workflow_field_updates": {
        "crm": "salesforce", "label": "Workflow field updates",
        "api": "WorkflowFieldUpdate (Tooling API)",
        "permission": "'View Setup and Configuration', via the Tooling API.",
        "required": False,
    },
    "salesforce_assignment_rules": {
        "crm": "salesforce", "label": "Assignment rules",
        "api": "AssignmentRule (standard SOQL)",
        "permission": "Read access — available to most profiles with API Enabled.",
        "required": False,
    },
    "salesforce_scheduled_jobs": {
        "crm": "salesforce", "label": "Scheduled jobs",
        "api": "CronTrigger + CronJobDetail (standard SOQL)",
        "permission": "'View Setup and Configuration' to see everyone's jobs; without it you see only your own.",
        "required": False,
    },
    "salesforce_entities": {
        "crm": "salesforce", "label": "Object inventory",
        "api": "EntityDefinition (standard SOQL) or describeGlobal",
        "permission": "API Enabled.",
        "required": False,
    },
    "salesforce_record_counts": {
        "crm": "salesforce", "label": "Record counts per object",
        "api": "GET /services/data/vXX.X/limits/recordCount?sObjects=…",
        "permission": "API Enabled. Counts are returned only for objects the identity can read.",
        "required": False,
    },
    "salesforce_field_definitions": {
        "crm": "salesforce", "label": "Field inventory (namespace fingerprints)",
        "api": "FieldDefinition (standard SOQL, must filter by EntityDefinition)",
        "permission": "'View Setup and Configuration'.",
        "required": False,
    },
    "salesforce_setup_audit_trail": {
        "crm": "salesforce", "label": "Setup audit trail (recent changes)",
        "api": "SetupAuditTrail (standard SOQL, 180-day retention)",
        "permission": "'View Setup and Configuration'.",
        "required": False,
    },
    "salesforce_api_events": {
        "crm": "salesforce", "label": "API call volume by consumer",
        "api": "ApiEvent / EventLogFile (Event Monitoring)",
        "permission": "Salesforce Shield Event Monitoring (paid add-on) + 'View Event Log Files' + API Enabled. "
                      "Without Shield, login-history application counts are the only proxy available.",
        "required": False,
    },
    "salesforce_flow_executions": {
        "crm": "salesforce", "label": "Flow execution counts (true dormancy)",
        "api": "FlowExecution event type in EventLogFile",
        "permission": "Salesforce Shield Event Monitoring. Without it, dormancy falls back to last-modified date, "
                      "which is a proxy and is labelled as one in the report.",
        "required": False,
    },
    # ---- HubSpot ----------------------------------------------------------
    "hubspot_account": {
        "crm": "hubspot", "label": "Portal details and sandbox flag",
        "api": "GET /account-info/v3/details",
        "permission": "Any valid private-app token or OAuth access token.",
        "required": False,
    },
    "hubspot_users": {
        "crm": "hubspot", "label": "Users",
        "api": "GET /settings/v3/users",
        "permission": "Scope `settings.users.read`.",
        "required": True,
    },
    "hubspot_owners": {
        "crm": "hubspot", "label": "Owners (active and archived)",
        "api": "GET /crm/v3/owners?archived=true|false",
        "permission": "Scope `crm.objects.owners.read`.",
        "required": False,
    },
    "hubspot_workflows": {
        "crm": "hubspot", "label": "Workflows and enrollment triggers",
        "api": "GET /automation/v4/flows",
        "permission": "Scope `automation`. Requires Operations Hub / Marketing Hub Professional or above — "
                      "on Starter portals this endpoint returns 403 and there is nothing to grant.",
        "required": False,
    },
    "hubspot_properties": {
        "crm": "hubspot", "label": "Property inventory (integration fingerprints)",
        "api": "GET /crm/v3/properties/{objectType}",
        "permission": "Scopes `crm.schemas.contacts.read`, `crm.schemas.companies.read`, `crm.schemas.deals.read`.",
        "required": False,
    },
    "hubspot_schemas": {
        "crm": "hubspot", "label": "Custom object schemas",
        "api": "GET /crm/v3/schemas",
        "permission": "Scope `crm.schemas.custom.read` (Enterprise only — custom objects do not exist below it).",
        "required": False,
    },
    "hubspot_object_counts": {
        "crm": "hubspot", "label": "Record counts per object",
        "api": "POST /crm/v3/objects/{objectType}/search with limit 1, read `total`",
        "permission": "Scope `crm.objects.{type}.read` for each object type.",
        "required": False,
    },
    "hubspot_api_usage": {
        "crm": "hubspot", "label": "Daily API usage",
        "api": "GET /account-info/v3/api-usage/daily",
        "permission": "Any valid token. NOTE: HubSpot reports usage for the portal, not per consumer — "
                      "there is no public per-app breakdown.",
        "required": False,
    },
    "hubspot_connected_apps": {
        "crm": "hubspot", "label": "Connected apps and private apps",
        "api": "No public API exists",
        "permission": "Cannot be granted. Export by hand: Settings → Integrations → Connected Apps (and → Private "
                      "Apps), save as raw/hs_connected_apps.json. Until then app orphan detection is blind.",
        "required": False,
    },
    "hubspot_login_activity": {
        "crm": "hubspot", "label": "Login and security activity",
        "api": "GET /account-info/v3/activity/login",
        "permission": "Scope `account-info.security.read` (Enterprise).",
        "required": False,
    },
}


# ------------------------------------------------------------- salesforce order of execution
#
# Salesforce runs automation in a documented order. Two automations at the SAME
# rank writing the same field is the dangerous case: Salesforce does not
# guarantee which of them runs last unless flow trigger ordering is set.
# https://developer.salesforce.com/docs — "Triggers and Order of Execution"

FIRE_RANK: List[Tuple[int, str]] = [
    (10, "Before-save record-triggered flow"),
    (20, "Apex before trigger"),
    (30, "Validation rule"),
    (40, "Duplicate rule"),
    (50, "Apex after trigger"),
    (60, "Assignment rule"),
    (70, "Auto-response rule"),
    (80, "Workflow rule field update"),
    (90, "After-save record-triggered flow / Process Builder"),
    (95, "Scheduled or async (scheduled flow, scheduled path, batch Apex)"),
]
FIRE_LABEL = dict(FIRE_RANK)

HS_OBJECT_TYPES = {
    "0-1": "Contact", "0-2": "Company", "0-3": "Deal", "0-5": "Ticket",
    "0-8": "LineItem", "0-7": "Product", "0-49": "Email", "0-11": "Quote",
    "0-48": "Call", "0-46": "Meeting", "0-27": "Task", "0-4": "Engagement",
}

# HubSpot v4 flow action type ids that write a property.
HS_SET_PROPERTY_ACTIONS = {"0-5", "0-3"}


# --------------------------------------------------------------------------- io helpers


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}). Re-run the :run skill's fetch step.") from exc


def rows(payload: Any) -> List[Dict[str, Any]]:
    """
    Normalise every response envelope this plugin sees into a list of dicts.

      Salesforce REST query   {"totalSize": n, "done": true, "records": [...]}
      Salesforce Tooling      same shape
      HubSpot v3/v4           {"results": [...], "paging": {...}}
      Bare list               [...]
      limits/recordCount      {"sObjects": [...]}
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("records", "results", "sObjects", "data", "items", "rows"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        return [payload]
    return []


def _get(record: Dict[str, Any], *path: str, default: Any = None) -> Any:
    """Safe nested get: _get(r, 'LastModifiedBy', 'Name')."""
    node: Any = record
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


def _person(record: Dict[str, Any], field: str, default: Any = None) -> Any:
    """
    Read a who-touched-this field that is a relationship on some sobjects and a
    plain string on others.

    On ConnectedApplication, ApexTrigger and WorkflowRule, LastModifiedBy is a
    relationship and the name lives at LastModifiedBy.Name. On FlowDefinitionView
    it is a plain text field already holding the name — selecting
    LastModifiedBy.Name there is an INVALID_FIELD that takes the whole query down,
    so the query rightly asks for it bare, and a relationship-only read then finds
    a string where it expected a dict and blanks every row.
    """
    node = record.get(field)
    if isinstance(node, dict):
        return node.get("Name") or default
    if isinstance(node, str) and node.strip():
        return node
    return record.get(f"{field}Name") or default


def _days_ago(value: Any, now: datetime) -> Optional[int]:
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return (now - parsed).days


def _iso(value: Any) -> str:
    parsed = parse_dt(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


# --------------------------------------------------------------------------- automation model


class Automation:
    """One piece of automation, normalised across Salesforce and HubSpot."""

    __slots__ = (
        "key", "kind", "kind_label", "name", "obj", "active", "version",
        "last_modified", "last_modified_by", "modified_by_active", "last_fired",
        "fire_rank", "fire_label", "trigger", "writes", "write_basis", "trigger_order", "source",
    )

    def __init__(self, **kw: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))
        self.writes = kw.get("writes") or []
        self.active = bool(kw.get("active"))

    def as_row(self) -> Dict[str, Any]:
        return {
            "Automation": self.name,
            "Type": self.kind_label,
            "Object": self.obj or "—",
            "Active": "yes" if self.active else "no",
            "Last modified": _iso(self.last_modified) or "unknown",
            "Modified by": self.last_modified_by or "unknown",
            "Fields written": len(self.writes),
        }


def _norm_field(obj: Optional[str], field: Optional[str]) -> Optional[str]:
    if not field:
        return None
    field = str(field).strip()
    # Flow assignment references arrive as "$Record.StageName" or "$Record.Account.Name".
    field = re.sub(r"^\$Record(\.|__)?", "", field)
    field = field.lstrip(".")
    if not field or field.startswith("$"):
        return None
    if "." in field and obj and field.split(".", 1)[0].lower() == str(obj).lower():
        field = field.split(".", 1)[1]
    return f"{obj or 'Unknown'}.{field}"


# ------------------------------------------------------------------ salesforce extraction


def _entity_lookup(entities: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    TableEnumOrId on ApexTrigger / WorkflowRule is the API name for standard
    objects and an 15/18-char durable Id for custom ones. EntityDefinition maps
    the Ids back to names.
    """
    out: Dict[str, str] = {}
    for e in entities:
        durable = str(e.get("DurableId") or "")
        api = str(e.get("QualifiedApiName") or "")
        if durable and api:
            out[durable] = api
            out[durable[:15]] = api
    return out


def _resolve_object(value: Any, lookup: Dict[str, str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in lookup:
        return lookup[text]
    if len(text) >= 15 and text[:15] in lookup:
        return lookup[text[:15]]
    return text


def _flow_writes(metadata: Dict[str, Any], default_object: str) -> Tuple[List[str], str]:
    """
    Pull every field a Flow writes out of its metadata. Covers the four shapes
    that actually appear:
      recordUpdates[].inputAssignments[].field       explicit update elements
      recordCreates[].inputAssignments[].field       create elements
      assignments[].assignmentItems[].assignToReference  $Record.Field (before-save)
      actionCalls[].inputParameters[]                Process Builder record updates
    """
    writes: List[str] = []
    basis = "flow_metadata"

    def add(obj: Optional[str], field: Any) -> None:
        norm = _norm_field(obj or default_object, field)
        if norm:
            writes.append(norm)

    for element in metadata.get("recordUpdates") or []:
        obj = element.get("object") or default_object
        for assignment in element.get("inputAssignments") or []:
            add(obj, assignment.get("field"))
        # A recordUpdate with inputReference and no explicit object writes the
        # triggering record.
        if not element.get("inputAssignments") and element.get("inputReference"):
            add(obj, element.get("inputReference"))

    for element in metadata.get("recordCreates") or []:
        obj = element.get("object") or default_object
        for assignment in element.get("inputAssignments") or []:
            add(obj, assignment.get("field"))

    for element in metadata.get("assignments") or []:
        for item in element.get("assignmentItems") or []:
            ref = str(item.get("assignToReference") or "")
            if ref.startswith("$Record"):
                add(default_object, ref)

    for element in metadata.get("actionCalls") or []:
        for param in element.get("inputParameters") or []:
            name = str(param.get("name") or "")
            if name.lower().startswith("field") or "fieldname" in name.lower():
                value = param.get("value") or {}
                add(default_object, value.get("stringValue") if isinstance(value, dict) else value)
                basis = "process_builder_metadata_partial"

    return sorted(set(writes)), basis


_APEX_LOOP = re.compile(
    r"for\s*\(\s*([A-Za-z_][\w]*)\s+([A-Za-z_][\w]*)\s*:\s*[Tt]rigger\.(?:new|old|newMap\.values\(\)|oldMap\.values\(\))",
)
_APEX_ASSIGN_TMPL = r"\b{var}\s*\.\s*([A-Za-z_][\w]*(?:__c)?)\s*=(?!=)"
_APEX_DIRECT = re.compile(
    r"[Tt]rigger\.(?:new|old)\s*\[\s*\w+\s*\]\s*\.\s*([A-Za-z_][\w]*(?:__c)?)\s*=(?!=)"
)


def _apex_writes(body: str, obj: str) -> Tuple[List[str], str]:
    """
    Infer field writes from Apex trigger source. HEURISTIC, and labelled as one
    everywhere it surfaces: it finds loop variables bound to Trigger.new/old and
    then any assignment onto them. It will miss writes made through a helper
    class, and it can over-report a variable that shadows the loop name.
    """
    if not body:
        return [], "apex_body_heuristic"
    writes: List[str] = []
    loop_vars = {m.group(2) for m in _APEX_LOOP.finditer(body)}
    # `Trigger.new[0].Field = ` style
    for match in _APEX_DIRECT.finditer(body):
        norm = _norm_field(obj, match.group(1))
        if norm:
            writes.append(norm)
    for var in loop_vars:
        for match in re.finditer(_APEX_ASSIGN_TMPL.format(var=re.escape(var)), body):
            norm = _norm_field(obj, match.group(1))
            if norm:
                writes.append(norm)
    return sorted(set(writes)), "apex_body_heuristic"


def build_salesforce_automations(
    raw: Dict[str, Any], cfg: Dict[str, Any], now: datetime
) -> List[Automation]:
    entities = rows(raw.get("sf_entities.json"))
    lookup = _entity_lookup(entities)
    ignore = {str(n).lower() for n in cfg.get("ignore_automation_names") or []}

    users_active = {}
    for user in rows(raw.get("sf_users.json")):
        users_active[str(user.get("Name") or "")] = bool(user.get("IsActive"))

    def modified_by_active(name: Any) -> Optional[bool]:
        if not name:
            return None
        return users_active.get(str(name))

    # Flow metadata keyed by API name AND by Id so either join works.
    flow_meta: Dict[str, Dict[str, Any]] = {}
    for record in rows(raw.get("sf_flow_metadata.json")):
        meta = record.get("Metadata") or {}
        for key in (record.get("FullName"), record.get("Id"), record.get("ApiName")):
            if key:
                flow_meta[str(key)] = meta

    out: List[Automation] = []

    # ---- Flows and Process Builder --------------------------------------
    for flow in rows(raw.get("sf_flows.json")):
        api_name = str(flow.get("ApiName") or flow.get("DurableId") or flow.get("Id") or "")
        label = str(flow.get("Label") or api_name)
        if label.lower() in ignore:
            continue
        process_type = str(flow.get("ProcessType") or "")
        trigger_type = str(flow.get("TriggerType") or "")
        obj = str(_get(flow, "TriggerObjectOrEvent", "QualifiedApiName", default="") or
                  flow.get("TriggerObjectOrEventLabel") or "")
        meta = flow_meta.get(api_name) or flow_meta.get(str(flow.get("Id") or "")) or {}
        writes, basis = _flow_writes(meta, obj) if meta else ([], "flow_metadata_unavailable")

        if process_type == "Workflow":
            kind, kind_label, rank = "process_builder", "Process Builder", 90
        elif trigger_type == "RecordBeforeSave":
            kind, kind_label, rank = "flow", "Flow (before-save)", 10
        elif trigger_type in ("RecordAfterSave", "RecordBeforeDelete"):
            kind, kind_label, rank = "flow", "Flow (after-save)", 90
        elif trigger_type == "Scheduled":
            kind, kind_label, rank = "flow", "Flow (scheduled)", 95
        else:
            kind, kind_label, rank = "flow", f"Flow ({process_type or 'screen'})", 90

        start = meta.get("start") if isinstance(meta.get("start"), dict) else {}
        trigger_order = meta.get("triggerOrder") or (start or {}).get("triggerOrder")

        out.append(Automation(
            key=f"flow:{api_name}", kind=kind, kind_label=kind_label, name=label, obj=obj,
            active=str(flow.get("IsActive")).lower() in ("true", "1"),
            version=flow.get("VersionNumber"),
            last_modified=flow.get("LastModifiedDate"),
            last_modified_by=_person(flow, "LastModifiedBy"),
            modified_by_active=modified_by_active(
                _person(flow, "LastModifiedBy")),
            last_fired=None, fire_rank=rank, trigger=trigger_type or process_type,
            writes=writes, write_basis=basis, trigger_order=trigger_order,
            source="salesforce_flows",
        ))

    # ---- Apex triggers ---------------------------------------------------
    for trigger in rows(raw.get("sf_apex_triggers.json")):
        obj = _resolve_object(trigger.get("TableEnumOrId"), lookup)
        name = str(trigger.get("Name") or "")
        if name.lower() in ignore:
            continue
        before = any(trigger.get(k) for k in
                     ("UsageBeforeInsert", "UsageBeforeUpdate", "UsageBeforeDelete"))
        writes, basis = _apex_writes(str(trigger.get("Body") or ""), obj)
        out.append(Automation(
            key=f"apex_trigger:{name}", kind="apex_trigger",
            kind_label="Apex trigger (before)" if before else "Apex trigger (after)",
            name=name, obj=obj,
            active=str(trigger.get("Status") or "Active") == "Active",
            version=trigger.get("ApiVersion"),
            last_modified=trigger.get("LastModifiedDate"),
            last_modified_by=_person(trigger, "LastModifiedBy"),
            modified_by_active=modified_by_active(_person(trigger, "LastModifiedBy")),
            last_fired=None, fire_rank=20 if before else 50,
            trigger="before" if before else "after",
            writes=writes, write_basis=basis, trigger_order=None,
            source="salesforce_apex_triggers",
        ))

    # ---- Workflow rules + their field updates ---------------------------
    field_updates: Dict[str, Dict[str, Any]] = {}
    for fu in rows(raw.get("sf_workflow_field_updates.json")):
        field_updates[str(fu.get("Name") or fu.get("FullName") or "")] = fu

    for rule in rows(raw.get("sf_workflow_rules.json")):
        obj = _resolve_object(rule.get("TableEnumOrId"), lookup)
        name = str(rule.get("Name") or rule.get("FullName") or "")
        if name.lower() in ignore:
            continue
        meta = rule.get("Metadata") or {}
        writes: List[str] = []
        for action in meta.get("actions") or []:
            if str(action.get("type") or "") != "FieldUpdate":
                continue
            fu = field_updates.get(str(action.get("name") or ""))
            if fu:
                fu_obj = _resolve_object(fu.get("TableEnumOrId"), lookup) or obj
                norm = _norm_field(fu_obj, _get(fu, "Metadata", "field"))
                if norm:
                    writes.append(norm)
        out.append(Automation(
            key=f"workflow_rule:{name}", kind="workflow_rule", kind_label="Workflow rule",
            name=name, obj=obj,
            active=bool(meta.get("active", rule.get("Active", True))),
            version=None, last_modified=rule.get("LastModifiedDate"),
            last_modified_by=_person(rule, "LastModifiedBy"),
            modified_by_active=modified_by_active(_person(rule, "LastModifiedBy")),
            last_fired=None, fire_rank=80, trigger=str(meta.get("triggerType") or ""),
            writes=sorted(set(writes)), write_basis="workflow_field_update", trigger_order=None,
            source="salesforce_workflow_rules",
        ))

    # ---- Validation rules (block writes rather than make them) ----------
    for rule in rows(raw.get("sf_validation_rules.json")):
        obj = _get(rule, "EntityDefinition", "QualifiedApiName",
                   default=_resolve_object(rule.get("EntityDefinitionId"), lookup))
        name = str(rule.get("ValidationName") or rule.get("Name") or "")
        if name.lower() in ignore:
            continue
        out.append(Automation(
            key=f"validation_rule:{obj}.{name}", kind="validation_rule",
            kind_label="Validation rule", name=name, obj=obj,
            active=bool(rule.get("Active")), version=None,
            last_modified=rule.get("LastModifiedDate"),
            last_modified_by=_person(rule, "LastModifiedBy"),
            modified_by_active=modified_by_active(_person(rule, "LastModifiedBy")),
            last_fired=None, fire_rank=30, trigger="save",
            writes=[], write_basis="n/a", trigger_order=None,
            source="salesforce_validation_rules",
        ))

    # ---- Assignment rules -----------------------------------------------
    for rule in rows(raw.get("sf_assignment_rules.json")):
        obj = str(rule.get("SobjectType") or "")
        name = str(rule.get("Name") or "")
        out.append(Automation(
            key=f"assignment_rule:{obj}.{name}", kind="assignment_rule",
            kind_label="Assignment rule", name=name, obj=obj,
            active=bool(rule.get("Active")), version=None,
            last_modified=rule.get("LastModifiedDate"),
            last_modified_by=_person(rule, "LastModifiedBy"),
            modified_by_active=modified_by_active(_person(rule, "LastModifiedBy")),
            last_fired=None, fire_rank=60, trigger="assignment",
            writes=[f"{obj}.OwnerId"] if obj else [], write_basis="assignment_rule_implicit",
            trigger_order=None, source="salesforce_assignment_rules",
        ))

    # ---- Scheduled jobs --------------------------------------------------
    if cfg.get("include_scheduled_jobs", True):
        for job in rows(raw.get("sf_scheduled_jobs.json")):
            name = str(_get(job, "CronJobDetail", "Name", default=job.get("Name") or ""))
            creator = _get(job, "CreatedBy", "Name")
            out.append(Automation(
                key=f"scheduled_job:{name}", kind="scheduled_job", kind_label="Scheduled job",
                name=name, obj=str(job.get("ObjectHint") or ""),
                active=str(job.get("State") or "").upper() in ("WAITING", "ACQUIRED", "EXECUTING"),
                version=None, last_modified=job.get("CreatedDate"),
                last_modified_by=creator,
                modified_by_active=modified_by_active(creator),
                last_fired=job.get("PreviousFireTime"), fire_rank=95,
                trigger=str(job.get("CronExpression") or _get(job, "CronJobDetail", "JobType", default="")),
                writes=[], write_basis="n/a", trigger_order=None,
                source="salesforce_scheduled_jobs",
            ))

    return out


# -------------------------------------------------------------------- hubspot extraction


def _hs_flow_writes(flow: Dict[str, Any], obj: str) -> Tuple[List[str], str]:
    writes: List[str] = []
    for action in flow.get("actions") or []:
        type_id = str(action.get("actionTypeId") or "")
        fields = action.get("fields") or {}
        prop = fields.get("property_name") or fields.get("propertyName") or fields.get("property")
        # The old fallback — `"property" in str(fields.keys())` — was dead code that
        # always fired: `prop` is only set when a key named property_name/propertyName/
        # property exists, so the string test could never be false once `prop` was
        # truthy. That made the actionTypeId allow-list moot and counted every
        # property-reading action (branches, filters, delays) as a field WRITE,
        # inflating the write map and the same-field-conflict finding built on it.
        # An action with no declared type is still treated as a write, because an
        # unrecognised action that names a property is more safely over-reported than
        # missed — but a known non-write type is now correctly excluded.
        if prop and (type_id in HS_SET_PROPERTY_ACTIONS or not type_id):
            norm = _norm_field(obj, prop)
            if norm:
                writes.append(norm)
        # Nested branch actions
        for branch in action.get("connection", {}).get("actions", []) if isinstance(
                action.get("connection"), dict) else []:
            sub = (branch.get("fields") or {}).get("property_name")
            norm = _norm_field(obj, sub)
            if norm:
                writes.append(norm)
    return sorted(set(writes)), "hubspot_flow_actions"


def build_hubspot_automations(
    raw: Dict[str, Any], cfg: Dict[str, Any], now: datetime
) -> List[Automation]:
    ignore = {str(n).lower() for n in cfg.get("ignore_automation_names") or []}
    users_active: Dict[str, bool] = {}
    for owner in rows(raw.get("hs_owners.json")):
        label = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip() or owner.get("email", "")
        users_active[str(label)] = not bool(owner.get("archived"))
        users_active[str(owner.get("id"))] = not bool(owner.get("archived"))
        if owner.get("userId") is not None:
            users_active[str(owner.get("userId"))] = not bool(owner.get("archived"))

    out: List[Automation] = []
    for flow in rows(raw.get("hs_workflows.json")):
        name = str(flow.get("name") or flow.get("flowName") or f"flow-{flow.get('id')}")
        if name.lower() in ignore:
            continue
        obj = HS_OBJECT_TYPES.get(str(flow.get("objectTypeId") or ""), str(flow.get("objectTypeId") or "Contact"))
        writes, basis = _hs_flow_writes(flow, obj)
        editor = str(flow.get("updatedByUserId") or flow.get("updatedBy") or "")
        enrollment = flow.get("enrollmentCriteria") or {}
        trigger = str(enrollment.get("type") or flow.get("type") or "")
        out.append(Automation(
            key=f"hs_workflow:{flow.get('id')}", kind="hubspot_workflow", kind_label="HubSpot workflow",
            name=name, obj=obj,
            active=bool(flow.get("isEnabled", flow.get("enabled", False))),
            version=flow.get("revisionId"),
            last_modified=flow.get("updatedAt"),
            last_modified_by=editor or None,
            modified_by_active=users_active.get(editor),
            last_fired=flow.get("lastEnrollmentAt"),
            # HubSpot does not publish or guarantee an execution order between
            # workflows. Everything sits at one rank and the report says so.
            fire_rank=50, fire_label="order not published by HubSpot", trigger=trigger,
            writes=writes, write_basis=basis, trigger_order=None,
            source="hubspot_workflows",
        ))
    return out


# --------------------------------------------------------------------------- analyses


def find_conflicts(
    automations: Sequence[Automation], cfg: Dict[str, Any], crm: str
) -> List[Dict[str, Any]]:
    """
    CONFLICT DETECTION — the finding that earns the install.

    Method:
      1. Every automation contributes a set of `Object.Field` targets, extracted
         deterministically from flow metadata / workflow field updates /
         HubSpot set-property actions, and heuristically from Apex source.
      2. Group by (Object, Field).
      3. A field written by two or more ACTIVE automations is a conflict.
      4. Rank the contenders by the platform's documented order of execution.
         Two contenders at the SAME rank is the dangerous case: on Salesforce
         the order between same-rank automations is not guaranteed unless flow
         trigger ordering is set; HubSpot does not guarantee workflow order at
         all. Those get `order_guaranteed: false`.
      5. Inactive automations touching the same field are carried as context —
         someone will reactivate one.
    """
    ignore = {str(f).lower() for f in cfg.get("conflict_ignore_fields") or []}
    by_field: Dict[str, List[Automation]] = {}
    for auto in automations:
        for target in auto.writes:
            if target.lower() in ignore:
                continue
            by_field.setdefault(target, []).append(auto)

    conflicts: List[Dict[str, Any]] = []
    for target, contenders in sorted(by_field.items()):
        active = [a for a in contenders if a.active]
        if len(active) < 2:
            continue
        active.sort(key=lambda a: (a.fire_rank or 999, a.name))
        ranks = [a.fire_rank for a in active]
        same_rank = len(set(ranks)) < len(ranks)
        order_guaranteed = (crm == "salesforce") and not same_rank
        obj, field = target.split(".", 1)
        conflicts.append({
            "object": obj,
            "field": field,
            "target": target,
            "automation_count": len(active),
            "order_guaranteed": order_guaranteed,
            "same_rank_collision": same_rank,
            "trigger_order_set": any(a.trigger_order is not None for a in active),
            "basis": sorted({a.write_basis for a in active}),
            "automations": [
                {
                    "name": a.name,
                    "type": a.kind_label,
                    "fires": a.fire_label or FIRE_LABEL.get(a.fire_rank or 0, "order not determinable"),
                    "rank": a.fire_rank,
                    "last_modified": _iso(a.last_modified),
                    "last_modified_by": a.last_modified_by or "unknown",
                    "trigger_order": a.trigger_order,
                }
                for a in active
            ],
            "inactive_also_touching": [a.name for a in contenders if not a.active],
        })
    conflicts.sort(key=lambda c: (-c["automation_count"], c["order_guaranteed"], c["target"]))
    return conflicts


def find_orphans(
    automations: Sequence[Automation], cfg: Dict[str, Any], now: datetime
) -> List[Dict[str, Any]]:
    """
    Orphaned automation: active, and last touched by someone who no longer has
    an active account. Nobody currently employed knows why it exists.
    """
    if not cfg.get("flag_orphaned_automation", True):
        return []
    out = []
    for auto in automations:
        if not auto.active or auto.modified_by_active is not False:
            continue
        out.append({
            "Automation": auto.name,
            "Type": auto.kind_label,
            "Object": auto.obj or "—",
            "Last modified": _iso(auto.last_modified) or "unknown",
            "Last modified by": f"{auto.last_modified_by} (inactive)",
            "Fields written": ", ".join(auto.writes[:4]) or "none detected",
        })
    return out


def find_dormant(
    automations: Sequence[Automation], cfg: Dict[str, Any], now: datetime
) -> List[Dict[str, Any]]:
    """
    Active but apparently idle. Where a true last-fired timestamp exists
    (CronTrigger.PreviousFireTime, HubSpot lastEnrollmentAt) we use it and say
    so. Where it does not, last-modified is the proxy and the row says which
    basis it used — this is the difference between "we measured it" and
    "we inferred it", and the customer is entitled to know.
    """
    threshold = int(cfg.get("dormancy_days", 90))
    out = []
    for auto in automations:
        if not auto.active:
            continue
        fired_days = _days_ago(auto.last_fired, now)
        if fired_days is not None:
            basis, age = "last fired", fired_days
        else:
            modified = _days_ago(auto.last_modified, now)
            if modified is None:
                continue
            basis, age = "last modified (proxy)", modified
        if age >= threshold:
            out.append({
                "Automation": auto.name,
                "Type": auto.kind_label,
                "Object": auto.obj or "—",
                "Days idle": age,
                "Basis": basis,
                "Owner of last change": auto.last_modified_by or "unknown",
            })
    out.sort(key=lambda r: -r["Days idle"])
    return out


def find_recent_changes(
    automations: Sequence[Automation], audit_rows: List[Dict[str, Any]],
    cfg: Dict[str, Any], now: datetime
) -> List[Dict[str, Any]]:
    """The inverse of dormancy: what changed in the last N days."""
    window = int(cfg.get("recent_change_days", 14))
    out = []
    for auto in automations:
        age = _days_ago(auto.last_modified, now)
        if age is not None and age <= window:
            out.append({
                "Automation": auto.name,
                "Type": auto.kind_label,
                "Object": auto.obj or "—",
                "Changed": _iso(auto.last_modified),
                "By": auto.last_modified_by or "unknown",
                "Active": "yes" if auto.active else "no",
            })
    for entry in audit_rows:
        age = _days_ago(entry.get("CreatedDate"), now)
        if age is None or age > window:
            continue
        section = str(entry.get("Section") or "")
        if not re.search(r"flow|workflow|apex|validation|process|trigger|assignment", section, re.I):
            continue
        out.append({
            "Automation": str(entry.get("Display") or "")[:110],
            "Type": f"Setup audit — {section}",
            "Object": "—",
            "Changed": _iso(entry.get("CreatedDate")),
            "By": _get(entry, "CreatedBy", "Name", default="unknown"),
            "Active": "—",
        })
    out.sort(key=lambda r: r["Changed"], reverse=True)
    return out


def object_surface(
    automations: Sequence[Automation], raw: Dict[str, Any], crm: str, cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """Which objects carry automation, and which carry records but none."""
    with_automation: Dict[str, int] = {}
    for auto in automations:
        if auto.obj:
            with_automation[auto.obj] = with_automation.get(auto.obj, 0) + 1

    counts: Dict[str, int] = {}
    if crm == "salesforce":
        for entry in rows(raw.get("sf_record_counts.json")):
            name, count = entry.get("name"), entry.get("count")
            if name is not None and count is not None:
                counts[str(name)] = int(count)
    else:
        for entry in rows(raw.get("hs_object_counts.json")):
            name = entry.get("objectType") or entry.get("name")
            total = entry.get("total", entry.get("count"))
            if name is not None and total is not None:
                counts[str(name)] = int(total)

    floor = int(cfg.get("object_surface_min_records", 1000))
    bare = [
        {"Object": name, "Records": count, "Automation": 0}
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1])
        if count >= floor and name not in with_automation
    ]
    covered = [
        {"Object": name, "Records": counts.get(name, "unknown"), "Automation": n}
        for name, n in sorted(with_automation.items(), key=lambda kv: -kv[1])
    ]
    return {"with_automation": covered, "records_but_no_automation": bare,
            "record_floor": floor, "objects_counted": len(counts)}


# ---------------------------------------------------------------- integration users / apps


def analyse_integration_users(
    raw: Dict[str, Any], cfg: Dict[str, Any], crm: str, now: datetime
) -> Dict[str, Any]:
    extra = [str(p).lower() for p in cfg.get("extra_integration_user_patterns") or []]

    if crm == "hubspot":
        # In HubSpot the real "integration user" is a private-app token, not a
        # seat: the token carries the scopes and the writes. So the identity
        # inventory is private apps first, service-account-looking seats second.
        archived_owners = set()
        for owner in rows(raw.get("hs_owners.json")):
            if owner.get("archived"):
                label = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
                archived_owners.add(label or str(owner.get("email") or ""))

        detected: List[Dict[str, Any]] = []
        for app in rows(raw.get("hs_connected_apps.json")):
            if str(app.get("type") or "") != "private_app":
                continue
            owner_name = str(app.get("createdBy") or app.get("installedBy") or "")
            owner_gone = owner_name in archived_owners
            scopes = [str(s) for s in (app.get("scopes") or [])]
            write_scopes = [s for s in scopes if s.endswith(".write") or ".write" in s]
            idle = _days_ago(app.get("lastUsedAt") or app.get("lastUsed"), now)
            detected.append({
                "Name": str(app.get("name") or ""),
                "Login": "private app token",
                "Type": "HubSpot private app",
                "Active": "yes",
                "Last login": _iso(app.get("lastUsedAt") or app.get("lastUsed")) or "never recorded",
                "Write access": ", ".join(write_scopes[:6]) if write_scopes else "none detected",
                "Owner": f"{owner_name} ({'deactivated' if owner_gone else 'active'})"
                         if owner_name else "unassigned",
                "_write": bool(write_scopes),
                "_owner_inactive": owner_gone,
                "_owner_name": owner_name,
                "_active": True,
                "_unused_days": idle,
            })

        for user in rows(raw.get("hs_users.json")):
            email = str(user.get("email") or "")
            name = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
            blob = f"{email} {name}"
            if not (fp.looks_like_service_account(blob) or any(p in blob.lower() for p in extra)):
                continue
            detected.append({
                "Name": name or email,
                "Login": email,
                "Type": "HubSpot seat used as a service account",
                "Active": "yes",
                "Last login": "not exposed by the API",
                "Write access": "cannot be measured — see note",
                "Owner": "—",
                "_write": False,
                "_owner_inactive": False,
                "_owner_name": "",
                "_active": True,
                "_unused_days": None,
            })

        return {
            "users": detected,
            "note": (
                "HubSpot exposes no per-object permission API for seats, so a human seat's write "
                "surface cannot be measured — only a private app's scopes can. Seats listed here "
                "are flagged on naming convention alone."
            ),
        }

    users = rows(raw.get("sf_users.json"))
    by_id = {str(u.get("Id") or ""): u for u in users}
    logins: Dict[str, List[Dict[str, Any]]] = {}
    for entry in rows(raw.get("sf_login_history.json")):
        logins.setdefault(str(entry.get("UserId") or ""), []).append(entry)

    # Which permission sets grant edit, and to whom.
    write_permsets = set()
    permset_objects: Dict[str, List[str]] = {}
    for perm in rows(raw.get("sf_object_permissions.json")):
        if perm.get("PermissionsEdit") or perm.get("PermissionsModifyAllRecords"):
            parent = str(perm.get("ParentId") or "")
            write_permsets.add(parent)
            permset_objects.setdefault(parent, []).append(str(perm.get("SobjectType") or ""))

    user_writes: Dict[str, List[str]] = {}
    for assignment in rows(raw.get("sf_permset_assignments.json")):
        assignee = str(assignment.get("AssigneeId") or "")
        permset_id = str(assignment.get("PermissionSetId") or "")
        if permset_id in write_permsets:
            user_writes.setdefault(assignee, []).extend(permset_objects.get(permset_id, []))
        if _get(assignment, "PermissionSet", "PermissionsModifyAllData"):
            user_writes.setdefault(assignee, []).append("ALL (Modify All Data)")

    detected = []
    for user in users:
        uid = str(user.get("Id") or "")
        username = str(user.get("Username") or "")
        name = str(user.get("Name") or "")
        email = str(user.get("Email") or "")
        profile = str(_get(user, "Profile", "Name", default=""))
        licence = str(_get(user, "Profile", "UserLicense", "Name", default=""))
        blob = f"{name} {username} {email} {profile} {licence}"

        is_integration = (
            fp.looks_like_service_account(name, username, email)
            or "integration" in licence.lower()
            or "api only" in profile.lower()
            or "integration" in profile.lower()
            or str(user.get("UserType") or "") == "IntegrationUser"
            or any(p in blob.lower() for p in extra)
        )
        if not is_integration:
            continue

        # "Owner" of a service account = its manager, falling back to whoever
        # created it. When that person is gone, nobody owns the credential.
        owner_id = str(user.get("ManagerId") or "") or str(user.get("CreatedById") or "")
        owner = by_id.get(owner_id, {})
        owner_name = (_get(user, "Manager", "Name")
                      or _get(user, "CreatedBy", "Name")
                      or owner.get("Name") or "")
        owner_active = _get(user, "Manager", "IsActive")
        if owner_active is None:
            owner_active = _get(user, "CreatedBy", "IsActive")
        if owner_active is None and owner:
            owner_active = owner.get("IsActive")

        last_login = user.get("LastLoginDate")
        for entry in logins.get(uid, []):
            if not last_login or str(entry.get("LoginTime") or "") > str(last_login):
                last_login = entry.get("LoginTime")
        idle = _days_ago(last_login, now)
        writes = sorted(set(user_writes.get(uid, [])))

        detected.append({
            "Name": name,
            "Login": username,
            "Type": profile or str(user.get("UserType") or ""),
            "Active": "yes" if user.get("IsActive") else "no",
            "Last login": _iso(last_login) or "never",
            "Write access": ", ".join(writes[:6]) if writes else "none detected",
            "Owner": (f"{owner_name} ({'inactive' if owner_active is False else 'active'})"
                      if owner_name else "unassigned"),
            "_write": bool(writes),
            "_owner_inactive": owner_active is False,
            "_owner_name": owner_name,
            "_active": bool(user.get("IsActive")),
            "_unused_days": idle,
        })

    # API client mix from login history — the only per-consumer volume signal
    # available without Shield Event Monitoring.
    app_mix: Dict[str, int] = {}
    for entry in rows(raw.get("sf_login_history.json")):
        app = str(entry.get("Application") or "unknown")
        app_mix[app] = app_mix.get(app, 0) + 1

    return {"users": detected,
            "login_application_mix": sorted(
                ({"Application": k, "Logins (90d)": v} for k, v in app_mix.items()),
                key=lambda r: -r["Logins (90d)"])[:20],
            "note": ""}


def analyse_apps(
    raw: Dict[str, Any], cfg: Dict[str, Any], crm: str, now: datetime
) -> Dict[str, Any]:
    dormancy = int(cfg.get("dormancy_days", 90))
    apps: List[Dict[str, Any]] = []

    if crm == "salesforce":
        users_active = {str(u.get("Name") or ""): bool(u.get("IsActive"))
                        for u in rows(raw.get("sf_users.json"))}
        usage: Dict[str, Dict[str, Any]] = {}
        for token in rows(raw.get("sf_oauth_tokens.json")):
            name = str(token.get("AppName") or "")
            current = usage.get(name, {"last": None, "count": 0})
            last = token.get("LastUsedDate")
            if last and (current["last"] is None or str(last) > str(current["last"])):
                current["last"] = last
            current["count"] += int(token.get("UseCount") or 0)
            usage[name] = current

        for app in rows(raw.get("sf_connected_apps.json")):
            name = str(app.get("Name") or "")
            creator = _get(app, "CreatedBy", "Name", default="")
            creator_active = users_active.get(str(creator))
            seen = usage.get(name, {})
            idle = _days_ago(seen.get("last"), now)
            orphan_reasons = []
            if creator_active is False:
                # The installer's name lives in the "Installed by" column, which the
                # report can redact. Keep it out of this free-text reason so PII
                # redaction cannot be defeated by a sentence.
                orphan_reasons.append("installed by an account that is now inactive")
            if idle is None:
                orphan_reasons.append("no recorded OAuth use at all")
            elif idle >= dormancy:
                orphan_reasons.append(f"last used {idle} days ago")
            apps.append({
                "App": name,
                "Installed": _iso(app.get("CreatedDate")) or "unknown",
                "Installed by": f"{creator} ({'inactive' if creator_active is False else 'active'})"
                                if creator else "unknown",
                "Last used": _iso(seen.get("last")) or "never recorded",
                "Uses": seen.get("count", 0),
                "Orphaned": "; ".join(orphan_reasons) or "no",
                "_orphaned": bool(orphan_reasons),
                "_reasons": orphan_reasons,
            })

        packages = []
        namespaces: Dict[str, str] = {}
        ignore_ns = {str(n).lower() for n in cfg.get("ignore_namespaces") or []}
        for pkg in rows(raw.get("sf_installed_packages.json")):
            ns = str(_get(pkg, "SubscriberPackage", "NamespacePrefix",
                          default=pkg.get("NamespacePrefix") or "") or "")
            label = str(_get(pkg, "SubscriberPackage", "Name", default=pkg.get("Name") or "") or "")
            if ns.lower() in ignore_ns:
                continue
            if ns:
                namespaces[ns] = label
            packages.append({
                "Package": label or ns or "(unnamed)",
                "Namespace": ns or "—",
                "Version": str(_get(pkg, "SubscriberPackageVersion", "Name", default="") or ""),
                "Publisher": str(_get(pkg, "SubscriberPackage", "PublisherName", default="") or ""),
            })
        return {"apps": apps, "packages": packages, "namespaces": namespaces}

    # HubSpot — connected apps come from a manual export when one exists.
    owners_archived = {}
    for owner in rows(raw.get("hs_owners.json")):
        label = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip() or str(owner.get("email") or "")
        owners_archived[label] = bool(owner.get("archived"))

    for app in rows(raw.get("hs_connected_apps.json")):
        name = str(app.get("name") or app.get("appName") or "")
        installer = str(app.get("installedBy") or app.get("createdBy") or "")
        installer_gone = owners_archived.get(installer)
        idle = _days_ago(app.get("lastUsedAt") or app.get("lastUsed"), now)
        reasons = []
        if installer_gone:
            reasons.append("installed by an account that is now deactivated")
        if idle is None:
            reasons.append("no recorded use")
        elif idle >= dormancy:
            reasons.append(f"last used {idle} days ago")
        apps.append({
            "App": name,
            "Installed": _iso(app.get("installedAt") or app.get("createdAt")) or "unknown",
            "Installed by": f"{installer} ({'deactivated' if installer_gone else 'active'})"
                            if installer else "unknown",
            "Last used": _iso(app.get("lastUsedAt") or app.get("lastUsed")) or "never recorded",
            "Uses": app.get("callsLast30Days", "—"),
            "Orphaned": "; ".join(reasons) or "no",
            "_orphaned": bool(reasons),
            "_reasons": reasons,
        })
    return {"apps": apps, "packages": [], "namespaces": {}}


# --------------------------------------------------------------------------- orchestration


RAW_FILES_SALESFORCE = [
    "sf_org.json", "sf_users.json", "sf_login_history.json", "sf_oauth_tokens.json",
    "sf_permset_assignments.json", "sf_object_permissions.json", "sf_connected_apps.json",
    "sf_installed_packages.json", "sf_flows.json", "sf_flow_versions.json",
    "sf_flow_metadata.json", "sf_apex_triggers.json", "sf_apex_classes.json",
    "sf_validation_rules.json", "sf_workflow_rules.json", "sf_workflow_field_updates.json",
    "sf_assignment_rules.json", "sf_scheduled_jobs.json", "sf_entities.json",
    "sf_record_counts.json", "sf_field_definitions.json", "sf_setup_audit_trail.json",
]
RAW_FILES_HUBSPOT = [
    "hs_account.json", "hs_users.json", "hs_owners.json", "hs_workflows.json",
    "hs_properties.json", "hs_schemas.json", "hs_object_counts.json",
    "hs_api_usage.json", "hs_connected_apps.json", "hs_login_activity.json",
]

AUTOMATION_SOURCES = {
    "salesforce_flows", "salesforce_apex_triggers", "salesforce_workflow_rules",
    "salesforce_validation_rules", "salesforce_assignment_rules",
    "salesforce_scheduled_jobs", "hubspot_workflows",
}


def load_raw(raw_dir: Path, crm: str) -> Dict[str, Any]:
    names = RAW_FILES_SALESFORCE if crm == "salesforce" else RAW_FILES_HUBSPOT
    out: Dict[str, Any] = {}
    for name in names:
        out[name] = _read_json(raw_dir / name)
    return out


def build_manifest(
    sources_doc: Dict[str, Any], raw: Dict[str, Any], run_dir: Path, window: Dict[str, str],
    automation_total: int, cfg: Dict[str, Any],
) -> Tuple[RunManifest, List[str]]:
    """
    Record every declared source, and turn every unreadable one into an entry in
    `unavailable` carrying the exact permission needed. This is the honesty
    contract: a section we could not read is never silently a clean section.
    """
    man = RunManifest(PLUGIN, run_dir, window=window)
    unavailable: List[str] = []
    declared = sources_doc.get("sources") or []
    seen = set()

    for entry in declared:
        name = str(entry.get("name") or "")
        seen.add(name)
        surface = SURFACES.get(name, {})
        payload = raw.get(str(entry.get("file") or ""))
        count = len(rows(payload)) if entry.get("ok", True) else 0
        required = bool(entry.get("required", surface.get("required", False)))
        permission = entry.get("permission") or surface.get("permission", "")
        label = surface.get("label", name)
        api = surface.get("api", "")

        diagnosis = entry.get("diagnosis") or ""
        if not diagnosis and permission:
            diagnosis = f"The connected identity cannot read {api or name}. Needed: {permission}"

        man.record(
            name, tool=str(entry.get("tool") or "unknown"), count=count,
            query=str(entry.get("query") or ""), required=required,
            note=str(entry.get("note") or ""), diagnosis=diagnosis,
        )
        if not entry.get("ok", True):
            reason = str(entry.get("error") or "").strip()
            unavailable.append(
                f"{label} — {api or name}. Could not be read. "
                + (f"Reported: {reason}. " if reason else "")
                + (f"Fix: {permission}" if permission else "No permission mapping recorded.")
            )
        elif count == 0:
            # Read successfully, returned nothing. That is usually "this instance has
            # none", and telling a customer to grant a permission they already hold is
            # its own kind of dishonesty. It still belongs on the list, because
            # Salesforce can answer an unpermitted read with an empty set rather than
            # an error — so name both readings and let them judge.
            unavailable.append(
                f"{label} — {api or name}. Read successfully and came back empty: either "
                f"this instance has none, or the connected identity can see the object but "
                f"none of its records."
                + (f" If you expected some, check: {permission}" if permission else "")
            )

    # Surfaces the run never even attempted still count as not-covered.
    crm = str(sources_doc.get("crm") or "salesforce")
    for name, surface in SURFACES.items():
        if surface.get("crm") != crm or name in seen:
            continue
        unavailable.append(
            f"{surface['label']} — {surface['api']}. Not attempted in this run. "
            f"Fix: {surface['permission']}"
        )
        man.warn(f"{name} was never queried")

    # The synthetic gate. If every automation surface came back empty we are
    # blind, not clean, and the run must stop.
    man.record(
        "automation_inventory",
        tool="derived",
        count=automation_total if not cfg.get("allow_empty_automation") else max(automation_total, 1),
        query="union of every readable automation surface",
        required=True,
        note="Synthetic source: the union of every automation surface that was readable.",
        diagnosis=(
            "Zero automations across every surface. Either the metadata surfaces are unreadable "
            "(see the unavailable list above for the exact permission each one needs) or this "
            "instance genuinely has no automation. If you have confirmed the latter, re-run with "
            "--allow-empty-automation to record it deliberately."
        ),
    )
    return man, unavailable


def main() -> int:
    parser = argparse.ArgumentParser(description="system-map — raw/*.json to findings.json")
    parser.add_argument("--run-dir", help="run directory containing raw/ ; findings.json is written here")
    parser.add_argument("--raw", help="explicit raw/ directory (use with --out, e.g. for fixtures)")
    parser.add_argument("--out", help="output directory when --raw is used")
    parser.add_argument("--config", help="path to a config json; defaults to ~/.leanscale-gtm/system-map.json")
    parser.add_argument("--allow-empty-automation", action="store_true",
                        help="record a genuinely automation-free instance instead of aborting")
    args = parser.parse_args()

    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
        raw_dir = run_dir / "raw"
    elif args.raw:
        raw_dir = Path(args.raw).expanduser().resolve()
        run_dir = Path(args.out).expanduser().resolve() if args.out else raw_dir.parent
    else:
        parser.error("pass --run-dir, or --raw with --out")
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)

    cfg = dict(DEFAULTS)
    try:
        cfg.update(load_plugin_config(PLUGIN, defaults=DEFAULTS))
    except ConfigError as exc:
        print(f"config warning: {exc}", file=sys.stderr)
    if args.config:
        override = _read_json(Path(args.config).expanduser())
        if isinstance(override, dict):
            cfg.update({k: v for k, v in override.items() if not k.startswith("_")})
    if args.allow_empty_automation:
        cfg["allow_empty_automation"] = True

    try:
        profile = load_profile(required=False)
    except ConfigError:
        profile = {}

    sources_doc = _read_json(raw_dir / "_sources.json") or {}
    if not sources_doc:
        print(
            f"No {raw_dir / '_sources.json'} found. The :run skill writes it alongside the raw "
            f"pulls and it records which metadata surfaces were readable. Without it this run "
            f"cannot tell 'clean' from 'blind', so it stops here.",
            file=sys.stderr,
        )
        return 2

    crm = str(sources_doc.get("crm") or (profile.get("crm") or {}).get("system") or "salesforce").lower()
    environment = str(sources_doc.get("environment") or cfg.get("environment") or "production").lower()
    org_label = (sources_doc.get("org_label") or cfg.get("org_label")
                 or profile.get("org_name") or "Your organization")

    now = datetime.now(timezone.utc)
    window = {
        "start": (now - timedelta(days=int(cfg.get("dormancy_days", 90)))).strftime("%Y-%m-%d"),
        "end": now.strftime("%Y-%m-%d"),
    }

    raw = load_raw(raw_dir, crm)

    if crm == "hubspot":
        automations = build_hubspot_automations(raw, cfg, now)
        audit_rows: List[Dict[str, Any]] = []
    else:
        automations = build_salesforce_automations(raw, cfg, now)
        audit_rows = rows(raw.get("sf_setup_audit_trail.json"))

    if not cfg.get("include_automation", True):
        automations = []

    man, unavailable = build_manifest(sources_doc, raw, run_dir, window, len(automations), cfg)
    try:
        man.finalize()
    except SourceEmptyError as exc:
        print(str(exc), file=sys.stderr)
        if unavailable:
            print("\nMetadata surfaces this run could not read:", file=sys.stderr)
            for item in unavailable:
                print(f"  · {item}", file=sys.stderr)
        return 3

    # ---------------------------------------------------------------- analyses
    conflicts = find_conflicts(automations, cfg, crm)
    orphans = find_orphans(automations, cfg, now) if cfg.get("flag_orphaned_automation", True) else []
    dormant = find_dormant(automations, cfg, now)
    recent = find_recent_changes(automations, audit_rows, cfg, now)
    surface = object_surface(automations, raw, crm, cfg)

    users_section = (analyse_integration_users(raw, cfg, crm, now)
                     if cfg.get("include_integration_users", True) else {"users": []})
    apps_section = (analyse_apps(raw, cfg, crm, now)
                    if cfg.get("include_connected_apps", True) else
                    {"apps": [], "packages": [], "namespaces": {}})
    if not cfg.get("include_managed_packages", True):
        apps_section["packages"] = []
        apps_section["namespaces"] = {}

    # Inactive flow version pile-up.
    version_rows = rows(raw.get("sf_flow_versions.json"))
    obsolete_versions: Dict[str, int] = {}
    for version in version_rows:
        if str(version.get("Status") or "").lower() in ("obsolete", "draft", "invaliddraft"):
            label = str(version.get("Label") or version.get("ApiName") or version.get("FlowDefinitionViewId") or "?")
            obsolete_versions[label] = obsolete_versions.get(label, 0) + 1

    # ---------------------------------------------------------------- stack map
    field_names = [str(f.get("QualifiedApiName") or "") for f in rows(raw.get("sf_field_definitions.json"))]
    property_names = [str(p.get("name") or "") for p in rows(raw.get("hs_properties.json"))]
    detections = fp.detect({
        "namespaces": apps_section.get("namespaces") or {},
        "apps": ([a["App"] for a in apps_section.get("apps") or []]
                 + [p.get("Publisher", "") for p in apps_section.get("packages") or []]
                 + [p.get("Package", "") for p in apps_section.get("packages") or []]),
        "users": [f"{u.get('Name', '')} {u.get('Login', '')}" for u in users_section.get("users") or []],
        "field_names": field_names,
        "property_names": property_names,
    })
    unknown_ns = fp.unidentified_namespaces(apps_section.get("namespaces") or {}, detections)
    believed = cfg.get("believed_tools") or []
    belief = fp.match_believed(believed, detections)

    clustered: Dict[str, List[Dict[str, Any]]] = {}
    for hit in detections:
        clustered.setdefault(hit["cluster"], []).append(hit)

    # ---------------------------------------------------------------- doc
    doc = FindingsDoc(
        plugin=PLUGIN,
        window=window,
        org_name=f"{org_label} ({environment})",
        unavailable=unavailable,
    )

    write_users = [u for u in users_section.get("users", []) if u.get("_write")]
    orphan_owner_users = [u for u in users_section.get("users", []) if u.get("_owner_inactive")]
    orphan_apps = [a for a in apps_section.get("apps", []) if a.get("_orphaned")]

    doc.add_score(Score(
        key="automations_counted", label="Automations counted", value=len(automations),
        unit="count", direction_good="flat",
        context=f"{sum(1 for a in automations if a.active)} active across "
                f"{len({a.obj for a in automations if a.obj})} objects",
    ))
    doc.add_score(Score(
        key="orphaned_automations", label="Orphaned automations", value=len(orphans),
        unit="count", direction_good="down",
        context="active, last changed by someone whose account is gone",
    ))
    doc.add_score(Score(
        key="contested_fields", label="Fields written by 2+ automations", value=len(conflicts),
        unit="count", direction_good="down",
        context=f"{sum(1 for c in conflicts if not c['order_guaranteed'])} with no guaranteed firing order",
    ))
    doc.add_score(Score(
        key="integration_users_write", label="Integration users with write access",
        value=len(write_users), unit="count", direction_good="down",
        context=f"{len(users_section.get('users', []))} integration identities found in total",
    ))
    doc.add_score(Score(
        key="tools_detected", label="Tools detected vs believed", value=len(detections),
        unit="count", direction_good="flat",
        context=f"you named {len(believed)} · {len(belief['undisclosed'])} undisclosed · "
                f"{len(belief['claimed_not_found'])} named but not found",
    ))

    # ------------------------------------------------------------- findings
    if conflicts:
        unguarded = [c for c in conflicts if not c["order_guaranteed"]]
        rows_out = []
        for conflict in conflicts[:40]:
            rows_out.append({
                "Object": conflict["object"],
                "Field": conflict["field"],
                "Automations": conflict["automation_count"],
                "Firing order": " → ".join(
                    f"{a['name']} [{a['fires']}]" for a in conflict["automations"][:4]
                ),
                "Order guaranteed": "yes" if conflict["order_guaranteed"] else "NO",
            })
        doc.add(Finding(
            id="same-field-write-conflicts",
            severity="critical" if unguarded else "high",
            title=f"{len(conflicts)} field(s) are written by more than one active automation",
            what=(
                f"{len(conflicts)} object/field targets have two or more active automations writing "
                f"them. {len(unguarded)} of those have no guaranteed firing order, which means the "
                f"final value depends on which automation the platform happens to run last."
                + (" HubSpot does not publish an execution order between workflows at all, so every "
                   "collision here is last-writer-wins with no way to predict the winner."
                   if crm == "hubspot" else
                   " Salesforce orders automation by type, but between two automations of the same "
                   "type on the same object the order is undefined unless flow trigger ordering is set.")
            ),
            why_it_matters=(
                "This is where silent data corruption comes from, and it is the source of every "
                "\"why did this field change?\" ticket your admin cannot answer. A field two "
                "automations fight over is not a field you can report on: the value in it is the "
                "outcome of a race, not a decision."
            ),
            recommended_fix=(
                "Take the highest-traffic contested field first. Decide which single automation owns "
                "it, strip the write out of the others, and — on Salesforce — set explicit flow "
                "trigger ordering for any same-object flows you keep. Write the owner into the field "
                "description so the next admin inherits the decision rather than rediscovering it."
            ),
            evidence={
                "count": len(conflicts),
                "rows": rows_out,
                "sample_ids": [c["target"] for c in conflicts[:10]],
                "query": _conflict_query(crm),
            },
            effort="project", owner_hint="RevOps / Salesforce admin" if crm == "salesforce" else "RevOps / HubSpot admin",
        ))

    if orphan_owner_users:
        doc.add(Finding(
            id="integration-user-owner-departed",
            severity="critical",
            title=f"{len(orphan_owner_users)} integration user(s) are owned by someone who has left",
            what=(
                "These service accounts are still active and still authenticating, but the person "
                "listed as their manager or creator no longer has an active account: "
                + ", ".join(f"{u['Name']} (owner {u.get('_owner_name') or 'unknown'})"
                            for u in orphan_owner_users[:6]) + "."
            ),
            why_it_matters=(
                "A live credential with no owner is the definition of unmanaged access. Nobody will "
                "rotate it, nobody will notice when it breaks, and nobody can tell you what it "
                "writes. If it holds write access it can corrupt data with no one to call."
            ),
            recommended_fix=(
                "Reassign each one to a named, current owner today — the person who would be paged "
                "if it stopped. Then confirm what it actually connects to, and deactivate the ones "
                "nobody can account for. Deactivate rather than delete so you can reverse it."
            ),
            evidence={
                "count": len(orphan_owner_users),
                "rows": [{k: v for k, v in u.items() if not k.startswith("_")}
                         for u in orphan_owner_users],
                "query": _integration_user_query(crm),
            },
            effort="quick", owner_hint="RevOps + IT/Security",
        ))

    if orphan_apps:
        doc.add(Finding(
            id="orphaned-connected-apps",
            severity="high",
            title=f"{len(orphan_apps)} connected app(s) are orphaned",
            what=(
                "These apps still hold a grant against your CRM but are either unused past the "
                f"{cfg.get('dormancy_days', 90)}-day threshold, or were installed by someone whose "
                "account is now inactive: "
                + ", ".join(a["App"] for a in orphan_apps[:6]) + "."
            ),
            why_it_matters=(
                "Every one of these is a standing key to your customer data held by software nobody "
                "is watching. Unused grants are the cheapest thing in your stack to revoke and the "
                "most expensive thing to explain in a security review."
            ),
            recommended_fix=(
                "Revoke the grants with no recorded use. For the rest, name a current owner per app "
                "and record it somewhere durable. Do the inactive-installer ones first — those are "
                "the ones where nobody will object because nobody knows they exist."
            ),
            evidence={
                "count": len(orphan_apps),
                "rows": [{k: v for k, v in a.items() if not k.startswith("_")} for a in orphan_apps],
                "query": _connected_app_query(crm),
            },
            effort="quick", owner_hint="RevOps + IT/Security",
        ))

    if orphans:
        doc.add(Finding(
            id="orphaned-automation",
            severity="high",
            title=f"{len(orphans)} active automation(s) were last changed by a departed user",
            what=(
                "These are running in production right now and the last person to touch them no "
                "longer has an account. Nobody currently employed can tell you why they exist."
            ),
            why_it_matters=(
                "Orphaned automation is what makes a CRM feel haunted. It fires, it writes fields, "
                "and the team works around it because changing it feels dangerous. That workaround "
                "cost compounds every quarter it survives."
            ),
            recommended_fix=(
                "Assign each one an owner this week. For any you cannot justify, deactivate rather "
                "than delete, wait a full business cycle, then remove. Deactivation is reversible in "
                "a minute; deletion of a flow with subflows is not."
            ),
            evidence={"count": len(orphans), "rows": orphans[:30],
                      "query": _automation_owner_query(crm)},
            effort="medium", owner_hint="RevOps",
        ))

    if write_users:
        idle_writers = [u for u in write_users
                        if u.get("_unused_days") is not None
                        and u["_unused_days"] >= int(cfg.get("dormancy_days", 90))]
        severity = "high" if idle_writers else "medium"
        doc.add(Finding(
            id="integration-users-write-access",
            severity=severity,
            title=f"{len(write_users)} integration user(s) hold write access"
                  + (f", {len(idle_writers)} of them unused" if idle_writers else ""),
            what=(
                f"{len(write_users)} service accounts can create or edit records. "
                + (f"{len(idle_writers)} have not authenticated in "
                   f"{cfg.get('dormancy_days', 90)}+ days but still hold that access."
                   if idle_writers else
                   "All of them have authenticated recently, which at least means they are in use.")
            ),
            why_it_matters=(
                "An unused credential with write access is pure downside: it cannot be delivering "
                "value, and it can still overwrite your pipeline. This is also the first list a "
                "security reviewer asks for, and not having it is its own finding."
            ),
            recommended_fix=(
                "For each: name the system it belongs to, name a human owner, and cut its object "
                "permissions to only the objects it actually writes. Deactivate anything with no "
                "login inside the dormancy window. Replace any 'Modify All Data' grant with a "
                "scoped permission set — that one is almost never necessary."
            ),
            evidence={
                "count": len(write_users),
                "rows": [{k: v for k, v in u.items() if not k.startswith("_")} for u in write_users],
                "query": _integration_user_query(crm),
            },
            effort="medium", owner_hint="RevOps + IT/Security",
        ))

    if dormant:
        proxy_only = all(row["Basis"].startswith("last modified") for row in dormant)
        doc.add(Finding(
            id="dormant-automation",
            severity="medium",
            title=f"{len(dormant)} active automation(s) look dormant",
            what=(
                f"Active, but idle for {cfg.get('dormancy_days', 90)}+ days. "
                + ("Every row here is measured against last-modified date, not execution — "
                   "true execution counts need the platform's event monitoring."
                   if proxy_only else
                   "Rows marked 'last fired' are measured; rows marked 'proxy' are inferred from "
                   "last-modified date.")
            ),
            why_it_matters=(
                "Dormant automation is the tax you pay on every future change. Each one has to be "
                "read, understood and ruled out before anyone can safely touch the object it sits "
                "on — which is why simple requests turn into two-week projects."
            ),
            recommended_fix=(
                "Deactivate in batches by object, starting with the object your team changes most. "
                "Watch one full business cycle before deleting anything. If you have event "
                "monitoring, confirm zero executions first rather than trusting the proxy."
            ),
            evidence={"count": len(dormant), "rows": dormant[:30],
                      "query": _dormancy_query(crm)},
            effort="medium", owner_hint="RevOps",
        ))

    if recent:
        doc.add(Finding(
            id="recent-automation-changes",
            severity="low",
            title=f"{len(recent)} automation change(s) in the last {cfg.get('recent_change_days', 14)} days",
            what="Recent edits to production automation, so you can tie any new data weirdness to a change.",
            why_it_matters=(
                "The most common cause of \"the numbers moved and nobody knows why\" is an "
                "undocumented automation change from the previous fortnight. Having this list to "
                "hand turns a two-day investigation into a five-minute one."
            ),
            recommended_fix=(
                "Check each change against your change log. Anything not in the log is a process "
                "gap, not a technical one — that is the thing to fix."
            ),
            evidence={"count": len(recent), "rows": recent[:30],
                      "query": _recent_change_query(crm)},
            effort="quick", owner_hint="RevOps",
        ))

    if obsolete_versions:
        total_versions = sum(obsolete_versions.values())
        doc.add(Finding(
            id="inactive-flow-versions",
            severity="low",
            title=f"{total_versions} inactive flow versions across {len(obsolete_versions)} flows",
            what="Obsolete and draft flow versions accumulating behind active ones.",
            why_it_matters=(
                "Harmless at runtime, expensive at read time: an admin opening a flow with fourteen "
                "versions cannot tell at a glance which logic is live, and half-finished drafts get "
                "activated by mistake."
            ),
            recommended_fix=(
                "Delete obsolete versions for flows with more than three. Keep the active version "
                "and one rollback. Drafts older than a quarter are abandoned work — delete them."
            ),
            evidence={
                "count": total_versions,
                "rows": [{"Flow": k, "Inactive versions": v}
                         for k, v in sorted(obsolete_versions.items(), key=lambda kv: -kv[1])[:25]],
                "query": "SELECT Label, ApiName, VersionNumber, Status FROM FlowVersionView "
                         "WHERE Status != 'Active' ORDER BY Label",
            },
            effort="quick", owner_hint="Salesforce admin",
        ))

    if surface["records_but_no_automation"]:
        doc.add(Finding(
            id="objects-with-records-no-automation",
            severity="medium",
            title=f"{len(surface['records_but_no_automation'])} object(s) hold records but carry no automation",
            what=(
                f"Objects with at least {surface['record_floor']:,} records and zero automation "
                "attached: " + ", ".join(r["Object"] for r in surface["records_but_no_automation"][:8]) + "."
            ),
            why_it_matters=(
                "An object with tens of thousands of records and no validation, no assignment and no "
                "triggered logic is usually a dumping ground — something writes to it and nothing "
                "governs it. Those are the objects whose data quality nobody has ever checked."
            ),
            recommended_fix=(
                "For each: find out what writes to it and whether anyone reads it. If it is a "
                "genuine integration landing zone, say so in the object description. If nobody "
                "reads it, stop writing to it."
            ),
            evidence={
                "count": len(surface["records_but_no_automation"]),
                "rows": surface["records_but_no_automation"][:25],
                "query": _object_surface_query(crm),
            },
            effort="medium", owner_hint="RevOps",
        ))

    if belief["undisclosed"]:
        doc.add(Finding(
            id="undisclosed-connected-tools",
            severity="high",
            title=f"{len(belief['undisclosed'])} connected tool(s) nobody named in setup",
            what=(
                "Detected in the instance but absent from the list your team gave: "
                + ", ".join(belief["undisclosed"][:10])
                + ("…" if len(belief["undisclosed"]) > 10 else "") + "."
            ),
            why_it_matters=(
                "The gap between the stack your team can name and the stack that actually holds a "
                "connection is the whole point of this run. Every unnamed tool is a system writing "
                "to your CRM that nobody budgeted for, nobody reviews, and nobody will notice "
                "breaking."
            ),
            recommended_fix=(
                "Walk the list with whoever owns the CRM. For each: is it still in use, who owns it, "
                "and does it write? Kill the dead ones. The survivors go on a one-page stack "
                "inventory with an owner beside each name."
            ),
            evidence={
                "count": len(belief["undisclosed"]),
                "rows": [
                    {"Tool": d["tool"], "Cluster": d["cluster"], "Confidence": d["confidence"],
                     "Detected via": "; ".join(d["signals"][:3])}
                    for d in detections if d["tool"] in set(belief["undisclosed"])
                ][:30],
                "query": _fingerprint_query(crm),
            },
            effort="quick", owner_hint="RevOps",
        ))

    if belief["claimed_not_found"]:
        doc.add(Finding(
            id="believed-tools-not-detected",
            severity="medium",
            title=f"{len(belief['claimed_not_found'])} tool(s) you named leave no trace in the instance",
            what=(
                "Named in setup but showing no package, app, property prefix or service account: "
                + ", ".join(belief["claimed_not_found"]) + "."
            ),
            why_it_matters=(
                "Two possibilities, both worth knowing. Either the tool is not actually connected to "
                "the CRM — so whatever workflow depends on that sync is quietly broken — or it "
                "connects in a way this run cannot see, which means your inventory has a blind spot."
            ),
            recommended_fix=(
                "Check each in the tool's own admin console for an active CRM connection. If it does "
                "connect but left no trace here, it is probably going through a middleware account — "
                "find out which one, and add it to the integration-user list."
            ),
            evidence={
                "count": len(belief["claimed_not_found"]),
                "rows": [{"Tool named in setup": t, "Evidence found": "none"}
                         for t in belief["claimed_not_found"]],
                "query": _fingerprint_query(crm),
            },
            effort="quick", owner_hint="RevOps",
        ))

    if unknown_ns:
        doc.add(Finding(
            id="unidentified-package-namespaces",
            severity="low",
            title=f"{len(unknown_ns)} installed package(s) this run could not identify",
            what=("Installed, holding a namespace, and not matched to any known GTM tool: "
                  + ", ".join(f"{n['namespace']} ({n['package']})" for n in unknown_ns[:8]) + "."),
            why_it_matters=(
                "Reported rather than dropped on purpose. An unrecognised managed package may be a "
                "niche vendor, an old proof of concept, or something a consultant installed in 2019. "
                "All three are worth a look."
            ),
            recommended_fix=(
                "Open each in the installed-packages screen and check the publisher and licence "
                "count. Uninstall anything with zero assigned licences and no dependent metadata."
            ),
            evidence={
                "count": len(unknown_ns),
                "rows": [{"Namespace": n["namespace"], "Package": n["package"]} for n in unknown_ns],
                "query": "SELECT SubscriberPackage.Name, SubscriberPackage.NamespacePrefix "
                         "FROM InstalledSubscriberPackage  -- Tooling API",
            },
            effort="quick", owner_hint="Salesforce admin",
        ))

    legacy = [a for a in automations if a.kind in ("workflow_rule", "process_builder") and a.active]
    if legacy and crm == "salesforce":
        doc.add(Finding(
            id="retired-automation-types-still-active",
            severity="medium",
            title=f"{len(legacy)} active Workflow Rules / Process Builder processes remain",
            what=("Salesforce has retired both tools in favour of Flow, and they are on a migration "
                  f"path rather than a support path. {len(legacy)} are still live here."),
            why_it_matters=(
                "Beyond the end-of-life risk, this is the most common source of field-write "
                "conflicts: a workflow field update and a flow both writing the same field, firing "
                "at different points in the order of execution, with nobody aware both exist."
            ),
            recommended_fix=(
                "Run the Migrate to Flow tool object by object, starting with whichever object shows "
                "up most in the conflict table above. Migrate and consolidate in the same pass — "
                "lifting three workflow rules into three separate flows just moves the problem."
            ),
            evidence={
                "count": len(legacy),
                "rows": [a.as_row() for a in legacy[:25]],
                "query": "SELECT Id, MasterLabel, TableEnumOrId, LastModifiedDate FROM WorkflowRule "
                         "-- Tooling API; and FlowDefinitionView WHERE ProcessType = 'Workflow'",
            },
            effort="project", owner_hint="Salesforce admin",
        ))

    multi_trigger: Dict[str, List[str]] = {}
    for auto in automations:
        if auto.kind == "apex_trigger" and auto.active and auto.obj:
            multi_trigger.setdefault(auto.obj, []).append(auto.name)
    offenders = {k: v for k, v in multi_trigger.items() if len(v) > 1}
    if offenders:
        doc.add(Finding(
            id="multiple-apex-triggers-per-object",
            severity="medium",
            title=f"{len(offenders)} object(s) carry more than one active Apex trigger",
            what=("; ".join(f"{obj}: {', '.join(names)}" for obj, names in list(offenders.items())[:6]) + "."),
            why_it_matters=(
                "Salesforce does not guarantee the execution order of multiple triggers on one "
                "object. Two triggers writing the same object is a race condition that surfaces "
                "only under load — which is to say, at quarter end."
            ),
            recommended_fix=(
                "Consolidate to one trigger per object delegating to a handler class. It is the "
                "single most reliable Apex refactor there is, and it makes the order explicit and "
                "testable."
            ),
            evidence={
                "count": len(offenders),
                "rows": [{"Object": k, "Triggers": ", ".join(v), "Count": len(v)}
                         for k, v in offenders.items()],
                "query": "SELECT Name, TableEnumOrId, Status FROM ApexTrigger WHERE Status = 'Active' "
                         "-- Tooling API",
            },
            effort="project", owner_hint="Salesforce developer",
        ))

    # ------------------------------------------------------------- sections
    doc.sections = {
        "environment": {"crm": crm, "environment": environment, "org_label": org_label,
                        "sandbox": environment != "production"},
        "automation_inventory": [a.as_row() for a in automations],
        "automation_by_type": _tally(a.kind_label for a in automations),
        "conflicts": conflicts,
        "orphaned_automation": orphans,
        "dormant_automation": dormant,
        "recent_changes": recent,
        "inactive_flow_versions": obsolete_versions,
        "object_surface": surface,
        "integration_users": users_section,
        "connected_apps": apps_section.get("apps", []),
        "installed_packages": apps_section.get("packages", []),
        "stack_map": {
            "clusters": {k: v for k, v in sorted(clustered.items())},
            "detected_count": len(detections),
            "believed": list(believed),
            "confirmed": belief["confirmed"],
            "undisclosed": belief["undisclosed"],
            "claimed_not_found": belief["claimed_not_found"],
            "unidentified_namespaces": unknown_ns,
        },
        "method": {
            "conflict_detection": (
                "Field-write targets are extracted per automation, grouped by object+field, and "
                "any target with two or more ACTIVE writers is reported. Contenders are ordered by "
                "the platform's documented order of execution; two contenders at the same rank are "
                "flagged as having no guaranteed order."
            ),
            "write_extraction_basis": sorted({a.write_basis for a in automations if a.write_basis}),
            "dormancy_basis": "true last-fired where the platform exposes it, last-modified as a "
                              "labelled proxy everywhere else",
            "fire_order_model": [{"rank": r, "stage": s} for r, s in FIRE_RANK],
        },
    }

    path = doc.write(run_dir)
    print(f"findings.json  -> {path}")
    print(f"automations={len(automations)}  conflicts={len(conflicts)}  orphan_apps={len(orphan_apps)}  "
          f"orphan_automations={len(orphans)}  integration_users={len(users_section.get('users', []))}  "
          f"tools_detected={len(detections)}")
    if unavailable:
        print(f"unavailable surfaces: {len(unavailable)} (listed in findings.json -> unavailable)")
    return 0


def _tally(values: Iterable[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ------------------------------------------------------- verification queries for the report
#
# Every finding ships the query that reproduces it. These are the ones a
# customer pastes into their own console to check our work in under a minute.


def _conflict_query(crm: str) -> str:
    if crm == "hubspot":
        return (
            "# Reproduce: list every workflow and the properties it sets, then group by property.\n"
            "GET https://api.hubapi.com/automation/v4/flows?limit=100\n"
            "  -> for each flow: flow.objectTypeId, and every\n"
            "     action where actionTypeId == '0-5' -> action.fields.property_name\n"
            "  -> group by (objectTypeId, property_name); anything with 2+ enabled flows is a conflict.\n"
            "# HubSpot does not publish an execution order between workflows, so any collision here\n"
            "# is last-writer-wins with no way to predict the winner."
        )
    return (
        "-- Reproduce the conflict list (Tooling API for Flow.Metadata, standard API for the rest)\n"
        "SELECT Id, ApiName, Label, ProcessType, TriggerType, TriggerObjectOrEvent.QualifiedApiName,\n"
        "       IsActive, VersionNumber, LastModifiedDate, LastModifiedBy\n"
        "FROM FlowDefinitionView WHERE IsActive = true\n"
        "-- LastModifiedBy is plain text on FlowDefinitionView, not a relationship;\n"
        "-- LastModifiedBy.Name here is an INVALID_FIELD and fails the whole query.\n"
        "\n"
        "-- then, ONE Id at a time (Tooling API restricts Metadata to a single-Id filter):\n"
        "SELECT Id, FullName, Metadata FROM Flow WHERE Id = '301...'\n"
        "  -> Metadata.recordUpdates[].inputAssignments[].field\n"
        "  -> Metadata.assignments[].assignmentItems[].assignToReference starting '$Record.'\n"
        "\n"
        "-- and the workflow side (Tooling API), list first:\n"
        "SELECT Id, Name, TableEnumOrId FROM WorkflowFieldUpdate\n"
        "-- then ONE Id at a time, because Metadata is refused on any query that\n"
        "-- can return more than one record:\n"
        "SELECT Id, Name, Metadata FROM WorkflowFieldUpdate WHERE Id = '04Y...'\n"
        "  -> Metadata.field is the field written; TableEnumOrId is the object\n"
        "\n"
        "-- group every (object, field) target; 2+ active writers is a conflict."
    )


def _integration_user_query(crm: str) -> str:
    if crm == "hubspot":
        return ("GET https://api.hubapi.com/settings/v3/users?limit=100   # scope: settings.users.read\n"
                "GET https://api.hubapi.com/crm/v3/owners?archived=true   # scope: crm.objects.owners.read\n"
                "# Match users whose email or name reads as a service account, then check whether the\n"
                "# person who created them still appears as an active owner.")
    return (
        "SELECT Id, Name, Username, Email, IsActive, UserType, Profile.Name,\n"
        "       Profile.UserLicense.Name, ManagerId, Manager.Name, Manager.IsActive,\n"
        "       CreatedBy.Name, CreatedBy.IsActive, LastLoginDate\n"
        "FROM User\n"
        "WHERE IsActive = true\n"
        "  AND (Profile.UserLicense.Name LIKE '%Integration%'\n"
        "       OR Username LIKE '%svc%' OR Username LIKE '%api%'\n"
        "       OR Username LIKE '%integration%' OR Username LIKE '%sync%')\n"
        "\n"
        "-- write surface:\n"
        "SELECT ParentId, SobjectType, PermissionsCreate, PermissionsEdit, PermissionsModifyAllRecords\n"
        "FROM ObjectPermissions WHERE PermissionsEdit = true\n"
        "SELECT AssigneeId, PermissionSetId, PermissionSet.Name,\n"
        "       PermissionSet.PermissionsModifyAllData\n"
        "FROM PermissionSetAssignment"
    )


def _connected_app_query(crm: str) -> str:
    if crm == "hubspot":
        return ("# HubSpot exposes no public connected-apps API. Export by hand:\n"
                "#   Settings -> Integrations -> Connected Apps  (and -> Private Apps)\n"
                "# Save the export as raw/hs_connected_apps.json before the next run.")
    return (
        "-- Tooling API:\n"
        "SELECT Id, Name, CreatedDate, CreatedBy.Name, LastModifiedDate, LastModifiedBy.Name\n"
        "FROM ConnectedApplication\n"
        "\n"
        "-- standard API — last use per app, and the join that proves dormancy:\n"
        "SELECT Id, AppName, UserId, User.Name, LastUsedDate, UseCount FROM OauthToken\n"
        "ORDER BY LastUsedDate NULLS FIRST"
    )


def _automation_owner_query(crm: str) -> str:
    if crm == "hubspot":
        return ("GET https://api.hubapi.com/automation/v4/flows?limit=100\n"
                "  -> flow.updatedByUserId, then check that user id against\n"
                "GET https://api.hubapi.com/crm/v3/owners?archived=true")
    return (
        "SELECT ApiName, Label, IsActive, LastModifiedDate, LastModifiedBy.Name\n"
        "FROM FlowDefinitionView WHERE IsActive = true\n"
        "-- then: SELECT Name, IsActive FROM User WHERE Name IN (…those LastModifiedBy names…)\n"
        "-- anything modified by an inactive user is orphaned."
    )


def _dormancy_query(crm: str) -> str:
    if crm == "hubspot":
        return ("GET https://api.hubapi.com/automation/v4/flows?limit=100\n"
                "  -> flow.updatedAt is the dormancy proxy. True enrollment recency is only\n"
                "     visible in the workflow UI's performance tab, not in the public API.")
    return (
        "SELECT Id, CronJobDetail.Name, CronExpression, State, PreviousFireTime, NextFireTime,\n"
        "       TimesTriggered, CreatedBy.Name\n"
        "FROM CronTrigger ORDER BY PreviousFireTime NULLS FIRST\n"
        "-- PreviousFireTime is a TRUE last-fired timestamp.\n"
        "-- Flows have no equivalent without Shield Event Monitoring, so their dormancy is measured\n"
        "-- from LastModifiedDate and labelled as a proxy in the report."
    )


def _recent_change_query(crm: str) -> str:
    if crm == "hubspot":
        return ("GET https://api.hubapi.com/automation/v4/flows?limit=100  -> flow.updatedAt\n"
                "GET https://api.hubapi.com/account-info/v3/activity/login  # scope: account-info.security.read")
    return (
        "SELECT Id, Action, Section, CreatedDate, CreatedBy.Name, Display\n"
        "FROM SetupAuditTrail\n"
        "WHERE CreatedDate = LAST_N_DAYS:30\n"
        "ORDER BY CreatedDate DESC\n"
        "-- SetupAuditTrail keeps 180 days. Filter Section for Flow / Workflow / Apex / Validation."
    )


def _object_surface_query(crm: str) -> str:
    if crm == "hubspot":
        return ("GET https://api.hubapi.com/crm/v3/schemas            # custom objects\n"
                "POST https://api.hubapi.com/crm/v3/objects/{type}/search  {\"limit\":1} -> read `total`")
    return (
        "GET /services/data/v62.0/limits/recordCount?sObjects=Account,Contact,Lead,Opportunity,…\n"
        "SELECT QualifiedApiName, Label, DurableId FROM EntityDefinition\n"
        "-- cross-reference against the objects that appear in the automation inventory."
    )


def _fingerprint_query(crm: str) -> str:
    if crm == "hubspot":
        return ("GET https://api.hubapi.com/crm/v3/properties/contacts   # and companies, deals, tickets\n"
                "  -> property name prefixes are the fingerprint (zi_, gong_, chilipiper_, …)\n"
                "GET https://api.hubapi.com/settings/v3/users            # service-account naming")
    return (
        "-- Tooling API:\n"
        "SELECT SubscriberPackage.Name, SubscriberPackage.NamespacePrefix,\n"
        "       SubscriberPackageVersion.Name FROM InstalledSubscriberPackage\n"
        "\n"
        "-- standard API:\n"
        "SELECT QualifiedApiName, EntityDefinition.QualifiedApiName, NamespacePrefix\n"
        "FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName = 'Opportunity'\n"
        "SELECT Name, Username FROM User WHERE IsActive = true"
    )


if __name__ == "__main__":
    sys.exit(main())
