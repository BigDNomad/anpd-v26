"""
publish_gate — Clearance check for manuscript export.

Reads manuscript_audit_REPORT.json and failure_report.json to decide
whether a manuscript is safe to export.  Pure function: no side effects,
no printing, no file writes.

Statuses: CLEARED, BLOCKED, UNAUDITED, OVERRIDDEN.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ClearanceResult:
    status: str          # CLEARED | BLOCKED | UNAUDITED | OVERRIDDEN
    reason: str          # none | generation | generation_filename | unaudited | audit | report_integrity
    detail: object = ""  # structured: failing_scenes list or string message
    findings: list[dict] = field(default_factory=list)  # CLASS_A finding dicts when reason=="audit"


def evaluate_clearance(
    manuscript_dir: Path,
    audit_report_path: Path,
) -> ClearanceResult:
    """Evaluate whether a manuscript is cleared for export.

    Implements spec §3 — six ordered branches, first match wins.
    """
    # 1. Generation block — failure_report.json present
    failure_report = manuscript_dir / "failure_report.json"
    if failure_report.exists():
        try:
            data = json.loads(failure_report.read_text(encoding="utf-8"))
            failing_scenes = data.get("failing_scenes", [])
        except (json.JSONDecodeError, OSError):
            failing_scenes = []
        return ClearanceResult(
            status="BLOCKED",
            reason="generation",
            detail=failing_scenes,
        )

    # 2. Generation-block filename signal (belt-and-suspenders)
    blocked_files = list(manuscript_dir.glob("manuscript_*BLOCKED*.md"))
    if blocked_files:
        return ClearanceResult(
            status="BLOCKED",
            reason="generation_filename",
            detail="manuscript named BLOCKED, no failure_report.json",
        )

    # 3. Audit report must exist
    if not audit_report_path.exists():
        return ClearanceResult(
            status="UNAUDITED",
            reason="unaudited",
            detail="no manuscript_audit_REPORT.json found",
        )

    # 4–5. Parse audit report
    try:
        report = json.loads(audit_report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return ClearanceResult(
            status="BLOCKED",
            reason="report_integrity",
            detail=f"cannot parse audit report: {exc}",
        )

    summary_class_a = report.get("summary", {}).get("class_a", 0)
    all_findings = report.get("all_findings", [])
    class_a_findings = [f for f in all_findings if f.get("severity") == "CLASS_A"]

    # 4. Report integrity — summary vs all_findings cross-check
    if summary_class_a != len(class_a_findings):
        return ClearanceResult(
            status="BLOCKED",
            reason="report_integrity",
            detail=f"summary.class_a={summary_class_a} but all_findings has {len(class_a_findings)} CLASS_A",
        )

    # 5. CLASS_A findings present
    if summary_class_a > 0:
        return ClearanceResult(
            status="BLOCKED",
            reason="audit",
            detail=class_a_findings,
            findings=class_a_findings,
        )

    # 6. Cleared
    return ClearanceResult(
        status="CLEARED",
        reason="none",
    )
