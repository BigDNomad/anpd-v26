"""Tests for V25 outline_comparator."""
import json
import os
import pytest
from outline_comparator import (
    compare_outline_to_synopsis, _parse_synopsis_chapters,
    _parse_synopsis_scenes, _extract_named_characters,
    _check_scene_structural,
    verify_scene_count_match,
)


@pytest.fixture
def outline_file(tmp_path):
    content = """Chapter 1 (Prologue)
Napoleon's army retreats from Moscow.
The fierce fighting spirit of the Cossacks.

Chapter 2
Hadeon plays piano at a concert.
He has an epileptic seizure on stage.
He is suspended from the conservatory.
"""
    path = tmp_path / "outline.md"
    path.write_text(content)
    return str(path)


@pytest.fixture
def matching_synopsis(tmp_path):
    content = """# Synopsis — Test
Generated: test

## Chapter 1 — Prologue

### Scene 1 — The Retreat [TYPE: ACTION] [POV: narrator]
Napoleon's army retreats from Moscow through the frozen landscape.
The Cossacks pursue relentlessly.

### Scene 2 — The Spirit [TYPE: ACTION] [POV: narrator]
The fierce fighting spirit of the Cossack ancestors on display.

## Chapter 2 — The Concert

### Scene 3 — The Performance [TYPE: MIXED] [POV: Hadeon]
Hadeon plays piano at the conservatory concert.
The audience watches as he performs.

### Scene 4 — The Seizure [TYPE: ACTION] [POV: Hadeon]
He has an epileptic seizure on stage during the performance.
He falls from the bench. He is taken to the hospital.

### Scene 5 — The Suspension [TYPE: NON-ACTION] [POV: Hadeon]
He is suspended from the conservatory pending medical clearance.
"""
    path = tmp_path / "synopsis.md"
    path.write_text(content)
    return str(path)


@pytest.fixture
def intake_file(tmp_path, outline_file):
    data = {
        "book_number": 1,
        "title": "Test",
        "series": "Test",
        "total_chapter_count": 2,
        "total_scene_count": 5,
        "target_word_count": 100000,
        "outline_path": str(outline_file),
        "historical_window": {"start_date": "1812-01-01", "end_date": "2022-12-31"},
        "historical_anchors_in_scope": [],
        "historical_anchors_out_of_scope": ["Bucha massacre"],
    }
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_matching_synopsis_passes(outline_file, matching_synopsis, intake_file):
    result = compare_outline_to_synopsis(
        outline_path=outline_file,
        synopsis_path=matching_synopsis,
        intake_path=intake_file,
        use_llm=False,
    )
    class_a = [f for f in result.findings if f.severity == "CLASS_A"]
    chapter_mismatch = [f for f in class_a if "Chapter count" in f.message]
    assert len(chapter_mismatch) == 0


def test_missing_chapter_fails(outline_file, tmp_path, intake_file):
    content = """# Synopsis
## Chapter 1 — Prologue
### Scene 1 — Test [TYPE: ACTION] [POV: narrator]
Content about Napoleon.
"""
    synopsis_path = tmp_path / "bad_synopsis.md"
    synopsis_path.write_text(content)
    result = compare_outline_to_synopsis(
        outline_path=outline_file,
        synopsis_path=str(synopsis_path),
        intake_path=intake_file,
        use_llm=False,
    )
    assert not result.passed
    assert any("Chapter count" in f.message or "missing from synopsis" in f.message for f in result.findings)


def test_out_of_scope_anchor_detected(outline_file, matching_synopsis, intake_file, tmp_path):
    with open(matching_synopsis, 'r') as f:
        content = f.read()
    content += "\nThe Bucha massacre was revealed.\n"
    bad_synopsis = tmp_path / "oos_synopsis.md"
    bad_synopsis.write_text(content)
    result = compare_outline_to_synopsis(
        outline_path=outline_file,
        synopsis_path=str(bad_synopsis),
        intake_path=intake_file,
        use_llm=False,
    )
    oos_findings = [f for f in result.findings if "Out-of-scope" in f.message]
    assert len(oos_findings) > 0


def test_parse_synopsis_chapters():
    text = """# Synopsis
## Chapter 1
Scene content here.
## Chapter 2
More content.
## Chapter 3
Final content.
"""
    chapters = _parse_synopsis_chapters(text)
    assert len(chapters) == 3
    assert 1 in chapters
    assert 2 in chapters
    assert 3 in chapters


# ═══════════════════════════════════════════════════════════════════════════
# SG-2: verify_scene_count_match tests
# ═══════════════════════════════════════════════════════════════════════════

def _make_scene_outline(tmp_path, n_scenes):
    """Create a scene-organized outline with n_scenes scenes."""
    lines = ["# Test Outline\n"]
    for i in range(1, n_scenes + 1):
        lines.append(f"**Scene {i} — Scene Title {i}**")
        lines.append("*Action*")
        lines.append(f"Beat content for scene {i}.")
        lines.append("\n---\n")
    path = tmp_path / "outline.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _make_synopsis(tmp_path, n_scenes, filename="synopsis.md"):
    """Create a synopsis with n_scenes ### Scene headers."""
    lines = ["# Synopsis\n"]
    for i in range(1, n_scenes + 1):
        lines.append(f"### Scene {i} — Title [TYPE: ACTION] [POV: Hank]")
        lines.append(f"- Beat for scene {i}.\n")
    path = tmp_path / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


class TestVerifySceneCountMatch:
    def test_verify_scene_count_match_pass(self, tmp_path):
        outline = _make_scene_outline(tmp_path, 5)
        synopsis = _make_synopsis(tmp_path, 5)
        passed, msg = verify_scene_count_match(outline, synopsis)
        assert passed is True
        assert "5 scenes" in msg

    def test_verify_scene_count_match_fail_too_few(self, tmp_path):
        outline = _make_scene_outline(tmp_path, 5)
        synopsis = _make_synopsis(tmp_path, 3)
        passed, msg = verify_scene_count_match(outline, synopsis)
        assert passed is False
        assert "mismatch" in msg.lower()

    def test_verify_scene_count_match_fail_too_many(self, tmp_path):
        outline = _make_scene_outline(tmp_path, 5)
        synopsis = _make_synopsis(tmp_path, 12)
        passed, msg = verify_scene_count_match(outline, synopsis)
        assert passed is False
        assert "12" in msg

    def test_verify_scene_count_match_decomposition_pattern(self, tmp_path):
        """Sub-scenes (Scene 1, Scene 1.1, Scene 1.2) each count as a scene header."""
        outline = _make_scene_outline(tmp_path, 2)
        synopsis_text = (
            "# Synopsis\n"
            "### Scene 1 — Main [TYPE: ACTION]\n- Beat.\n"
            "### Scene 1.1 — Sub A [TYPE: ACTION]\n- Beat.\n"
            "### Scene 1.2 — Sub B [TYPE: ACTION]\n- Beat.\n"
            "### Scene 2 — Main [TYPE: ACTION]\n- Beat.\n"
        )
        syn_path = tmp_path / "synopsis.md"
        syn_path.write_text(synopsis_text, encoding="utf-8")
        passed, msg = verify_scene_count_match(str(outline), str(syn_path))
        assert passed is False
        assert "4" in msg  # 4 headers detected, expected 2

    def test_mandate_outline_against_current_synopsis_passes(self):
        """Post-SG-3: synopsis.md has 100 scenes matching outline. Gate passes."""
        outline = "/anpd/v25/series/black_tide/b01/work/outline.md"
        synopsis = "/anpd/v25/series/black_tide/b01/work/synopsis.md"
        if not os.path.exists(outline) or not os.path.exists(synopsis):
            pytest.skip("Mandate files not available")
        passed, msg = verify_scene_count_match(outline, synopsis)
        assert passed is True
        assert "100" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Per-scene scope tests (scene-organized outlines)
# ═══════════════════════════════════════════════════════════════════════════

def _make_scene_organized_outline(tmp_path, scenes):
    """Create a scene-organized outline.

    scenes: list of dicts with keys: num, title, type_tag, pov (optional), content
    """
    lines = ["# Test Outline\n"]
    for sc in scenes:
        lines.append(f"**Scene {sc['num']} — {sc['title']}**")
        tag = sc.get('type_tag', 'Action')
        if sc.get('pov'):
            tag += f" — POV: {sc['pov']}"
        lines.append(f"*{tag}*")
        lines.append("")
        lines.append(sc['content'])
        lines.append("\n---\n")
    path = tmp_path / "outline.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _make_scene_organized_synopsis(tmp_path, scenes, filename="synopsis.md"):
    """Create a synopsis matching scene-organized format.

    scenes: list of dicts with keys: num, title, type_tag, focus (optional), content
    """
    lines = ["# Synopsis\n"]
    for sc in scenes:
        header = f"### Scene {sc['num']} — {sc['title']} [TYPE: {sc.get('type_tag', 'ACTION')}]"
        if sc.get('focus'):
            header += f" [FOCUS: {sc['focus']}]"
        lines.append(header)
        lines.append(sc['content'])
        lines.append("")
    path = tmp_path / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _make_simple_intake(tmp_path, outline_path):
    data = {
        "book_number": 1, "title": "Test", "series": "Test",
        "total_chapter_count": 1, "total_scene_count": 3,
        "target_word_count": 100000,
        "outline_path": outline_path,
        "historical_window": {"start_date": "2026-01-01", "end_date": "2026-12-31"},
        "historical_anchors_in_scope": [],
        "historical_anchors_out_of_scope": [],
    }
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(data))
    return str(path)


class TestPerSceneScope:
    """Tests for per-scene 1:1 comparison on scene-organized outlines."""

    def test_parse_synopsis_scenes(self):
        text = (
            "# Synopsis\n"
            "## Chapter 1\n"
            "### Scene 1 — Title A [TYPE: ACTION]\n- Beat 1.\n"
            "### Scene 2 — Title B [TYPE: NON-ACTION]\n- Beat 2.\n"
            "### Scene 3 — Title C [TYPE: SUSPENSE]\n- Beat 3.\n"
        )
        scenes = _parse_synopsis_scenes(text)
        assert len(scenes) == 3
        assert 1 in scenes and 2 in scenes and 3 in scenes
        assert "Beat 1" in scenes[1]
        assert "Beat 3" in scenes[3]

    def test_scene_organized_matching_passes(self, tmp_path):
        """Scene-organized outline+synopsis with matching content passes Class A."""
        outline_scenes = [
            {"num": 1, "title": "The Raid", "type_tag": "Action", "pov": "Hank Reyes",
             "content": "Hank leads the raid on the compound. Delta clears the wing."},
            {"num": 2, "title": "The Meeting", "type_tag": "Non-action", "pov": "Lena Ibarra",
             "content": "Lena meets Marco at the apartment. She tells him about the assignment."},
        ]
        synopsis_scenes = [
            {"num": 1, "title": "Compound Assault", "type_tag": "ACTION", "focus": "Hank Reyes",
             "content": "- Hank leads the raid. Delta clears the compound wing.\n"},
            {"num": 2, "title": "The Conversation", "type_tag": "NON-ACTION", "focus": "Lena Ibarra",
             "content": "- Lena meets Marco. Assignment discussed.\n"},
        ]
        outline = _make_scene_organized_outline(tmp_path, outline_scenes)
        synopsis = _make_scene_organized_synopsis(tmp_path, synopsis_scenes)
        intake = _make_simple_intake(tmp_path, outline)

        result = compare_outline_to_synopsis(outline, synopsis, intake, use_llm=False)
        class_a = [f for f in result.findings if f.severity == "CLASS_A"]
        assert len(class_a) == 0
        assert result.passed is True

    def test_missing_scene_is_class_a(self, tmp_path):
        """A scene present in outline but missing from synopsis is Class A."""
        outline_scenes = [
            {"num": 1, "title": "The Raid", "type_tag": "Action",
             "content": "Hank leads the raid."},
            {"num": 2, "title": "The Meeting", "type_tag": "Non-action",
             "content": "Lena meets Marco."},
        ]
        synopsis_scenes = [
            {"num": 1, "title": "The Raid", "type_tag": "ACTION",
             "content": "- Hank leads the raid.\n"},
            # Scene 2 missing
        ]
        outline = _make_scene_organized_outline(tmp_path, outline_scenes)
        synopsis = _make_scene_organized_synopsis(tmp_path, synopsis_scenes)
        intake = _make_simple_intake(tmp_path, outline)

        result = compare_outline_to_synopsis(outline, synopsis, intake, use_llm=False)
        class_a = [f for f in result.findings if f.severity == "CLASS_A"]
        assert result.passed is False
        assert any("missing" in f.message.lower() for f in class_a)

    def test_type_mismatch_is_class_a(self, tmp_path):
        """ACTION in outline but NON-ACTION in synopsis is Class A."""
        outline_scenes = [
            {"num": 1, "title": "The Raid", "type_tag": "Action", "pov": "Hank",
             "content": "Hank storms the building."},
        ]
        synopsis_scenes = [
            {"num": 1, "title": "The Raid", "type_tag": "NON-ACTION", "focus": "Hank",
             "content": "- Hank storms the building.\n"},
        ]
        outline = _make_scene_organized_outline(tmp_path, outline_scenes)
        synopsis = _make_scene_organized_synopsis(tmp_path, synopsis_scenes)
        intake = _make_simple_intake(tmp_path, outline)

        result = compare_outline_to_synopsis(outline, synopsis, intake, use_llm=False)
        class_a = [f for f in result.findings if f.severity == "CLASS_A"]
        assert any("type mismatch" in f.message.lower() for f in class_a)

    def test_suspense_to_action_is_not_class_a(self, tmp_path):
        """SUSPENSE outline → ACTION synopsis should NOT be a Class A failure."""
        outline_scenes = [
            {"num": 1, "title": "Tension", "type_tag": "Suspense",
             "content": "Vera watches the building."},
        ]
        synopsis_scenes = [
            {"num": 1, "title": "Tension", "type_tag": "ACTION",
             "content": "- Vera watches the building.\n"},
        ]
        outline = _make_scene_organized_outline(tmp_path, outline_scenes)
        synopsis = _make_scene_organized_synopsis(tmp_path, synopsis_scenes)
        intake = _make_simple_intake(tmp_path, outline)

        result = compare_outline_to_synopsis(outline, synopsis, intake, use_llm=False)
        type_findings = [f for f in result.findings if "type mismatch" in f.message.lower()]
        assert len(type_findings) == 0

    def test_beat_coverage_is_class_b(self, tmp_path):
        """Uncovered beats produce Class B findings, not Class A."""
        outline_scenes = [
            {"num": 1, "title": "Complex Scene", "type_tag": "Action",
             "content": "Hank infiltrates the compound. He plants the charge. The explosion levels the east wing. Three guards are neutralized."},
        ]
        synopsis_scenes = [
            {"num": 1, "title": "Complex Scene", "type_tag": "ACTION",
             "content": "- Hank enters the compound.\n"},  # Missing most beats
        ]
        outline = _make_scene_organized_outline(tmp_path, outline_scenes)
        synopsis = _make_scene_organized_synopsis(tmp_path, synopsis_scenes)
        intake = _make_simple_intake(tmp_path, outline)

        result = compare_outline_to_synopsis(outline, synopsis, intake, use_llm=False)
        class_a = [f for f in result.findings if f.severity == "CLASS_A"]
        class_b = [f for f in result.findings if f.severity == "CLASS_B"]
        # Beat gaps are Class B, not Class A
        beat_class_a = [f for f in class_a if "beat" in f.message.lower()]
        assert len(beat_class_a) == 0
        # Gate still passes (no Class A)
        assert result.passed is True

    def test_extract_named_characters(self):
        text = "Hank Reyes leads the team. Lena Ibarra runs comms. Cole at the window."
        names = _extract_named_characters(text)
        assert "Hank" in names
        assert "Reyes" in names
        assert "Lena" in names
        assert "Cole" in names
