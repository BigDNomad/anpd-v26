"""
Tests for S-3 gate enforcement: Class A failures block manuscript ship.

Six tests per spec §5.4:
1. Assembler writes BLOCKED filename when class_a_failures > 0
2. Assembler writes canonical filename when class_a_failures == 0
3. failure_report.json well-formed
4. Retry feedback differs by attempt
5. Deterministic checks run with --skip-llm-audit
6. LLM checks do not run with --skip-llm-audit
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manuscript_assembler import assemble_manuscript
from manuscript_orchestrator import (
    _build_retry_feedback,
    _build_failure_report,
    SceneResult,
)
from scene_auditor import (
    audit_scene,
    Finding,
)
from synopsis_parser import SceneEntry, ChapterEntry, SynopsisStructure


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_synopsis(n_chapters=2, scenes_per_chapter=2):
    """Build a minimal SynopsisStructure."""
    chapters = []
    for ch in range(1, n_chapters + 1):
        scenes = []
        for sc in range(1, scenes_per_chapter + 1):
            scenes.append(SceneEntry(
                chapter_number=ch,
                scene_number=sc,
                title=f"Scene {sc}",
                scene_type="MIXED",
                pov="Archer",
                body=f"Synopsis body for ch{ch} sc{sc}.",
                position_in_chapter=sc,
            ))
        chapters.append(ChapterEntry(chapter_number=ch, title=f"Chapter {ch}", scenes=scenes))
    return SynopsisStructure(chapters=chapters)


def _make_scene_results(synopsis, prose="Test prose. " * 100):
    """Build scene_results dict matching the synopsis."""
    results = {}
    for ch in synopsis.chapters:
        for sc in ch.scenes:
            results[(ch.chapter_number, sc.scene_number)] = prose
    return results


# ── Test 1: Assembler writes BLOCKED filename ────────────────────────────

class TestAssemblerBlocked:

    def test_blocked_filename_when_class_a_failures(self):
        """class_a_failures > 0 produces manuscript_BLOCKED.md, not act1_full.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            synopsis = _make_synopsis()
            scene_results = _make_scene_results(synopsis)

            paths = assemble_manuscript(scene_results, tmpdir, synopsis,
                                         class_a_failures=1)

            assert os.path.exists(os.path.join(tmpdir, "manuscript_BLOCKED.md"))
            assert not os.path.exists(os.path.join(tmpdir, "act1_full.md"))
            assert paths["blocked"] is True


# ── Test 2: Assembler writes canonical filename ──────────────────────────

class TestAssemblerCanonical:

    def test_canonical_filename_when_no_failures(self):
        """class_a_failures == 0 produces act1_full.md, not manuscript_BLOCKED.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            synopsis = _make_synopsis()
            scene_results = _make_scene_results(synopsis)

            paths = assemble_manuscript(scene_results, tmpdir, synopsis,
                                         class_a_failures=0)

            assert os.path.exists(os.path.join(tmpdir, "act1_full.md"))
            assert not os.path.exists(os.path.join(tmpdir, "manuscript_BLOCKED.md"))
            assert paths["blocked"] is False


# ── Test 3: failure_report.json well-formed ──────────────────────────────

class TestFailureReport:

    def test_failure_report_well_formed(self):
        """failure_report.json has blocked: true, contains failing scene, has retry_history."""
        scene_results = {
            (1, 1): SceneResult(
                chapter=1, scene=1, title="Scene 1", scene_type="MIXED",
                pov="Archer", word_count=850, attempts=1, passed=True,
                findings_summary=[],
            ),
            (1, 2): SceneResult(
                chapter=1, scene=2, title="Scene 2 Problem", scene_type="MIXED",
                pov="Archer", word_count=1764, attempts=3, passed=False,
                findings_summary=["CLASS_A: Word count 1764 above maximum 1100"],
            ),
        }

        retry_history = {
            (1, 1): [{"attempt": 1, "word_count": 850, "outcome": "PASS", "trips": [], "elapsed_seconds": 5.0}],
            (1, 2): [
                {"attempt": 1, "word_count": 1764, "outcome": "FAIL", "trips": ["word_count"], "elapsed_seconds": 5.0},
                {"attempt": 2, "word_count": 1700, "outcome": "FAIL", "trips": ["word_count"], "elapsed_seconds": 5.0},
                {"attempt": 3, "word_count": 1650, "outcome": "FAIL", "trips": ["word_count"], "elapsed_seconds": 5.0},
            ],
        }

        report = _build_failure_report(scene_results, retry_history, run_id="test_run")

        assert report["blocked"] is True
        assert report["class_a_failures"] == 1
        assert len(report["failing_scenes"]) == 1

        failing = report["failing_scenes"][0]
        assert failing["chapter"] == 1
        assert failing["scene"] == 2
        assert failing["attempts"] == 3
        assert len(failing["retry_history"]) == 3
        assert failing["retry_history"][0]["outcome"] == "FAIL"


# ── Test 4: Retry feedback differs by attempt ────────────────────────────

class TestRetryFeedbackDiffers:

    def test_feedback_strings_differ_by_attempt(self):
        """Attempt 2 and 3 feedback are distinct; attempt 1 produces empty."""
        findings = [Finding(
            id="WC-HIGH", check="word_count", severity="CLASS_A",
            message="Word count 1764 above maximum 1100",
        )]

        fb_after_1 = _build_retry_feedback(findings, attempt=1, max_attempts=3, target_words=850)
        fb_after_2 = _build_retry_feedback(findings, attempt=2, max_attempts=3, target_words=850)

        # After attempt 1, feedback for attempt 2
        assert "ATTEMPT 2 of 3" in fb_after_1
        assert "FINAL" not in fb_after_1
        assert "cut at least 664 words" in fb_after_1

        # After attempt 2, feedback for attempt 3
        assert "ATTEMPT 3 of 3" in fb_after_2
        assert "FINAL" in fb_after_2

        # The two are distinct
        assert fb_after_1 != fb_after_2


# ── Test 5: Deterministic checks run with --skip-llm-audit ───────────────

class TestDeterministicRunsWithSkipLlm:

    def test_deterministic_checks_run(self):
        """With use_llm=False, deterministic gates still fire."""
        scene = SceneEntry(
            chapter_number=7,
            scene_number=1,
            title="Test",
            scene_type="MIXED",
            pov="Archer",
            body="Test body.",
            position_in_chapter=1,
        )

        # Prose with dead character Taras acting (deterministic gate)
        prose = "Taras said hello to the group. " * 30  # ~210 words, under 700 = WC-LOW too

        result = audit_scene(
            prose=prose,
            scene=scene,
            use_llm=False,
        )

        checks_found = {f.check for f in result.findings}
        # word_count should fire (under 700) and/or character_state (Taras dead in ch7)
        assert "word_count" in checks_found or "character_state" in checks_found


# ── Test 6: LLM checks do not run with --skip-llm-audit ─────────────────

class TestLlmDoesNotRunWithSkip:

    def test_llm_checks_skipped(self):
        """With use_llm=False, LLM-based checks are not called."""
        scene = SceneEntry(
            chapter_number=1,
            scene_number=1,
            title="Test",
            scene_type="MIXED",
            pov="Archer",
            body="Test body with many beats.\n\nBeat two.\n\nBeat three.",
            position_in_chapter=1,
        )

        prose = "This is valid prose. " * 100  # 500 words, will hit WC-LOW but that's deterministic

        # Mock at the import source — scene_auditor imports call_llm locally
        with patch.dict("sys.modules", {"llm_client": MagicMock()}) as mock_modules:
            mock_llm = sys.modules["llm_client"].call_llm
            result = audit_scene(
                prose=prose,
                scene=scene,
                use_llm=False,
                prior_prose_in_chapter=["Some prior prose here."],
            )

            # LLM client should NOT have been called
            mock_llm.assert_not_called()

        # LLM checks: beat_coverage, reintroduction, logistics_continuity
        llm_checks = {"beat_coverage", "reintroduction", "logistics_continuity"}
        checks_found = {f.check for f in result.findings}
        assert len(checks_found & llm_checks) == 0, f"LLM checks ran: {checks_found & llm_checks}"
