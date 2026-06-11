"""Tests for synopsis_auditor — V25 scene parser + callable."""
from __future__ import annotations

import os
import pytest

from synopsis_auditor import parse_synopsis, Scene, audit_synopsis


# ═══════════════════════════════════════════════════════════════════════════
# V25 scene format parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestV25SceneFormat:
    def test_v25_scene_format_parses(self):
        text = "### Scene 1 \u2014 Opening Shot [TYPE: ACTION] [POV: Hank]\n\n- Beat one.\n"
        scenes = parse_synopsis(text)
        assert len(scenes) == 1
        assert scenes[0].number == 1
        assert scenes[0].title == "Opening Shot"
        assert scenes[0].scene_type == "ACTION"
        assert scenes[0].pov == "Hank"

    def test_v25_em_dash_accepted(self):
        text = "### Scene 5 \u2014 Title [TYPE: SUSPENSE] [POV: Lena]\n\nBody.\n"
        scenes = parse_synopsis(text)
        assert len(scenes) == 1
        assert scenes[0].number == 5

    def test_v25_en_dash_accepted(self):
        text = "### Scene 5 \u2013 Title [TYPE: SUSPENSE] [POV: Lena]\n\nBody.\n"
        scenes = parse_synopsis(text)
        assert len(scenes) == 1
        assert scenes[0].number == 5

    def test_v25_missing_type_defaults_unknown(self):
        text = "### Scene 3 \u2014 No Type [POV: Hank]\n\nBody.\n"
        scenes = parse_synopsis(text)
        assert len(scenes) == 1
        assert scenes[0].scene_type == "UNKNOWN"

    def test_v25_missing_pov_defaults_empty(self):
        text = "### Scene 3 \u2014 No POV [TYPE: ACTION]\n\nBody.\n"
        scenes = parse_synopsis(text)
        assert len(scenes) == 1
        assert scenes[0].pov == ""

    def test_v25_three_scenes_parsed(self):
        text = (
            "### Scene 1 \u2014 First [TYPE: ACTION] [POV: Hank]\n\n- Beat 1.\n\n"
            "### Scene 2 \u2014 Second [TYPE: NON-ACTION] [POV: Lena]\n\n- Beat 2.\n\n"
            "### Scene 3 \u2014 Third [TYPE: SUSPENSE] [POV: Hank]\n\n- Beat 3.\n"
        )
        scenes = parse_synopsis(text)
        assert len(scenes) == 3
        assert [s.number for s in scenes] == [1, 2, 3]
        assert scenes[0].scene_type == "ACTION"
        assert scenes[1].scene_type == "NON_ACTION"
        assert scenes[2].scene_type == "SUSPENSE"


# ═══════════════════════════════════════════════════════════════════════════
# V24 backward compatibility
# ═══════════════════════════════════════════════════════════════════════════

class TestV24Fallback:
    def test_v24_format_fallback_still_works(self):
        text = "## SCENE 1: The Opening\n\nScene body here.\n\n## SCENE 2: The Middle\n\nBody.\n"
        scenes = parse_synopsis(text)
        assert len(scenes) == 2
        assert scenes[0].number == 1
        assert scenes[0].title == "The Opening"
        # V24 format does not have TYPE/POV
        assert scenes[0].scene_type == "UNKNOWN"
        assert scenes[0].pov == ""

    def test_v24_and_v25_mixed_does_not_crash(self):
        """If both formats appear, V25 wins (first-try match)."""
        text = (
            "### Scene 1 \u2014 V25 Scene [TYPE: ACTION] [POV: Hank]\n\n- Beat.\n\n"
            "## SCENE 2: V24 Scene\n\nBody.\n"
        )
        # Should not crash; V25 regex matches first scene
        scenes = parse_synopsis(text)
        assert len(scenes) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# audit_synopsis callable
# ═══════════════════════════════════════════════════════════════════════════

class TestAuditSynopsisCallable:
    def test_audit_synopsis_callable_returns_dict(self, tmp_path):
        """audit_synopsis() returns a dict with required keys."""
        # Create minimal synopsis
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("### Scene 1 \u2014 Test [TYPE: ACTION] [POV: Hank]\n\n- Beat.\n")
        intake = tmp_path / "intake.json"
        intake.write_text('{"title": "Test", "total_scene_count": 1}')
        # This will likely fail (no series_config) but should return a dict, not crash
        result = audit_synopsis(
            synopsis_path=str(synopsis),
            intake_path=str(intake),
            series_dir=str(tmp_path),
            series_config_path=str(tmp_path / "nonexistent_config.json"),
        )
        assert isinstance(result, dict)
        assert "verdict" in result
        assert "fails" in result
        assert "weaks" in result
        assert "total_scenes" in result


# ═══════════════════════════════════════════════════════════════════════════
# Mandate calibration anchor
# ═══════════════════════════════════════════════════════════════════════════

class TestMandateCalibration:
    def test_mandate_synopsis_post_sg3_parses(self):
        """SG-3 output should parse to 100 scenes with TYPE populated."""
        path = "/anpd/v25/series/black_tide/b01/work/synopsis.md"
        if not os.path.exists(path):
            pytest.skip("Mandate synopsis not available")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        scenes = parse_synopsis(text)
        assert len(scenes) == 100
        # All scenes should have TYPE populated (V25 format)
        typed = [s for s in scenes if s.scene_type != "UNKNOWN"]
        assert len(typed) == 100
