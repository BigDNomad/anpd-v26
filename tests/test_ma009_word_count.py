"""
Tests for MA-009 word_count_discipline.

Covers:
  - Word count: at target, below floor, above ceiling, boundaries, soft deviation
  - Scene count: below floor, above ceiling, soft deviation
  - Book config overrides
  - Module auto-discovery
  - Mandate calibration anchor
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    REGISTRY,
    discover_and_register,
)
from audit_checks.word_count_discipline import (
    WordCountDiscipline,
    MA009_DEFAULTS,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_manuscript(total_words, scene_count):
    """Create a manuscript with approximately total_words spread across scene_count scenes."""
    words_per_scene = max(1, total_words // scene_count)
    scenes = []
    remaining = total_words
    for i in range(1, scene_count + 1):
        if i == scene_count:
            w = remaining
        else:
            w = words_per_scene
            remaining -= w
        text = " ".join(["word"] * w)
        scenes.append(SceneText(scene_number=i, text=text, file_path=f"/fake/sc_{i:03d}.md"))
    return ManuscriptArtifact(scenes=scenes, manuscript_dir="/fake")


def _make_briefs(**kwargs):
    defaults = {"series_bible": {}, "character_profiles": {"characters": []}}
    defaults.update(kwargs)
    return BriefBundle(**defaults)


def _run(total_words, scene_count, briefs=None):
    ms = _make_manuscript(total_words, scene_count)
    if briefs is None:
        briefs = _make_briefs()
    check = WordCountDiscipline()
    return check.run(ms, briefs)


# ─── Word Count ───────────────────────────────────────────────────────────────

class TestWordCount:

    def test_at_target_no_finding(self):
        """85K words, 100 scenes -> 0 findings."""
        findings = _run(85_000, 100)
        assert len(findings) == 0

    def test_below_floor_class_a(self):
        """60K words -> CLASS_A."""
        findings = _run(60_000, 100)
        word_findings = [f for f in findings if "Word count" in f.description]
        assert any(f.severity == "CLASS_A" for f in word_findings)

    def test_above_ceiling_class_a(self):
        """100K words -> CLASS_A."""
        findings = _run(100_000, 100)
        word_findings = [f for f in findings if "Word count" in f.description]
        assert any(f.severity == "CLASS_A" for f in word_findings)

    def test_at_floor_boundary_no_class_a(self):
        """Exactly 65K words -> no CLASS_A (at boundary, not below)."""
        findings = _run(65_000, 100)
        word_a = [f for f in findings if "Word count" in f.description and f.severity == "CLASS_A"]
        assert len(word_a) == 0

    def test_at_ceiling_boundary_no_class_a(self):
        """Exactly 95K words -> no CLASS_A (at boundary, not above)."""
        findings = _run(95_000, 100)
        word_a = [f for f in findings if "Word count" in f.description and f.severity == "CLASS_A"]
        assert len(word_a) == 0

    def test_within_range_soft_deviation_class_b(self):
        """75K words (11.8% off target, within range) -> CLASS_B."""
        findings = _run(75_000, 100)
        word_b = [f for f in findings if "Word count" in f.description and f.severity == "CLASS_B"]
        assert len(word_b) >= 1

    def test_within_range_small_deviation_no_finding(self):
        """88K words (3.5% off target) -> no finding."""
        findings = _run(88_000, 100)
        word_findings = [f for f in findings if "Word count" in f.description]
        assert len(word_findings) == 0


# ─── Scene Count ──────────────────────────────────────────────────────────────

class TestSceneCount:

    def test_scene_count_below_floor_class_a(self):
        """70 scenes -> CLASS_A."""
        findings = _run(85_000, 70)
        scene_findings = [f for f in findings if "Scene count" in f.description]
        assert any(f.severity == "CLASS_A" for f in scene_findings)

    def test_scene_count_above_ceiling_class_a(self):
        """130 scenes -> CLASS_A."""
        findings = _run(85_000, 130)
        scene_findings = [f for f in findings if "Scene count" in f.description]
        assert any(f.severity == "CLASS_A" for f in scene_findings)

    def test_scene_count_within_range_soft_deviation_class_b(self):
        """80 scenes (20% off target, within range) -> CLASS_B."""
        findings = _run(85_000, 80)
        scene_b = [f for f in findings if "Scene count" in f.description and f.severity == "CLASS_B"]
        assert len(scene_b) >= 1


# ─── Book Config Override ─────────────────────────────────────────────────────

class TestBookConfigOverride:

    def test_book_config_override(self):
        """book_config with custom floor -> uses overridden value."""
        # Default floor is 65K. Override to 50K.
        # 60K would be CLASS_A with defaults, but passes with 50K floor.
        briefs = _make_briefs(book_config={"word_count_floor": 50_000})
        findings = _run(60_000, 100, briefs=briefs)
        word_a = [f for f in findings if "Word count" in f.description and f.severity == "CLASS_A"]
        assert len(word_a) == 0


# ─── Module Interface ────────────────────────────────────────────────────────

class TestModuleInterface:

    def test_has_required_interface(self):
        check = WordCountDiscipline()
        assert check.check_id == "MA-009-word-count-discipline"
        assert check.severity == "CLASS_A"
        assert hasattr(check, "run")

    def test_module_auto_discovered(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-009-word-count-discipline" in check_ids
        REGISTRY.clear()


# ─── Mandate Calibration Anchor ──────────────────────────────────────────────

class TestMandateCalibration:

    def test_mandate_calibration(self):
        """Mandate at 85,272 words / 100 scenes -> 0 CLASS_A findings."""
        from manuscript_auditor_v25 import load_manuscript, load_briefs

        cal_dir = "/anpd/v25/_calibration/mandate_v1_uncleaned_20260515/"
        if not os.path.isdir(cal_dir):
            pytest.skip("Calibration baseline not available")

        manuscript = load_manuscript(cal_dir)
        briefs = load_briefs(
            series_bible_path="/anpd/v25/series/black_tide/series_bible.json",
            character_profiles_path="/anpd/v25/series/black_tide/character_profiles.json",
        )

        check = WordCountDiscipline()
        findings = check.run(manuscript, briefs)

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) == 0, (
            f"Mandate should produce 0 CLASS_A findings. Got: "
            + "; ".join(f.description for f in class_a)
        )
