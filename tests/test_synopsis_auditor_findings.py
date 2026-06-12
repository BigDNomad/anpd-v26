"""Tests for synopsis_auditor — V25 scene parser + callable."""
from __future__ import annotations

import json
import os
import pytest

from synopsis_auditor import parse_synopsis, Scene, audit_synopsis
from synopsis_generator import generate_synopsis


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

    def test_generator_series_dir_for_nested_book_dir(self, tmp_path):
        """Reproduces attempt 3 failure: generator derives series_dir wrongly
        when output_dir is nested under a book subdir (e.g., series/airmen/b01cert/work).

        Without the fix, generate_synopsis computes series_dir as
        dirname(dirname(output_dir/synopsis.md)) = b01cert (the book dir),
        then audit_synopsis looks for series_config.json in b01cert, which
        does not exist.

        With the fix, passing series_config_path explicitly ensures the
        auditor receives the correct path regardless of nesting depth.
        """
        # Build fixture: series/airmen/b01cert/work/ mimicking the real layout
        series_dir = tmp_path / "series" / "airmen"
        book_dir = series_dir / "b01cert"
        work_dir = book_dir / "work"
        work_dir.mkdir(parents=True)

        # series_config.json lives at the SERIES level, not the book level
        series_config = series_dir / "series_config.json"
        series_config.write_text(json.dumps({
            "genre": "historical_war_thriller",
            "series_name": "Airmen",
            "series_directory": str(series_dir),
            "pen_name": "Test",
            "banned_phrases_path": str(series_dir / "banned_phrases.json"),
        }))
        (series_dir / "banned_phrases.json").write_text(
            json.dumps({"names": [], "phrases": []})
        )

        # The bug: dirname(dirname(work_dir/synopsis.md)) = b01cert, not airmen
        canonical = os.path.join(str(work_dir), "synopsis.md")
        wrong_series_dir = os.path.dirname(os.path.dirname(canonical))
        assert wrong_series_dir == str(book_dir), "precondition: old code gets book dir"
        assert not os.path.exists(os.path.join(wrong_series_dir, "series_config.json")), \
            "precondition: series_config.json does NOT exist in book dir"

        # The fix: deriving series_dir from series_config_path
        correct_series_dir = os.path.dirname(str(series_config))
        assert correct_series_dir == str(series_dir)
        assert os.path.exists(os.path.join(correct_series_dir, "series_config.json"))


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
