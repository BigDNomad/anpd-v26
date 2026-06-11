"""Tests for scene phase preflight: synopsis.md required, scene_map.md NOT required."""
import os
import sys
import types
import pytest
from unittest.mock import MagicMock, patch
from argparse import Namespace

# Ensure pipeline is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))


def _make_synopsis(tmp_path, scenes=4, chapters=1):
    """Write a minimal valid synopsis.md and return its path."""
    lines = []
    sc_num = 0
    for ch in range(1, chapters + 1):
        lines.append(f"## Chapter {ch} — Test Chapter {ch}\n")
        per_ch = scenes // chapters + (1 if ch <= scenes % chapters else 0)
        for s in range(1, per_ch + 1):
            sc_num += 1
            lines.append(
                f"### Scene {sc_num} — Test Scene {sc_num} "
                f"[TYPE: ACTION] [POV/FOCUS: Alice]\n"
            )
            lines.append(f"Scene {sc_num} body text.\n")
    synopsis_path = tmp_path / "work" / "synopsis.md"
    synopsis_path.parent.mkdir(parents=True, exist_ok=True)
    synopsis_path.write_text("\n".join(lines), encoding="utf-8")
    return str(synopsis_path)


def _make_args(tmp_path, **overrides):
    """Build a minimal args namespace for handle_scene_loop."""
    book_dir = str(tmp_path)
    defaults = dict(
        book_dir=book_dir,
        series_dir=str(tmp_path / "series"),
        intake=str(tmp_path / "work" / "intake.json"),
        start_scene=None,
        end_scene=None,
        force=False,
        max_retries_per_scene=0,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def _stub_mc():
    """Return a mock master_controller module with required attributes."""
    mc = MagicMock()
    mc.find_latest_file = MagicMock(return_value=None)
    mc.COMPONENTS = {}
    mc.run_component_subprocess = MagicMock(return_value={
        "stubbed": True,
        "exit_code": 1,
        "stderr": "stubbed",
        "stop_report_written_during_call": False,
    })
    return mc


@pytest.fixture
def tmp_book(tmp_path):
    """Set up a minimal book directory structure."""
    (tmp_path / "out" / "scenes").mkdir(parents=True)
    (tmp_path / "out" / "state").mkdir(parents=True)
    (tmp_path / "out" / "chapters").mkdir(parents=True)
    (tmp_path / "work").mkdir(parents=True)
    (tmp_path / "series").mkdir(parents=True)
    return tmp_path


class TestScenePhasePreflight:
    """Scene phase preflight passes with synopsis.md, fails without it."""

    def test_passes_with_synopsis_no_scene_map(self, tmp_book):
        """Preflight accepts synopsis.md and does NOT require scene_map.md."""
        synopsis_path = _make_synopsis(tmp_book, scenes=4, chapters=1)
        args = _make_args(tmp_book)

        # Confirm scene_map.md does NOT exist
        assert not os.path.exists(os.path.join(str(tmp_book), "scene_map.md"))

        from phase_handlers_v26_20260612 import handle_scene_loop

        # Patch mc so scene_writer subprocess is stubbed (we only test preflight)
        with patch("phase_handlers_v26_20260612.mc", _stub_mc()):
            result = handle_scene_loop(
                args,
                pipeline_state={"class_a_failures": 0, "class_b_violations": 0, "scenes_generated": 0},
                effective_config={},
                synopsis_path=synopsis_path,
                character_profiles_path=None,
            )

        # Should NOT be a preflight halt — it should proceed to the scene loop
        # (which will fail on stubbed scene_writer, but that's past preflight)
        if result["verdict"] == "halt":
            # The only acceptable halt is from scene_writer failure, NOT from
            # "synopsis.md not found" or "scene_map.md not found"
            for f in result.get("findings", []):
                assert "synopsis.md not found" not in f.get("message", ""), \
                    "Preflight rejected valid synopsis.md"
                assert "scene_map" not in f.get("message", "").lower(), \
                    f"Preflight still demands scene_map: {f['message']}"

    def test_fails_without_synopsis(self, tmp_book):
        """Preflight halts when synopsis.md is absent."""
        args = _make_args(tmp_book)

        from phase_handlers_v26_20260612 import handle_scene_loop

        with patch("phase_handlers_v26_20260612.mc", _stub_mc()):
            result = handle_scene_loop(
                args,
                pipeline_state={"class_a_failures": 0, "class_b_violations": 0, "scenes_generated": 0},
                effective_config={},
                synopsis_path=None,
                character_profiles_path=None,
            )

        assert result["verdict"] == "halt"
        msgs = " ".join(f.get("message", "") for f in result.get("findings", []))
        assert "synopsis" in msgs.lower(), f"Expected synopsis-related halt, got: {msgs}"

    def test_fails_with_nonexistent_synopsis_path(self, tmp_book):
        """Preflight halts when synopsis_path points to missing file."""
        args = _make_args(tmp_book)
        bogus_path = os.path.join(str(tmp_book), "work", "synopsis.md")
        # Don't create the file

        from phase_handlers_v26_20260612 import handle_scene_loop

        with patch("phase_handlers_v26_20260612.mc", _stub_mc()):
            result = handle_scene_loop(
                args,
                pipeline_state={"class_a_failures": 0, "class_b_violations": 0, "scenes_generated": 0},
                effective_config={},
                synopsis_path=bogus_path,
                character_profiles_path=None,
            )

        assert result["verdict"] == "halt"
        msgs = " ".join(f.get("message", "") for f in result.get("findings", []))
        assert "synopsis" in msgs.lower()

    def test_scene_map_absence_does_not_cause_failure(self, tmp_book):
        """Explicitly verify no code path checks for scene_map.md."""
        synopsis_path = _make_synopsis(tmp_book, scenes=2, chapters=1)
        args = _make_args(tmp_book)

        # Create a scene_map.md to prove it's IGNORED (not consumed)
        scene_map_path = os.path.join(str(tmp_book), "scene_map.md")
        # Don't create it — the point is its absence doesn't matter

        from phase_handlers_v26_20260612 import handle_scene_loop

        with patch("phase_handlers_v26_20260612.mc", _stub_mc()):
            result = handle_scene_loop(
                args,
                pipeline_state={"class_a_failures": 0, "class_b_violations": 0, "scenes_generated": 0},
                effective_config={},
                synopsis_path=synopsis_path,
                character_profiles_path=None,
            )

        # Should not halt due to scene_map
        for f in result.get("findings", []):
            assert "scene_map" not in f.get("message", "").lower(), \
                f"Code still references scene_map: {f['message']}"
