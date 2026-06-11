"""Tests for V25 outline_parser."""
import os
import pytest
from outline_parser import (
    parse_outline, parse_outline_scenes,
    ChapterSpec, ParsedScene, ParsedOutline,
    _extract_heading_tag,
)


@pytest.fixture
def markdown_outline(tmp_path):
    content = """ARCS — Multiple arcs in this story.
1. Hadeon's growth arc.
2. Unit formation arc.

Chapter 1 (Prologue)
The story opens with a brief historical account of Napoleon's army.
It sets the tone for the fierce fighting spirit.

Chapter 2
Hadeon Kovalenko plays piano during a concert.
Half way through, Hadeon has an epileptic attack. [ACTION]
Hadeon is suspended from the conservatory.

Chapter 3
February 24 2022 - The Russians cross the border. [ACTION]
Yaroslav fights at the airport defense.
Yaroslav is wounded.

Chapter 4
After the funeral for their parents.
Hadeon finds the broken saber. [MIXED]
Kalyna records Hadeon's recruitment video.
"""
    path = tmp_path / "outline.md"
    path.write_text(content)
    return str(path)


def test_parse_markdown_chapter_count(markdown_outline):
    result = parse_outline(markdown_outline)
    assert len(result.chapters) == 4


def test_parse_chapter_numbers(markdown_outline):
    result = parse_outline(markdown_outline)
    numbers = [ch.chapter_number for ch in result.chapters]
    assert numbers == [1, 2, 3, 4]


def test_parse_chapter_content(markdown_outline):
    result = parse_outline(markdown_outline)
    ch2 = result.chapters[1]
    assert "piano" in ch2.content.lower()
    assert "epileptic" in ch2.content.lower()


def test_parse_beats_extracted(markdown_outline):
    result = parse_outline(markdown_outline)
    ch2 = result.chapters[1]
    assert len(ch2.beats) >= 2


def test_parse_annotations(markdown_outline):
    result = parse_outline(markdown_outline)
    ch2 = result.chapters[1]
    assert ch2.annotations.get("scene_type") == "ACTION"


def test_parse_top_matter(markdown_outline):
    result = parse_outline(markdown_outline)
    assert "raw" in result.top_matter
    assert "arcs" in result.top_matter["raw"].lower()


def test_parse_real_outline():
    """Test parsing the actual operator outline PDF if available."""
    outline_path = "/anpd/v25/series/hadeons_cossacks/b01/inputs/outline.pdf"
    if not os.path.exists(outline_path):
        pytest.skip("Operator outline not staged")
    result = parse_outline(outline_path)
    # The outline has chapters 1-23 (8 with content, rest empty)
    content_chapters = [ch for ch in result.chapters if ch.content.strip()]
    assert len(content_chapters) == 8
    assert result.chapters[0].chapter_number == 1


def test_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        parse_outline("/nonexistent/file.md")


def test_passthrough_directive_detected(tmp_path):
    content = """Chapter 1 (Prologue)
The story opens. (Use existing prologue scene)

Chapter 2
Hadeon plays piano. Normal content.
"""
    path = tmp_path / "outline.md"
    path.write_text(content)
    result = parse_outline(str(path))
    assert result.chapters[0].passthrough is True
    assert result.chapters[1].passthrough is False


def test_no_passthrough_when_absent(tmp_path):
    content = """Chapter 1
Normal content without directives.
"""
    path = tmp_path / "outline.md"
    path.write_text(content)
    result = parse_outline(str(path))
    assert result.chapters[0].passthrough is False


# ═══════════════════════════════════════════════════════════════════════════
# SG-1: Scene-organized outline parsing tests
# ═══════════════════════════════════════════════════════════════════════════

_SCENE_OUTLINE = """\
# Test Outline

## Scene-by-Scene Outline

### ACT ONE — BEGINNING (Scenes 1–3)

**Scene 1 — Opening Shot**
*Action*
The team breaches the compound. Explosions shake the walls.
They secure the target in under two minutes.

---

**Scene 2 — The Candidates**
*Non-action*
Hank watches from behind the glass. He evaluates candidates.

---

**Scene 3 — Arrival: Caracas**
*Suspense*
Lena lands in Caracas. She observes the city from the car.

### ACT TWO — MIDDLE (Scenes 4–5)

**Scene 4 — The Market**
*Suspense (transitioning to Action)*
Pursuit through the crowded market. Lena tracks from overwatch.
The target slips into an alley. Hank follows on foot.

---

**Scene 5 — Debrief**
*Mixed*
The team gathers. They review what went wrong.
"""


@pytest.fixture
def scene_outline(tmp_path):
    path = tmp_path / "scene_outline.md"
    path.write_text(_SCENE_OUTLINE, encoding="utf-8")
    return str(path)


class TestSceneMarkerRecognition:
    def test_recognize_single_scene_marker(self, tmp_path):
        text = "**Scene 1 — Title**\n*Action*\nBeat text here.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.total_scene_count == 1

    def test_recognize_multiple_scenes_with_separators(self, scene_outline):
        result = parse_outline_scenes(scene_outline)
        assert result.total_scene_count == 5
        numbers = [s.number for s in result.scenes]
        assert numbers == [1, 2, 3, 4, 5]

    def test_scene_number_extraction(self, tmp_path):
        text = "**Scene 25 — Funes**\n*Action*\nBeat.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].number == 25

    def test_scene_title_extraction_with_punctuation(self, tmp_path):
        text = '**Scene 6 — Arrival: Caracas**\n*Action*\nBeat.\n'
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].title == "Arrival: Caracas"

    def test_em_dash_separator_accepted(self, tmp_path):
        text = "**Scene 1 \u2014 Title**\n*Action*\nBeat.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.total_scene_count == 1

    def test_en_dash_separator_accepted(self, tmp_path):
        text = "**Scene 1 \u2013 Title**\n*Action*\nBeat.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.total_scene_count == 1


class TestTypeTagRecognition:
    def test_type_tag_action(self, tmp_path):
        text = "**Scene 1 — X**\n*Action*\nBeat.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].type == "ACTION"

    def test_type_tag_non_action(self, tmp_path):
        text = "**Scene 1 — X**\n*Non-action*\nBeat.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].type == "NON-ACTION"

    def test_type_tag_suspense(self, tmp_path):
        text = "**Scene 1 — X**\n*Suspense*\nBeat.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].type == "SUSPENSE"

    def test_type_tag_mixed_qualifier(self, tmp_path):
        text = "**Scene 1 — X**\n*Suspense (transitioning to Action)*\nBeat.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].type == "SUSPENSE"
        assert "transitioning" in result.scenes[0].type_raw

    def test_type_tag_missing_returns_unknown(self, tmp_path):
        text = "**Scene 1 — X**\nBeat text directly, no type tag.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].type == "UNKNOWN"


class TestBeatExtraction:
    def test_beat_extraction_single_paragraph(self, tmp_path):
        text = "**Scene 1 — X**\n*Action*\nThe team breaches the compound.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert "team breaches" in result.scenes[0].beats

    def test_beat_extraction_multiple_paragraphs(self, tmp_path):
        text = "**Scene 1 — X**\n*Action*\nFirst paragraph.\n\nSecond paragraph.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert "First paragraph" in result.scenes[0].beats
        assert "Second paragraph" in result.scenes[0].beats

    def test_beats_stop_at_horizontal_rule(self, scene_outline):
        result = parse_outline_scenes(scene_outline)
        s1 = result.scenes[0]
        # Scene 1 beats should not contain scene 2's content
        assert "Hank watches" not in s1.beats

    def test_beats_stop_at_next_scene_marker(self, tmp_path):
        text = ("**Scene 1 — A**\n*Action*\nBeat one.\n"
                "**Scene 2 — B**\n*Action*\nBeat two.\n")
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert "Beat two" not in result.scenes[0].beats
        assert "Beat two" in result.scenes[1].beats


class TestActAssignment:
    def test_act_assignment_from_header(self, scene_outline):
        result = parse_outline_scenes(scene_outline)
        s1 = result.scenes[0]
        assert s1.act == "ACT ONE"

    def test_act_assignment_prologue(self, tmp_path):
        text = ("### PROLOGUE + ACT ONE — BEFORE THE DARK (Scenes 1–2)\n\n"
                "**Scene 1 — X**\n*Action*\nBeat.\n---\n"
                "**Scene 2 — Y**\n*Action*\nBeat.\n")
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.scenes[0].act == "ACT ONE"

    def test_scene_outside_act_range_recorded_with_warning(self, tmp_path):
        # Act header says "Scenes 77–100" but scene 76 appears under it
        text = ("### ACT THREE — THE DARK (Scenes 77–100)\n\n"
                "**Scene 76 — Misplaced**\n*Action*\nBeat.\n")
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        # Scene 76 is parsed and assigned to ACT THREE
        assert result.scenes[0].number == 76
        assert result.scenes[0].act == "ACT THREE"


class TestActTwoTransition:
    def test_act_two_scenes(self, scene_outline):
        result = parse_outline_scenes(scene_outline)
        s4 = next(s for s in result.scenes if s.number == 4)
        assert s4.act == "ACT TWO"


# ═══════════════════════════════════════════════════════════════════════════
# Mandate calibration anchor tests (real outline data)
# ═══════════════════════════════════════════════════════════════════════════

_MANDATE_OUTLINE = "/anpd/v25/series/black_tide/b01/work/outline.md"


@pytest.fixture
def mandate_parsed():
    if not os.path.exists(_MANDATE_OUTLINE):
        pytest.skip("Mandate outline not available")
    return parse_outline_scenes(_MANDATE_OUTLINE)


class TestMandateCalibration:
    def test_mandate_outline_returns_100_scenes(self, mandate_parsed):
        assert mandate_parsed.total_scene_count == 100

    def test_mandate_scene_1_is_action_type(self, mandate_parsed):
        s1 = mandate_parsed.scenes[0]
        assert s1.number == 1
        assert s1.type == "ACTION"

    @pytest.mark.xfail(reason="pinned to pre-25-chapter Mandate outline; awaiting operator re-authored outline (Decision 2 / Path A)", strict=False)
    def test_mandate_scene_25_is_funes(self, mandate_parsed):
        s25 = next(s for s in mandate_parsed.scenes if s.number == 25)
        # Scene 25 is the Twist 1 anchor — title should reference payphone or Funes
        assert "FUNES" in s25.title.upper() or "PAYPHONE" in s25.title.upper() or "TWIST" in s25.title.upper()

    @pytest.mark.xfail(reason="pinned to pre-25-chapter Mandate outline; awaiting operator re-authored outline (Decision 2 / Path A)", strict=False)
    def test_mandate_act_distribution(self, mandate_parsed):
        from collections import Counter
        acts = Counter(s.act for s in mandate_parsed.scenes)
        # ACT ONE: scenes 1-25 = 25 scenes
        assert acts.get("ACT ONE", 0) == 25
        # ACT TWO: scenes 26-76 = 51 scenes
        assert acts.get("ACT TWO", 0) >= 49  # allow small variance
        # ACT THREE + RESOLUTION: remaining
        assert acts.get("ACT THREE", 0) + acts.get("RESOLUTION", 0) >= 20


# ═══════════════════════════════════════════════════════════════════════════
# SG-4: Minimal "Scene N - [TAG]" format tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMinimalFormat:
    """Tests for the minimal 'Scene N - [TAG]' format (SG-4)."""

    def test_tagged_scene_detected(self, tmp_path):
        """'Scene 1 - [ACTION]' with prose body: detected, type=ACTION, title empty."""
        text = "Scene 1 - [ACTION]\nHank storms the compound. He breaches the door.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline(str(path))
        assert len(result.chapters) == 1
        ch = result.chapters[0]
        assert ch.chapter_number == 1
        assert ch.title == ""
        assert ch.annotations.get("scene_type") == "ACTION"
        assert len(ch.beats) >= 1
        assert result.top_matter.get("format") == "scene-organized"

    def test_untagged_scene_detected(self, tmp_path):
        """'Scene 2 -' (no tag) with prose: detected, type absent/UNKNOWN, NOT chapter-organized."""
        text = "Scene 1 -\nHank drives through the city. He stops at the market.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline(str(path))
        assert len(result.chapters) == 1
        ch = result.chapters[0]
        assert ch.chapter_number == 1
        assert ch.title == ""
        # scene_type should be absent or UNKNOWN — NOT causing format flip
        assert result.top_matter.get("format") == "scene-organized"

    def test_mixed_tagged_untagged_mini_outline(self, tmp_path):
        """Full minimal-format outline: 3 scenes, mixed tagged/untagged."""
        text = """\
Scene 1 - [ACTION]
Hank leads the raid on the compound. Delta clears the wing.
Three targets neutralized in under two minutes.

Scene 2 -
Lena meets Marco at the apartment. She tells him about the assignment.
He says yes without performance.

Scene 3 - [NON-ACTION]
Hank briefs the team. Target list presented. Lena runs technical intelligence.
"""
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline(str(path))
        assert len(result.chapters) == 3
        assert result.top_matter.get("format") == "scene-organized"

        # Scene 1: tagged ACTION
        ch1 = result.chapters[0]
        assert ch1.chapter_number == 1
        assert ch1.annotations.get("scene_type") == "ACTION"
        assert ch1.title == ""
        assert len(ch1.beats) >= 1

        # Scene 2: untagged — no scene_type in annotations
        ch2 = result.chapters[1]
        assert ch2.chapter_number == 2
        assert ch2.annotations.get("scene_type") is None
        assert ch2.title == ""
        assert len(ch2.beats) >= 1

        # Scene 3: tagged NON-ACTION
        ch3 = result.chapters[2]
        assert ch3.chapter_number == 3
        assert ch3.annotations.get("scene_type") == "NON-ACTION"

    def test_parse_outline_scenes_minimal_format(self, tmp_path):
        """parse_outline_scenes also works with minimal format."""
        text = """\
Scene 1 - [ACTION]
Raid happens. Targets down.

Scene 2 -
Meeting occurs.
"""
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline_scenes(str(path))
        assert result.total_scene_count == 2
        assert result.scenes[0].type == "ACTION"
        assert result.scenes[1].type == "UNKNOWN"

    def test_old_format_still_works(self, tmp_path):
        """Mandate-style '**Scene N — Title**' + italic type still parses identically."""
        text = """\
**Scene 1 — Prologue: Operation Absolute Resolve**
*Action — POV: Hank Reyes*

Hank leads the raid. Delta clears the wing.

---

**Scene 2 — The Candidates**
*Non-action*

Hank watches candidates.
"""
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline(str(path))
        assert len(result.chapters) == 2
        assert result.top_matter.get("format") == "scene-organized"

        ch1 = result.chapters[0]
        assert ch1.chapter_number == 1
        assert ch1.title == "Prologue: Operation Absolute Resolve"
        assert ch1.annotations.get("scene_type") == "ACTION"
        assert ch1.annotations.get("pov") == "Hank Reyes"

        ch2 = result.chapters[1]
        assert ch2.annotations.get("scene_type") == "NON-ACTION"

    def test_extract_heading_tag(self):
        """_extract_heading_tag strips bracket tags from title text."""
        scene_type, cleaned = _extract_heading_tag("[ACTION]")
        assert scene_type == "ACTION"
        assert cleaned == ""

        scene_type, cleaned = _extract_heading_tag("The Raid [ACTION]")
        assert scene_type == "ACTION"
        assert cleaned == "The Raid"

        scene_type, cleaned = _extract_heading_tag("No tag here")
        assert scene_type == ""
        assert cleaned == "No tag here"

        scene_type, cleaned = _extract_heading_tag("[NON-ACTION]")
        assert scene_type == "NON-ACTION"
        assert cleaned == ""

    def test_bare_scene_heading_no_content_after_dash(self, tmp_path):
        """'Scene 1 -' with nothing after dash: detected as scene."""
        text = "Scene 1 -\nSome prose here.\n"
        path = tmp_path / "o.md"
        path.write_text(text)
        result = parse_outline(str(path))
        assert len(result.chapters) == 1
        assert result.chapters[0].chapter_number == 1
