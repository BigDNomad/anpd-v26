"""Tests for the scene-loop contiguity gate.

The contiguity gate halts with Class A if scene numbers from the
synopsis are not contiguous 1..N.  This test uses a mock synopsis
with gaps to verify the halt fires.
"""

import os
import sys
import json
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "pipeline"))


def _write_synopsis_with_gap(path: str) -> None:
    """Write a synopsis with scenes 1, 2, 4 (gap at 3)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "## Chapter 1\n\n"
            "### Scene 1 — Start [TYPE: ACTION]\n\nBody.\n\n"
            "### Scene 2 — Middle [TYPE: ACTION]\n\nBody.\n\n"
            "### Scene 4 — End [TYPE: ACTION]\n\nBody.\n"
        )


def _write_synopsis_contiguous(path: str) -> None:
    """Write a synopsis with scenes 1, 2, 3 (no gaps)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "## Chapter 1\n\n"
            "### Scene 1 — Start [TYPE: ACTION]\n\nBody.\n\n"
            "### Scene 2 — Middle [TYPE: ACTION]\n\nBody.\n\n"
            "### Scene 3 — End [TYPE: ACTION]\n\nBody.\n"
        )


def _make_args(book_dir, series_dir, synopsis_path):
    """Create a minimal args namespace for handle_scene_loop."""
    import argparse
    args = argparse.Namespace()
    args.book_dir = book_dir
    args.series_dir = series_dir
    args.start_scene = None
    args.end_scene = None
    args.force = False
    args.max_retries_per_scene = 0
    args.intake = os.path.join(book_dir, "work", "intake.json")
    args.series_config = os.path.join(series_dir, "series_config.json")
    return args


def test_contiguity_gate_halts_on_gap():
    """Scene loop must halt when synopsis has non-contiguous scene numbers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        book_dir = os.path.join(tmpdir, "b01")
        series_dir = os.path.join(tmpdir, "series")
        work_dir = os.path.join(book_dir, "work")
        os.makedirs(work_dir)
        os.makedirs(series_dir)
        os.makedirs(os.path.join(book_dir, "out", "scenes"))
        os.makedirs(os.path.join(book_dir, "out", "state"))
        os.makedirs(os.path.join(book_dir, "out", "chapters"))

        synopsis_path = os.path.join(work_dir, "synopsis.md")
        _write_synopsis_with_gap(synopsis_path)

        # Write minimal intake + series_config
        with open(os.path.join(work_dir, "intake.json"), "w") as fh:
            json.dump({"book_number": 1}, fh)
        with open(os.path.join(series_dir, "series_config.json"), "w") as fh:
            json.dump({}, fh)
        with open(os.path.join(series_dir, "character_profiles.json"), "w") as fh:
            json.dump({}, fh)

        args = _make_args(book_dir, series_dir, synopsis_path)
        pipeline_state = {
            "scenes_generated": 0,
            "class_a_failures": 0,
            "components_called": {},
            "invocation_timeline": [],
        }
        effective_config = {}

        import phase_handlers
        result = phase_handlers.handle_scene_loop(
            args, pipeline_state, effective_config,
            synopsis_path, None,
        )

        assert result["verdict"] == "halt", f"Expected halt, got {result['verdict']}"
        assert any("missing scenes [3]" in f.get("message", "")
                    for f in result.get("findings", [])), (
            f"Expected finding about missing scene 3, got: {result.get('findings')}"
        )


def test_contiguity_gate_passes_when_contiguous():
    """Scene loop must NOT halt when synopsis has contiguous scene numbers.

    Pre-creates scene files so the loop auto-skips them (avoids invoking
    scene_writer, which would block on LLM calls).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        book_dir = os.path.join(tmpdir, "b01")
        series_dir = os.path.join(tmpdir, "series")
        work_dir = os.path.join(book_dir, "work")
        scenes_dir = os.path.join(book_dir, "out", "scenes")
        os.makedirs(work_dir)
        os.makedirs(series_dir)
        os.makedirs(scenes_dir)
        os.makedirs(os.path.join(book_dir, "out", "state"))
        os.makedirs(os.path.join(book_dir, "out", "chapters"))

        synopsis_path = os.path.join(work_dir, "synopsis.md")
        _write_synopsis_contiguous(synopsis_path)

        # Pre-create scene files so auto-skip kicks in
        for sc_num, title in [(1, "start"), (2, "middle"), (3, "end")]:
            with open(os.path.join(scenes_dir, f"sc{sc_num:02d}_{title}.md"), "w") as fh:
                fh.write(f"Scene {sc_num} prose.")

        with open(os.path.join(work_dir, "intake.json"), "w") as fh:
            json.dump({"book_number": 1}, fh)
        with open(os.path.join(series_dir, "series_config.json"), "w") as fh:
            json.dump({}, fh)
        with open(os.path.join(series_dir, "character_profiles.json"), "w") as fh:
            json.dump({}, fh)

        args = _make_args(book_dir, series_dir, synopsis_path)
        # Use --start-scene/--end-scene to prevent archive-and-purge
        args.start_scene = 1
        args.end_scene = 3
        pipeline_state = {
            "scenes_generated": 0,
            "class_a_failures": 0,
            "components_called": {},
            "invocation_timeline": [],
        }
        effective_config = {}

        import phase_handlers
        result = phase_handlers.handle_scene_loop(
            args, pipeline_state, effective_config,
            synopsis_path, None,
        )

        # All scenes were pre-created and auto-skipped → should pass
        assert result["verdict"] == "pass", (
            f"Expected pass (all scenes pre-created), got: {result}"
        )
        assert result.get("scenes_skipped_existing") == [1, 2, 3]
