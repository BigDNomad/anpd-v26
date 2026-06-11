"""Tests for fixer_preflight — pre-flight judgment layer for Tier 1 fixes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from audit_checks import Finding, ManuscriptArtifact, SceneText, BriefBundle
from fixer_preflight import (
    PreFlightResult,
    preflight_tier_1,
    _check_replacement_already_in_manuscript,
    _check_replacement_is_known_character,
    _check_target_is_canonical_character,
    _check_target_in_series_bible,
    _check_target_appears_only_once_in_scene,
    _check_target_is_well_formed_marker,
    _check_target_is_complete_sentence,
    _check_deletion_does_not_orphan_paragraph,
    _check_target_occupies_full_line,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _ms(scenes: list[tuple[int, str]]) -> ManuscriptArtifact:
    return ManuscriptArtifact(
        scenes=[SceneText(scene_number=n, text=t, file_path=f"sc_{n:03d}.md")
                for n, t in scenes],
        manuscript_dir="/tmp/test",
    )


def _briefs(
    characters: list[dict] | None = None,
    series_bible: dict | None = None,
) -> BriefBundle:
    return BriefBundle(
        series_bible=series_bible or {},
        character_profiles={"characters": characters or []},
        book_config={},
        scene_map={},
        entity_ledger={},
    )


def _finding(**kwargs) -> Finding:
    defaults = {
        "check_id": "MA-002-character-name-registry",
        "severity": "CLASS_A",
        "scene_number": 1,
        "description": "test",
    }
    defaults.update(kwargs)
    return Finding(**defaults)


def _safe_llm():
    mock = MagicMock()
    mock.return_value = MagicMock(text="SAFE\nNo issues detected.")
    return mock


def _unsafe_llm():
    mock = MagicMock()
    mock.return_value = MagicMock(text="UNSAFE\nBreaks sentence structure.")
    return mock


# ── Replace_span checks ────────────────────────────────────────────────────

class TestReplaceSpanChecks:
    def test_replacement_not_in_manuscript_passes(self):
        ms = _ms([(1, "Webb walked."), (2, "He ran.")])
        passed, _ = _check_replacement_already_in_manuscript("Reyes", 1, ms)
        assert passed

    def test_replacement_in_other_scene_fails(self):
        ms = _ms([(1, "Webb walked."), (2, "Reyes was there.")])
        passed, reason = _check_replacement_already_in_manuscript("Reyes", 1, ms)
        assert not passed
        assert "scene 2" in reason

    def test_replacement_in_same_scene_ok(self):
        ms = _ms([(1, "Webb and Reyes walked.")])
        passed, _ = _check_replacement_already_in_manuscript("Reyes", 1, ms)
        assert passed

    def test_replacement_is_known_character_fails(self):
        br = _briefs(characters=[{"name": "Reyes"}])
        passed, reason = _check_replacement_is_known_character("Reyes", br)
        assert not passed
        assert "known character" in reason

    def test_replacement_not_known_character_passes(self):
        br = _briefs(characters=[{"name": "Archer"}])
        passed, _ = _check_replacement_is_known_character("Reyes", br)
        assert passed

    def test_target_is_canonical_character_fails(self):
        br = _briefs(characters=[{"name": "Webb"}])
        passed, reason = _check_target_is_canonical_character("Webb", br)
        assert not passed
        assert "canonical character" in reason

    def test_target_in_series_bible_fails(self):
        br = _briefs(series_bible={"factions": [{"name": "Webb Syndicate"}]})
        passed, reason = _check_target_in_series_bible("Webb", br)
        assert not passed
        assert "series bible" in reason

    def test_target_not_in_series_bible_passes(self):
        br = _briefs(series_bible={"factions": []})
        passed, _ = _check_target_in_series_bible("Webb", br)
        assert passed


# ── Occurrence and marker checks ───────────────────────────────────────────

class TestOccurrenceAndMarkerChecks:
    def test_target_appears_once_passes(self):
        passed, _ = _check_target_appears_only_once_in_scene("Webb", "Webb walked to the door.")
        assert passed

    def test_target_appears_multiple_times_fails(self):
        passed, reason = _check_target_appears_only_once_in_scene("Webb", "Webb met Webb at the bar.")
        assert not passed
        assert "2 times" in reason

    def test_well_formed_marker_passes(self):
        passed, _ = _check_target_is_well_formed_marker("[NOTE: revise this]")
        assert passed

    def test_non_marker_fails(self):
        passed, _ = _check_target_is_well_formed_marker("He walked to the door.")
        assert not passed


# ── Sentence and paragraph checks ──────────────────────────────────────────

class TestSentenceAndParagraphChecks:
    def test_complete_sentence_passes(self):
        scene = "He ran. The door opened. She waited."
        passed, _ = _check_target_is_complete_sentence("The door opened.", scene)
        assert passed

    def test_incomplete_sentence_fails(self):
        scene = "He ran to the door and opened it."
        passed, _ = _check_target_is_complete_sentence("the door", scene)
        assert not passed

    def test_deletion_orphans_paragraph_fails(self):
        scene = "First paragraph.\n\nThe only sentence.\n\nThird paragraph."
        passed, reason = _check_deletion_does_not_orphan_paragraph("The only sentence.", scene)
        assert not passed
        assert "orphan" in reason

    def test_deletion_preserves_paragraph_passes(self):
        scene = "First sentence. Second sentence."
        passed, _ = _check_deletion_does_not_orphan_paragraph("First sentence.", scene)
        assert passed

    def test_full_line_passes(self):
        scene = "Line one.\nTarget line here\nLine three."
        passed, _ = _check_target_occupies_full_line("Target line here", scene)
        assert passed

    def test_partial_line_fails(self):
        scene = "Some text Target line here more text"
        passed, _ = _check_target_occupies_full_line("Target line here", scene)
        assert not passed


# ── Full dispatcher tests ──────────────────────────────────────────────────

class TestPreFlightDispatcher:
    def test_replace_span_all_pass_returns_apply(self):
        ms = _ms([(1, "Webb walked."), (2, "He ran.")])
        br = _briefs()
        result = preflight_tier_1(
            finding=_finding(),
            operation="replace_span",
            params={"replacement": "Reyes"},
            target_text="Webb",
            scene_text="Webb walked.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
            llm_callable=_safe_llm(),
        )
        assert result.decision == "APPLY"
        assert "llm_surrounding_integrity" in result.checks_run

    def test_replace_span_collision_escalates_t3(self):
        ms = _ms([(1, "Webb walked."), (47, "Reyes waited.")])
        br = _briefs()
        result = preflight_tier_1(
            finding=_finding(),
            operation="replace_span",
            params={"replacement": "Reyes"},
            target_text="Webb",
            scene_text="Webb walked.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
        )
        assert result.decision == "ESCALATE_TIER_3"
        assert "replacement_already_in_manuscript" in result.checks_failed

    def test_replace_span_multi_occurrence_escalates_t2(self):
        ms = _ms([(1, "Webb met Webb at the Webb bar.")])
        br = _briefs()
        result = preflight_tier_1(
            finding=_finding(),
            operation="replace_span",
            params={"replacement": "Reyes"},
            target_text="Webb",
            scene_text="Webb met Webb at the Webb bar.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
        )
        assert result.decision == "ESCALATE_TIER_2"
        assert "target_appears_only_once" in result.checks_failed

    def test_delete_span_well_formed_marker_skips_llm(self):
        ms = _ms([(1, "He walked. [NOTE: revise] She waited.")])
        br = _briefs()
        mock_llm = _safe_llm()
        result = preflight_tier_1(
            finding=_finding(check_id="MA-005-pipeline-note-leak"),
            operation="delete_span",
            params={},
            target_text="[NOTE: revise]",
            scene_text="He walked. [NOTE: revise] She waited.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
            llm_callable=mock_llm,
        )
        assert result.decision == "APPLY"
        mock_llm.assert_not_called()

    def test_delete_span_non_marker_calls_llm(self):
        ms = _ms([(1, "He walked. Some random text. She waited.")])
        br = _briefs()
        mock_llm = _safe_llm()
        result = preflight_tier_1(
            finding=_finding(check_id="MA-005-pipeline-note-leak"),
            operation="delete_span",
            params={},
            target_text="Some random text.",
            scene_text="He walked. Some random text. She waited.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
            llm_callable=mock_llm,
        )
        assert result.decision == "APPLY"
        mock_llm.assert_called_once()

    def test_delete_sentence_all_pass(self):
        scene = "He ran. The door opened. She waited."
        ms = _ms([(1, scene)])
        br = _briefs()
        result = preflight_tier_1(
            finding=_finding(check_id="MA-005-pipeline-note-leak"),
            operation="delete_sentence",
            params={},
            target_text="The door opened.",
            scene_text=scene,
            scene_number=1,
            manuscript=ms,
            briefs=br,
            llm_callable=_safe_llm(),
        )
        assert result.decision == "APPLY"

    def test_delete_line_full_line_passes(self):
        scene = "Line one.\nTarget line here.\nLine three."
        ms = _ms([(1, scene)])
        br = _briefs()
        result = preflight_tier_1(
            finding=_finding(check_id="MA-007-voice-register-adherence"),
            operation="delete_line",
            params={},
            target_text="Target line here.",
            scene_text=scene,
            scene_number=1,
            manuscript=ms,
            briefs=br,
        )
        assert result.decision == "APPLY"

    def test_llm_failure_escalates_t3(self):
        ms = _ms([(1, "Webb walked.")])
        br = _briefs()
        mock_llm = MagicMock(side_effect=RuntimeError("API timeout"))
        result = preflight_tier_1(
            finding=_finding(),
            operation="replace_span",
            params={"replacement": "Reyes"},
            target_text="Webb",
            scene_text="Webb walked.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
            llm_callable=mock_llm,
        )
        assert result.decision == "ESCALATE_TIER_3"
        assert "LLM call failed" in result.reasoning

    def test_llm_unsafe_escalates_t3(self):
        ms = _ms([(1, "Webb walked.")])
        br = _briefs()
        result = preflight_tier_1(
            finding=_finding(),
            operation="replace_span",
            params={"replacement": "Reyes"},
            target_text="Webb",
            scene_text="Webb walked.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
            llm_callable=_unsafe_llm(),
        )
        assert result.decision == "ESCALATE_TIER_3"
        assert "UNSAFE" in result.reasoning

    def test_no_llm_client_skips_llm_check(self):
        ms = _ms([(1, "Webb walked.")])
        br = _briefs()
        result = preflight_tier_1(
            finding=_finding(),
            operation="replace_span",
            params={"replacement": "Reyes"},
            target_text="Webb",
            scene_text="Webb walked.",
            scene_number=1,
            manuscript=ms,
            briefs=br,
            llm_callable=None,
        )
        assert result.decision == "APPLY"
        assert "llm_surrounding_integrity" not in result.checks_run
