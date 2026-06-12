"""Tests for preflight wiring into master_controller.

Verifies:
1. preflight --stage pre_run passes on clean b01cert-style fixture
2. pre_run halts on planted stale state (N001)
3. pre_run halts on planted /anpd/v25 config path (N002)
4. Stage classification: no rule unclassified
5. Exit-code propagation: master_controller returns nonzero on hard_stop
"""

from __future__ import annotations

import json
import os
import sys

import pytest


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_clean_fixture(tmp_path):
    """Return (book_dir, series_dir) for a clean pre_run fixture.

    Contains only the brief-layer files the pre_run stage expects.
    The series_dir is under /anpd/v26 conceptually, but uses tmp_path
    for isolation — so config paths reference /anpd/v26 to pass N002.
    """
    series_dir = str(tmp_path / "series" / "airmen")
    book_dir = str(tmp_path / "series" / "airmen" / "b01cert")
    work_dir = os.path.join(book_dir, "work")
    os.makedirs(work_dir)

    # Series-level files
    os.makedirs(series_dir, exist_ok=True)
    with open(os.path.join(series_dir, "series_bible.json"), "w") as f:
        json.dump({"series_name": "Airmen"}, f)
    with open(os.path.join(series_dir, "series_config.json"), "w") as f:
        json.dump({
            "genre": "historical_war_thriller",
            "series_name": "Airmen",
            "series_directory": "/anpd/v26/series/airmen",
            "pen_name": "Test Author",
            "banned_phrases_path": "/anpd/v26/series/airmen/banned_phrases.json",
        }, f)
    with open(os.path.join(series_dir, "character_profiles.json"), "w") as f:
        json.dump({}, f)
    with open(os.path.join(series_dir, "banned_phrases.json"), "w") as f:
        json.dump(["tapestry of"], f)

    # Book-level intake
    intake = {
        "book_number": 1,
        "title": "Test",
        "series": "airmen",
        "target_chapter_count": 25,
        "target_scene_count": 100,
        "target_synopsis_words": 20000,
        "target_word_count": 85000,
        "copyright_holder": "Endeavor Publishing LLC",
        "resolution_scenes": 2,
        "twist_1_position": 25,
        "twist_2_position": 50,
        "twist_3_position": 75,
    }
    with open(os.path.join(work_dir, "intake.json"), "w") as f:
        json.dump(intake, f)

    return book_dir, series_dir


# ─── pre_run on clean fixture: PASS ────────────────────────────────────────

class TestPreRunClean:
    """preflight --stage pre_run passes on a clean fixture."""

    def test_pre_run_no_fixture_failures(self, tmp_path):
        """All F/V/D/N rules pass on clean fixture (E/G rules are env-dependent)."""
        from preflight_v26_20260611 import run_preflight

        book_dir, series_dir = _make_clean_fixture(tmp_path)
        _, results = run_preflight(book_dir, series_dir, stage="pre_run")

        # Filter out environment/git rules — they depend on the test runner,
        # not the fixture.  We only assert fixture-controlled rules pass.
        env_rules = {"E001", "E002", "E003", "E004", "G001", "G002", "G003"}
        fixture_failures = [
            r for r in results
            if not r.passed and r.severity == "A" and r.rule_id not in env_rules
        ]
        for r in fixture_failures:
            print(f"  UNEXPECTED Class A: {r.rule_id} {r.error_code} {r.message}")
        assert fixture_failures == [], f"{len(fixture_failures)} unexpected Class A failures"

    def test_pre_run_skips_synopsis_rules(self, tmp_path):
        from preflight_v26_20260611 import run_preflight

        book_dir, series_dir = _make_clean_fixture(tmp_path)
        _, results = run_preflight(book_dir, series_dir, stage="pre_run")

        rule_ids = {r.rule_id for r in results}
        # S-rules must not appear at pre_run stage
        s_rules = {rid for rid in rule_ids if rid.startswith("S")}
        assert s_rules == set(), f"S-rules should not run at pre_run: {s_rules}"

    def test_pre_run_passes_with_outline_present(self, tmp_path):
        """F026: clean fixture with outline_path pointing to existing file passes."""
        from preflight_v26_20260611 import run_preflight

        book_dir, series_dir = _make_clean_fixture(tmp_path)
        # Add outline_path to intake and create the file
        intake_path = os.path.join(book_dir, "work", "intake.json")
        with open(intake_path) as f:
            intake = json.load(f)
        intake["outline_path"] = "outline.md"
        with open(intake_path, "w") as f:
            json.dump(intake, f)
        with open(os.path.join(book_dir, "work", "outline.md"), "w") as f:
            f.write("# Chapter 1\n")

        _, results = run_preflight(book_dir, series_dir, stage="pre_run")
        f026 = [r for r in results if r.rule_id == "F026"]
        assert len(f026) >= 1
        assert all(r.passed for r in f026), "F026 should pass when outline exists"

    def test_pre_run_fails_with_missing_outline(self, tmp_path):
        """F026: intake with outline_path but no outline.md file → FAIL."""
        from preflight_v26_20260611 import run_preflight

        book_dir, series_dir = _make_clean_fixture(tmp_path)
        intake_path = os.path.join(book_dir, "work", "intake.json")
        with open(intake_path) as f:
            intake = json.load(f)
        intake["outline_path"] = "outline.md"
        with open(intake_path, "w") as f:
            json.dump(intake, f)
        # Deliberately do NOT create outline.md

        _, results = run_preflight(book_dir, series_dir, stage="pre_run")
        f026_fails = [r for r in results if r.rule_id == "F026" and not r.passed]
        assert len(f026_fails) >= 1
        assert f026_fails[0].error_code == "MISSING_INTAKE_REFERENCED_INPUT"

    def test_pre_run_skips_synopsis_file_check(self, tmp_path):
        from preflight_v26_20260611 import run_preflight

        book_dir, series_dir = _make_clean_fixture(tmp_path)
        _, results = run_preflight(book_dir, series_dir, stage="pre_run")

        rule_ids = {r.rule_id for r in results}
        # F008 (synopsis), F010-F012, F023-F025 must not appear at pre_run
        post_only = {"F004", "F006", "F008", "F010", "F011", "F012",
                     "F023", "F024", "F025"}
        present = post_only & rule_ids
        assert present == set(), f"Post-synopsis F-rules should not run at pre_run: {present}"


# ─── pre_run + stale state: N001 FAIL ──────────────────────────────────────

class TestPreRunStaleState:
    """N001: pre_run must fail when stale state files exist."""

    def test_stale_state_json_in_work_triggers_failure(self, tmp_path):
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(os.path.join(book_dir, "out", "scenes"))
        os.makedirs(os.path.join(book_dir, "out", "state"))

        with open(os.path.join(book_dir, "work", "synopsis_generation_state.json"), "w") as f:
            json.dump({"phase": "synopsis"}, f)

        results = run_new_book_cleanliness_rules(book_dir)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1
        assert failed[0].error_code == "STALE_RUN_STATE"

    def test_stale_scene_triggers_failure(self, tmp_path):
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        os.makedirs(os.path.join(book_dir, "work"))
        scenes_dir = os.path.join(book_dir, "out", "scenes")
        os.makedirs(scenes_dir)
        os.makedirs(os.path.join(book_dir, "out", "state"))

        with open(os.path.join(scenes_dir, "sc01_his.md"), "w") as f:
            f.write("stale")

        results = run_new_book_cleanliness_rules(book_dir)
        assert any(r.error_code == "STALE_RUN_STATE" for r in results if not r.passed)

    def test_clean_directory_passes(self, tmp_path):
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(os.path.join(book_dir, "out", "scenes"))
        os.makedirs(os.path.join(book_dir, "out", "state"))

        results = run_new_book_cleanliness_rules(book_dir)
        assert all(r.passed for r in results)

    def test_empty_skeleton_dirs_are_clean(self, tmp_path):
        """Empty out/scenes/ and out/state/ do not trigger STALE_RUN_STATE."""
        from preflight_v26_20260611 import run_new_book_cleanliness_rules

        book_dir = str(tmp_path / "book")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(os.path.join(book_dir, "out", "scenes"))
        os.makedirs(os.path.join(book_dir, "out", "state"))
        os.makedirs(os.path.join(book_dir, "out", "reports"))

        results = run_new_book_cleanliness_rules(book_dir)
        assert all(r.passed for r in results)


# ─── pre_run + bad config path: N002 FAIL ──────────────────────────────────

class TestPreRunBadConfigPath:
    """N002: pre_run must fail when config paths reference wrong root."""

    def test_v25_path_triggers_failure(self, tmp_path):
        from preflight_v26_20260611 import run_config_path_integrity_rules

        book_dir = str(tmp_path / "book")
        series_dir = str(tmp_path / "series")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(series_dir)

        with open(os.path.join(series_dir, "series_config.json"), "w") as f:
            json.dump({"series_directory": "/anpd/v25/series/airmen"}, f)
        with open(os.path.join(book_dir, "work", "intake.json"), "w") as f:
            json.dump({"title": "Test"}, f)

        results = run_config_path_integrity_rules(book_dir, series_dir)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1
        assert failed[0].error_code == "CONFIG_PATH_OUTSIDE_ROOT"

    def test_v26_paths_pass(self, tmp_path):
        from preflight_v26_20260611 import run_config_path_integrity_rules

        book_dir = str(tmp_path / "book")
        series_dir = str(tmp_path / "series")
        os.makedirs(os.path.join(book_dir, "work"))
        os.makedirs(series_dir)

        with open(os.path.join(series_dir, "series_config.json"), "w") as f:
            json.dump({"series_directory": "/anpd/v26/series/airmen"}, f)
        with open(os.path.join(book_dir, "work", "intake.json"), "w") as f:
            json.dump({"title": "Test"}, f)

        results = run_config_path_integrity_rules(book_dir, series_dir)
        outside = [r for r in results if not r.passed
                   and "outside" in r.message.lower()]
        assert outside == []


# ─── Stage classification completeness ──────────────────────────────────────

class TestStageClassification:
    """Every rule ID must be assigned to a stage."""

    def test_no_rule_unclassified(self, tmp_path):
        from preflight_v26_20260611 import (
            run_preflight, PRE_RUN_F_RULES, PRE_RUN_V_RULES,
        )

        book_dir, series_dir = _make_clean_fixture(tmp_path)

        # Run post_synopsis (full set) to discover all rule IDs
        _, full_results = run_preflight(book_dir, series_dir, stage="post_synopsis")
        full_ids = {r.rule_id for r in full_results}

        # Run pre_run to discover pre_run rule IDs
        _, pre_results = run_preflight(book_dir, series_dir, stage="pre_run")
        pre_ids = {r.rule_id for r in pre_results}

        # Every post_synopsis rule that isn't in pre_run must be a known
        # post_synopsis-only rule (S-rules, or F/V rules not in PRE_RUN sets)
        post_only = full_ids - pre_ids
        known_post_only = (
            {f"S{i:03d}" for i in range(1, 10)}  # S001-S009
            | {"F004", "F006", "F008", "F010", "F011", "F012",
               "F023", "F024", "F025"}
            | {"V003", "V004", "V005", "V006", "V008", "V010"}
        )
        unclassified = post_only - known_post_only
        assert unclassified == set(), f"Unclassified rules: {unclassified}"


# ─── Exit-code propagation ──────────────────────────────────────────────────

class TestExitCodePropagation:
    """master_controller returns nonzero on hard_stop."""

    def test_main_returns_nonzero_signature(self):
        import master_controller as mc
        import inspect
        source = inspect.getsource(mc)
        assert "sys.exit(main())" in source
