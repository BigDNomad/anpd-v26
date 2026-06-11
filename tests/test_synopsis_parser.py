"""Tests for V25 synopsis_parser."""
import os
import pytest
from synopsis_parser import parse_synopsis


@pytest.fixture
def synopsis_file(tmp_path):
    content = """# Synopsis — Test
Generated: test

## Chapter 1 — Prologue

### Scene 1 — The Crossing [TYPE: ACTION] [POV: Omniscient Historical]

Napoleon's army retreats. The Cossacks pursue.

---

### Scene 2 — The Inheritance [TYPE: NON-ACTION] [POV: Omniscient Historical]

The Cossacks did not begin as soldiers.

---

## Chapter 2

### Scene 1 — The Concert [TYPE: MIXED] [POV: Hadeon Kovalenko]

Hadeon plays piano. The seizure comes.

---

### Scene 2 — The Border [TYPE: NON-ACTION] [POV: Yaroslav Kovalenko]

Yaroslav watches the Russian buildup.
"""
    path = tmp_path / "synopsis.md"
    path.write_text(content)
    return str(path)


def test_parse_chapter_count(synopsis_file):
    result = parse_synopsis(synopsis_file)
    assert len(result.chapters) == 2


def test_parse_scene_count(synopsis_file):
    result = parse_synopsis(synopsis_file)
    assert result.scene_count == 4


def test_parse_scene_metadata(synopsis_file):
    result = parse_synopsis(synopsis_file)
    sc1 = result.chapters[0].scenes[0]
    assert sc1.scene_number == 1
    assert sc1.title == "The Crossing"
    assert sc1.scene_type == "ACTION"
    assert sc1.pov == "Omniscient Historical"


def test_parse_scene_body(synopsis_file):
    result = parse_synopsis(synopsis_file)
    sc1 = result.chapters[0].scenes[0]
    assert "Napoleon" in sc1.body
    assert "Cossacks" in sc1.body


def test_parse_chapter_title(synopsis_file):
    result = parse_synopsis(synopsis_file)
    assert result.chapters[0].title == "Prologue"
    assert result.chapters[1].title == ""


def test_all_scenes_property(synopsis_file):
    result = parse_synopsis(synopsis_file)
    all_sc = result.all_scenes
    assert len(all_sc) == 4
    assert all_sc[0].title == "The Crossing"
    assert all_sc[3].title == "The Border"


def test_position_in_chapter(synopsis_file):
    result = parse_synopsis(synopsis_file)
    ch1_scenes = result.chapters[0].scenes
    assert ch1_scenes[0].position_in_chapter == 1
    assert ch1_scenes[1].position_in_chapter == 2


def test_parse_real_synopsis():
    """Parse the actual approved synopsis."""
    path = "/anpd/v25/series/hadeons_cossacks/b01/out/synopsis_20260511_0600.md"
    if not os.path.exists(path):
        pytest.skip("Approved synopsis not present")
    result = parse_synopsis(path)
    assert len(result.chapters) == 8
    assert result.scene_count == 32


def test_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        parse_synopsis("/nonexistent/synopsis.md")


def test_focus_tag_parsed_as_pov(tmp_path):
    """[FOCUS: X] must parse into the pov field, same as [POV: X]."""
    content = """# Synopsis — Test

## Chapter 1 — Test

### Scene 1 — Raid [TYPE: ACTION] [FOCUS: Hank Reyes]

Hank moves.
"""
    path = tmp_path / "synopsis.md"
    path.write_text(content)
    result = parse_synopsis(str(path))
    assert result.scene_count == 1
    assert result.chapters[0].scenes[0].pov == "Hank Reyes"


def test_no_third_tag_parses(tmp_path):
    """Scene headers with only [TYPE: X] and no POV/FOCUS must still parse."""
    content = """# Synopsis — Test

## Chapter 1 — Test

### Scene 1 — The Safehouse [TYPE: NON-ACTION]

Lena waits.
"""
    path = tmp_path / "synopsis.md"
    path.write_text(content)
    result = parse_synopsis(str(path))
    assert result.scene_count == 1
    assert result.chapters[0].scenes[0].pov == ""


def test_mode_tag_skipped(tmp_path):
    """[MODE: X] between TYPE and FOCUS must not break parsing."""
    content = """# Synopsis — Test

## Chapter 1 — Test

### Scene 53 — The Transfer [TYPE: ACTION] [MODE: SUSPENSE] [FOCUS: split Mia / Lena]

Transfer happens.
"""
    path = tmp_path / "synopsis.md"
    path.write_text(content)
    result = parse_synopsis(str(path))
    assert result.scene_count == 1
    assert result.chapters[0].scenes[0].pov == "split Mia / Lena"
