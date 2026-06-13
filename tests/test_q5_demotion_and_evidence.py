"""
Tests for Q5 (character_profile_fidelity) demotion + structured evidence.

Q5 was observed returning [unstable: WEAK/FAIL/PASS] across 3 passes —
the rubric is too subjective to converge.  A non-converging check must
not gate at Class A and must not feed a fixer (same principle as MA-001).

Covers:
  - Q5 FAIL → capped at Class B (never A) — demotion
  - Q5 WEAK → Class B (unchanged)
  - Q5 is in _ADVISORY_ONLY_CHECKS
  - Q5 evidence: violations array piped through when present
  - Q5 evidence: missing violations → fallback evidence dict
  - Majority verdict instability: 3-way split → worst verdict, not stable
  - Majority verdict stability: 2/3 agree → majority, stable
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_auditor import (
    items_to_findings,
    consolidate,
    _ADVISORY_ONLY_CHECKS,
    _majority_verdict,
)


def _effective_config():
    return {
        "target_chapter_count": 25,
        "target_synopsis_word_min": 8000,
        "target_synopsis_word_max": 15000,
        "action_scene_percentage_min": 0.40,
    }


def _make_call_data(items):
    return {"items": items}


EMPTY_CALL = {"items": []}


# ─── Demotion tests ──────────────────────────────────────────────────────────

class TestQ5Demotion:

    def test_q5_in_advisory_only_checks(self):
        assert "Q5" in _ADVISORY_ONLY_CHECKS

    def test_q5_fail_capped_at_class_b(self):
        """Q5 FAIL must produce Class B, never Class A."""
        call_1 = _make_call_data([{
            "id": "Q5",
            "verdict": "FAIL",
            "note": "Protagonist violates inviolable rule in scene 12.",
            "violations": [
                {"character": "Hank", "scene": 12, "profile_rule": "never retreats under fire"}
            ],
        }])
        findings = items_to_findings(call_1, EMPTY_CALL, "/fake/synopsis.md", _effective_config())
        q5_findings = [f for f in findings if f["location"]["rubric_id"] == "Q5"]
        assert len(q5_findings) == 1
        assert q5_findings[0]["class_"] == "B", "Q5 FAIL must be capped at B"

    def test_q5_weak_stays_class_b(self):
        """Q5 WEAK → Class B (same as before demotion — no change)."""
        call_1 = _make_call_data([{
            "id": "Q5",
            "verdict": "WEAK",
            "note": "Minor inconsistency in scene 8.",
        }])
        findings = items_to_findings(call_1, EMPTY_CALL, "/fake/synopsis.md", _effective_config())
        q5_findings = [f for f in findings if f["location"]["rubric_id"] == "Q5"]
        assert len(q5_findings) == 1
        assert q5_findings[0]["class_"] == "B"

    def test_non_q5_fail_still_class_a(self):
        """Other checks (e.g. Q1) FAIL → Class A, unaffected by Q5 demotion."""
        call_1 = _make_call_data([{
            "id": "Q1",
            "verdict": "FAIL",
            "note": "Missing intake element.",
        }])
        findings = items_to_findings(call_1, EMPTY_CALL, "/fake/synopsis.md", _effective_config())
        q1_findings = [f for f in findings if f["location"]["rubric_id"] == "Q1"]
        assert len(q1_findings) == 1
        assert q1_findings[0]["class_"] == "A"


# ─── Evidence tests ──────────────────────────────────────────────────────────

class TestQ5Evidence:

    def test_violations_array_piped_through(self):
        """Q5 with violations array → evidence contains violations."""
        violations = [
            {"character": "Hank", "scene": 12, "profile_rule": "never retreats under fire"},
            {"character": "Lena", "scene": 23, "profile_rule": "always protects the team"},
        ]
        call_1 = _make_call_data([{
            "id": "Q5",
            "verdict": "FAIL",
            "note": "Two violations found.",
            "violations": violations,
        }])
        findings = items_to_findings(call_1, EMPTY_CALL, "/fake/synopsis.md", _effective_config())
        q5 = [f for f in findings if f["location"]["rubric_id"] == "Q5"][0]
        assert q5["evidence"] is not None
        assert q5["evidence"]["violations"] == violations
        assert len(q5["evidence"]["violations"]) == 2
        # Verify fields present
        for v in q5["evidence"]["violations"]:
            assert "character" in v
            assert "scene" in v
            assert "profile_rule" in v

    def test_missing_violations_fallback(self):
        """Q5 without violations array → evidence has empty violations + note."""
        call_1 = _make_call_data([{
            "id": "Q5",
            "verdict": "FAIL",
            "note": "Some issue but no structured evidence.",
        }])
        findings = items_to_findings(call_1, EMPTY_CALL, "/fake/synopsis.md", _effective_config())
        q5 = [f for f in findings if f["location"]["rubric_id"] == "Q5"][0]
        assert q5["evidence"] is not None
        assert q5["evidence"]["violations"] == []
        assert "no structured evidence" in q5["evidence"]["note"]

    def test_non_q5_evidence_still_none(self):
        """Non-Q5 checks still have evidence=None (no accidental bleed)."""
        call_1 = _make_call_data([{
            "id": "Q1",
            "verdict": "WEAK",
            "note": "Minor gap.",
        }])
        findings = items_to_findings(call_1, EMPTY_CALL, "/fake/synopsis.md", _effective_config())
        q1 = [f for f in findings if f["location"]["rubric_id"] == "Q1"][0]
        assert q1["evidence"] is None


# ─── Majority verdict stability tests ────────────────────────────────────────

class TestMajorityVerdictStability:

    def test_three_way_split_is_unstable(self):
        """3-way split → worst verdict, not stable."""
        verdict, stable = _majority_verdict(["WEAK", "FAIL", "PASS"])
        assert verdict == "FAIL"
        assert stable is False

    def test_two_of_three_agree_is_stable(self):
        """2/3 agree → majority verdict, stable."""
        verdict, stable = _majority_verdict(["PASS", "FAIL", "PASS"])
        assert verdict == "PASS"
        assert stable is True

    def test_unanimous_is_stable(self):
        """3/3 agree → stable."""
        verdict, stable = _majority_verdict(["FAIL", "FAIL", "FAIL"])
        assert verdict == "FAIL"
        assert stable is True


# ─── Consolidation demotion tests ─────────────────────────────────────────────

class TestConsolidationDemotion:

    def _make_call_data(self, items):
        return {
            "items": items,
            "total_scenes": 100,
            "action_scenes": 67,
            "action_scene_percentage": 67.0,
            "resolution_scenes": 2,
        }

    def test_q5_fail_does_not_make_overall_verdict_fail(self):
        """Q5 FAIL alone must not produce overall verdict FAIL."""
        call_1 = self._make_call_data([
            {"id": "Q1", "verdict": "PASS", "note": ""},
            {"id": "Q5", "verdict": "FAIL", "note": "Violation found."},
        ])
        call_2 = self._make_call_data([])
        output, _, verdict = consolidate(
            call_1, call_2, "Test", 10000, _effective_config()
        )
        assert verdict != "FAIL", "Q5 FAIL must not gate — it is advisory-only"
        # Q5 should appear in weaks, not fails
        assert "Q5" not in output.get("fails", [])
        assert "Q5" in output.get("weaks", [])

    def test_non_advisory_fail_still_gates(self):
        """A non-advisory check (e.g. Q1) FAIL must still produce FAIL verdict."""
        call_1 = self._make_call_data([
            {"id": "Q1", "verdict": "FAIL", "note": "Missing element."},
            {"id": "Q5", "verdict": "PASS", "note": ""},
        ])
        call_2 = self._make_call_data([])
        _, _, verdict = consolidate(
            call_1, call_2, "Test", 10000, _effective_config()
        )
        assert verdict == "FAIL"

    def test_q5_fail_plus_other_fail_overall_still_fail(self):
        """If both Q5 and a real check FAIL, overall is still FAIL (from the real check)."""
        call_1 = self._make_call_data([
            {"id": "Q1", "verdict": "FAIL", "note": "Missing."},
            {"id": "Q5", "verdict": "FAIL", "note": "Violation."},
        ])
        call_2 = self._make_call_data([])
        output, _, verdict = consolidate(
            call_1, call_2, "Test", 10000, _effective_config()
        )
        assert verdict == "FAIL"
        assert "Q1" in output["fails"]
        assert "Q5" not in output["fails"]
        assert "Q5" in output["weaks"]
