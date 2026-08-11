"""
Baselines — the reason these agents are worth running twice.

A health score with no baseline is a vibe. We have lost a renewal after a year
of real delivery because there was no kickoff snapshot to prove the change
against. So run one takes a baseline and says so, plainly, in the report; every
run after it shows movement.

Snapshots live in ~/.leanscale-gtm/baselines/<plugin>/<timestamp>.json and are
never pruned automatically — they are the customer's evidence trail.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import GTM_HOME


def baseline_dir(plugin: str) -> Path:
    return GTM_HOME / "baselines" / plugin


def list_baselines(plugin: str) -> List[Path]:
    d = baseline_dir(plugin)
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def load_previous_baseline(plugin: str) -> Optional[Dict[str, Any]]:
    """Most recent prior snapshot, or None if this is the first run."""
    files = list_baselines(plugin)
    if not files:
        return None
    try:
        with files[-1].open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def save_baseline(plugin: str, doc: Dict[str, Any]) -> Path:
    """
    Persist the comparable slice of a findings doc: the scores, the severity
    counts, and each finding's headline count. Deliberately not the whole
    document — we want a small, stable, diffable record.
    """
    d = baseline_dir(plugin)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    snapshot = {
        "plugin": plugin,
        "taken_at": stamp,
        "window": doc.get("window", {}),
        "counts_by_severity": doc.get("counts_by_severity", {}),
        "scores": {s["key"]: s.get("value") for s in doc.get("scores", [])},
        "finding_counts": {
            f["id"]: (f.get("evidence") or {}).get("count")
            for f in doc.get("findings", [])
        },
    }
    path = d / f"{stamp}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, default=str)
        fh.write("\n")
    return path


def _numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def diff_scores(
    current: Dict[str, Any], previous: Optional[Dict[str, Any]]
) -> Tuple[Dict[str, Optional[float]], Dict[str, Optional[float]]]:
    """
    Return (score_deltas, finding_deltas) keyed the same way as the snapshot.
    A None delta means "not comparable" — new metric, or non-numeric.
    """
    if not previous:
        return {}, {}

    score_deltas: Dict[str, Optional[float]] = {}
    prev_scores = previous.get("scores", {})
    for score in current.get("scores", []):
        now, before = _numeric(score.get("value")), _numeric(prev_scores.get(score["key"]))
        score_deltas[score["key"]] = None if now is None or before is None else round(now - before, 4)

    finding_deltas: Dict[str, Optional[float]] = {}
    prev_findings = previous.get("finding_counts", {})
    for finding in current.get("findings", []):
        now = _numeric((finding.get("evidence") or {}).get("count"))
        before = _numeric(prev_findings.get(finding["id"]))
        finding_deltas[finding["id"]] = (
            None if now is None or before is None else round(now - before, 4)
        )
    return score_deltas, finding_deltas


def apply_deltas(doc: Dict[str, Any], plugin: str) -> Dict[str, Any]:
    """
    Attach deltas to a findings doc in place and set is_baseline_run correctly.
    Call this after building findings and before rendering.
    """
    previous = load_previous_baseline(plugin)
    doc["is_baseline_run"] = previous is None
    if previous is None:
        return doc

    score_deltas, finding_deltas = diff_scores(doc, previous)
    for score in doc.get("scores", []):
        score["delta_vs_last"] = score_deltas.get(score["key"])
    for finding in doc.get("findings", []):
        finding["delta_vs_last"] = finding_deltas.get(finding["id"])
    doc["compared_to"] = previous.get("taken_at")
    return doc


BASELINE_RUN_NOTE = (
    "This is your baseline run. Every number here is a starting point, not a verdict — "
    "the comparison begins on your next run, which will show what moved and by how much. "
    "Keep the snapshot: it is the evidence that the work changed something."
)
