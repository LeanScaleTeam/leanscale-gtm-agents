"""
CRM record helpers — the small, boring functions every plugin would otherwise
reimplement slightly differently.

Handles the two shapes these agents actually see:

  Salesforce (SOQL via MCP)    {"Id": "006...", "Amount": 50000, "StageName": "..."}
                               nested relationships: {"Owner": {"Name": "..."}}
  HubSpot (CRM search via MCP) {"id": "123", "properties": {"amount": "50000", ...}}

normalize_records() flattens both into plain dicts with dotted keys so the
analysis code never branches on CRM vendor.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, date, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse the date shapes Salesforce and HubSpot emit. Returns tz-aware UTC or None."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):  # HubSpot epoch milliseconds
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit() and len(text) >= 10:
        return parse_dt(int(text))
    cleaned = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", text)
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def days_between(start: Any, end: Any) -> Optional[int]:
    a, b = parse_dt(start), parse_dt(end)
    if a is None or b is None:
        return None
    return (b - a).days


def _flatten(record: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in record.items():
        if key == "attributes":  # Salesforce response noise
            continue
        label = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, prefix=f"{label}."))
        else:
            out[label] = value
    return out


def normalize_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Flatten Salesforce nested relationships and lift HubSpot's `properties` bag
    to the top level, so both look the same to the analysis code.
    """
    out: List[Dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if "properties" in record and isinstance(record["properties"], dict):
            merged = {"Id": record.get("id"), **record["properties"]}
            for key, value in record.items():
                if key not in ("properties", "id"):
                    merged.setdefault(key, value)
            out.append(_flatten(merged))
        else:
            out.append(_flatten(record))
    return out


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().lower() in ("null", "none", "n/a", "-")
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def fill_rate(records: Sequence[Dict[str, Any]], field: str) -> float:
    """Share of records where `field` carries a real value. 0.0–1.0."""
    if not records:
        return 0.0
    filled = sum(1 for r in records if not is_blank(r.get(field)))
    return filled / len(records)


def to_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[,$\s]", "", str(value))
    try:
        return float(text)
    except ValueError:
        return None


def median(values: Sequence[float]) -> Optional[float]:
    nums = sorted(v for v in values if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return float(nums[mid])
    return (nums[mid - 1] + nums[mid]) / 2.0


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile. p is 0–100."""
    nums = sorted(v for v in values if v is not None)
    if not nums:
        return None
    if len(nums) == 1:
        return float(nums[0])
    k = (len(nums) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(nums) - 1)
    if lo == hi:
        return float(nums[lo])
    return nums[lo] + (nums[hi] - nums[lo]) * (k - lo)


def pct(numerator: float, denominator: float, digits: int = 1) -> float:
    """Percentage, guarded against a zero denominator."""
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, digits)


def email_domain(value: Any) -> Optional[str]:
    if is_blank(value):
        return None
    match = re.search(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", str(value))
    return match.group(1).lower() if match else None


FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "protonmail.com", "live.com", "msn.com", "me.com", "mail.com",
}


def normalize_company(name: Any) -> Optional[str]:
    """
    Aggressive company-name key for dedupe candidates. Deliberately lossy —
    surface these as *candidates* for a human, never auto-merge on them.
    """
    if is_blank(name):
        return None
    text = str(name).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    stop = r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|sa|bv|ag|holdings|group|the)\b"
    text = re.sub(stop, " ", text)
    text = re.sub(r"\s+", "", text)
    return text or None


def redact_name(value: Any, salt: str = "leanscale") -> str:
    """Stable pseudonym for PII-redacted reports. Same input -> same label within a run."""
    if is_blank(value):
        return "unknown"
    digest = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:6]
    return f"person-{digest}"


def bucket(value: Optional[float], edges: Sequence[float], labels: Sequence[str]) -> str:
    """bucket(37, [30, 60, 90], ['<30','30-60','60-90','90+']) -> '30-60'"""
    if value is None:
        return "unknown"
    for i, edge in enumerate(edges):
        if value < edge:
            return labels[i]
    return labels[-1]
