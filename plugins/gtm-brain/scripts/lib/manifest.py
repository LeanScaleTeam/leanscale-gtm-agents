"""
Run manifest — provenance and the fail-loud contract.

Autonomous agents fail silently. We have watched a customer's model-backed
enrichment job stop authenticating and sit dead for six weeks because the
output still looked plausible. So: every run records what it read and how
much came back, and a REQUIRED source that returns zero records aborts the
run instead of emitting a confident, empty, wrong report.

A report that says "0 issues found" because auth failed is worse than a crash.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class SourceEmptyError(RuntimeError):
    """A required source returned no records. Message must name the likely cause."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunManifest:
    """
    Accumulates per-source provenance for one run.

        m = RunManifest("crm-hygiene", run_dir)
        m.record("opportunities", tool="run_soql_query", query=SOQL,
                 count=len(rows), required=True)
        m.finalize()          # raises SourceEmptyError if a required source was empty
    """

    def __init__(self, plugin: str, run_dir: Path, window: Optional[Dict[str, str]] = None):
        self.plugin = plugin
        self.run_dir = Path(run_dir)
        self.window = window or {}
        self.started_at = _utcnow()
        self.sources: List[Dict[str, Any]] = []
        self.warnings: List[str] = []

    def record(
        self,
        name: str,
        *,
        tool: str,
        count: int,
        query: str = "",
        required: bool = True,
        note: str = "",
        diagnosis: str = "",
    ) -> None:
        """
        Register one source read.

        `diagnosis` is the plain-English "here's what's probably wrong" message shown
        if this source comes back empty. Always supply one for required sources —
        "Slack returned nothing" is useless; "the connected Slack identity is a bot
        that isn't a member of the channel" is actionable.
        """
        self.sources.append(
            {
                "name": name,
                "tool": tool,
                "query": query,
                "record_count": int(count),
                "required": bool(required),
                "note": note,
                "diagnosis": diagnosis,
                "read_at": _utcnow(),
            }
        )

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def empty_required(self) -> List[Dict[str, Any]]:
        return [s for s in self.sources if s["required"] and s["record_count"] == 0]

    def unavailable_optional(self) -> List[str]:
        return [s["name"] for s in self.sources if not s["required"] and s["record_count"] == 0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin": self.plugin,
            "started_at": self.started_at,
            "finished_at": _utcnow(),
            "window": self.window,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sources": self.sources,
            "warnings": self.warnings,
            "total_records": sum(s["record_count"] for s in self.sources),
        }

    def write(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / "manifest.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")
        return path

    def finalize(self) -> Path:
        """Write the manifest, then abort if a required source came back empty."""
        path = self.write()
        empties = self.empty_required
        if empties:
            lines = [
                "Run aborted — a required data source returned zero records.",
                "This is almost always a connection or permission problem, not an "
                "empty CRM. Reporting 'no issues' here would be a lie.",
                "",
            ]
            for src in empties:
                lines.append(f"  · {src['name']} (via {src['tool']}) returned 0 records.")
                if src["diagnosis"]:
                    lines.append(f"    Likely cause: {src['diagnosis']}")
                if src["query"]:
                    q = src["query"].replace("\n", " ")
                    lines.append(f"    Query: {q[:300]}")
            lines += ["", f"Manifest written to {path}", "Re-run the plugin's :setup skill to diagnose."]
            raise SourceEmptyError("\n".join(lines))
        return path
