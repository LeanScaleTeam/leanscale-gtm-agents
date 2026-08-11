#!/usr/bin/env python3
"""
crm-hygiene — Layer 2. Pure, offline transform of raw/*.json into findings.json.

    python3 analyze.py --raw <run>/raw --out <run> [--config path] [--as-of YYYY-MM-DD]

No network, no MCP, no third-party packages: Claude does the fetching in
skills/run/SKILL.md and drops the results in raw/. Everything here is a
deterministic function of those files, which is what makes the output testable
against fixtures/ and reproducible for a customer who wants to argue with it.

Every finding carries a record count, sample IDs and the query that produced
them, so a skeptical RevOps lead can verify any line in their own CRM in under
a minute. That is the whole bar.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    ConfigError,
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    SourceEmptyError,
    apply_deltas,
    crmutil,
    load_plugin_config,
    load_profile,
)

PLUGIN = "crm-hygiene"

# --------------------------------------------------------------------------- defaults
# Mirrors config.example.json. Anything the customer's ~/.leanscale-gtm/crm-hygiene.json
# does not set falls back to here, so the plugin runs before setup has ever been done.
DEFAULTS: Dict[str, Any] = {
    "crm": "salesforce",
    "objects_in_scope": ["Account", "Contact", "Opportunity", "Lead"],
    "window_days": 365,
    "dead_field_threshold_pct": 5.0,
    "min_records_for_fill_rate": 30,
    "policy_required_fields": {
        "Opportunity": ["NextStep", "Type", "LeadSource"],
        "Account": ["Industry", "Website"],
        "Contact": ["Email", "Title"],
        "Lead": ["LeadSource"],
    },
    "policy_required_scope": "open_and_recent",
    "dedupe_keys": ["domain", "company_name", "contact_email", "contact_name_account"],
    "dedupe_min_cluster": 2,
    "ignore_domains": [],
    "exclude_hierarchy_clusters": True,
    "open_opp_staleness_days": 30,
    "lead_staleness_days": 45,
    "implausible_close_date_days": 730,
    "owner_concentration_pct": 20.0,
    "picklist_fields": {
        "Opportunity": ["StageName", "Type", "LeadSource", "ForecastCategoryName"],
        "Account": ["Type", "Industry"],
        "Contact": ["LeadSource"],
        "Lead": ["Status", "LeadSource", "Industry"],
    },
    "picklist_unused_min_records": 100,
    "known_dead_fields": [],
    "min_finding_count": 1,
    "data_quality_owner": "RevOps",
    "expect_inactive_users_own_records": False,
    "hygiene_index_weights": {
        "duplicates": 20,
        "field_discipline": 15,
        "policy_compliance": 20,
        "ownership": 15,
        "freshness": 20,
        "structure": 10,
    },
}

# Which raw files must be present and non-empty. A required source that comes back
# empty aborts the run (SPEC §5) — a clean-looking report produced by a broken
# connector is worse than a crash.
SOURCES: Dict[str, Dict[str, Any]] = {
    "accounts": {
        "required": True,
        "diagnosis": "The connected identity may lack read access to Account/Companies, or the "
                     "query filtered everything out. Re-run /crm-hygiene:setup to re-probe.",
    },
    "contacts": {
        "required": True,
        "diagnosis": "Contact/Contacts returned nothing. Check object-level read permission on "
                     "the integration user, and that the query has no stray WHERE clause.",
    },
    "opportunities": {
        "required": True,
        "diagnosis": "Opportunity/Deals returned nothing. In Salesforce this is usually sharing "
                     "rules on a private OWD; in HubSpot it is usually a scope missing from the "
                     "private app token (crm.objects.deals.read).",
    },
    "users": {
        "required": True,
        "diagnosis": "No users came back, so ownership checks cannot run at all. Salesforce: the "
                     "integration user needs read on User. HubSpot: the owners endpoint needs "
                     "the crm.objects.owners.read scope.",
    },
    "field_metadata": {
        "required": True,
        "diagnosis": "Field metadata is empty, so dead-field and picklist analysis is impossible. "
                     "Salesforce: FieldDefinition needs a WHERE clause on "
                     "EntityDefinition.QualifiedApiName. HubSpot: GET /crm/v3/properties/{object} "
                     "needs the schemas scope.",
    },
    # Optional sources. `label` means the check group is genuinely unavailable without it;
    # `degrade` means the analysis still runs on a weaker basis and says so in the method note.
    "leads": {"required": False, "label": "Lead hygiene (duplicate leads, stale leads, lead/contact collisions)"},
    "opportunity_contact_roles": {"required": False,
                                  "label": "Contact-role coverage on open opportunities"},
    "field_fill": {"required": False,
                   "degrade": "No aggregate fill-rate queries were supplied, so fill rates are "
                              "measured on the records fetched rather than on the full window."},
    "picklist_metadata": {"required": False,
                          "degrade": "No picklist value sets were fetched, so values that are "
                                     "defined but never used cannot be detected — only "
                                     "collisions between values that are in use."},
    "picklist_usage": {"required": False,
                       "degrade": "No picklist usage counts were fetched, so usage is counted "
                                  "from the records that were pulled."},
    "governance": {"required": False,
                   "label": "Governance (duplicate rules, validation rules, record types)"},
}

OBJECT_ALIASES = {
    "account": "Account", "accounts": "Account", "company": "Account", "companies": "Account",
    "contact": "Contact", "contacts": "Contact",
    "opportunity": "Opportunity", "opportunities": "Opportunity", "deal": "Opportunity",
    "deals": "Opportunity",
    "lead": "Lead", "leads": "Lead",
}

# Canonical attribute -> the keys each CRM actually uses, first match wins.
FIELDS: Dict[str, Dict[str, Dict[str, List[str]]]] = {
    "salesforce": {
        "Account": {
            "id": ["Id"], "name": ["Name"], "website": ["Website"], "owner_id": ["OwnerId"],
            "owner_name": ["Owner.Name"], "owner_active": ["Owner.IsActive"],
            "created": ["CreatedDate"], "modified": ["LastModifiedDate"],
            "last_activity": ["LastActivityDate"], "parent_id": ["ParentId"],
            "record_type_id": ["RecordTypeId"], "country": ["BillingCountry"],
        },
        "Contact": {
            "id": ["Id"], "name": ["Name"], "first": ["FirstName"], "last": ["LastName"],
            "email": ["Email"], "extra_emails": [], "account_id": ["AccountId"],
            "owner_id": ["OwnerId"], "owner_name": ["Owner.Name"],
            "owner_active": ["Owner.IsActive"], "created": ["CreatedDate"],
            "modified": ["LastModifiedDate"], "last_activity": ["LastActivityDate"],
            "title": ["Title"],
        },
        "Opportunity": {
            "id": ["Id"], "name": ["Name"], "account_id": ["AccountId"], "stage": ["StageName"],
            "amount": ["Amount"], "close_date": ["CloseDate"], "is_closed": ["IsClosed"],
            "is_won": ["IsWon"], "owner_id": ["OwnerId"], "owner_name": ["Owner.Name"],
            "owner_active": ["Owner.IsActive"], "created": ["CreatedDate"],
            "modified": ["LastModifiedDate"], "last_activity": ["LastActivityDate"],
            "record_type_id": ["RecordTypeId"], "next_step": ["NextStep"],
        },
        "Lead": {
            "id": ["Id"], "name": ["Name"], "first": ["FirstName"], "last": ["LastName"],
            "email": ["Email"], "company": ["Company"], "status": ["Status"],
            "converted": ["IsConverted"], "owner_id": ["OwnerId"], "owner_name": ["Owner.Name"],
            "owner_active": ["Owner.IsActive"], "created": ["CreatedDate"],
            "modified": ["LastModifiedDate"], "last_activity": ["LastActivityDate"],
        },
        "User": {
            "id": ["Id"], "name": ["Name"], "email": ["Email"], "active": ["IsActive"],
            "profile": ["Profile.Name"], "last_login": ["LastLoginDate"],
        },
    },
    "hubspot": {
        "Account": {
            "id": ["Id", "id", "hs_object_id"], "name": ["name"],
            "website": ["domain", "website"], "owner_id": ["hubspot_owner_id"],
            "owner_name": [], "owner_active": [], "created": ["createdate", "createdAt"],
            "modified": ["hs_lastmodifieddate", "updatedAt"],
            "last_activity": ["notes_last_updated", "hs_last_activity_date", "notes_last_contacted"],
            "parent_id": ["hs_parent_company_id"], "record_type_id": [], "country": ["country"],
        },
        "Contact": {
            "id": ["Id", "id", "hs_object_id"], "name": [], "first": ["firstname"],
            "last": ["lastname"], "email": ["email"], "extra_emails": ["hs_additional_emails"],
            "account_id": ["associatedcompanyid"], "owner_id": ["hubspot_owner_id"],
            "owner_name": [], "owner_active": [], "created": ["createdate", "createdAt"],
            "modified": ["lastmodifieddate", "updatedAt"],
            "last_activity": ["notes_last_contacted", "hs_last_sales_activity_timestamp"],
            "title": ["jobtitle"],
        },
        "Opportunity": {
            "id": ["Id", "id", "hs_object_id"], "name": ["dealname"], "account_id": [],
            "stage": ["dealstage"], "amount": ["amount"], "close_date": ["closedate"],
            "is_closed": ["hs_is_closed"], "is_won": ["hs_is_closed_won"],
            "owner_id": ["hubspot_owner_id"], "owner_name": [], "owner_active": [],
            "created": ["createdate", "createdAt"], "modified": ["hs_lastmodifieddate", "updatedAt"],
            "last_activity": ["hs_last_activity_date", "notes_last_contacted"],
            "record_type_id": ["pipeline"], "next_step": ["next_step", "hs_next_step"],
        },
        "Lead": {
            "id": ["Id", "id", "hs_object_id"], "name": [], "first": ["firstname"],
            "last": ["lastname"], "email": ["email"], "company": ["company"],
            "status": ["hs_lead_status"], "converted": [], "owner_id": ["hubspot_owner_id"],
            "owner_name": [], "owner_active": [], "created": ["createdate", "createdAt"],
            "modified": ["lastmodifieddate", "updatedAt"],
            "last_activity": ["notes_last_contacted"],
        },
        "User": {
            "id": ["Id", "id"], "name": [], "email": ["email"], "active": [],
            "profile": [], "last_login": [],
        },
    },
}

EMAIL_RE = re.compile(r"^[^@\s,;]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")


# --------------------------------------------------------------------------- small helpers


def _blank(v: Any) -> bool:
    return crmutil.is_blank(v)


def truthy(value: Any) -> Optional[bool]:
    """HubSpot returns booleans as the strings 'true'/'false'. Salesforce sends real bools."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


def canon_object(name: Any) -> Optional[str]:
    return OBJECT_ALIASES.get(str(name or "").strip().lower())


def plural(n: int, one: str, many: Optional[str] = None) -> str:
    return one if n == 1 else (many or one + "s")


def money(v: Optional[float]) -> str:
    return "${:,.0f}".format(v or 0)


class Ctx:
    """Everything the checks need, resolved once."""

    def __init__(self, raw: Dict[str, Dict[str, Any]], cfg: Dict[str, Any],
                 profile: Dict[str, Any], as_of: datetime, coverage: Dict[str, Any]):
        self.raw, self.cfg, self.profile, self.as_of = raw, cfg, profile, as_of
        self.coverage = coverage
        self.crm = self._detect_crm()
        self.map = FIELDS[self.crm]

        self.accounts = self._records("accounts")
        self.contacts = self._records("contacts")
        self.opps = self._records("opportunities")
        self.users = self._records("users")
        self.leads = self._records("leads")
        self.roles = self._parse_roles()

        self.window_start = as_of - timedelta(days=int(cfg["window_days"]))
        self.min_count = int(cfg.get("min_finding_count", 1))
        self.owner_hint = cfg.get("data_quality_owner") or "RevOps"

        self.user_by_id = {self.g("User", u, "id"): u for u in self.users}
        self.inactive_users: Set[str] = set()
        for u in self.users:
            uid = self.g("User", u, "id")
            if uid is None:
                continue
            active = truthy(self.g("User", u, "active"))
            if active is None:                       # HubSpot owners use `archived`
                archived = truthy(u.get("archived"))
                active = (not archived) if archived is not None else True
            if not active:
                self.inactive_users.add(str(uid))
        self.user_label = {
            str(self.g("User", u, "id")): (
                self.g("User", u, "name")
                or " ".join(str(x) for x in [u.get("firstName"), u.get("lastName")] if x).strip()
                or self.g("User", u, "email")
                or str(self.g("User", u, "id"))
            )
            for u in self.users if self.g("User", u, "id") is not None
        }

        self.stage_labels = self._stage_labels()
        self.account_by_id = {str(self.g("Account", a, "id")): a for a in self.accounts}

        # defect registries — the Hygiene Index reads these rather than re-deriving anything
        self.defects: Dict[str, Set[str]] = defaultdict(set)
        self.pillars: Dict[str, Dict[str, Any]] = {}
        self.sections: Dict[str, Any] = {}
        self.unavailable: List[str] = []
        self.notes: List[str] = []

    # ---------------------------------------------------------------- plumbing

    def _detect_crm(self) -> str:
        declared = {str(env.get("crm", "")).strip().lower()
                    for env in self.raw.values() if env.get("crm")}
        declared.discard("")
        if len(declared) == 1:
            crm = declared.pop()
            if crm in FIELDS:
                return crm
        if len(declared) > 1:
            raise ConfigError(
                f"raw/ mixes CRMs ({', '.join(sorted(declared))}). One run reads one CRM — "
                f"split them into two run directories."
            )
        crm = str(self.cfg.get("crm", "salesforce")).lower()
        return crm if crm in FIELDS else "salesforce"

    def _records(self, source: str) -> List[Dict[str, Any]]:
        env = self.raw.get(source)
        if not env:
            return []
        return crmutil.normalize_records(env.get("records") or [])

    def rows(self, source: str) -> List[Dict[str, Any]]:
        """Raw rows for the normalized/derived sources that are already flat."""
        env = self.raw.get(source)
        return list(env.get("records") or []) if env else []

    def has(self, source: str) -> bool:
        return bool(self.raw.get(source, {}).get("records"))

    def query(self, source: str) -> str:
        return str((self.raw.get(source) or {}).get("query") or "")

    def g(self, obj: str, rec: Dict[str, Any], attr: str) -> Any:
        for key in self.map.get(obj, {}).get(attr, []):
            if key in rec and not _blank(rec[key]):
                return rec[key]
        return None

    def assoc_ids(self, rec: Dict[str, Any], kind: str) -> List[str]:
        """HubSpot inline associations survive flattening as a list at associations.<kind>.results."""
        results = rec.get(f"associations.{kind}.results")
        if isinstance(results, list):
            return [str(r.get("id")) for r in results if isinstance(r, dict) and r.get("id")]
        return []

    def account_of(self, obj: str, rec: Dict[str, Any]) -> Optional[str]:
        direct = self.g(obj, rec, "account_id")
        if direct:
            return str(direct)
        ids = self.assoc_ids(rec, "companies")
        return ids[0] if ids else None

    def _parse_roles(self) -> List[Dict[str, str]]:
        """Accepts Salesforce OpportunityContactRole rows or HubSpot v4 association results."""
        out: List[Dict[str, str]] = []
        for row in self.rows("opportunity_contact_roles"):
            if not isinstance(row, dict):
                continue
            if row.get("OpportunityId"):
                out.append({"opportunity_id": str(row["OpportunityId"]),
                            "contact_id": str(row.get("ContactId") or ""),
                            "role": row.get("Role") or ""})
            elif isinstance(row.get("from"), dict):                    # HubSpot v4 batch read
                src = str(row["from"].get("id"))
                for to in row.get("to") or []:
                    label = ""
                    for at in (to.get("associationTypes") or []):
                        label = at.get("label") or label
                    out.append({"opportunity_id": src,
                                "contact_id": str(to.get("toObjectId") or ""),
                                "role": label})
            elif row.get("opportunity_id"):
                out.append({"opportunity_id": str(row["opportunity_id"]),
                            "contact_id": str(row.get("contact_id") or ""),
                            "role": row.get("role") or ""})
        # HubSpot inline associations on the deal record are just as good a source
        if not out and self.crm == "hubspot":
            for o in self.opps:
                oid = str(self.g("Opportunity", o, "id"))
                for cid in self.assoc_ids(o, "contacts"):
                    out.append({"opportunity_id": oid, "contact_id": cid, "role": ""})
        return out

    def _stage_labels(self) -> Dict[str, str]:
        """HubSpot deals store a stage id; the pipelines API carries the human label."""
        labels: Dict[str, str] = {}
        for row in self.rows("governance"):
            if isinstance(row, dict) and row.get("kind") == "pipeline_stage":
                labels[str(row.get("stage_id"))] = str(row.get("label") or row.get("stage_id"))
        for row in self.rows("picklist_metadata"):
            if isinstance(row, dict) and row.get("field") == "dealstage" and row.get("label"):
                labels.setdefault(str(row.get("value")), str(row["label"]))
        return labels

    def stage(self, opp: Dict[str, Any]) -> str:
        raw = self.g("Opportunity", opp, "stage")
        return self.stage_labels.get(str(raw), str(raw)) if raw is not None else "(blank)"

    # ---------------------------------------------------------------- derived views

    def is_open(self, opp: Dict[str, Any]) -> bool:
        closed = truthy(self.g("Opportunity", opp, "is_closed"))
        if closed is not None:
            return not closed
        stage = self.stage(opp).lower()
        return not any(w in stage for w in ("closed", "won", "lost", "churn", "renewed"))

    def is_won(self, opp: Dict[str, Any]) -> bool:
        won = truthy(self.g("Opportunity", opp, "is_won"))
        if won is not None:
            return won
        return "won" in self.stage(opp).lower()

    def amount(self, opp: Dict[str, Any]) -> Optional[float]:
        return crmutil.to_number(self.g("Opportunity", opp, "amount"))

    def open_opps(self) -> List[Dict[str, Any]]:
        return [o for o in self.opps if self.is_open(o)]

    def domain_of_account(self, acct: Dict[str, Any]) -> Optional[str]:
        site = self.g("Account", acct, "website")
        if _blank(site):
            return None
        text = str(site).strip().lower()
        text = re.sub(r"^https?://", "", text)
        text = text.split("/")[0].split("?")[0].strip()
        text = re.sub(r"^www\.", "", text)
        if not text or "." not in text or " " in text:
            return None
        return text

    def emails_of_contact(self, con: Dict[str, Any]) -> List[str]:
        out = []
        primary = self.g("Contact", con, "email")
        if primary:
            out.append(str(primary).strip().lower())
        extra = self.g("Contact", con, "extra_emails")
        if extra:
            out += [e.strip().lower() for e in re.split(r"[;,]", str(extra)) if e.strip()]
        return out

    def ignored_domains(self) -> Set[str]:
        return set(crmutil.FREE_EMAIL_DOMAINS) | {
            str(d).strip().lower().replace("www.", "")
            for d in (self.cfg.get("ignore_domains") or []) if str(d).strip()
        }

    def in_scope(self, obj: str) -> bool:
        return obj in (self.cfg.get("objects_in_scope") or [])


# --------------------------------------------------------------------------- loading


def load_raw(raw_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    if not raw_dir.exists():
        raise ConfigError(
            f"No raw directory at {raw_dir}.\n"
            f"Run /crm-hygiene:run first — the skill fetches from your CRM and writes raw/*.json, "
            f"then calls this script. To test offline, point --raw at the bundled fixtures."
        )
    envelopes: Dict[str, Dict[str, Any]] = {}
    coverage: Dict[str, Any] = {}
    files = sorted(raw_dir.glob("*.json"))
    if not files:
        raise ConfigError(f"{raw_dir} contains no *.json files — nothing was fetched.")
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path.name} is not valid JSON ({exc}).") from exc
        if isinstance(payload, list):                       # tolerate a bare array
            payload = {"source": path.stem, "records": payload}
        if not isinstance(payload, dict):
            raise ConfigError(f"{path.name} must contain a JSON object or array.")
        name = str(payload.get("source") or path.stem)
        if path.name.startswith("_"):
            coverage = payload
            continue
        envelopes[name] = payload
    return envelopes, coverage


def build_manifest(raw: Dict[str, Dict[str, Any]], coverage: Dict[str, Any],
                   run_dir: Path, window: Dict[str, str]) -> RunManifest:
    man = RunManifest(PLUGIN, run_dir, window=window)
    for name, spec in SOURCES.items():
        env = raw.get(name)
        if env is None:
            if spec["required"]:
                man.record(name, tool="(not fetched)", count=0, required=True,
                           diagnosis=f"raw/{name}.json was never written. {spec['diagnosis']}")
            continue
        man.record(
            name,
            tool=str(env.get("tool") or "unknown"),
            count=len(env.get("records") or []),
            query=str(env.get("query") or ""),
            required=bool(spec["required"]),
            note=str(env.get("note") or ""),
            diagnosis=str(spec.get("diagnosis", "")),
        )
        if env.get("truncated"):
            man.warn(f"{name}: the fetch was truncated — counts are a floor, not a total.")
    for extra in sorted(set(raw) - set(SOURCES)):
        env = raw[extra]
        man.record(extra, tool=str(env.get("tool") or "unknown"),
                   count=len(env.get("records") or []), required=False)
    for entry in (coverage.get("unavailable") or []):
        man.warn(f"unavailable: {entry.get('check')} — {entry.get('reason')}")
    return man


# --------------------------------------------------------------------------- clustering


def cluster(pairs: Iterable[Tuple[str, str]], min_size: int) -> Dict[str, List[str]]:
    """key -> [record ids], keeping only keys shared by at least min_size records."""
    groups: Dict[str, List[str]] = defaultdict(list)
    for key, rid in pairs:
        groups[key].append(rid)
    return {k: v for k, v in groups.items() if len(v) >= min_size}


def cluster_rows(ctx: Ctx, groups: Dict[str, List[str]], obj: str, key_label: str,
                 limit: int = 40) -> List[Dict[str, Any]]:
    rows = []
    for key, ids in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]:
        if obj == "Account":
            names = [ctx.account_by_id.get(str(i), {}) for i in ids]
            labels = [str(ctx.g("Account", n, "name")) for n in names if n]
        else:
            labels = []
        rows.append({
            key_label: key,
            "Records": len(ids),
            "Names": "; ".join(labels[:4]) + ("…" if len(labels) > 4 else "") if labels else "",
            "Sample IDs": ", ".join(str(i) for i in ids[:4]),
        })
    return rows


# --------------------------------------------------------------------------- checks
# Each check appends zero or one Finding and registers its defective record ids on
# ctx.defects so the Hygiene Index can be computed from measured sets rather than
# from a second, subtly different pass.


def add(doc: FindingsDoc, ctx: Ctx, **kw: Any) -> None:
    count = (kw.get("evidence") or {}).get("count")
    if isinstance(count, (int, float)) and count < ctx.min_count:
        return
    kw.setdefault("owner_hint", ctx.owner_hint)
    doc.add(Finding(**kw))


def ids_sample(ids: Sequence[Any], n: int = 20) -> List[str]:
    return [str(i) for i in list(ids)[:n]]


# ---- duplicates -------------------------------------------------------------------


def check_dupe_accounts_domain(doc: FindingsDoc, ctx: Ctx) -> None:
    if "domain" not in (ctx.cfg.get("dedupe_keys") or []) or not ctx.accounts:
        return
    ignore = ctx.ignored_domains()
    pairs, hierarchy_keys = [], set()
    by_domain: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for acct in ctx.accounts:
        dom = ctx.domain_of_account(acct)
        if not dom or dom in ignore:
            continue
        by_domain[dom].append(acct)
        pairs.append((dom, str(ctx.g("Account", acct, "id"))))

    groups = cluster(pairs, int(ctx.cfg["dedupe_min_cluster"]))

    # A shared domain across a parent and its subsidiaries is a hierarchy, not a duplicate.
    # Excluding these is the difference between a finding and an argument.
    if ctx.cfg.get("exclude_hierarchy_clusters", True):
        for dom, members in by_domain.items():
            if dom not in groups:
                continue
            ids = {str(ctx.g("Account", a, "id")) for a in members}
            parents = {str(ctx.g("Account", a, "parent_id")) for a in members
                       if ctx.g("Account", a, "parent_id")}
            linked = bool(parents) and (parents <= ids or len(parents) == 1)
            if linked and len(parents | ids) >= len(ids):
                hierarchy_keys.add(dom)
    excluded = {k: groups[k] for k in hierarchy_keys}
    groups = {k: v for k, v in groups.items() if k not in hierarchy_keys}
    if not groups:
        return

    affected = [rid for ids in groups.values() for rid in ids]
    ctx.defects["dupe_accounts"].update(affected)
    note = ""
    if excluded:
        n_ex = sum(len(v) for v in excluded.values())
        note = (f" A further {n_ex} accounts across {len(excluded)} domains were excluded as "
                f"legitimate parent/child hierarchies.")
    add(doc, ctx,
        id="dupe-accounts-by-domain",
        severity="high",
        title=f"{len(affected):,} accounts share {len(groups):,} corporate email domains",
        what=(f"{len(affected):,} account records collapse into {len(groups):,} distinct company "
              f"domains once free-email and excluded domains are removed. The largest cluster holds "
              f"{max(len(v) for v in groups.values())} records for one domain.{note}"),
        why_it_matters=("Every downstream system treats these as different companies. Routing sends "
                        "the same buyer to different reps, account-level pipeline and ARR are split "
                        "across records so neither looks material, and a CSM working the renewal "
                        "cannot see the expansion opportunity sitting on the twin."),
        recommended_fix=("Review these clusters in a merge queue — do not bulk-merge. Start with "
                         "the clusters that contain open pipeline. Then turn on a domain-based "
                         "duplicate rule so the next one is blocked at create time."),
        evidence={"count": len(affected), "sample_ids": ids_sample(affected),
                  "rows": cluster_rows(ctx, groups, "Account", "Domain"),
                  "clusters": len(groups),
                  "excluded_hierarchies": sum(len(v) for v in excluded.values()),
                  "query": ctx.query("accounts")},
        effort="medium")


def check_dupe_accounts_name(doc: FindingsDoc, ctx: Ctx) -> None:
    if "company_name" not in (ctx.cfg.get("dedupe_keys") or []) or not ctx.accounts:
        return
    pairs = []
    for acct in ctx.accounts:
        key = crmutil.normalize_company(ctx.g("Account", acct, "name"))
        if key and len(key) >= 4:
            pairs.append((key, str(ctx.g("Account", acct, "id"))))
    groups = cluster(pairs, int(ctx.cfg["dedupe_min_cluster"]))
    if not groups:
        return
    affected = [rid for ids in groups.values() for rid in ids]
    already = ctx.defects["dupe_accounts"]
    new_only = [rid for rid in affected if rid not in already]
    ctx.defects["dupe_accounts"].update(affected)
    add(doc, ctx,
        id="dupe-accounts-by-name",
        severity="high" if len(new_only) >= 10 else "medium",
        title=f"{len(affected):,} accounts collide on a normalized company name",
        what=(f"{len(affected):,} accounts across {len(groups):,} name clusters are the same "
              f"company written differently once legal suffixes, punctuation and case are "
              f"stripped ('Acme, Inc.' = 'ACME LLC'). {len(new_only):,} of them are not caught by "
              f"the domain check, usually because the website field is empty."),
        why_it_matters=("Name-only duplicates are the ones that survive a domain-based dedupe "
                        "project and quietly re-inflate the account count a quarter later. They "
                        "are also the ones a rep creates by hand mid-quarter."),
        recommended_fix=("Work the clusters that have no website first — filling the domain is "
                         "usually the cheaper fix and it makes the record dedupe-able forever. "
                         "Add a fuzzy name matching rule alongside the domain rule."),
        evidence={"count": len(affected), "sample_ids": ids_sample(affected),
                  "rows": cluster_rows(ctx, groups, "Account", "Normalized name"),
                  "clusters": len(groups), "not_caught_by_domain": len(new_only),
                  "query": ctx.query("accounts")},
        effort="medium")


def check_dupe_contacts_email(doc: FindingsDoc, ctx: Ctx) -> None:
    if "contact_email" not in (ctx.cfg.get("dedupe_keys") or []) or not ctx.contacts:
        return
    pairs = []
    for con in ctx.contacts:
        cid = str(ctx.g("Contact", con, "id"))
        for email in set(ctx.emails_of_contact(con)):
            if EMAIL_RE.match(email):
                pairs.append((email, cid))
    groups = cluster(pairs, 2)
    groups = {k: sorted(set(v)) for k, v in groups.items() if len(set(v)) >= 2}
    if not groups:
        return
    affected = sorted({rid for ids in groups.values() for rid in ids})
    ctx.defects["dupe_contacts"].update(affected)
    hs_note = ""
    if ctx.crm == "hubspot":
        hs_note = (" HubSpot blocks duplicate primary emails on create, so these arrived by "
                   "import or API — or the duplicate is hiding in hs_additional_emails, which "
                   "this check reads.")
    add(doc, ctx,
        id="dupe-contacts-by-email",
        severity="high",
        title=f"{len(affected):,} contact records share {len(groups):,} email addresses",
        what=(f"{len(affected):,} contacts resolve to {len(groups):,} unique email addresses — the "
              f"same human exists more than once.{hs_note}"),
        why_it_matters=("Duplicate contacts double-count engagement, send the same person the same "
                        "sequence twice, split activity history across records so neither looks "
                        "engaged, and corrupt any per-contact attribution."),
        recommended_fix=("Merge on exact email — this is the safest merge key you have and can be "
                         "largely automated with a review step. Then enforce email uniqueness at "
                         "create time."),
        evidence={"count": len(affected), "sample_ids": ids_sample(affected),
                  "rows": [{"Email": k, "Records": len(v), "Sample IDs": ", ".join(v[:4])}
                           for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:40]],
                  "clusters": len(groups), "query": ctx.query("contacts")},
        effort="quick")


def check_dupe_contacts_name_account(doc: FindingsDoc, ctx: Ctx) -> None:
    if "contact_name_account" not in (ctx.cfg.get("dedupe_keys") or []) or not ctx.contacts:
        return
    pairs = []
    for con in ctx.contacts:
        acct = ctx.account_of("Contact", con)
        first, last = ctx.g("Contact", con, "first"), ctx.g("Contact", con, "last")
        if not acct or _blank(first) or _blank(last):
            continue
        key = f"{acct}|{str(first).strip().lower()} {str(last).strip().lower()}"
        pairs.append((key, str(ctx.g("Contact", con, "id"))))
    groups = cluster(pairs, 2)
    if not groups:
        return
    affected = sorted({rid for ids in groups.values() for rid in ids})
    new_only = [r for r in affected if r not in ctx.defects["dupe_contacts"]]
    ctx.defects["dupe_contacts"].update(affected)
    add(doc, ctx,
        id="dupe-contacts-by-name-on-account",
        severity="medium",
        title=f"{len(affected):,} contacts are the same name twice on the same account",
        what=(f"{len(groups):,} people appear more than once under a single account. "
              f"{len(new_only):,} of these are invisible to an email-based dedupe because at "
              f"least one of the copies has no email address at all."),
        why_it_matters=("These are the duplicates an email merge leaves behind. They keep "
                        "appearing in contact-role lists and org charts, and they make a "
                        "'how many people do we know at this account' number meaningless."),
        recommended_fix=("Merge the pairs, keeping the record that carries the email and the "
                         "activity history. Where neither has an email, that is the real defect "
                         "— fix that first."),
        evidence={"count": len(affected), "sample_ids": ids_sample(affected),
                  "clusters": len(groups), "email_blind": len(new_only),
                  "query": ctx.query("contacts")},
        effort="quick")


def check_dupe_open_opps(doc: FindingsDoc, ctx: Ctx) -> None:
    pairs = []
    for opp in ctx.open_opps():
        acct = ctx.account_of("Opportunity", opp)
        name = ctx.g("Opportunity", opp, "name")
        if not acct or _blank(name):
            continue
        pairs.append((f"{acct}|{str(name).strip().lower()}", str(ctx.g("Opportunity", opp, "id"))))
    groups = cluster(pairs, 2)
    if not groups:
        return
    affected = sorted({rid for ids in groups.values() for rid in ids})
    by_id = {str(ctx.g("Opportunity", o, "id")): o for o in ctx.opps}
    dup_value = sum(ctx.amount(by_id[r]) or 0 for r in affected)
    ctx.defects["dupe_opps"].update(affected)
    add(doc, ctx,
        id="dupe-open-opportunities",
        severity="high",
        title=f"{len(affected):,} open opportunities are duplicated on the same account",
        what=(f"{len(groups):,} accounts carry two or more open opportunities with an identical "
              f"name. Together these records hold {money(dup_value)} of open pipeline, some "
              f"share of which is the same deal counted twice."),
        why_it_matters=("Duplicated pipeline is the fastest way to blow a forecast. Coverage "
                        "ratios look healthy, the board sees a number that cannot close, and the "
                        "quarter ends short with no single record to point at."),
        recommended_fix=("Have the owning reps close the duplicate as 'Duplicate' rather than "
                         "deleting it, so the audit trail survives. Then check whether the twin "
                         "was created by an integration — a repeating pattern means a broken "
                         "sync, not sloppy reps."),
        evidence={"count": len(affected), "sample_ids": ids_sample(affected),
                  "clusters": len(groups), "value": round(dup_value, 2),
                  "query": ctx.query("opportunities")},
        effort="quick")


def check_lead_contact_collision(doc: FindingsDoc, ctx: Ctx) -> None:
    if not ctx.leads or not ctx.contacts:
        return
    contact_emails: Dict[str, str] = {}
    for con in ctx.contacts:
        for email in ctx.emails_of_contact(con):
            contact_emails.setdefault(email, str(ctx.g("Contact", con, "id")))
    hits, rows = [], []
    for lead in ctx.leads:
        if truthy(ctx.g("Lead", lead, "converted")):
            continue
        email = ctx.g("Lead", lead, "email")
        key = str(email).strip().lower() if email else ""
        if key and key in contact_emails:
            lid = str(ctx.g("Lead", lead, "id"))
            hits.append(lid)
            owner = ctx.user_label.get(str(ctx.g("Lead", lead, "owner_id")), "—")
            rows.append({"Email": key, "Lead ID": lid, "Lead owner": owner,
                         "Existing contact": contact_emails[key]})
    if not hits:
        return
    ctx.defects["lead_collisions"].update(hits)
    add(doc, ctx,
        id="lead-contact-email-collision",
        severity="high",
        title=f"{len(hits):,} open leads are already contacts in the CRM",
        what=(f"{len(hits):,} unconverted leads carry an email address that already exists on a "
              f"contact record. The same human is sitting in two objects with two owners."),
        why_it_matters=("This is the single most visible data-quality failure to a customer: two "
                        "reps call the same person in the same week. It also corrupts source "
                        "attribution — the lead's source overwrites the account's original one on "
                        "conversion."),
        recommended_fix=("Route these leads to the existing contact's account owner instead of "
                         "through new-lead routing, and turn on the Lead-to-Contact matching rule "
                         "so the next one surfaces at create time."),
        evidence={"count": len(hits), "sample_ids": ids_sample(hits), "rows": rows[:40],
                  "query": ctx.query("leads")},
        effort="quick")


# ---- fields -----------------------------------------------------------------------


def field_inventory(ctx: Ctx) -> List[Dict[str, Any]]:
    """One row per field: object, api name, custom?, schema-required?, fill rate, source of fill."""
    # 1. metadata
    meta: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in ctx.rows("field_metadata"):
        if not isinstance(row, dict):
            continue
        obj = canon_object(row.get("object")
                           or (row.get("EntityDefinition") or {}).get("QualifiedApiName")
                           or row.get("entity") or row.get("sobject"))
        api = row.get("QualifiedApiName") or row.get("name") or row.get("api_name")
        if not obj or not api:
            continue
        api = str(api)
        if "IsCustom" in row:
            is_custom = bool(row["IsCustom"])
        elif "hubspotDefined" in row:
            is_custom = not bool(row.get("hubspotDefined"))
        else:
            is_custom = api.endswith("__c")
        nillable = row.get("IsNillable")
        required = (not bool(nillable)) if nillable is not None else None   # HubSpot: unknowable
        meta[(obj, api)] = {
            "object": obj, "field": api,
            "label": row.get("Label") or row.get("label") or api,
            "type": row.get("DataType") or row.get("type") or "",
            "custom": is_custom,
            "schema_required": required,
            "namespace": row.get("NamespacePrefix") or "",
            "business_status": row.get("BusinessStatus") or "",
            "last_modified": row.get("LastModifiedDate") or row.get("updatedAt") or "",
            "calculated": bool(row.get("IsCalculated") or row.get("calculated")),
        }

    # 2. exact fill rates from aggregate queries, where the fetch layer supplied them
    exact: Dict[Tuple[str, str], Tuple[int, int]] = {}
    for row in ctx.rows("field_fill"):
        if not isinstance(row, dict):
            continue
        obj, api = canon_object(row.get("object")), row.get("field")
        total, filled = row.get("total"), row.get("filled")
        if obj and api and isinstance(total, (int, float)) and isinstance(filled, (int, float)):
            exact[(obj, str(api))] = (int(total), int(filled))

    # 3. sampled fill rates as the fallback, straight off the records we already pulled
    samples = {"Account": ctx.accounts, "Contact": ctx.contacts,
               "Opportunity": ctx.opps, "Lead": ctx.leads}
    out: List[Dict[str, Any]] = []
    for (obj, api), row in sorted(meta.items()):
        entry = dict(row)
        if (obj, api) in exact:
            total, filled = exact[(obj, api)]
            entry["measure"] = "exact"
        else:
            recs = samples.get(obj) or []
            total = len(recs)
            filled = sum(1 for r in recs if not _blank(r.get(api)))
            entry["measure"] = "sampled"
        entry["total"] = total
        entry["filled"] = filled
        entry["fill_pct"] = crmutil.pct(filled, total, 2) if total else None
        out.append(entry)
    return out


def check_dead_fields(doc: FindingsDoc, ctx: Ctx) -> None:
    inventory = ctx.sections["field_inventory"]
    if not inventory:
        return
    exact_used = sum(1 for f in inventory if f.get("measure") == "exact")
    fill_basis = ("field counts are window totals from aggregate queries"
                  if exact_used else "field counts are measured on the records fetched")
    threshold = float(ctx.cfg["dead_field_threshold_pct"])
    floor = int(ctx.cfg["min_records_for_fill_rate"])
    known_dead = {str(k).strip().lower() for k in (ctx.cfg.get("known_dead_fields") or [])}

    custom = [f for f in inventory if f["custom"] and ctx.in_scope(f["object"])]
    measurable = [f for f in custom if (f["total"] or 0) >= floor and f["fill_pct"] is not None]
    never = [f for f in measurable if f["fill_pct"] == 0 and not f["calculated"]]
    low = [f for f in measurable
           if f["fill_pct"] is not None and 0 < f["fill_pct"] < threshold and not f["calculated"]]

    ctx.sections["field_summary"] = {
        "total_fields": len(inventory),
        "custom_fields": len(custom),
        "measurable_custom_fields": len(measurable),
        "never_populated": len(never),
        "low_fill": len(low),
        "dead_share_pct": crmutil.pct(len(never) + len(low), len(measurable)) if measurable else 0,
        "by_object": {
            obj: {
                "custom": sum(1 for f in custom if f["object"] == obj),
                "never_populated": sum(1 for f in never if f["object"] == obj),
                "low_fill": sum(1 for f in low if f["object"] == obj),
            }
            for obj in sorted({f["object"] for f in custom})
        },
    }
    ctx.pillars["field_discipline"] = {
        "label": "Field discipline",
        "clean": 1 - ((len(never) + len(low)) / len(measurable)) if measurable else None,
        "detail": f"{len(never) + len(low)} of {len(measurable)} measurable custom fields are dead",
    }

    def rows_for(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{"Object": f["object"], "API name": f["field"], "Label": f["label"],
                 "Type": f["type"], "Fill": "0%" if f["fill_pct"] == 0 else f"{f['fill_pct']}%",
                 "Basis": f"{f['filled']:,}/{f['total']:,} ({f['measure']})"}
                for f in sorted(items, key=lambda x: (x["object"], x["field"]))
                if f"{f['object']}.{f['field']}".lower() not in known_dead][:60]

    if never:
        shown = rows_for(never)
        objs = ", ".join(f"{o}: {c['never_populated']}"
                         for o, c in ctx.sections["field_summary"]["by_object"].items()
                         if c["never_populated"])
        add(doc, ctx,
            id="dead-fields-never-populated",
            severity="high",
            title=f"{len(never):,} custom fields have never been populated",
            what=(f"{len(never):,} of {len(measurable):,} measurable custom fields hold a value on "
                  f"zero records in the last {ctx.cfg['window_days']} days ({objs}). They still "
                  f"appear on page layouts, in report builders and in every field picker."),
            why_it_matters=("Empty fields are not free. They lengthen every page layout a rep "
                            "scrolls past, pollute report builders so the right field is hard to "
                            "find, and each one is a question somebody will eventually ask you to "
                            "answer. On Salesforce they also consume the per-object field limit "
                            "that a real project will need later."),
            recommended_fix=("Take the list below to whoever requested each field. Anything with "
                             "no owner gets removed from page layouts this week and deleted next "
                             "quarter — layout removal is reversible, deletion is the follow-up."),
            evidence={"count": len(never), "rows": shown,
                      "sample_ids": [f"{f['object']}.{f['field']}" for f in never[:20]],
                      "basis": fill_basis,
                      "query": ctx.query("field_fill") or ctx.query("field_metadata")},
            effort="medium")

    if low:
        add(doc, ctx,
            id="dead-fields-low-fill",
            severity="medium",
            title=f"{len(low):,} custom fields are filled on under {threshold:g}% of records",
            what=(f"{len(low):,} custom fields carry a value on fewer than {threshold:g}% of "
                  f"records in the window. They are populated often enough that somebody believes "
                  f"in them and rarely enough that nothing can be reported off them."),
            why_it_matters=("A field at 3% fill is worse than an empty one: it produces charts "
                            "that look real. Any report grouped on one of these silently describes "
                            "a 3% sample as if it were the business."),
            recommended_fix=("For each field decide one of three things and write it down: make it "
                             "required at the stage where it matters, automate it, or kill it. "
                             "'Encourage the team to fill it in' is the option that got you here."),
            evidence={"count": len(low), "rows": rows_for(low),
                      "sample_ids": [f"{f['object']}.{f['field']}" for f in low[:20]],
                      "basis": fill_basis,
                      "query": ctx.query("field_fill") or ctx.query("field_metadata")},
            effort="medium")


def check_policy_required(doc: FindingsDoc, ctx: Ctx) -> None:
    """The fields the team says are mandatory but which nothing enforces."""
    policy = ctx.cfg.get("policy_required_fields") or {}
    inventory = {(f["object"], f["field"]): f for f in ctx.sections["field_inventory"]}
    scope_mode = ctx.cfg.get("policy_required_scope", "open_and_recent")
    samples = {"Account": ctx.accounts, "Contact": ctx.contacts,
               "Opportunity": ctx.opps, "Lead": ctx.leads}

    def in_policy_scope(obj: str, rec: Dict[str, Any]) -> bool:
        if scope_mode == "all":
            return True
        if obj == "Opportunity":
            if ctx.is_open(rec):
                return True
            if scope_mode == "open_only":
                return False
        elif scope_mode == "open_only":
            return True
        created = crmutil.parse_dt(ctx.g(obj, rec, "created"))
        return bool(created and created >= ctx.window_start)

    gap_rows, missing_fields, compliance = [], [], []
    for obj, fields in policy.items():
        obj = canon_object(obj) or obj
        if not ctx.in_scope(obj):
            continue
        recs = [r for r in (samples.get(obj) or []) if in_policy_scope(obj, r)]
        if not recs:
            continue
        blanks_rows, worst_ids = [], []
        for field in fields:
            meta = inventory.get((obj, field))
            if meta is None:
                missing_fields.append(f"{obj}.{field}")
                continue
            blank_ids = [str(ctx.g(obj, r, "id")) for r in recs if _blank(r.get(field))]
            rate = crmutil.pct(len(recs) - len(blank_ids), len(recs))
            compliance.append(rate)
            enforced = meta["schema_required"]
            gap_rows.append({
                "Object": obj, "Field": field,
                "Policy": "required",
                "Schema enforces it": {True: "yes", False: "no", None: "not expressible"}[enforced],
                "Filled": f"{rate}%",
                "Blank records": f"{len(blank_ids):,} of {len(recs):,}",
            })
            if blank_ids:
                blanks_rows.append((field, len(blank_ids), rate))
                worst_ids += blank_ids[:10]
                ctx.defects[f"policy_blank_{obj}"].update(blank_ids)
        if blanks_rows:
            worst = sorted(blanks_rows, key=lambda x: -x[1])
            total_blank = len({i for i in worst_ids})
            add(doc, ctx,
                id=f"policy-required-empty-{obj.lower()}",
                severity="high" if worst[0][2] < 70 else "medium",
                title=(f"{obj}: {worst[0][0]} is blank on {worst[0][1]:,} in-scope records "
                       f"despite being mandatory"),
                what=("Fields your team is told are mandatory, measured against "
                      + {"all": "every record",
                         "open_only": "open records only",
                         "open_and_recent": f"open records plus anything created in the last "
                                            f"{ctx.cfg['window_days']} days"}[scope_mode]
                      + ": " + "; ".join(f"{f} blank on {n:,} ({r}% filled)"
                                         for f, n, r in worst[:6]) + "."),
                why_it_matters=("A policy nobody enforces is a policy nobody follows, and every "
                                "report built on these fields quietly drops the blank records "
                                "instead of flagging them. The team is not being careless — they "
                                "are responding correctly to a system that lets them skip it."),
                recommended_fix=("Pick the two fields that actually drive a decision and enforce "
                                 "them at the stage where they matter — not at create, which is "
                                 "where enforcement goes to die. Everything else on the list "
                                 "should be demoted from 'required' in writing."),
                evidence={"count": total_blank, "sample_ids": ids_sample(worst_ids),
                          "rows": [{"Field": f, "Blank records": n, "Filled": f"{r}%"}
                                   for f, n, r in worst],
                          "scope": scope_mode, "records_in_scope": len(recs),
                          "query": ctx.query(obj.lower() + "s" if obj != "Opportunity"
                                             else "opportunities")},
                effort="medium")

    if gap_rows:
        unenforced = [r for r in gap_rows if r["Schema enforces it"] != "yes"]
        ctx.sections["policy_vs_schema"] = gap_rows
        if unenforced:
            hs = (" HubSpot has no object-level required flag at all — 'required' there is a "
                  "property of a form or a workflow, never of the schema — so every field on "
                  "this list is unenforced by construction."
                  if ctx.crm == "hubspot" else "")
            add(doc, ctx,
                id="policy-required-not-enforced-by-schema",
                severity="high",
                title=f"{len(unenforced)} of {len(gap_rows)} 'required' fields are not required",
                what=(f"{len(unenforced)} fields your team treats as mandatory can be saved blank "
                      f"by anyone, any time, through the UI or the API.{hs}"),
                why_it_matters=("This is the gap that produces every other finding in this "
                                "report. The team was told these fields matter; the system says "
                                "they do not. When those two disagree, the system wins."),
                recommended_fix=("For each row below, choose: enforce it in the schema or in a "
                                 "validation rule, or stop calling it required. Enforcing all of "
                                 "them at once is how you get reps typing 'n/a' — pick the two "
                                 "that change a decision and start there."),
                evidence={"count": len(unenforced), "rows": gap_rows,
                          "query": ctx.query("field_metadata")},
                effort="quick")

    if missing_fields:
        add(doc, ctx,
            id="policy-required-field-not-found",
            severity="medium",
            title=f"{len(missing_fields)} configured 'required' fields do not exist",
            what=("These fields are listed as policy-required in your crm-hygiene config but do "
                  "not appear in the CRM's field metadata: " + ", ".join(missing_fields[:12])
                  + ("…" if len(missing_fields) > 12 else "") + "."),
            why_it_matters=("Either the field was renamed or deleted and the policy was never "
                            "updated, or the config carries a typo. Both mean part of your stated "
                            "data policy is being measured against nothing."),
            recommended_fix=("Correct the API names in ~/.leanscale-gtm/crm-hygiene.json under "
                             "policy_required_fields, or drop the entries that refer to fields "
                             "that no longer exist."),
            evidence={"count": len(missing_fields), "sample_ids": missing_fields[:20],
                      "query": ctx.query("field_metadata")},
            effort="quick")

    ctx.pillars["policy_compliance"] = {
        "label": "Policy compliance",
        "clean": (sum(compliance) / len(compliance) / 100.0) if compliance else None,
        "detail": (f"mean fill of {len(compliance)} policy-required fields"
                   if compliance else "no policy-required fields resolved"),
    }


# ---- ownership --------------------------------------------------------------------


def _owner_inactive(ctx: Ctx, obj: str, rec: Dict[str, Any]) -> bool:
    flag = truthy(ctx.g(obj, rec, "owner_active"))
    if flag is not None:
        return not flag
    owner = ctx.g(obj, rec, "owner_id")
    return bool(owner) and str(owner) in ctx.inactive_users


def check_ownership(doc: FindingsDoc, ctx: Ctx) -> None:
    expected = bool(ctx.cfg.get("expect_inactive_users_own_records"))
    owned_total = 0
    defective: Set[str] = set()

    specs = [
        ("Opportunity", [o for o in ctx.opps if ctx.is_open(o)], "open opportunities", "critical"),
        ("Account", ctx.accounts, "accounts", "high"),
        ("Contact", ctx.contacts, "contacts", "medium"),
        ("Lead", [l_ for l_ in ctx.leads if not truthy(ctx.g("Lead", l_, "converted"))],
         "open leads", "high"),
    ]
    per_owner: Dict[str, int] = defaultdict(int)

    for obj, records, label, severity in specs:
        if not records or not ctx.in_scope(obj):
            continue
        owned_total += len(records)
        orphaned = [r for r in records if _owner_inactive(ctx, obj, r)]
        ownerless = [r for r in records if _blank(ctx.g(obj, r, "owner_id"))]
        if obj == "Account":
            for r in records:
                oid = ctx.g("Account", r, "owner_id")
                if oid:
                    per_owner[str(oid)] += 1

        if orphaned:
            ids = [str(ctx.g(obj, r, "id")) for r in orphaned]
            defective.update(ids)
            ctx.defects["owner_inactive"].update(ids)
            by_user: Dict[str, int] = defaultdict(int)
            for r in orphaned:
                by_user[ctx.user_label.get(str(ctx.g(obj, r, "owner_id")), "(unknown user)")] += 1
            extra = {}
            if obj == "Opportunity":
                value = sum(ctx.amount(r) or 0 for r in orphaned)
                extra = {"value": round(value, 2)}
                impact = (f"{money(value)} of open pipeline is assigned to someone who no longer "
                          f"logs in. It is still in the forecast roll-up and nobody is working it.")
            else:
                impact = (f"Inbound routing, alerts and follow-up tasks on these {label} are being "
                          f"delivered to a deactivated user, which means to nobody.")
            add(doc, ctx,
                id=f"{obj.lower()}-owned-by-inactive-user",
                severity="low" if expected else severity,
                title=f"{len(orphaned):,} {label} are owned by deactivated users",
                what=(f"{len(orphaned):,} {label} are assigned to {len(by_user)} users who are "
                      f"deactivated in the CRM"
                      + (" (you told us during setup that this is deliberate)." if expected
                         else ". Top holders: "
                              + "; ".join(f"{u} ({n})" for u, n in
                                          sorted(by_user.items(), key=lambda kv: -kv[1])[:5])
                              + ".")),
                why_it_matters=impact,
                recommended_fix=("Reassign these to the current territory owner before the next "
                                 "forecast call, then add owner reassignment to your offboarding "
                                 "checklist so the next departure does not recreate this."),
                evidence={"count": len(orphaned), "sample_ids": ids_sample(ids),
                          "rows": [{"Deactivated owner": u, "Records": n}
                                   for u, n in sorted(by_user.items(), key=lambda kv: -kv[1])[:20]],
                          "query": ctx.query(
                              "opportunities" if obj == "Opportunity"
                              else ("accounts" if obj == "Account"
                                    else ("contacts" if obj == "Contact" else "leads"))),
                          **extra},
                effort="quick")

        if ownerless:
            ids = [str(ctx.g(obj, r, "id")) for r in ownerless]
            defective.update(ids)
            ctx.defects["owner_missing"].update(ids)
            add(doc, ctx,
                id=f"{obj.lower()}-with-no-owner",
                severity="high" if obj in ("Opportunity", "Lead") else "medium",
                title=f"{len(ownerless):,} {label} have no owner at all",
                what=f"{len(ownerless):,} {label} carry an empty owner field.",
                why_it_matters=("An unowned record is invisible to every roll-up report, every "
                                "territory view and every 'my accounts' filter. It exists, it just "
                                "does not belong to anyone's number."),
                recommended_fix=("Assign these through your normal territory logic. If the count "
                                 "is large, the cause is almost always an integration or import "
                                 "that does not set an owner — fix the writer, not the records."),
                evidence={"count": len(ownerless), "sample_ids": ids_sample(ids),
                          "query": ctx.query(
                              "opportunities" if obj == "Opportunity"
                              else ("accounts" if obj == "Account"
                                    else ("contacts" if obj == "Contact" else "leads")))},
                effort="quick")

    # one user quietly holding the book
    if per_owner and ctx.accounts:
        top_id, top_n = max(per_owner.items(), key=lambda kv: kv[1])
        share = crmutil.pct(top_n, len(ctx.accounts))
        if share >= float(ctx.cfg["owner_concentration_pct"]):
            name = ctx.user_label.get(top_id, top_id)
            add(doc, ctx,
                id="account-owner-concentration",
                severity="medium",
                title=f"One user owns {share}% of all accounts",
                what=(f"{name} owns {top_n:,} of {len(ctx.accounts):,} accounts ({share}%). "
                      f"The next largest holder owns "
                      f"{sorted(per_owner.values(), reverse=True)[1] if len(per_owner) > 1 else 0:,}."),
                why_it_matters=("At this concentration the owner is almost always an integration "
                                "user, an admin, or a departed rep whose book was never "
                                "redistributed. Either way those accounts are not being worked, "
                                "and every 'accounts per rep' number you publish is wrong."),
                recommended_fix=("Confirm who this user is. If it is an integration, set a real "
                                 "default owner on the writing integration. If it is a person, "
                                 "redistribute the book."),
                evidence={"count": top_n, "value": share,
                          "rows": [{"Owner": ctx.user_label.get(u, u), "Accounts": n,
                                    "Share": f"{crmutil.pct(n, len(ctx.accounts))}%"}
                                   for u, n in sorted(per_owner.items(),
                                                      key=lambda kv: -kv[1])[:10]],
                          "query": ctx.query("accounts")},
                effort="quick")

    ctx.pillars["ownership"] = {
        "label": "Ownership",
        "clean": 1 - (len(defective) / owned_total) if owned_total else None,
        "detail": f"{len(defective):,} of {owned_total:,} owned records have a broken owner",
    }


# ---- freshness --------------------------------------------------------------------


def check_freshness(doc: FindingsDoc, ctx: Ctx) -> None:
    open_opps = ctx.open_opps()
    if not open_opps:
        ctx.pillars["freshness"] = {"label": "Pipeline freshness", "clean": None,
                                    "detail": "no open opportunities in scope"}
        return
    stale_days = int(ctx.cfg["open_opp_staleness_days"])
    floor = crmutil.to_number(ctx.profile.get("material_deal_floor")) or 0
    defective: Set[str] = set()

    # 1. open, but the close date is already in the past
    past, past_value = [], 0.0
    for opp in open_opps:
        close = crmutil.parse_dt(ctx.g("Opportunity", opp, "close_date"))
        if close and close < ctx.as_of:
            past.append(opp)
            past_value += ctx.amount(opp) or 0
    if past:
        ids = [str(ctx.g("Opportunity", o, "id")) for o in past]
        defective.update(ids)
        ctx.defects["stale_opps"].update(ids)
        ages = [crmutil.days_between(ctx.g("Opportunity", o, "close_date"), ctx.as_of) for o in past]
        oldest = max(a for a in ages if a is not None)
        add(doc, ctx,
            id="open-opps-past-close-date",
            severity="critical",
            title=f"{len(past):,} open opportunities have a close date in the past",
            what=(f"{len(past):,} opportunities are still open with a close date that has already "
                  f"passed, carrying {money(past_value)}. The median is "
                  f"{int(crmutil.median([a for a in ages if a is not None]) or 0)} days overdue; "
                  f"the worst is {oldest:,} days."),
            why_it_matters=("This is the number an executive spots in thirty seconds, and it "
                            "invalidates every period-based report at once: pipeline created, "
                            "coverage, conversion by close month, quarterly forecast. Deals that "
                            "cannot close in a period are still being counted in it."),
            recommended_fix=("Give every owner their list this week with three options: push the "
                             "date with a reason, close it lost, or explain why it is still live. "
                             "Then add a validation rule blocking a past close date on save, and "
                             "a weekly report so the queue never rebuilds."),
            evidence={"count": len(past), "value": round(past_value, 2),
                      "sample_ids": ids_sample(ids),
                      "rows": [{"Opportunity": str(ctx.g("Opportunity", o, "name"))[:60],
                                "Stage": ctx.stage(o),
                                "Close date": str(ctx.g("Opportunity", o, "close_date"))[:10],
                                "Days overdue": crmutil.days_between(
                                    ctx.g("Opportunity", o, "close_date"), ctx.as_of),
                                "Amount": money(ctx.amount(o)),
                                "Owner": ctx.user_label.get(
                                    str(ctx.g("Opportunity", o, "owner_id")), "—")}
                               for o in sorted(past, key=lambda x: -(ctx.amount(x) or 0))[:30]],
                      "query": ctx.query("opportunities")},
            effort="quick")

    # 2. open, no activity in a long time
    stale, stale_value, no_activity_data = [], 0.0, 0
    for opp in open_opps:
        raw_activity = ctx.g("Opportunity", opp, "last_activity")
        last = crmutil.parse_dt(raw_activity) or crmutil.parse_dt(ctx.g("Opportunity", opp, "modified"))
        if raw_activity is None:
            no_activity_data += 1
        if last is None or (ctx.as_of - last).days > stale_days:
            stale.append(opp)
            stale_value += ctx.amount(opp) or 0
    if stale:
        ids = [str(ctx.g("Opportunity", o, "id")) for o in stale]
        defective.update(ids)
        ctx.defects["stale_opps"].update(ids)
        big = [o for o in stale if (ctx.amount(o) or 0) >= floor]
        add(doc, ctx,
            id="open-opps-no-recent-activity",
            severity="high",
            title=f"{len(stale):,} open opportunities have gone quiet for {stale_days}+ days",
            what=(f"{len(stale):,} of {len(open_opps):,} open opportunities "
                  f"({crmutil.pct(len(stale), len(open_opps))}%) show no logged activity in "
                  f"{stale_days} days, holding {money(stale_value)}. "
                  f"{len(big):,} of them are above your material deal floor of {money(floor)}. "
                  f"{no_activity_data:,} carry no activity date at all, which usually means "
                  f"activity is not being logged rather than not happening."),
            why_it_matters=("Silent pipeline is the most reliable predictor of a miss. It also "
                            "distorts stage-conversion math: deals that quietly died sit in stage "
                            "forever and make every stage look slower than it is."),
            recommended_fix=("Split the list in two. Deals with no activity because nothing is "
                             "happening get closed. Deals with activity that is not being logged "
                             "are a tooling problem — check that email and calendar sync is on for "
                             "those owners before you ask anyone to try harder."),
            evidence={"count": len(stale), "value": round(stale_value, 2),
                      "sample_ids": ids_sample(ids), "above_floor": len(big),
                      "no_activity_date": no_activity_data,
                      "query": ctx.query("opportunities")},
            effort="medium")

    # 3. open far longer than this org's own median win cycle
    cycles = []
    for opp in ctx.opps:
        if ctx.is_open(opp) or not ctx.is_won(opp):
            continue
        days = crmutil.days_between(ctx.g("Opportunity", opp, "created"),
                                    ctx.g("Opportunity", opp, "close_date"))
        if days is not None and 0 < days < 1500:
            cycles.append(days)
    median_cycle = crmutil.median(cycles)
    if median_cycle and len(cycles) >= 12:
        limit = median_cycle * 2
        long_open = []
        for opp in open_opps:
            age = crmutil.days_between(ctx.g("Opportunity", opp, "created"), ctx.as_of)
            if age is not None and age > limit:
                long_open.append((opp, age))
        if long_open:
            ids = [str(ctx.g("Opportunity", o, "id")) for o, _ in long_open]
            defective.update(ids)
            value = sum(ctx.amount(o) or 0 for o, _ in long_open)
            add(doc, ctx,
                id="open-opps-older-than-two-win-cycles",
                severity="medium",
                title=f"{len(long_open):,} open deals are older than two of your own win cycles",
                what=(f"Your median closed-won cycle is {int(median_cycle)} days, measured across "
                      f"{len(cycles):,} won deals. {len(long_open):,} open opportunities have been "
                      f"open longer than {int(limit)} days, holding {money(value)}."),
                why_it_matters=("This threshold is measured from your own history, not a "
                                "benchmark. A deal past twice your median cycle has, empirically, "
                                "already failed to behave like a deal that wins."),
                recommended_fix=("Run these as a single scrub. Most will close lost; the few that "
                                 "survive are usually mis-staged renewals or multi-year projects "
                                 "that deserve their own record type rather than a slot in new-"
                                 "business pipeline."),
                evidence={"count": len(long_open), "value": round(value, 2),
                          "sample_ids": ids_sample(ids), "median_win_cycle_days": int(median_cycle),
                          "query": ctx.query("opportunities")},
                effort="medium")
        ctx.sections["win_cycle_days"] = {"median": int(median_cycle), "sample": len(cycles)}

    # 4. placeholder close dates
    horizon = int(ctx.cfg["implausible_close_date_days"])
    far = []
    for opp in open_opps:
        close = crmutil.parse_dt(ctx.g("Opportunity", opp, "close_date"))
        if close and (close - ctx.as_of).days > horizon:
            far.append(opp)
    if far:
        ids = [str(ctx.g("Opportunity", o, "id")) for o in far]
        defective.update(ids)
        value = sum(ctx.amount(o) or 0 for o in far)
        add(doc, ctx,
            id="open-opps-implausible-close-date",
            severity="medium",
            title=f"{len(far):,} open deals have a close date more than {horizon} days out",
            what=(f"{len(far):,} open opportunities carry a close date beyond "
                  f"{horizon} days, holding {money(value)}. The furthest is "
                  f"{max(str(ctx.g('Opportunity', o, 'close_date'))[:10] for o in far)}."),
            why_it_matters=("These are placeholder dates, not forecasts. They inflate any "
                            "long-range pipeline view and they are the reason 'total open "
                            "pipeline' and 'pipeline closing this year' diverge."),
            recommended_fix=("Set a real date or close the record. A validation rule capping the "
                             "close date at a sensible horizon stops the pattern permanently."),
            evidence={"count": len(far), "value": round(value, 2), "sample_ids": ids_sample(ids),
                      "query": ctx.query("opportunities")},
            effort="quick")

    # 5. closed records dated into the future
    future_closed = []
    for opp in ctx.opps:
        if ctx.is_open(opp):
            continue
        close = crmutil.parse_dt(ctx.g("Opportunity", opp, "close_date"))
        if close and close > ctx.as_of:
            future_closed.append(opp)
    if future_closed:
        ids = [str(ctx.g("Opportunity", o, "id")) for o in future_closed]
        value = sum(ctx.amount(o) or 0 for o in future_closed)
        add(doc, ctx,
            id="closed-opps-with-future-close-date",
            severity="medium",
            title=f"{len(future_closed):,} closed deals are dated in the future",
            what=(f"{len(future_closed):,} opportunities are marked closed but carry a close date "
                  f"that has not happened yet, worth {money(value)}."),
            why_it_matters=("Bookings land in the wrong period. A deal closed in August and dated "
                            "in November is missing from this quarter's attainment and will "
                            "appear in next quarter's, which is how a rep gets paid twice or not "
                            "at all."),
            recommended_fix=("Correct the dates to the actual close date, then check whether a "
                             "CPQ or billing integration is writing a contract start date into "
                             "the close date field — that is the usual cause."),
            evidence={"count": len(future_closed), "value": round(value, 2),
                      "sample_ids": ids_sample(ids), "query": ctx.query("opportunities")},
            effort="quick")

    # 6. open pipeline with no amount
    no_amount = [o for o in open_opps if ctx.amount(o) in (None, 0)]
    if no_amount:
        ids = [str(ctx.g("Opportunity", o, "id")) for o in no_amount]
        defective.update(ids)
        add(doc, ctx,
            id="open-opps-missing-amount",
            severity="high",
            title=f"{len(no_amount):,} open opportunities have no amount",
            what=(f"{len(no_amount):,} of {len(open_opps):,} open opportunities "
                  f"({crmutil.pct(len(no_amount), len(open_opps))}%) carry a blank or zero amount."),
            why_it_matters=("Every coverage ratio, every weighted forecast and every 'pipeline "
                            "created' number silently treats these as zero. Your real coverage is "
                            "better than the dashboard says, which sounds harmless until someone "
                            "makes a hiring decision on it."),
            recommended_fix=("Require an amount at the first stage where a number exists — "
                             "usually the stage after discovery, not at create. If products or "
                             "quotes should be driving the amount, the roll-up is broken."),
            evidence={"count": len(no_amount), "sample_ids": ids_sample(ids),
                      "query": ctx.query("opportunities")},
            effort="medium")

    ctx.pillars["freshness"] = {
        "label": "Pipeline freshness",
        "clean": 1 - (len(defective) / len(open_opps)),
        "detail": f"{len(defective):,} of {len(open_opps):,} open opportunities are defective",
    }
    ctx.sections["pipeline"] = {
        "open_opportunities": len(open_opps),
        "open_value": round(sum(ctx.amount(o) or 0 for o in open_opps), 2),
        "past_close_date": len(past),
        "past_close_value": round(past_value, 2),
        "stale": len(stale),
        "stale_value": round(stale_value, 2),
    }


def check_stale_leads(doc: FindingsDoc, ctx: Ctx) -> None:
    if not ctx.leads or not ctx.in_scope("Lead"):
        return
    limit = int(ctx.cfg["lead_staleness_days"])
    dead_statuses = {"unqualified", "disqualified", "recycled", "nurture", "closed", "junk"}
    stale = []
    for lead in ctx.leads:
        if truthy(ctx.g("Lead", lead, "converted")):
            continue
        status = str(ctx.g("Lead", lead, "status") or "").strip().lower()
        if any(word in status for word in dead_statuses):
            continue
        last = (crmutil.parse_dt(ctx.g("Lead", lead, "last_activity"))
                or crmutil.parse_dt(ctx.g("Lead", lead, "created")))
        if last and (ctx.as_of - last).days > limit:
            stale.append(lead)
    if not stale:
        return
    ids = [str(ctx.g("Lead", lead, "id")) for lead in stale]
    by_owner: Dict[str, int] = defaultdict(int)
    for lead in stale:
        by_owner[ctx.user_label.get(str(ctx.g("Lead", lead, "owner_id")), "(unknown)")] += 1
    add(doc, ctx,
        id="stale-open-leads",
        severity="medium",
        title=f"{len(ids):,} leads are still 'open' with no activity in {limit}+ days",
        what=(f"{len(ids):,} unconverted leads that have not been disqualified show no activity "
              f"in over {limit} days. They are held by {len(by_owner)} owners."),
        why_it_matters=("Open-but-untouched leads are the reason speed-to-lead metrics look fine "
                        "while conversion does not. They also inflate every funnel denominator, "
                        "so MQL-to-SQL conversion reads lower than the team's actual performance."),
        recommended_fix=("Auto-recycle anything past this threshold into nurture with a status "
                         "that says so, so the open-lead count means something. Then look at the "
                         "owners with the largest piles — that is a capacity or routing problem, "
                         "not an effort problem."),
        evidence={"count": len(ids), "sample_ids": ids_sample(ids),
                  "rows": [{"Owner": u, "Stale leads": n}
                           for u, n in sorted(by_owner.items(), key=lambda kv: -kv[1])[:15]],
                  "query": ctx.query("leads")},
        effort="medium")


# ---- structure --------------------------------------------------------------------


def check_structure(doc: FindingsDoc, ctx: Ctx) -> None:
    checked = 0
    defective: Set[str] = set()

    contacts_by_account: Dict[str, List[str]] = defaultdict(list)
    for con in ctx.contacts:
        acct = ctx.account_of("Contact", con)
        if acct:
            contacts_by_account[acct].append(str(ctx.g("Contact", con, "id")))

    # contacts with no account
    if ctx.contacts:
        checked += len(ctx.contacts)
        orphans = [c for c in ctx.contacts if not ctx.account_of("Contact", c)]
        if orphans:
            ids = [str(ctx.g("Contact", c, "id")) for c in orphans]
            defective.update(ids)
            with_email = sum(1 for c in orphans if ctx.g("Contact", c, "email"))
            add(doc, ctx,
                id="contacts-with-no-account",
                severity="medium",
                title=f"{len(orphans):,} contacts are not linked to any account",
                what=(f"{len(orphans):,} of {len(ctx.contacts):,} contacts "
                      f"({crmutil.pct(len(orphans), len(ctx.contacts))}%) have no parent account. "
                      f"{with_email:,} of them have an email address, so most can be re-parented "
                      f"by domain automatically."),
                why_it_matters=("An unlinked contact is invisible from the account it belongs to. "
                                "The rep working that account cannot see them, they are excluded "
                                "from account-based campaigns, and they never appear in a "
                                "buying-committee count."),
                recommended_fix=("Re-parent by email domain where a matching account exists — "
                                 "that clears most of the list mechanically. Then find what "
                                 "creates account-less contacts (usually a form or a list import) "
                                 "and fix the writer."),
                evidence={"count": len(orphans), "sample_ids": ids_sample(ids),
                          "recoverable_by_domain": with_email,
                          "query": ctx.query("contacts")},
                effort="medium")

    # accounts with no contacts, and the sharper cut: with open pipeline
    if ctx.accounts and ctx.contacts:
        checked += len(ctx.accounts)
        empty = [a for a in ctx.accounts
                 if not contacts_by_account.get(str(ctx.g("Account", a, "id")))]
        open_by_account: Dict[str, float] = defaultdict(float)
        won_accounts: Set[str] = set()
        for opp in ctx.opps:
            acct = ctx.account_of("Opportunity", opp)
            if not acct:
                continue
            if ctx.is_open(opp):
                open_by_account[acct] += ctx.amount(opp) or 0
            elif ctx.is_won(opp):
                won_accounts.add(acct)

        blind_deals = [a for a in empty if open_by_account.get(str(ctx.g("Account", a, "id")))]
        if blind_deals:
            ids = [str(ctx.g("Account", a, "id")) for a in blind_deals]
            defective.update(ids)
            value = sum(open_by_account[i] for i in ids)
            add(doc, ctx,
                id="accounts-with-open-pipeline-and-no-contacts",
                severity="high",
                title=f"{len(blind_deals):,} accounts have open pipeline and zero contacts",
                what=(f"{len(blind_deals):,} accounts carry {money(value)} of open pipeline while "
                      f"holding no contact records at all."),
                why_it_matters=("A deal with no known humans is a deal with no known buyer. When "
                                "the rep who holds the relationship in their head leaves — or "
                                "takes a week off — the deal has no thread back into the account."),
                recommended_fix=("Make at least one contact role a requirement to advance past "
                                 "your first qualification stage. It is the single highest-"
                                 "leverage stage gate in a CRM and it is nearly free to add."),
                evidence={"count": len(blind_deals), "value": round(value, 2),
                          "sample_ids": ids_sample(ids), "query": ctx.query("accounts")},
                effort="quick")

        plain_empty = [a for a in empty if not open_by_account.get(str(ctx.g("Account", a, "id")))]
        if plain_empty:
            ids = [str(ctx.g("Account", a, "id")) for a in plain_empty]
            defective.update(ids)
            add(doc, ctx,
                id="accounts-with-no-contacts",
                severity="medium",
                title=f"{len(plain_empty):,} accounts have no contacts",
                what=(f"{len(plain_empty):,} of {len(ctx.accounts):,} accounts "
                      f"({crmutil.pct(len(plain_empty), len(ctx.accounts))}%) hold no contact "
                      f"records and no open pipeline."),
                why_it_matters=("These are unworkable accounts taking up space in territory "
                                "counts, coverage models and per-rep account loads. They make "
                                "capacity look tighter than it is."),
                recommended_fix=("Decide whether these are real targets or import residue. Real "
                                 "targets go to enrichment; residue gets archived so account "
                                 "counts mean something again."),
                evidence={"count": len(plain_empty), "sample_ids": ids_sample(ids),
                          "query": ctx.query("accounts")},
                effort="medium")

        won_no_contacts = [i for i in won_accounts if not contacts_by_account.get(i)]
        if won_no_contacts:
            defective.update(won_no_contacts)
            add(doc, ctx,
                id="closed-won-accounts-with-no-contacts",
                severity="medium",
                title=f"{len(won_no_contacts):,} customers have no contacts on record",
                what=(f"{len(won_no_contacts):,} accounts with at least one closed-won "
                      f"opportunity have no contact records at all."),
                why_it_matters=("You sold to these companies and cannot name a single person at "
                                "them. Renewal, expansion, advocacy, onboarding, escalation — "
                                "every post-sale motion starts with a human, and there is none "
                                "here."),
                recommended_fix=("Backfill from the signed order or the onboarding thread for "
                                 "anything renewing in the next two quarters, and require a "
                                 "primary contact role before an opportunity can be marked won."),
                evidence={"count": len(won_no_contacts), "sample_ids": ids_sample(won_no_contacts),
                          "query": ctx.query("accounts")},
                effort="medium")

    # opportunities with no account
    open_opps = ctx.open_opps()
    if open_opps:
        checked += len(open_opps)
        detached = [o for o in open_opps if not ctx.account_of("Opportunity", o)]
        if detached:
            ids = [str(ctx.g("Opportunity", o, "id")) for o in detached]
            defective.update(ids)
            value = sum(ctx.amount(o) or 0 for o in detached)
            add(doc, ctx,
                id="open-opps-with-no-account",
                severity="high",
                title=f"{len(detached):,} open opportunities are not linked to an account",
                what=(f"{len(detached):,} open opportunities holding {money(value)} have no "
                      f"parent account."
                      + (" In HubSpot a deal keeps its company link as an association rather than "
                         "a property, so these genuinely have no association — they are not a "
                         "reading error." if ctx.crm == "hubspot" else "")),
                why_it_matters=("These deals are absent from every account-level view: account "
                                "planning, ABM reporting, customer 360, territory roll-ups. They "
                                "count in the forecast and nowhere else."),
                recommended_fix=("Link them to the right account now, then make the account "
                                 "relationship required at create — in HubSpot, enforce it in the "
                                 "deal creation workflow since there is no schema-level way to "
                                 "require an association."),
                evidence={"count": len(detached), "value": round(value, 2),
                          "sample_ids": ids_sample(ids), "query": ctx.query("opportunities")},
                effort="quick")

        # contact roles / associated contacts
        if ctx.roles or ctx.has("opportunity_contact_roles"):
            roles_by_opp: Dict[str, int] = defaultdict(int)
            for role in ctx.roles:
                roles_by_opp[role["opportunity_id"]] += 1
            zero = [o for o in open_opps if roles_by_opp.get(str(ctx.g("Opportunity", o, "id")), 0) == 0]
            single = [o for o in open_opps
                      if roles_by_opp.get(str(ctx.g("Opportunity", o, "id")), 0) == 1]
            if zero:
                ids = [str(ctx.g("Opportunity", o, "id")) for o in zero]
                defective.update(ids)
                value = sum(ctx.amount(o) or 0 for o in zero)
                label = ("associated contacts" if ctx.crm == "hubspot" else "contact roles")
                add(doc, ctx,
                    id="open-opps-with-no-contact-roles",
                    severity="high",
                    title=f"{len(zero):,} open opportunities have zero {label}",
                    what=(f"{len(zero):,} of {len(open_opps):,} open opportunities "
                          f"({crmutil.pct(len(zero), len(open_opps))}%) have no {label} at all, "
                          f"holding {money(value)}. A further {len(single):,} are single-threaded "
                          f"with exactly one."),
                    why_it_matters=("Zero-threaded is worse than single-threaded: there is not "
                                    "even one named person to lose. These are also the deals that "
                                    "cannot be reassigned when a rep leaves, because nobody else "
                                    "knows who to call."),
                    recommended_fix=("Require one contact with a role to advance past "
                                     "qualification, and put multi-threading on the deal-review "
                                     "checklist. Track the zero-threaded count weekly — it moves "
                                     "fast once it is visible."),
                    evidence={"count": len(zero), "value": round(value, 2),
                              "sample_ids": ids_sample(ids), "single_threaded": len(single),
                              "query": ctx.query("opportunity_contact_roles")},
                    effort="quick")

    ctx.pillars["structure"] = {
        "label": "Structural integrity",
        "clean": 1 - (len(defective) / checked) if checked else None,
        "detail": f"{len(defective):,} of {checked:,} records checked are orphaned or unlinked",
    }


def check_contactability(doc: FindingsDoc, ctx: Ctx) -> None:
    if not ctx.contacts:
        return
    no_email, bad_email, mismatch = [], [], []
    ignore = ctx.ignored_domains()
    for con in ctx.contacts:
        cid = str(ctx.g("Contact", con, "id"))
        raw = ctx.g("Contact", con, "email")
        if _blank(raw):
            no_email.append(cid)
            continue
        email = str(raw).strip().lower()
        if not EMAIL_RE.match(email):
            bad_email.append(cid)
            continue
        acct_id = ctx.account_of("Contact", con)
        acct = ctx.account_by_id.get(str(acct_id)) if acct_id else None
        if acct:
            acct_domain = ctx.domain_of_account(acct)
            con_domain = crmutil.email_domain(email)
            if (acct_domain and con_domain and con_domain not in ignore
                    and acct_domain not in ignore and con_domain != acct_domain
                    and not con_domain.endswith("." + acct_domain)
                    and not acct_domain.endswith("." + con_domain)):
                mismatch.append({"Contact ID": cid, "Contact domain": con_domain,
                                 "Account": str(ctx.g("Account", acct, "name"))[:40],
                                 "Account domain": acct_domain})

    if no_email:
        add(doc, ctx,
            id="contacts-with-no-email",
            severity="medium",
            title=f"{len(no_email):,} contacts have no email address",
            what=(f"{len(no_email):,} of {len(ctx.contacts):,} contacts "
                  f"({crmutil.pct(len(no_email), len(ctx.contacts))}%) carry no email address."),
            why_it_matters=("A contact with no email cannot be marketed to, cannot be matched on "
                            "conversion, cannot be deduplicated reliably, and inflates your "
                            "database size while contributing nothing to reachable audience."),
            recommended_fix=("Enrich the ones attached to accounts with open or won pipeline "
                             "first. Require email or phone at create so the pile stops growing."),
            evidence={"count": len(no_email), "sample_ids": ids_sample(no_email),
                      "query": ctx.query("contacts")},
            effort="medium")

    if bad_email:
        add(doc, ctx,
            id="contacts-with-malformed-email",
            severity="low",
            title=f"{len(bad_email):,} contacts have a malformed email address",
            what=(f"{len(bad_email):,} contacts hold something in the email field that is not a "
                  f"valid address — truncated at the @, spaces in the local part, doubled @, or "
                  f"free text."),
            why_it_matters=("These hard-bounce, which damages sending reputation for every other "
                            "campaign, and they defeat email-based deduplication and matching."),
            recommended_fix=("Clear or correct them in bulk, then add format validation on the "
                             "field so the next import cannot write junk into it."),
            evidence={"count": len(bad_email), "sample_ids": ids_sample(bad_email),
                      "query": ctx.query("contacts")},
            effort="quick")

    if len(mismatch) >= max(ctx.min_count, 5):
        add(doc, ctx,
            id="contacts-on-mismatched-account-domain",
            severity="medium",
            title=f"{len(mismatch):,} contacts sit on an account whose domain they do not share",
            what=(f"{len(mismatch):,} contacts have a corporate email domain that differs from "
                  f"their parent account's domain (personal and free-mail addresses excluded). "
                  f"Some are legitimate — partners, agencies, people who changed jobs — but a "
                  f"cluster on one account is usually a merge that went to the wrong parent."),
            why_it_matters=("Mis-parented contacts poison account-based routing and reporting in "
                            "a way that is very hard to spot: the account looks well covered, but "
                            "the people on it work somewhere else."),
            recommended_fix=("Sort by account and review the accounts with several mismatched "
                             "contacts first — those are merges, not exceptions. Single "
                             "mismatches are usually fine and can be left alone."),
            evidence={"count": len(mismatch), "rows": mismatch[:40],
                      "sample_ids": [m["Contact ID"] for m in mismatch[:20]],
                      "query": ctx.query("contacts")},
            effort="medium")


def check_account_domains(doc: FindingsDoc, ctx: Ctx) -> None:
    if not ctx.accounts:
        return
    missing = [str(ctx.g("Account", a, "id")) for a in ctx.accounts
               if not ctx.domain_of_account(a)]
    if not missing:
        return
    add(doc, ctx,
        id="accounts-with-no-domain",
        severity="medium",
        title=f"{len(missing):,} accounts have no usable website domain",
        what=(f"{len(missing):,} of {len(ctx.accounts):,} accounts "
              f"({crmutil.pct(len(missing), len(ctx.accounts))}%) have an empty or unparseable "
              f"website field."),
        why_it_matters=("Domain is the join key for the entire GTM stack: enrichment, intent "
                        "data, ad audiences, lead-to-account matching, and duplicate detection. "
                        "Every account without one is invisible to all of it, and permanently "
                        "un-dedupable."),
        recommended_fix=("Backfill from contact email domains — most of these can be filled "
                         "mechanically from a contact already on the account. Then require the "
                         "field on the account create path used by reps."),
        evidence={"count": len(missing), "sample_ids": ids_sample(missing),
                  "query": ctx.query("accounts")},
        effort="quick")


# ---- picklists --------------------------------------------------------------------


def _pick_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def check_picklists(doc: FindingsDoc, ctx: Ctx) -> None:
    scope = ctx.cfg.get("picklist_fields") or {}
    wanted = {(canon_object(o) or o, f) for o, fields in scope.items() for f in fields}

    defined: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in ctx.rows("picklist_metadata"):
        if not isinstance(row, dict):
            continue
        obj = canon_object(row.get("object"))
        field = row.get("field")
        if obj and field:
            defined[(obj, str(field))].append(row)

    usage: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(dict)
    for row in ctx.rows("picklist_usage"):
        if not isinstance(row, dict):
            continue
        obj, field = canon_object(row.get("object")), row.get("field")
        if obj and field and row.get("value") is not None:
            usage[(obj, str(field))][str(row["value"])] = int(row.get("count") or 0)
    # fall back to counting the records we already pulled
    samples = {"Account": ctx.accounts, "Contact": ctx.contacts,
               "Opportunity": ctx.opps, "Lead": ctx.leads}
    for (obj, field) in wanted:
        if (obj, field) in usage or obj not in samples:
            continue
        counts: Dict[str, int] = defaultdict(int)
        for rec in samples[obj]:
            val = rec.get(field)
            if not _blank(val):
                counts[str(val)] += 1
        if counts:
            usage[(obj, field)] = dict(counts)
            ctx.notes.append(f"picklist usage for {obj}.{field} counted from the sampled records")

    if not defined and not usage:
        ctx.unavailable.append(
            "Picklist audit — neither a value set nor usage counts were fetched")
        return

    usage_basis = ("picklist counts are window totals from aggregate queries"
                   if ctx.has("picklist_usage")
                   else "picklist counts are measured on the records fetched")
    unused_rows, dupe_rows, undeclared_rows = [], [], []
    min_records = int(ctx.cfg["picklist_unused_min_records"])

    keys = sorted(set(defined) | set(usage))
    for (obj, field) in keys:
        if wanted and (obj, field) not in wanted:
            continue
        values = defined.get((obj, field), [])
        counts = usage.get((obj, field), {})
        total = sum(counts.values())
        active = [v for v in values if v.get("active", True)]
        label_of = {str(v.get("value")): str(v.get("label") or v.get("value")) for v in values}

        # 1. declared but never used
        if active and total >= min_records:
            for v in active:
                val = str(v.get("value"))
                if counts.get(val, 0) == 0:
                    unused_rows.append({"Object": obj, "Field": field,
                                        "Value": label_of.get(val, val),
                                        "Records in window": 0})
        # 2. values that differ only in case/punctuation
        buckets: Dict[str, List[str]] = defaultdict(list)
        for val in {str(v.get("value")) for v in active} | set(counts):
            key = _pick_key(label_of.get(val, val))
            if key:
                buckets[key].append(val)
        for key, vals in buckets.items():
            if len(set(vals)) > 1:
                dupe_rows.append({
                    "Object": obj, "Field": field,
                    # show the label, not the internal id — HubSpot stage ids mean nothing to a human
                    "Colliding values": " / ".join(
                        sorted({label_of.get(v, v) for v in vals})[:5]),
                    "Records": sum(counts.get(v, 0) for v in set(vals)),
                    "Confidence": "exact after case+punctuation",
                })
        # 3. in the data but not in the active value set
        if values:
            declared_vals = {str(v.get("value")) for v in values}
            for val, n in counts.items():
                if val not in declared_vals and n > 0:
                    undeclared_rows.append({"Object": obj, "Field": field, "Value": val,
                                            "Records in window": n})
                elif n > 0 and val in declared_vals and val not in {str(v.get("value")) for v in active}:
                    undeclared_rows.append({"Object": obj, "Field": field,
                                            "Value": f"{label_of.get(val, val)} (inactive value)",
                                            "Records in window": n})

    ctx.sections["picklists"] = {
        "fields_audited": len(keys),
        "unused_values": len(unused_rows),
        "near_duplicate_groups": len(dupe_rows),
        "undeclared_values": len(undeclared_rows),
        "usage": {f"{o}.{f}": usage[(o, f)] for (o, f) in sorted(usage)},
    }

    if unused_rows:
        add(doc, ctx,
            id="picklist-values-unused",
            severity="low",
            title=f"{len(unused_rows):,} picklist values have not been used in {ctx.cfg['window_days']} days",
            what=(f"{len(unused_rows):,} active picklist values across "
                  f"{len({(r['Object'], r['Field']) for r in unused_rows})} fields have zero "
                  f"records in the window."),
            why_it_matters=("Unused values make every dropdown longer than it needs to be and "
                            "every report legend longer than it should be. When a stage or "
                            "status value has no records at all, the process it describes is not "
                            "happening the way the picklist says it does."),
            recommended_fix=("Deactivate rather than delete — deactivating preserves history on "
                             "old records while removing the value from the dropdown. Check the "
                             "unused stage values first; those describe a sales process nobody is "
                             "running."),
            evidence={"count": len(unused_rows), "rows": unused_rows[:50],
                      "basis": usage_basis, "query": ctx.query("picklist_usage")},
            effort="quick")

    if dupe_rows:
        add(doc, ctx,
            id="picklist-near-duplicate-values",
            severity="medium",
            title=f"{len(dupe_rows)} picklist fields carry values that differ only in case or punctuation",
            what=("Values like 'Enterprise' and 'enterprise' coexist in the same field. Reports "
                  "group on the literal string, so each spelling becomes its own row: "
                  + "; ".join(f"{r['Object']}.{r['Field']}: {r['Colliding values']}"
                              for r in dupe_rows[:5]) + "."),
            why_it_matters=("This is the defect that makes an executive distrust a dashboard. One "
                            "segment appears twice on the same chart with different totals, and "
                            "nobody can explain which one is right — because both are."),
            recommended_fix=("Pick the canonical spelling, mass-update the records on the losing "
                             "value, then deactivate it. Check what wrote the variant: an "
                             "integration or import writing a raw string past the picklist is the "
                             "usual source, and it will recreate the problem next week."),
            evidence={"count": len(dupe_rows), "rows": dupe_rows[:40],
                      "basis": usage_basis, "query": ctx.query("picklist_metadata")},
            effort="quick")

    if undeclared_rows:
        total = sum(r["Records in window"] for r in undeclared_rows)
        add(doc, ctx,
            id="picklist-values-not-in-value-set",
            severity="medium",
            title=f"{len(undeclared_rows)} picklist values exist on records but not in the active value set",
            what=(f"{total:,} records carry a picklist value that is inactive or absent from the "
                  f"field's current value set — a retired stage or status that was never cleaned "
                  f"off the records that still hold it."),
            why_it_matters=("These records fall out of any report filtered on the current value "
                            "list, silently. A stage report that filters to your live stages "
                            "excludes them entirely, so the funnel adds up to less than the "
                            "pipeline and nobody can find the gap."),
            recommended_fix=("Map each retired value to its live equivalent and mass-update the "
                             "records. In Salesforce, use the picklist value replace tool so the "
                             "history stays intact."),
            evidence={"count": total, "rows": undeclared_rows[:40],
                      "basis": usage_basis, "query": ctx.query("picklist_usage")},
            effort="medium")


# ---- governance -------------------------------------------------------------------


def check_governance(doc: FindingsDoc, ctx: Ctx) -> None:
    rows = [r for r in ctx.rows("governance") if isinstance(r, dict)]
    if not rows:
        ctx.unavailable.append("Governance — no duplicate rules, validation rules or record "
                               "types were fetched")
        return

    dupe_rules = [r for r in rows if r.get("kind") == "duplicate_rule"]
    val_rules = [r for r in rows if r.get("kind") == "validation_rule"]
    rec_types = [r for r in rows if r.get("kind") == "record_type"]
    rt_usage = {str(r.get("RecordTypeId")): int(r.get("count") or 0)
                for r in rows if r.get("kind") == "record_type_usage"}
    stages = [r for r in rows if r.get("kind") == "pipeline_stage"]

    ctx.sections["governance"] = {
        "duplicate_rules": len(dupe_rules),
        "duplicate_rules_active": sum(1 for r in dupe_rules if truthy(r.get("IsActive"))),
        "validation_rules": len(val_rules),
        "validation_rules_active": sum(1 for r in val_rules if truthy(r.get("Active"))),
        "record_types": len(rec_types),
        "pipeline_stages": len(stages),
    }

    if dupe_rules:
        off = [r for r in dupe_rules if not truthy(r.get("IsActive"))]
        if off:
            add(doc, ctx,
                id="duplicate-rules-inactive",
                severity="high",
                title=f"{len(off)} of {len(dupe_rules)} duplicate rules are switched off",
                what=("Inactive duplicate rules: "
                      + "; ".join(f"{r.get('MasterLabel') or r.get('DeveloperName')} "
                                  f"({r.get('SobjectType')})" for r in off[:8]) + "."),
                why_it_matters=("This is the mechanism that would have prevented the duplicate "
                                "findings above. Cleaning duplicates without turning these on is "
                                "a project you will run again in twelve months."),
                recommended_fix=("Turn them on in report-only ('allow, but alert') mode first so "
                                 "you can measure the hit rate without blocking anybody. Move to "
                                 "block once the false-positive rate is known."),
                evidence={"count": len(off),
                          "rows": [{"Rule": r.get("MasterLabel") or r.get("DeveloperName"),
                                    "Object": r.get("SobjectType"), "Active": "no"} for r in off],
                          "query": ctx.query("governance")},
                effort="quick")
    elif ctx.crm == "salesforce":
        add(doc, ctx,
            id="duplicate-rules-absent",
            severity="high",
            title="No duplicate rules exist in this org",
            what="No DuplicateRule records were returned, so nothing blocks or warns on duplicate creation.",
            why_it_matters=("Duplicate prevention is native, free and off. Every duplicate in "
                            "this report was created by a system that had the ability to stop it."),
            recommended_fix=("Create standard duplicate rules for Account, Contact and Lead in "
                             "alert mode. Start with the standard matching rules before writing "
                             "custom ones."),
            evidence={"count": 0, "value": 0, "query": ctx.query("governance")},
            effort="quick")

    if val_rules:
        off = [r for r in val_rules if not truthy(r.get("Active"))]
        if off:
            def obj_of(r: Dict[str, Any]) -> str:
                return str((r.get("EntityDefinition") or {}).get("QualifiedApiName")
                           or r.get("object") or "?")
            add(doc, ctx,
                id="validation-rules-inactive",
                severity="medium",
                title=f"{len(off)} of {len(val_rules)} validation rules are inactive",
                what=("Rules that were written, deployed and then switched off: "
                      + "; ".join(f"{obj_of(r)}.{r.get('ValidationName')}" for r in off[:8]) + "."),
                why_it_matters=("Somebody identified each of these problems and built the fix. "
                                "An inactive validation rule is a decision that was reversed under "
                                "pressure and never revisited — and several of them map directly "
                                "onto findings in this report."),
                recommended_fix=("For each rule, find out whether it was turned off for a "
                                 "migration that has since finished. Re-enable those first; they "
                                 "cost nothing and were already tested."),
                evidence={"count": len(off),
                          "rows": [{"Object": obj_of(r), "Rule": r.get("ValidationName"),
                                    "Description": str(r.get("Description") or "")[:80]}
                                   for r in off[:30]],
                          "query": ctx.query("governance")},
                effort="quick")

    if rec_types and rt_usage:
        unused = [r for r in rec_types if truthy(r.get("IsActive")) is not False
                  and rt_usage.get(str(r.get("Id")), 0) == 0]
        if unused:
            add(doc, ctx,
                id="record-types-unused",
                severity="low",
                title=f"{len(unused)} record types have no records in {ctx.cfg['window_days']} days",
                what=("Active record types with zero records in the window: "
                      + "; ".join(f"{r.get('SobjectType')}.{r.get('DeveloperName')}"
                                  for r in unused[:8]) + "."),
                why_it_matters=("Every record type multiplies page layouts, picklist value sets "
                                "and assignment rules that somebody has to maintain. Unused ones "
                                "are pure carrying cost, and they make onboarding an admin harder."),
                recommended_fix=("Deactivate them. If a record type exists for a motion you plan "
                                 "to launch, note that in its description so the next audit does "
                                 "not flag it again."),
                evidence={"count": len(unused),
                          "rows": [{"Object": r.get("SobjectType"),
                                    "Record type": r.get("DeveloperName"), "Records": 0}
                                   for r in unused[:20]],
                          "query": ctx.query("governance")},
                effort="quick")

    if stages:
        archived = [s for s in stages if truthy(s.get("archived")) or truthy(s.get("pipeline_archived"))]
        stage_usage = ctx.sections.get("picklists", {}).get("usage", {}).get("Opportunity.dealstage", {})
        live_on_archived = [s for s in archived if stage_usage.get(str(s.get("stage_id")), 0) > 0]
        if live_on_archived:
            add(doc, ctx,
                id="deals-in-archived-pipeline-stages",
                severity="medium",
                title=f"{len(live_on_archived)} archived deal stages still hold records",
                what=("Deals sit in stages belonging to archived pipelines or archived stages: "
                      + "; ".join(f"{s.get('pipeline_label')} / {s.get('label')} "
                                  f"({stage_usage.get(str(s.get('stage_id')), 0):,} deals)"
                                  for s in live_on_archived[:6]) + "."),
                why_it_matters=("Archived stages disappear from pipeline reports and board views "
                                "but the deals stay in the database. They are open pipeline that "
                                "no report shows and no rep sees."),
                recommended_fix=("Move these deals into a live pipeline stage or close them, then "
                                 "delete the archived pipeline so it cannot collect more."),
                evidence={"count": sum(stage_usage.get(str(s.get("stage_id")), 0)
                                       for s in live_on_archived),
                          "rows": [{"Pipeline": s.get("pipeline_label"), "Stage": s.get("label"),
                                    "Deals": stage_usage.get(str(s.get("stage_id")), 0)}
                                   for s in live_on_archived[:20]],
                          "query": ctx.query("governance")},
                effort="medium")


# --------------------------------------------------------------------------- scoring


def hygiene_index(ctx: Ctx) -> Tuple[int, List[Dict[str, Any]], List[str]]:
    """
    A weighted average of six 'clean rates', each a measured ratio in 0..1.

        index = 100 * Σ(weight_p × clean_p) / Σ(weight_p)   over measurable pillars only

    A pillar with no denominator (no records, no metadata, connector missing) is
    dropped and the remaining weights renormalize, so a partial run still gives a
    comparable number — and the report names which pillars were excluded.

    Governance is deliberately not a pillar: it is a handful of binary switches, and
    one toggle should not move a trend line by ten points.
    """
    weights = {k: float(v) for k, v in (ctx.cfg.get("hygiene_index_weights") or {}).items()
               if isinstance(v, (int, float)) and v > 0}

    # duplicates pillar — the union of duplicate-flagged accounts and contacts
    dupes = len(ctx.defects["dupe_accounts"]) + len(ctx.defects["dupe_contacts"])
    denom = len(ctx.accounts) + len(ctx.contacts)
    ctx.pillars["duplicates"] = {
        "label": "Duplicates",
        "clean": 1 - (dupes / denom) if denom else None,
        "detail": f"{dupes:,} of {denom:,} accounts + contacts sit in a duplicate cluster",
    }

    table, excluded, total_weight, acc = [], [], 0.0, 0.0
    for key in ("duplicates", "field_discipline", "policy_compliance", "ownership",
                "freshness", "structure"):
        pillar = ctx.pillars.get(key)
        weight = weights.get(key, 0.0)
        if not pillar or pillar.get("clean") is None or weight <= 0:
            if weight > 0:
                excluded.append((pillar or {}).get("label", key))
                table.append({"Pillar": (pillar or {}).get("label", key), "Weight": f"{weight:g}",
                              "Clean rate": "not measurable", "Points": "—",
                              "Basis": (pillar or {}).get("detail", "no data for this pillar")})
            continue
        clean = max(0.0, min(1.0, float(pillar["clean"])))
        total_weight += weight
        acc += weight * clean
        table.append({"Pillar": pillar["label"], "Weight": f"{weight:g}",
                      "Clean rate": f"{round(clean * 100, 1)}%",
                      "Points": round(weight * clean, 1), "Basis": pillar["detail"]})
    if not total_weight:
        return 0, table, excluded
    index = int(round(100.0 * acc / total_weight))
    for row in table:
        if isinstance(row["Points"], float):
            row["Points"] = f"{row['Points']:g} of {row['Weight']}"
    return index, table, excluded


# --------------------------------------------------------------------------- main


def build(ctx: Ctx, window: Dict[str, str]) -> FindingsDoc:
    doc = FindingsDoc(plugin=PLUGIN, window=window,
                      org_name=str(ctx.profile.get("org_name") or ""))

    ctx.sections["field_inventory"] = field_inventory(ctx)

    check_dupe_accounts_domain(doc, ctx)
    check_dupe_accounts_name(doc, ctx)
    check_dupe_contacts_email(doc, ctx)
    check_dupe_contacts_name_account(doc, ctx)
    check_dupe_open_opps(doc, ctx)
    check_lead_contact_collision(doc, ctx)

    check_dead_fields(doc, ctx)
    check_policy_required(doc, ctx)

    check_ownership(doc, ctx)

    check_freshness(doc, ctx)
    check_stale_leads(doc, ctx)

    check_structure(doc, ctx)
    check_contactability(doc, ctx)
    check_account_domains(doc, ctx)

    check_picklists(doc, ctx)
    check_governance(doc, ctx)

    index, table, excluded = hygiene_index(ctx)
    ctx.sections["hygiene_index"] = {
        "value": index, "pillars": table, "excluded_pillars": excluded,
        "formula": ("100 x sum(weight x clean_rate) / sum(weight), over measurable pillars only. "
                    "Governance findings are reported but excluded from the index."),
    }

    dupe_records = len(ctx.defects["dupe_accounts"]) + len(ctx.defects["dupe_contacts"])
    fields = ctx.sections.get("field_summary", {})
    pipeline = ctx.sections.get("pipeline", {})
    policy = ctx.pillars.get("policy_compliance", {})

    doc.add_score(Score(
        key="hygiene_index", label="Hygiene Index", value=index, unit="score_0_100",
        direction_good="up",
        context=("weighted across " + ", ".join(
            r["Pillar"].lower() for r in table if r["Points"] != "—") or "no measurable pillars")))
    doc.add_score(Score(
        key="duplicate_records", label="Records in duplicate clusters", value=dupe_records,
        unit="count", direction_good="down",
        context=f"of {len(ctx.accounts) + len(ctx.contacts):,} accounts and contacts read"))
    doc.add_score(Score(
        key="dead_custom_fields", label="Dead custom fields", value=
        int(fields.get("never_populated", 0)) + int(fields.get("low_fill", 0)),
        unit="count", direction_good="down",
        context=f"of {fields.get('measurable_custom_fields', 0):,} measurable custom fields"))
    doc.add_score(Score(
        key="pipeline_past_close", label="Pipeline past its close date",
        value=round(float(pipeline.get("past_close_value", 0))), unit="currency",
        direction_good="down",
        context=f"{pipeline.get('past_close_date', 0):,} open deals already past close"))
    measured = policy.get("clean") is not None
    doc.add_score(Score(
        key="policy_field_compliance", label="Policy-field compliance",
        value=round(policy["clean"] * 100, 1) if measured else "not measured",
        unit="percent" if measured else "", direction_good="up",
        context=(str(policy.get("detail", "")) if measured else
                 "none of the fields in policy_required_fields resolved against this CRM's "
                 "schema — fix the API names in your config")))

    # Person names that ended up in finding prose. report.py scrubs these when the shared
    # profile asks for PII redaction — redacting only table columns would leak the names
    # that are quoted in a sentence, which is exactly where they are most readable.
    ctx.sections["people"] = sorted(
        {name for name in ctx.user_label.values() if name and len(str(name)) >= 4},
        key=lambda n: (-len(str(n)), str(n)))

    doc.sections = ctx.sections
    doc.unavailable = ctx.unavailable
    return doc


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="crm-hygiene analyzer (offline, stdlib only)")
    ap.add_argument("--raw", required=True, help="directory of raw/*.json written by the run skill")
    ap.add_argument("--out", required=True, help="run directory to write findings.json + manifest.json")
    ap.add_argument("--config", help="explicit config file; default is ~/.leanscale-gtm/crm-hygiene.json")
    ap.add_argument("--as-of", help="treat this date as today (YYYY-MM-DD); default is now")
    ap.add_argument("--no-baseline", action="store_true",
                    help="do not compare against the stored baseline")
    args = ap.parse_args(argv)

    raw_dir, out_dir = Path(args.raw).expanduser(), Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, coverage = load_raw(raw_dir)
    fixture = any(env.get("fixture") for env in raw.values()) or bool(coverage.get("fixture"))

    cfg = dict(DEFAULTS)
    if args.config:
        override = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in override.items() if not k.startswith("_")})
    else:
        cfg = load_plugin_config(PLUGIN, defaults=DEFAULTS)
    try:
        profile = load_profile(required=False)
    except ConfigError:
        profile = {}

    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif fixture:
        stamps = [env.get("as_of") for env in raw.values() if env.get("as_of")]
        as_of = (datetime.strptime(sorted(stamps)[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                 if stamps else datetime.now(timezone.utc))
    else:
        as_of = datetime.now(timezone.utc)

    window = {"start": (as_of - timedelta(days=int(cfg["window_days"]))).strftime("%Y-%m-%d"),
              "end": as_of.strftime("%Y-%m-%d")}

    manifest = build_manifest(raw, coverage, out_dir, window)
    manifest.finalize()          # raises SourceEmptyError if a required source came back empty

    ctx = Ctx(raw, cfg, profile, as_of, coverage)
    for entry in (coverage.get("unavailable") or []):
        ctx.unavailable.append(f"{entry.get('check')} — {entry.get('reason')}")
    for name, spec in SOURCES.items():
        if spec["required"] or raw.get(name):
            continue
        if spec.get("degrade"):
            ctx.notes.append(spec["degrade"])
        elif spec.get("label"):
            # contact roles have a second, equally good source: inline HubSpot associations
            if name == "opportunity_contact_roles" and ctx.roles:
                ctx.notes.append("Contact roles were read from inline deal-to-contact "
                                 "associations rather than from a separate association fetch.")
                continue
            ctx.unavailable.append(f"{spec['label']} — raw/{name}.json was not fetched")

    doc = build(ctx, window)
    payload = doc.to_dict()
    payload["crm"] = ctx.crm
    payload["as_of"] = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["config_used"] = {k: v for k, v in cfg.items() if not k.startswith("_")}
    payload["sections"]["notes"] = ctx.notes

    baseline_key = PLUGIN + ("-fixture" if fixture else "")
    baseline_enabled = not args.no_baseline and not (fixture and "LEANSCALE_GTM_HOME" not in __import__("os").environ)
    payload["sections"]["run"] = {"baseline_key": baseline_key, "fixture": fixture,
                                  "baseline_enabled": baseline_enabled}
    if baseline_enabled:
        payload = apply_deltas(payload, baseline_key)
    else:
        payload["is_baseline_run"] = True

    path = out_dir / "findings.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    if fixture:
        print("FIXTURE MODE — synthetic sample data, not a real CRM."
              + ("" if baseline_enabled else " Baselines are not written for fixture runs."))
    print(f"crm       : {ctx.crm}")
    print(f"read      : {sum(len(e.get('records') or []) for e in raw.values()):,} records "
          f"across {len(raw)} sources")
    print(f"findings  : {len(payload['findings'])} "
          f"({', '.join(f'{v} {k}' for k, v in payload['counts_by_severity'].items() if v) or 'none'})")
    print(f"hygiene   : {payload['scores'][0]['value']}/100")
    if payload.get("unavailable"):
        print(f"unavailable: {len(payload['unavailable'])} check group(s) — see the report")
    print(f"wrote     : {path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SourceEmptyError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except ConfigError as exc:
        print(f"Config problem:\n{exc}", file=sys.stderr)
        sys.exit(3)
