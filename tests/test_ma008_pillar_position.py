"""
Tests for MA-008 pillar_position_verification (recalibrated 2026-06-13).

Covers:
  - Action opening (pillar 1): pass with ACTION/MIXED in 1-5, fail if none
  - Briefing-opening advisory (CLASS_B): first 3 all NON-ACTION
  - Final battle position (pillar 3): last ACTION/MIXED >= 85%, fail if < 85%
  - Synopsis missing fallback
  - Module auto-discovery
  - Calibration anchor: airmen b01 type map must PASS both pillars
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    REGISTRY,
    discover_and_register,
)
from audit_checks.pillar_position_verification import (
    PillarPositionVerification,
    MA008_OPENING_WINDOW,
    MA008_BRIEFING_WINDOW,
    MA008_FINAL_POSITION_PCT,
)


# --- Helpers ----------------------------------------------------------------

def _make_manuscript(n_scenes):
    """Create a manuscript with n_scenes of dummy text."""
    return ManuscriptArtifact(
        scenes=[
            SceneText(scene_number=i, text=f"Scene {i} text.", file_path=f"/fake/sc_{i:03d}.md")
            for i in range(1, n_scenes + 1)
        ],
        manuscript_dir="/fake",
    )


def _make_briefs():
    return BriefBundle(
        series_bible={},
        character_profiles={"characters": []},
    )


def _run_check(n_scenes, scene_type_map):
    """Run the check with a given scene_type_map, patching the loader."""
    ms = _make_manuscript(n_scenes)
    briefs = _make_briefs()
    check = PillarPositionVerification()
    with patch("audit_checks.pillar_position_verification.load_scene_type_map",
               return_value=scene_type_map):
        return check.run(ms, briefs)


# --- Sub-check A: Action Opening (Pillar 1) --------------------------------

class TestActionOpening:

    def test_action_in_scene_1_passes(self):
        """Scene 1 is ACTION -> Pillar 1 passes."""
        type_map = {i: "ACTION" if i == 1 else "NON-ACTION" for i in range(1, 21)}
        type_map[20] = "ACTION"  # pass pillar 3
        findings = _run_check(20, type_map)
        pillar1_a = [f for f in findings if "Pillar 1 violation" in f.description]
        assert len(pillar1_a) == 0

    def test_action_at_scene_5_passes(self):
        """Scenes 1-4 NON-ACTION, scene 5 ACTION -> Pillar 1 passes."""
        type_map = {i: "NON-ACTION" for i in range(1, 21)}
        type_map[5] = "ACTION"
        type_map[20] = "ACTION"  # pass pillar 3
        findings = _run_check(20, type_map)
        pillar1_a = [f for f in findings if "Pillar 1 violation" in f.description]
        assert len(pillar1_a) == 0

    def test_mixed_at_scene_3_passes(self):
        """Scenes 1-2 NON-ACTION, scene 3 MIXED -> Pillar 1 passes."""
        type_map = {i: "NON-ACTION" for i in range(1, 21)}
        type_map[3] = "MIXED"
        type_map[20] = "ACTION"  # pass pillar 3
        findings = _run_check(20, type_map)
        pillar1_a = [f for f in findings if "Pillar 1 violation" in f.description]
        assert len(pillar1_a) == 0

    def test_no_action_in_first_5_fails(self):
        """Scenes 1-5 all NON-ACTION/SUSPENSE -> CLASS_A."""
        type_map = {i: "NON-ACTION" for i in range(1, 21)}
        type_map[4] = "SUSPENSE"
        type_map[6] = "ACTION"
        type_map[20] = "ACTION"  # pass pillar 3
        findings = _run_check(20, type_map)
        pillar1_a = [f for f in findings if "Pillar 1 violation" in f.description]
        assert len(pillar1_a) == 1
        assert pillar1_a[0].severity == "CLASS_A"

    def test_briefing_opening_advisory_class_b(self):
        """First 3 scenes all NON-ACTION -> CLASS_B advisory (not CLASS_A)."""
        type_map = {i: "NON-ACTION" for i in range(1, 21)}
        type_map[5] = "ACTION"  # pass pillar 1
        type_map[20] = "ACTION"  # pass pillar 3
        findings = _run_check(20, type_map)
        briefing = [f for f in findings if "advisory" in f.description and "briefing" in f.description]
        assert len(briefing) == 1
        assert briefing[0].severity == "CLASS_B"

    def test_no_briefing_advisory_when_scene_2_action(self):
        """Scene 2 is ACTION -> no briefing advisory."""
        type_map = {i: "NON-ACTION" for i in range(1, 21)}
        type_map[2] = "ACTION"
        type_map[20] = "ACTION"  # pass pillar 3
        findings = _run_check(20, type_map)
        briefing = [f for f in findings if "briefing" in f.description]
        assert len(briefing) == 0


# --- Sub-check B: Final Battle Position (Pillar 3) -------------------------

class TestFinalBattle:

    def test_last_action_at_95pct_passes(self):
        """100-scene manuscript, last ACTION at scene 95 (95%) -> passes."""
        type_map = {i: "NON-ACTION" for i in range(1, 101)}
        type_map[1] = "ACTION"  # pass pillar 1
        type_map[95] = "ACTION"  # 95% position
        findings = _run_check(100, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description]
        assert len(pillar3) == 0

    def test_last_action_at_85pct_passes(self):
        """100-scene manuscript, last ACTION at scene 85 (85%) -> passes (boundary)."""
        type_map = {i: "NON-ACTION" for i in range(1, 101)}
        type_map[1] = "ACTION"  # pass pillar 1
        type_map[85] = "ACTION"  # exactly 85%
        findings = _run_check(100, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description]
        assert len(pillar3) == 0

    def test_last_action_at_84pct_fails(self):
        """100-scene manuscript, last ACTION at scene 84 (84%) -> CLASS_A."""
        type_map = {i: "NON-ACTION" for i in range(1, 101)}
        type_map[1] = "ACTION"  # pass pillar 1
        type_map[84] = "ACTION"  # 84% < 85%
        findings = _run_check(100, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description]
        assert len(pillar3) == 1
        assert pillar3[0].severity == "CLASS_A"

    def test_last_mixed_at_90pct_passes(self):
        """MIXED scene at 90% also satisfies Pillar 3."""
        type_map = {i: "NON-ACTION" for i in range(1, 101)}
        type_map[1] = "ACTION"  # pass pillar 1
        type_map[90] = "MIXED"  # 90% position
        findings = _run_check(100, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description]
        assert len(pillar3) == 0

    def test_no_action_anywhere_fails(self):
        """No ACTION or MIXED in entire manuscript -> CLASS_A."""
        type_map = {i: "NON-ACTION" for i in range(1, 21)}
        findings = _run_check(20, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description]
        assert len(pillar3) == 1
        assert pillar3[0].severity == "CLASS_A"

    def test_20_scene_last_action_at_17_passes(self):
        """20-scene manuscript, last ACTION at scene 17 (85%) -> passes."""
        type_map = {i: "NON-ACTION" for i in range(1, 21)}
        type_map[1] = "ACTION"
        type_map[17] = "ACTION"  # 17/20 = 85%
        findings = _run_check(20, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description]
        assert len(pillar3) == 0


# --- Synopsis Missing -------------------------------------------------------

class TestSynopsisMissing:

    def test_synopsis_missing_returns_empty(self):
        """No synopsis available -> empty findings, no crash."""
        findings = _run_check(10, {})  # empty map = no synopsis
        assert findings == []


# --- Module Interface -------------------------------------------------------

class TestModuleInterface:

    def test_has_required_interface(self):
        check = PillarPositionVerification()
        assert check.check_id == "MA-008-pillar-position-verification"
        assert check.severity == "CLASS_A"
        assert hasattr(check, "run")

    def test_module_auto_discovered(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-008-pillar-position-verification" in check_ids
        REGISTRY.clear()


# --- Calibration Anchor: Airmen B01 -----------------------------------------

class TestAirmenCalibrationAnchor:
    """Calibration anchor: airmen b01 actual TYPE map must PASS both pillars.

    This book has:
      - First ACTION scene at scene 5 (within 1-5 window) -> Pillar 1 PASS
      - First 3 scenes all NON-ACTION -> CLASS_B advisory (acceptable)
      - Last ACTION scene at scene 95 (95/100 = 95% >= 85%) -> Pillar 3 PASS

    Expected: 0 CLASS_A findings, 1 CLASS_B advisory (briefing-opening).
    """

    def test_airmen_b01_calibration(self):
        """Airmen b01 type map produces 0 CLASS_A, 1 CLASS_B."""
        synopsis_path = "/anpd/v26/series/airmen/b01/work/synopsis.md"
        if not os.path.isfile(synopsis_path):
            pytest.skip("Airmen b01 synopsis not available")

        from audit_checks._lib.synopsis_scene_types import load_scene_type_map
        type_map = load_scene_type_map(synopsis_path)

        assert len(type_map) == 100, f"Expected 100 scenes, got {len(type_map)}"

        # Verify anchor assumptions
        assert type_map[5] == "ACTION", "Scene 5 should be ACTION"
        assert type_map[95] == "ACTION", "Scene 95 should be ACTION"
        assert all(type_map[i] == "NON-ACTION" for i in range(1, 4)), \
            "Scenes 1-3 should all be NON-ACTION"

        findings = _run_check(100, type_map)

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        class_b = [f for f in findings if f.severity == "CLASS_B"]

        assert len(class_a) == 0, (
            f"Calibration anchor expects 0 CLASS_A but got {len(class_a)}: "
            + "; ".join(f.description for f in class_a)
        )
        assert len(class_b) == 1, (
            f"Calibration anchor expects 1 CLASS_B advisory but got {len(class_b)}"
        )
        assert "briefing" in class_b[0].description
