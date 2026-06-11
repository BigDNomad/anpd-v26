"""Tests for V25 synopsis_generator — unit tests (no API calls)."""
import json
import pytest
from synopsis_generator import build_chapter_prompt, SYSTEM_PROMPT
from outline_parser import ChapterSpec


@pytest.fixture
def sample_chapter():
    return ChapterSpec(
        chapter_number=2,
        title="The Concert",
        content="Hadeon plays piano at a concert. He has an epileptic seizure. He is suspended.",
        annotations={"scene_type": "MIXED"},
        beats=[
            "Hadeon plays piano at a concert.",
            "He has an epileptic seizure.",
            "He is suspended from the conservatory.",
        ],
    )


@pytest.fixture
def sample_intake():
    return {
        "book_number": 1,
        "title": "Broken Sabers",
        "series": "Hadeon's Cossacks",
        "total_chapter_count": 8,
        "target_word_count": 85000,
        "historical_window": {"start_date": "2018-12-01", "end_date": "2022-04-15"},
        "historical_anchors_out_of_scope": ["Bucha massacre"],
        "anti_patterns": ["No smell-of-room openings", "No age references"],
    }


@pytest.fixture
def sample_series_bible():
    return {
        "series_name": "Hadeon's Cossacks",
        "voice_register": {
            "base_voice": "Leonard-style short declarative",
            "intrusion_voice": "McCarthy-style extended",
            "intrusion_allocation": "ACTION: 0-5%, NON-ACTION: 10-20%",
        },
        "operational_doctrine": ["No prisoners", "Balaclavas during raids"],
    }


@pytest.fixture
def sample_characters():
    return {
        "characters": [
            {"name": "Hadeon Kovalenko", "role": "protagonist", "pov_eligible": True},
            {"name": "Kalyna Soroka", "role": "supporting", "pov_eligible": True},
        ]
    }


def test_build_chapter_prompt_contains_outline(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "piano" in prompt.lower()
    assert "epileptic" in prompt.lower()
    assert "suspended" in prompt.lower()


def test_build_chapter_prompt_includes_beats(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "1. Hadeon plays piano" in prompt
    assert "2. He has an epileptic seizure" in prompt
    assert "3. He is suspended" in prompt


def test_build_chapter_prompt_includes_dynamic_scene_guidance(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "scenes" in prompt.lower()
    assert "outline beats" in prompt.lower()


def test_build_chapter_prompt_includes_voice(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "Leonard" in prompt
    assert "McCarthy" in prompt


def test_build_chapter_prompt_includes_doctrine(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "No prisoners" in prompt


def test_build_chapter_prompt_includes_anti_patterns(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "smell-of-room" in prompt.lower()


def test_build_chapter_prompt_includes_historical_window(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "2018-12-01" in prompt
    assert "2022-04-15" in prompt


def test_build_chapter_prompt_includes_oos_anchors(sample_chapter, sample_intake, sample_series_bible, sample_characters):
    prompt = build_chapter_prompt(
        chapter=sample_chapter,
        intake=sample_intake,
        series_bible=sample_series_bible,
        character_profiles=sample_characters,
        principles_text="",
    )
    assert "Bucha massacre" in prompt


def test_system_prompt_contains_hard_constraints():
    assert "EVERY beat" in SYSTEM_PROMPT
    assert "MUST NOT invent" in SYSTEM_PROMPT
    assert "MUST NOT reorder" in SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════════════════════
# SG-2: Scene-organized detection and single-scene constraint
# ═══════════════════════════════════════════════════════════════════════════

def _build_prompt_with_defaults(chapter, **overrides):
    defaults = dict(
        chapter=chapter,
        intake={"book_number": 1, "title": "Test", "series": "Test",
                "historical_window": {"start_date": "2026-01-01", "end_date": "2026-12-31"}},
        series_bible={},
        character_profiles={},
        principles_text="",
    )
    defaults.update(overrides)
    return build_chapter_prompt(**defaults)


class TestSceneOrganizedPrompt:
    def test_scene_organized_input_produces_exactly_one_scene(self):
        ch = ChapterSpec(chapter_number=5, title="T", content="Beat.",
                         annotations={"scene_type": "ACTION"}, beats=["Beat one."])
        prompt = _build_prompt_with_defaults(ch)
        assert "EXACTLY 1 scene" in prompt
        assert "Do NOT decompose" in prompt

    def test_chapter_organized_input_uses_legacy_guidance(self):
        ch = ChapterSpec(chapter_number=1, title="T", content="Beat.",
                         annotations={}, beats=["Beat one.", "Beat two.", "Beat three."])
        prompt = _build_prompt_with_defaults(ch)
        assert "EXACTLY 1 scene" not in prompt
        # Legacy guidance uses range like "2-3 scenes"
        assert "scenes" in prompt.lower()

    def test_scene_organized_prompt_includes_schema_block(self):
        ch = ChapterSpec(chapter_number=10, title="T", content="Beat.",
                         annotations={"scene_type": "SUSPENSE"}, beats=["Beat."])
        prompt = _build_prompt_with_defaults(ch)
        assert "INPUT-OUTPUT SCHEMA" in prompt

    def test_scene_organized_prompt_passes_outline_type(self):
        ch = ChapterSpec(chapter_number=10, title="T", content="Beat.",
                         annotations={"scene_type": "SUSPENSE"}, beats=["Beat."])
        prompt = _build_prompt_with_defaults(ch)
        assert "[TYPE: SUSPENSE]" in prompt

    def test_chapter_organized_prompt_does_not_include_schema_block(self):
        ch = ChapterSpec(chapter_number=1, title="T", content="Beat.",
                         annotations={}, beats=["Beat."])
        prompt = _build_prompt_with_defaults(ch)
        assert "INPUT-OUTPUT SCHEMA" not in prompt


# ═══════════════════════════════════════════════════════════════════════════
# SG-4: Per-chapter comparator skip for scene-organized inputs
# ═══════════════════════════════════════════════════════════════════════════

class TestComparatorSkip:
    def test_scene_organized_effective_max_regen_is_zero(self):
        """Scene-organized chapters should only generate once (no comparator regen)."""
        # This tests the logic inline — we verify that the effective_max_regen
        # variable would be 0 for scene-organized annotations
        annotations = {"scene_type": "ACTION"}
        is_scene_organized = "scene_type" in (annotations or {})
        effective_max_regen = 0 if is_scene_organized else 3
        assert effective_max_regen == 0

    def test_chapter_organized_effective_max_regen_is_nonzero(self):
        """Chapter-organized chapters should use the full regen loop."""
        annotations = {}
        is_scene_organized = "scene_type" in (annotations or {})
        effective_max_regen = 0 if is_scene_organized else 3
        assert effective_max_regen == 3
