"""Tests for publish_gate — one test per clearance branch (spec §3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from publish_gate import ClearanceResult, evaluate_clearance


# ── Helpers ─────────────────────────────────────────────────────────────────

def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _clean_report(class_a: int = 0, class_b: int = 2, class_c: int = 5) -> dict:
    """Build a minimal audit report with the given CLASS_A count."""
    findings = []
    for i in range(class_a):
        findings.append({
            "check_id": f"MA-{i+1:03d}",
            "severity": "CLASS_A",
            "description": f"class-a finding {i+1}",
            "evidence": ["x"],
        })
    for i in range(class_b):
        findings.append({
            "check_id": f"MA-{100+i:03d}",
            "severity": "CLASS_B",
            "description": f"class-b finding {i+1}",
            "evidence": [],
        })
    for i in range(class_c):
        findings.append({
            "check_id": f"MA-{200+i:03d}",
            "severity": "CLASS_C",
            "description": f"class-c finding {i+1}",
            "evidence": [],
        })
    return {
        "header": {},
        "summary": {
            "class_a": class_a,
            "class_b": class_b,
            "class_c": class_c,
            "total": class_a + class_b + class_c,
        },
        "findings_by_check": {},
        "all_findings": findings,
    }


# ── Branch 1: failure_report.json → BLOCKED(generation) ────────────────────

def test_branch1_generation_blocked(tmp_path: Path) -> None:
    ms_dir = tmp_path / "manuscript_20260527_0953"
    ms_dir.mkdir()
    failing = [{"chapter": 1, "scene": 19, "title": "Ejection", "attempts": 3}]
    _write_json(ms_dir / "failure_report.json", {
        "run_id": "r1",
        "blocked": True,
        "class_a_failures": 1,
        "class_b_warnings": 0,
        "failing_scenes": failing,
        "remediation_path": "fix",
    })
    # Even if an audit report exists, generation block wins (order).
    report = tmp_path / "report.json"
    _write_json(report, _clean_report())

    result = evaluate_clearance(ms_dir, report)
    assert result.status == "BLOCKED"
    assert result.reason == "generation"
    assert result.detail == failing


# ── Branch 2: BLOCKED filename → BLOCKED(generation_filename) ──────────────

def test_branch2_blocked_filename(tmp_path: Path) -> None:
    ms_dir = tmp_path / "manuscript_20260527_0953"
    ms_dir.mkdir()
    (ms_dir / "manuscript_BLOCKED.md").write_text("blocked content")
    report = tmp_path / "report.json"
    _write_json(report, _clean_report())

    result = evaluate_clearance(ms_dir, report)
    assert result.status == "BLOCKED"
    assert result.reason == "generation_filename"


# ── Branch 3: no audit report → UNAUDITED ──────────────────────────────────

def test_branch3_unaudited(tmp_path: Path) -> None:
    ms_dir = tmp_path / "manuscript_20260527_0953"
    ms_dir.mkdir()
    nonexistent = tmp_path / "does_not_exist.json"

    result = evaluate_clearance(ms_dir, nonexistent)
    assert result.status == "UNAUDITED"
    assert result.reason == "unaudited"


# ── Branch 4: report integrity mismatch → BLOCKED(report_integrity) ────────

def test_branch4_report_integrity(tmp_path: Path) -> None:
    ms_dir = tmp_path / "manuscript_20260527_0953"
    ms_dir.mkdir()
    report_path = tmp_path / "report.json"
    # summary says 0 CLASS_A, but all_findings has 2 CLASS_A → mismatch
    report = _clean_report(class_a=0)
    report["all_findings"].append({
        "check_id": "MA-047",
        "severity": "CLASS_A",
        "description": "phantom finding 1",
        "evidence": [],
    })
    report["all_findings"].append({
        "check_id": "MA-011",
        "severity": "CLASS_A",
        "description": "phantom finding 2",
        "evidence": [],
    })
    _write_json(report_path, report)

    result = evaluate_clearance(ms_dir, report_path)
    assert result.status == "BLOCKED"
    assert result.reason == "report_integrity"
    assert "0" in str(result.detail) and "2" in str(result.detail)


# ── Branch 5: CLASS_A findings → BLOCKED(audit) ────────────────────────────

def test_branch5_audit_blocked(tmp_path: Path) -> None:
    ms_dir = tmp_path / "manuscript_20260527_0953"
    ms_dir.mkdir()
    report_path = tmp_path / "report.json"
    _write_json(report_path, _clean_report(class_a=3))

    result = evaluate_clearance(ms_dir, report_path)
    assert result.status == "BLOCKED"
    assert result.reason == "audit"
    assert len(result.findings) == 3
    assert all(f["severity"] == "CLASS_A" for f in result.findings)


# ── Branch 6: clean report → CLEARED ───────────────────────────────────────

def test_branch6_cleared(tmp_path: Path) -> None:
    ms_dir = tmp_path / "manuscript_20260527_0953"
    ms_dir.mkdir()
    report_path = tmp_path / "report.json"
    _write_json(report_path, _clean_report(class_a=0, class_b=1, class_c=3))

    result = evaluate_clearance(ms_dir, report_path)
    assert result.status == "CLEARED"
    assert result.reason == "none"
    assert result.findings == []


# ── Edge: corrupt report JSON → BLOCKED(report_integrity) ──────────────────

def test_corrupt_report_json(tmp_path: Path) -> None:
    ms_dir = tmp_path / "manuscript_20260527_0953"
    ms_dir.mkdir()
    report_path = tmp_path / "report.json"
    report_path.write_text("{invalid json", encoding="utf-8")

    result = evaluate_clearance(ms_dir, report_path)
    assert result.status == "BLOCKED"
    assert result.reason == "report_integrity"
