"""Tests for V26 pillar marker emission and deterministic Q2.

Covers:
  - Outline parser: pillar marker extraction from HTML comments
  - Generator: [PILLAR:] tag emission in scene headers
  - Auditor: deterministic Q2 band computation, missing-marker FAIL, fallback path
"""

import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

from outline_parser_v26_20260612 import (
    parse_outline,
    parse_outline_scenes,
    _extract_pillar_marker,
    _PILLAR_COMMENT_RE,
    ChapterSpec,
)
from synopsis_generator_v26_20260612 import build_chapter_prompt
from synopsis_auditor_v26_20260612_T2000 import (
    Scene,
    parse_synopsis,
    _q2_has_pillar_markers,
    _q2_deterministic_verdict,
    _get_deterministic_checks,
    VERDICT_SEVERITY,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

FIXTURE_OUTLINE = """\
---
format: scene-organized
title: Test
scene_count: 10
---

# Test Outline

## ACT ONE

**Scene 1 - [ACTION]**

Opening scene beat.

**Scene 2 - [NON-ACTION]**

Setup beat.

<!-- TWIST 1 -->
**Scene 3 - [ACTION]**

Twist one beat.

**Scene 4 - [SUSPENSE]**

Post-twist beat.

## ACT TWO

<!-- TWIST 2 -->
**Scene 5 - [SUSPENSE]**

Midpoint twist beat.

**Scene 6 - [ACTION]**

Rising action beat.

<!-- TWIST 3 -->
**Scene 7 - [ACTION]**

Act two twist beat.

<!-- LOWEST POINT -->
**Scene 8 - [NON-ACTION]**

Lowest point beat.

## ACT THREE

<!-- FINAL BATTLE -->
**Scene 9 - [ACTION]**

Final battle beat.

**Scene 10 - [NON-ACTION]**

Denouement beat.
"""

EFFECTIVE_CONFIG_DEFAULT = {
    'twist_1_position': 25,
    'twist_2_position': 50,
    'twist_3_position': 75,
}


@pytest.fixture
def fixture_outline_file(tmp_path):
    path = tmp_path / "outline.md"
    path.write_text(FIXTURE_OUTLINE)
    return str(path)


@pytest.fixture
def sample_intake():
    return {
        "book_number": 1,
        "title": "Test Book",
        "series": "Test",
        "total_chapter_count": 10,
        "total_scene_count": 10,
        "target_scene_count": 10,
        "target_word_count": 50000,
        "outline_path": "outline.md",
        "historical_window": {"start_date": "1969-01-01", "end_date": "1969-12-31"},
        "voice_register": "Lean prose.",
        "anti_patterns": [],
    }


# ── Outline Parser: Pillar Marker Extraction ─────────────────────────────────

class TestOutlineParserPillarExtraction:

    def test_parse_outline_extracts_all_pillars(self, fixture_outline_file):
        result = parse_outline(fixture_outline_file)
        pillar_map = {
            ch.chapter_number: ch.annotations.get('pillar')
            for ch in result.chapters if ch.annotations.get('pillar')
        }
        assert pillar_map == {
            3: 'TWIST1',
            5: 'TWIST2',
            7: 'TWIST3',
            8: 'LOWEST_POINT',
            9: 'FINAL_BATTLE',
        }

    def test_parse_outline_scenes_extracts_all_pillars(self, fixture_outline_file):
        result = parse_outline_scenes(fixture_outline_file)
        pillar_map = {
            s.number: s.pillar for s in result.scenes if s.pillar
        }
        assert pillar_map == {
            3: 'TWIST1',
            5: 'TWIST2',
            7: 'TWIST3',
            8: 'LOWEST_POINT',
            9: 'FINAL_BATTLE',
        }

    def test_scenes_without_pillar_have_none(self, fixture_outline_file):
        result = parse_outline(fixture_outline_file)
        non_pillar = [ch for ch in result.chapters if not ch.annotations.get('pillar')]
        assert len(non_pillar) == 5  # scenes 1,2,4,6,10

    def test_pillar_marker_count(self, fixture_outline_file):
        result = parse_outline(fixture_outline_file)
        pillar_count = sum(1 for ch in result.chapters if ch.annotations.get('pillar'))
        assert pillar_count == 5

    def test_extract_pillar_marker_function(self):
        lines = [
            "Some content",
            "<!-- TWIST 1 -->",
            "**Scene 5 - [ACTION]**",
        ]
        assert _extract_pillar_marker(lines, 2, 0) == 'TWIST1'

    def test_extract_pillar_marker_no_marker(self):
        lines = [
            "Some content",
            "",
            "**Scene 5 - [ACTION]**",
        ]
        assert _extract_pillar_marker(lines, 2, 0) is None

    def test_pillar_comment_regex_variants(self):
        """Test that the regex handles whitespace variants."""
        for comment, expected in [
            ("<!-- TWIST 1 -->", "TWIST 1"),
            ("<!-- TWIST1 -->", "TWIST1"),
            ("<!--  TWIST 2  -->", "TWIST 2"),
            ("<!-- LOWEST POINT -->", "LOWEST POINT"),
            ("<!-- FINAL BATTLE -->", "FINAL BATTLE"),
            ("<!-- twist 3 -->", "twist 3"),
        ]:
            m = _PILLAR_COMMENT_RE.search(comment)
            assert m is not None, f"Failed to match: {comment}"


# ── Generator: Pillar Tag in Scene Headers ───────────────────────────────────

class TestGeneratorPillarEmission:

    def test_pillar_tag_in_prompt_when_annotated(self, sample_intake):
        chapter = ChapterSpec(
            chapter_number=24,
            title="The Fall",
            content="Archer falls from the penetrator.",
            annotations={"scene_type": "ACTION", "pillar": "TWIST1"},
            beats=["Archer falls from the penetrator."],
        )
        prompt = build_chapter_prompt(
            chapter, sample_intake,
            series_bible={"hard_constraints": {}},
            character_profiles={},
            principles_text="",
            is_scene_organized=True,
        )
        assert "[PILLAR: TWIST1]" in prompt

    def test_no_pillar_tag_when_not_annotated(self, sample_intake):
        chapter = ChapterSpec(
            chapter_number=25,
            title="On the Ground",
            content="Archer wakes alone.",
            annotations={"scene_type": "SUSPENSE"},
            beats=["Archer wakes alone."],
        )
        prompt = build_chapter_prompt(
            chapter, sample_intake,
            series_bible={"hard_constraints": {}},
            character_profiles={},
            principles_text="",
            is_scene_organized=True,
        )
        assert "[PILLAR:" not in prompt

    def test_pillar_tag_format_in_type_line(self, sample_intake):
        """The [PILLAR:] tag appears after [TYPE:] in the instruction."""
        chapter = ChapterSpec(
            chapter_number=77,
            title="Capture",
            content="Coyle is captured.",
            annotations={"scene_type": "ACTION", "pillar": "TWIST3"},
            beats=["Coyle is captured."],
        )
        prompt = build_chapter_prompt(
            chapter, sample_intake,
            series_bible={"hard_constraints": {}},
            character_profiles={},
            principles_text="",
            is_scene_organized=True,
        )
        # TYPE should come before PILLAR
        type_pos = prompt.find("[TYPE: ACTION]")
        pillar_pos = prompt.find("[PILLAR: TWIST3]")
        assert type_pos < pillar_pos


# ── Auditor: Scene Parser Pillar Extraction ──────────────────────────────────

class TestAuditorPillarParsing:

    def test_parse_synopsis_extracts_pillar(self):
        synopsis = """\
### Scene 1 — Opening [TYPE: ACTION]

Some content.

### Scene 24 — The Fall [TYPE: ACTION] [PILLAR: TWIST1]

Archer falls.

### Scene 25 — Recovery [TYPE: SUSPENSE]

Archer recovers.
"""
        scenes = parse_synopsis(synopsis)
        assert len(scenes) == 3
        assert scenes[0].pillar == ""
        assert scenes[1].pillar == "TWIST1"
        assert scenes[2].pillar == ""

    def test_parse_synopsis_cleans_pillar_from_title(self):
        synopsis = "### Scene 56 — Kill Order [TYPE: SUSPENSE] [PILLAR: TWIST2]\n\nContent."
        scenes = parse_synopsis(synopsis)
        assert scenes[0].title == "Kill Order"
        assert scenes[0].pillar == "TWIST2"

    def test_has_pillar_markers_true(self):
        scenes = [
            Scene(1, "A", "body", "ACTION", "", ""),
            Scene(24, "B", "body", "ACTION", "", "TWIST1"),
            Scene(56, "C", "body", "SUSPENSE", "", "TWIST2"),
        ]
        assert _q2_has_pillar_markers(scenes) is True

    def test_has_pillar_markers_false_no_twists(self):
        scenes = [
            Scene(1, "A", "body", "ACTION", "", ""),
            Scene(86, "B", "body", "ACTION", "", "FINAL_BATTLE"),
        ]
        assert _q2_has_pillar_markers(scenes) is False

    def test_has_pillar_markers_false_empty(self):
        assert _q2_has_pillar_markers([]) is False


# ── Auditor: Deterministic Q2 Band Computation ──────────────────────────────

class TestDeterministicQ2:

    def _make_scenes(self, total, twist_positions):
        """Build a scene list with pillars at specified positions."""
        scenes = []
        for n in range(1, total + 1):
            pillar = twist_positions.get(n, "")
            scenes.append(Scene(n, f"Scene {n}", "body", "ACTION", "", pillar))
        return scenes

    def test_perfect_placement_pass(self):
        """Twists at 25/50/75 out of 100 → all within ±7% → PASS."""
        scenes = self._make_scenes(100, {25: "TWIST1", 50: "TWIST2", 75: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'PASS'

    def test_within_pass_band(self):
        """Twists at 30/53/72 → all within ±7% → PASS."""
        scenes = self._make_scenes(100, {30: "TWIST1", 53: "TWIST2", 72: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'PASS'

    def test_pass_band_boundary(self):
        """Twists at 32/57/82 → exactly ±7% → PASS."""
        scenes = self._make_scenes(100, {32: "TWIST1", 57: "TWIST2", 82: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'PASS'

    def test_weak_band(self):
        """Twist1 at 35% → delta 10% → within ±12% but beyond ±7% → WEAK."""
        scenes = self._make_scenes(100, {35: "TWIST1", 50: "TWIST2", 75: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'WEAK'

    def test_weak_band_boundary(self):
        """Twist1 at 37% → delta 12% → exactly at WEAK boundary → WEAK."""
        scenes = self._make_scenes(100, {37: "TWIST1", 50: "TWIST2", 75: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'WEAK'

    def test_fail_beyond_weak_band(self):
        """Twist2 at 77% → delta 27% → beyond ±12% → FAIL."""
        scenes = self._make_scenes(100, {25: "TWIST1", 77: "TWIST2", 75: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'FAIL'

    def test_missing_twist_marker_fail(self):
        """Only TWIST1 and TWIST3 present → TWIST2 missing → FAIL."""
        scenes = self._make_scenes(100, {25: "TWIST1", 75: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'FAIL'
        assert 'TWIST2' in note
        assert 'missing' in note.lower()

    def test_all_twists_missing_fail(self):
        """No twist markers → FAIL (deficit)."""
        scenes = self._make_scenes(100, {})
        # _q2_has_pillar_markers would return False, so this wouldn't normally
        # be called, but if called directly it should still FAIL on missing.
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'FAIL'

    def test_non_twist_pillars_ignored(self):
        """LOWEST_POINT and FINAL_BATTLE don't count as twist markers."""
        scenes = self._make_scenes(100, {
            25: "TWIST1", 50: "TWIST2", 75: "TWIST3",
            78: "LOWEST_POINT", 86: "FINAL_BATTLE",
        })
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert verdict == 'PASS'

    def test_note_contains_positions(self):
        scenes = self._make_scenes(100, {25: "TWIST1", 50: "TWIST2", 75: "TWIST3"})
        verdict, note = _q2_deterministic_verdict(scenes, EFFECTIVE_CONFIG_DEFAULT)
        assert 'TWIST1' in note
        assert 'TWIST2' in note
        assert 'TWIST3' in note
        assert 'deterministic' in note.lower()


# ── Auditor: Fallback Path (no markers) ─────────────────────────────────────

class TestQ2FallbackPath:

    def test_deterministic_checks_without_markers(self):
        """Without pillar markers, Q2 is NOT in deterministic checks."""
        scenes = [Scene(1, "A", "body", "ACTION", "", "")]
        checks = _get_deterministic_checks(scenes)
        assert 'Q2' not in checks
        assert 'Q8' in checks
        assert 'Q19' in checks

    def test_deterministic_checks_with_markers(self):
        """With pillar markers, Q2 IS in deterministic checks."""
        scenes = [
            Scene(25, "A", "body", "ACTION", "", "TWIST1"),
            Scene(50, "B", "body", "ACTION", "", "TWIST2"),
            Scene(75, "C", "body", "ACTION", "", "TWIST3"),
        ]
        checks = _get_deterministic_checks(scenes)
        assert 'Q2' in checks
        assert 'Q8' in checks
        assert 'Q19' in checks
