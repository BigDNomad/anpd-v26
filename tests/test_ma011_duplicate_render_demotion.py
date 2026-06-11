"""
Tests for MA-011 duplicate-render-detection CLASS_B demotion (2026-06-13).

Operator ruling: all duplicate-render findings demoted to CLASS_B advisory.
The verbatim cross-scene check (cross_scene_duplication) is untouched.

1. Contradiction findings are now CLASS_B, not CLASS_A.
2. Clean-duplicate findings remain CLASS_B.
3. Class-level severity attribute is CLASS_B.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import ManuscriptArtifact, SceneText, BriefBundle
from audit_checks.duplicate_render_detection import (
    DuplicateRenderDetection,
    _build_findings,
)


def _make_sigs_with_contradiction() -> dict[int, dict]:
    """Two adjacent scenes with same beat + contradicting object state."""
    return {
        1: {
            "scene_number": 1,
            "characters": ["Archer"],
            "location": "tarmac",
            "core_action": "loads gear into jeep",
            "objects": [{"name": "duffel bag", "state": "dropped on ground"}],
        },
        2: {
            "scene_number": 2,
            "characters": ["Archer"],
            "location": "tarmac",
            "core_action": "loads gear into jeep",
            "objects": [{"name": "duffel bag", "state": "squared in back seat"}],
        },
    }


def _make_sigs_clean_duplicate() -> dict[int, dict]:
    """Two adjacent scenes with same beat, no contradiction."""
    return {
        1: {
            "scene_number": 1,
            "characters": ["Coyle"],
            "location": "cockpit",
            "core_action": "fires miniguns at convoy",
            "objects": [],
        },
        2: {
            "scene_number": 2,
            "characters": ["Coyle"],
            "location": "cockpit",
            "core_action": "fires miniguns at convoy",
            "objects": [],
        },
    }


class TestDuplicateRenderDemotion:

    def test_contradiction_finding_is_class_b(self):
        """Contradiction duplicate renders must be CLASS_B (demoted from A)."""
        sigs = _make_sigs_with_contradiction()
        findings = _build_findings(sigs)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_B", (
            f"Expected CLASS_B, got {findings[0].severity}"
        )

    def test_clean_duplicate_remains_class_b(self):
        """Clean duplicates were already CLASS_B and must stay CLASS_B."""
        sigs = _make_sigs_clean_duplicate()
        findings = _build_findings(sigs)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_B"

    def test_class_attribute_is_class_b(self):
        """The check module's class-level severity must be CLASS_B."""
        check = DuplicateRenderDetection()
        assert check.severity == "CLASS_B"

    def test_no_class_a_findings_from_build(self):
        """No findings from _build_findings should ever be CLASS_A."""
        sigs = _make_sigs_with_contradiction()
        sigs.update({
            3: {
                "scene_number": 3,
                "characters": ["Archer"],
                "location": "tarmac",
                "core_action": "loads gear into jeep",
                "objects": [{"name": "duffel bag", "state": "torn open"}],
            },
        })
        findings = _build_findings(sigs)
        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert class_a == [], (
            f"Found {len(class_a)} CLASS_A findings, expected 0"
        )
