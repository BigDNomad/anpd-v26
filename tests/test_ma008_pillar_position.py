"""
Tests for MA-008 pillar_position_verification.

Covers:
  - Action opening (pillar 1): pass, fail, briefing-opening anti-pattern
  - Final battle (pillar 3): present, absent, rushed ending
  - Synopsis missing fallback
  - Module auto-discovery
  - Mandate calibration
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
    MA008_BRIEFING_OPENING_WINDOW,
    MA008_RUSHED_ENDING_WINDOW,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

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


# ─── Sub-check A: Action Opening (Pillar 1) ──────────────────────────────────

class TestActionOpening:

    def test_action_opening_passes(self):
        """Scene 1 is ACTION -> no Pillar 1 finding."""
        type_map = {i: "ACTION" if i <= 2 else "NON-ACTION" for i in range(1, 11)}
        findings = _run_check(10, type_map)
        pillar1 = [f for f in findings if "Pillar 1" in f.description]
        assert len(pillar1) == 0

    def test_non_action_opening_fails(self):
        """Scene 1 is NON-ACTION -> CLASS_A."""
        type_map = {1: "NON-ACTION", 2: "ACTION", 3: "ACTION"}
        type_map.update({i: "NON-ACTION" for i in range(4, 11)})
        findings = _run_check(10, type_map)
        pillar1 = [f for f in findings if "Pillar 1" in f.description and "scene 1 TYPE" in f.description]
        assert len(pillar1) >= 1
        assert pillar1[0].severity == "CLASS_A"

    def test_briefing_opening_first_3_all_non_action_fails(self):
        """Scenes 1-3 all NON-ACTION -> CLASS_A briefing-opening."""
        type_map = {i: "NON-ACTION" for i in range(1, 11)}
        findings = _run_check(10, type_map)
        briefing = [f for f in findings if "briefing-opening" in f.description]
        assert len(briefing) >= 1
        assert briefing[0].severity == "CLASS_A"

    def test_mixed_opening_does_not_trigger_briefing(self):
        """Scene 1 ACTION, scenes 2-3 NON-ACTION -> no briefing finding."""
        type_map = {1: "ACTION", 2: "NON-ACTION", 3: "NON-ACTION"}
        type_map.update({i: "NON-ACTION" for i in range(4, 11)})
        findings = _run_check(10, type_map)
        briefing = [f for f in findings if "briefing-opening" in f.description]
        assert len(briefing) == 0


# ─── Sub-check B: Final Battle (Pillar 3) ────────────────────────────────────

class TestFinalBattle:

    def test_final_battle_present_passes(self):
        """10-scene manuscript with scene 10 ACTION -> no Pillar 3 finding."""
        type_map = {i: "NON-ACTION" for i in range(1, 11)}
        type_map[1] = "ACTION"  # pass pillar 1
        type_map[10] = "ACTION"  # final battle
        findings = _run_check(10, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description]
        assert len(pillar3) == 0

    def test_final_battle_absent_fails(self):
        """10-scene manuscript, scene 10 NON-ACTION, no ACTION in final 10% -> CLASS_A."""
        type_map = {i: "ACTION" for i in range(1, 10)}
        type_map[10] = "NON-ACTION"
        findings = _run_check(10, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description and "no ACTION" in f.description]
        assert len(pillar3) >= 1

    def test_no_action_in_last_10pct_fails(self):
        """100-scene manuscript, scenes 91-100 all NON-ACTION -> CLASS_A."""
        type_map = {i: "ACTION" for i in range(1, 91)}
        type_map.update({i: "NON-ACTION" for i in range(91, 101)})
        findings = _run_check(100, type_map)
        pillar3 = [f for f in findings if "Pillar 3" in f.description and "no ACTION" in f.description]
        assert len(pillar3) >= 1
        assert pillar3[0].severity == "CLASS_A"

    def test_rushed_ending_last_5_no_action_or_mixed_fails(self):
        """20-scene manuscript, scenes 16-20 all NON-ACTION -> CLASS_A rushed ending."""
        type_map = {i: "ACTION" for i in range(1, 16)}
        type_map.update({i: "NON-ACTION" for i in range(16, 21)})
        findings = _run_check(20, type_map)
        rushed = [f for f in findings if "rushed/quiet ending" in f.description]
        assert len(rushed) >= 1
        assert rushed[0].severity == "CLASS_A"


# ─── Synopsis Missing ────────────────────────────────────────────────────────

class TestSynopsisMissing:

    def test_synopsis_missing_returns_empty(self):
        """No synopsis available -> empty findings, no crash."""
        findings = _run_check(10, {})  # empty map = no synopsis
        assert findings == []


# ─── Module Interface ────────────────────────────────────────────────────────

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


# ─── Mandate Calibration ─────────────────────────────────────────────────────

class TestMandateCalibration:

    @pytest.mark.xfail(reason="pinned to pre-25-chapter Mandate outline; awaiting operator re-authored outline (Decision 2 / Path A)", strict=False)
    def test_mandate_calibration(self):
        """Frozen Mandate baseline -> document pillar outcomes.

        Mandate scene 1 is [TYPE: ACTION] (Maduro extraction) -> Pillar 1 passes.
        Mandate final scenes include ACTION (scenes 99, 100) -> Pillar 3 passes.
        Expected: 0 findings.
        """
        from manuscript_auditor_v25 import load_manuscript, load_briefs

        cal_dir = "/anpd/v25/_calibration/mandate_v1_uncleaned_20260515/"
        if not os.path.isdir(cal_dir):
            pytest.skip("Calibration baseline not available")

        manuscript = load_manuscript(cal_dir)
        briefs = load_briefs(
            series_bible_path="/anpd/v25/series/black_tide/series_bible.json",
            character_profiles_path="/anpd/v25/series/black_tide/character_profiles.json",
        )

        check = PillarPositionVerification()
        findings = check.run(manuscript, briefs)

        # Document outcome — both pillars should pass on Mandate
        pillar1 = [f for f in findings if "Pillar 1" in f.description]
        pillar3 = [f for f in findings if "Pillar 3" in f.description]

        assert len(pillar1) == 0, f"Pillar 1 should pass (scene 1 is ACTION): {pillar1}"
        assert len(pillar3) == 0, f"Pillar 3 should pass (final scenes include ACTION): {pillar3}"
