"""
The shared findings envelope.

All nine plugins emit the same shape so the suite reads as one product and a
customer can diff a hygiene report against a pipeline report without a decoder
ring.

Severity means the same thing everywhere:
    critical - the number an executive is looking at is wrong, or revenue is leaking now
    high     - a decision is being made on bad data; fix this quarter
    medium   - real drag on the team; fix when convenient
    low      - hygiene, cosmetic, or a watch item
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SEVERITIES = ("critical", "high", "medium", "low")
EFFORTS = ("quick", "medium", "project")


def severity_rank(sev: str) -> int:
    try:
        return SEVERITIES.index(sev)
    except ValueError:
        return len(SEVERITIES)


class FindingsError(ValueError):
    pass


@dataclass
class Score:
    """A headline number. Keep it to at most five per plugin — a wall of KPIs says nothing."""

    key: str
    label: str
    value: Any
    unit: str = "count"
    delta_vs_last: Optional[float] = None
    direction_good: str = "down"  # "down" | "up" | "flat" — which way is improvement
    context: str = ""


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    what: str
    why_it_matters: str
    recommended_fix: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    effort: str = "medium"
    owner_hint: str = "RevOps"
    delta_vs_last: Optional[float] = None

    def validate(self) -> None:
        if self.severity not in SEVERITIES:
            raise FindingsError(f"{self.id}: severity {self.severity!r} not in {SEVERITIES}")
        if self.effort not in EFFORTS:
            raise FindingsError(f"{self.id}: effort {self.effort!r} not in {EFFORTS}")
        # A finding the customer cannot verify in their own system is not shippable.
        ev = self.evidence or {}
        if not any(k in ev for k in ("count", "sample_ids", "query", "rows", "value")):
            raise FindingsError(
                f"{self.id}: evidence must carry at least one of "
                f"count / sample_ids / query / rows / value so the customer can verify it."
            )
        for attr in ("what", "why_it_matters", "recommended_fix"):
            if not str(getattr(self, attr)).strip():
                raise FindingsError(f"{self.id}: {attr} is empty")


@dataclass
class FindingsDoc:
    plugin: str
    window: Dict[str, str]
    is_baseline_run: bool = True
    scores: List[Score] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    sections: Dict[str, Any] = field(default_factory=dict)
    unavailable: List[str] = field(default_factory=list)
    org_name: str = ""
    generated_at: str = ""

    def add(self, finding: Finding) -> None:
        finding.validate()
        if any(f.id == finding.id for f in self.findings):
            raise FindingsError(f"duplicate finding id {finding.id!r}")
        self.findings.append(finding)

    def add_score(self, score: Score) -> None:
        self.scores.append(score)

    def sorted_findings(self) -> List[Finding]:
        return sorted(self.findings, key=lambda f: (severity_rank(f.severity), f.title))

    def counts_by_severity(self) -> Dict[str, int]:
        out = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin": self.plugin,
            "org_name": self.org_name,
            "generated_at": self.generated_at
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": self.window,
            "is_baseline_run": self.is_baseline_run,
            "counts_by_severity": self.counts_by_severity(),
            "scores": [asdict(s) for s in self.scores],
            "findings": [asdict(f) for f in self.sorted_findings()],
            "unavailable": self.unavailable,
            "sections": self.sections,
        }

    def write(self, run_dir: Path) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "findings.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, default=str)
            fh.write("\n")
        return path


def load_findings(path: Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)
