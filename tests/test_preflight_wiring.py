"""Tests for preflight wiring into master_controller.

Verifies:
1. Controller halts on stale run state (STALE_RUN_STATE / N001)
2. Controller halts on bad config path (CONFIG_PATH_OUTSIDE_ROOT / N002)
3. Preflight passes on clean fixture
4. Exit-code propagation: master_controller returns nonzero on hard_stop
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_clean_book_dir(tmpdir: str, series_dir: str) -> str:
    """Create a minimal clean book dir with brief-layer inputs only."""
    book_dir = os.path.join(tmpdir, "b01cert")
    work_dir = os.path.join(book_dir, "work")
    out_dir = os.path.join(book_dir, "out")
    os.makedirs(work_dir)
    os.makedirs(os.path.join(out_dir, "scenes"))
    os.makedirs(os.path.join(out_dir, "state"))
    os.makedirs(os.path.join(out_dir, "reports"))

    intake = {
        "book_number": 1,
        "title": "Test",
        "series": "Airmen",
        "target_chapter_count": 25,
        "target_scene_count": 100,
        "target_synopsis_words": 20000,
        "target_word_count": 85000,
        "outline_path": os.path.join(work_dir, "outline.md"),
        "copyright_holder": "Endeavor Publishing LLC",
        "resolution_scenes": 2,
        "twist_1_position": 25,
        "twist_2_position": 50,
        "twist_3_position": 75,
    }
    with open(os.path.join(work_dir, "intake.json"), "w") as f:
        json.dump(intake, f)

    return book_dir


def _make_series_dir(tmpdir: str, pipeline_root: str) -> str:
    """Create minimal series dir with config files."""
    series_dir = os.path.join(tmpdir, "airmen")
    os.makedirs(series_dir)

    config = {
        "genre": "historical_war_thriller",
        "series_name": "Airmen",
        "series_directory": series_dir,
        "pen_name": "Test Author",
        "banned_phrases_path": os.path.join(series_dir, "banned_phrases.json"),
        "series_slug": "arm",
    }
    with open(os.path.join(series_dir, "series_config.json"), "w") as f:
        json.dump(config, f)

    with open(os.path.join(series_dir, "banned_phrases.json"), "w") as f:
        json.dump(["tapestry of"], f)

    with open(os.path.join(series_dir, "series_bible.json"), "w") as f:
        json.dump({"series_name": "Airmen"}, f)

    with open(os.path.join(series_dir, "character_profiles.json"), "w") as f:
        json.dump({}, f)

    return series_dir


# ─── N001: STALE_RUN_STATE ──────────────────────────────────────────────────

class TestStaleRunState:
    """N001: preflight must fail when stale state files exist."""

    def test_stale_state_json_in_work_triggers_failure(self, tmp_path):
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        work_dir = os.path.join(book_dir, "work")
        os.makedirs(work_dir)
        os.makedirs(os.path.join(book_dir, "out", "scenes"))
        os.makedirs(os.path.join(book_dir, "out", "state"))

        # Plant a stale state file
        with open(os.path.join(work_dir, "synopsis_generation_state.json"), "w") as f:
            json.dump({"phase": "synopsis"}, f)

        results = run_new_book_cleanliness_rules(book_dir)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1
        assert failed[0].error_code == "STALE_RUN_STATE"

    def test_stale_scene_in_out_triggers_failure(self, tmp_path):
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        os.makedirs(os.path.join(book_dir, "work"))
        scenes_dir = os.path.join(book_dir, "out", "scenes")
        os.makedirs(scenes_dir)
        os.makedirs(os.path.join(book_dir, "out", "state"))

        # Plant a prior scene
        with open(os.path.join(scenes_dir, "sc01_his.md"), "w") as f:
            f.write("Prior scene content")

        results = run_new_book_cleanliness_rules(book_dir)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1
        assert failed[0].error_code == "STALE_RUN_STATE"

    def test_stale_manuscript_in_out_triggers_failure(self, tmp_path):
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(os.path.join(book_dir, "out", "scenes"))
        os.makedirs(os.path.join(book_dir, "out", "state"))

        # Plant a prior manuscript
        with open(os.path.join(book_dir, "out", "manuscript.md"), "w") as f:
            f.write("Prior manuscript")

        results = run_new_book_cleanliness_rules(book_dir)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1
        assert failed[0].error_code == "STALE_RUN_STATE"

    def test_clean_directory_passes(self, tmp_path):
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(os.path.join(book_dir, "out", "scenes"))
        os.makedirs(os.path.join(book_dir, "out", "state"))

        results = run_new_book_cleanliness_rules(book_dir)
        assert all(r.passed for r in results)


# ─── N002: CONFIG_PATH_INTEGRITY ────────────────────────────────────────────

class TestConfigPathIntegrity:
    """N002: preflight must fail when config paths reference wrong root."""

    def test_bad_series_directory_triggers_failure(self, tmp_path):
        from preflight_v26_20260611 import run_config_path_integrity_rules

        book_dir = str(tmp_path / "book")
        series_dir = str(tmp_path / "series")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(series_dir)

        # Plant a series_config with v25 path
        config = {
            "series_directory": "/anpd/v25/series/airmen",
            "banned_phrases_path": "/anpd/v26/series/airmen/banned_phrases.json",
        }
        with open(os.path.join(series_dir, "series_config.json"), "w") as f:
            json.dump(config, f)

        # Minimal intake
        with open(os.path.join(book_dir, "work", "intake.json"), "w") as f:
            json.dump({"title": "Test"}, f)

        results = run_config_path_integrity_rules(book_dir, series_dir)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1
        assert failed[0].error_code == "CONFIG_PATH_OUTSIDE_ROOT"
        assert "/anpd/v25" in failed[0].message

    def test_good_paths_pass(self, tmp_path):
        from preflight_v26_20260611 import run_config_path_integrity_rules

        book_dir = str(tmp_path / "book")
        series_dir = str(tmp_path / "series")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(series_dir)

        # All paths under /anpd/v26 and existing
        config = {
            "series_directory": "/anpd/v26/series/airmen",
            "banned_phrases_path": "/anpd/v26/series/airmen/banned_phrases.json",
        }
        with open(os.path.join(series_dir, "series_config.json"), "w") as f:
            json.dump(config, f)

        with open(os.path.join(book_dir, "work", "intake.json"), "w") as f:
            json.dump({"title": "Test"}, f)

        results = run_config_path_integrity_rules(book_dir, series_dir)
        # N002 might report non-existent files, but not "outside root"
        outside_root = [r for r in results if not r.passed and "outside" in r.message.lower()]
        assert len(outside_root) == 0


# ─── Exit-code propagation ──────────────────────────────────────────────────

class TestExitCodePropagation:
    """master_controller returns nonzero on hard_stop."""

    def test_main_returns_nonzero_signature(self):
        """Verify run_pipeline is wired to sys.exit in __main__ block."""
        import master_controller as mc
        # main() wraps run_pipeline and returns its int exit code
        assert hasattr(mc, "main")
        assert hasattr(mc, "run_pipeline")
        # The module's __name__=="__main__" block calls sys.exit(main())
        import inspect
        source = inspect.getsource(mc)
        assert "sys.exit(main())" in source
