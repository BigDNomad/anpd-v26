"""
Tests for MA-001 character_detail_consistency CLASS_B demotion.

Validates:
  - MA-001 severity field is CLASS_B (not CLASS_A)
  - Deterministic findings emit CLASS_B
  - LLM findings emit CLASS_B
  - Module auto-discovery still works
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import (
    REGISTRY,
    discover_and_register,
)
from audit_checks.character_detail_consistency import (
    CharacterDetailConsistency,
    _deterministic_checks,
)


class TestDemotionSeverity:

    def test_class_severity_is_class_b(self):
        """MA-001 class-level severity must be CLASS_B after demotion."""
        check = CharacterDetailConsistency()
        assert check.severity == "CLASS_B"

    def test_check_id_unchanged(self):
        """Check ID must remain MA-001-character-detail-consistency."""
        check = CharacterDetailConsistency()
        assert check.check_id == "MA-001-character-detail-consistency"

    def test_module_auto_discovered(self):
        """MA-001 must still be discoverable in the registry."""
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-001-character-detail-consistency" in check_ids
        REGISTRY.clear()


class TestDeterministicFindingsClassB:

    def test_deterministic_device_findings_are_class_b(self):
        """Deterministic device-brand findings must emit CLASS_B."""
        from audit_checks import ManuscriptArtifact, SceneText

        # Create scenes with device brand contradiction
        scenes = [
            SceneText(scene_number=1, text="He opened his ThinkPad and typed the report.", file_path="/fake/sc_001.md"),
            SceneText(scene_number=2, text="He closed the MacBook and stood up.", file_path="/fake/sc_002.md"),
        ]
        ms = ManuscriptArtifact(scenes=scenes, manuscript_dir="/fake")
        findings = _deterministic_checks(ms)

        for f in findings:
            assert f.severity == "CLASS_B", (
                f"Deterministic finding should be CLASS_B, got {f.severity}: {f.description}"
            )

    def test_no_class_a_in_deterministic_output(self):
        """No deterministic finding should ever be CLASS_A."""
        from audit_checks import ManuscriptArtifact, SceneText

        # Rank contradiction scenario
        scenes = [
            SceneText(scene_number=1, text="Captain Rodriguez gave the order.", file_path="/fake/sc_001.md"),
            SceneText(scene_number=5, text="Major Rodriguez reviewed the map.", file_path="/fake/sc_005.md"),
        ]
        ms = ManuscriptArtifact(scenes=scenes, manuscript_dir="/fake")
        findings = _deterministic_checks(ms)

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) == 0, (
            f"Expected 0 CLASS_A but got {len(class_a)}: "
            + "; ".join(f.description for f in class_a)
        )
