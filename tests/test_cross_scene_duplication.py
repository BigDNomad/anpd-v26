"""
Tests for MA-011 cross-scene duplication detector.

All tests synthetic — no fixture files required.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Add pipeline to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit_checks.cross_scene_duplication import (
    normalize_text,
    find_maximal_matches,
    split_assembled_manuscript,
    CrossSceneDuplication,
    RK_BASE,
    RK_MOD,
    DEFAULT_CLASS_A_THRESHOLD,
    DEFAULT_CLASS_B_THRESHOLD,
)
from audit_checks import ManuscriptArtifact, SceneText, BriefBundle


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_manuscript(scene_texts: list[str], tmpdir: str | None = None) -> ManuscriptArtifact:
    """Create a ManuscriptArtifact from a list of scene text strings."""
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp()
    scenes = []
    for i, text in enumerate(scene_texts, 1):
        scenes.append(SceneText(
            scene_number=i,
            text=text,
            file_path=os.path.join(tmpdir, f"sc_{i:03d}.md"),
        ))
    return ManuscriptArtifact(scenes=scenes, manuscript_dir=tmpdir)


def _make_passage(n_words: int, seed: str = "alpha") -> str:
    """Generate a passage of exactly n_words unique words."""
    words = []
    for i in range(n_words):
        words.append(f"{seed}{i}")
    return " ".join(words)


def _make_briefs(**config_overrides) -> BriefBundle:
    """Create a BriefBundle with optional cross_scene_duplication config."""
    bundle = BriefBundle()
    if config_overrides:
        bundle.book_config = {"cross_scene_duplication": config_overrides}
    return bundle


# ── Normalization tests (spec §4.1) ───────────────────────────────────────

class TestNormalization:

    def test_markdown_stripping(self):
        """Headers, blockquotes, and *** lines are stripped."""
        text = "# Chapter 1\n\nHello world.\n\n***\n\n> A blockquote.\n\nFoo bar."
        words, _ = normalize_text(text)
        assert "chapter" not in words
        assert "blockquote" not in words
        assert "hello" in words
        assert "world" in words
        assert "foo" in words
        assert "bar" in words

    def test_lowercasing_and_punctuation(self):
        """Text is lowercased and punctuation stripped."""
        text = 'He said, "HELLO, World!" — and left.'
        words, _ = normalize_text(text)
        assert "hello" in words
        assert "world" in words
        assert "HELLO" not in words
        # No punctuation-only tokens
        assert all(w.isalnum() for w in words)

    def test_whitespace_collapse(self):
        """Multiple spaces and tabs collapse to single space boundaries."""
        text = "the   quick\t\tbrown    fox"
        words, _ = normalize_text(text)
        assert words == ["the", "quick", "brown", "fox"]

    def test_word_to_line_tracking(self):
        """Word positions map back to correct source lines."""
        text = "line one text\n\nline three text"
        words, word_to_line = normalize_text(text)
        # "line" at position 0 is on line 1
        assert word_to_line[0] == 1
        # "text" at position 2 is on line 1
        assert word_to_line[2] == 1
        # "line" at position 3 is on line 3
        assert word_to_line[3] == 3


# ── Hash collision insurance ──────────────────────────────────────────────

class TestHashCollisionInsurance:

    def test_literal_comparison_rejects_false_match(self):
        """Even if hashes collide, literal comparison prevents false match."""
        # Create two 25-word sequences that are different
        passage_a = _make_passage(25, seed="alpha")
        passage_b = _make_passage(25, seed="beta")

        # Add unique context around them
        scene_a_text = f"intro words here. {passage_a} outro words there."
        scene_b_text = f"other intro. {passage_b} other outro."

        words_a, _ = normalize_text(scene_a_text)
        words_b, _ = normalize_text(scene_b_text)

        matches = find_maximal_matches(words_a, words_b, 25)
        # Should find no matches since the passages are different
        assert len(matches) == 0


# ── Threshold boundary tests (spec §4.3) ─────────────────────────────────

class TestThresholdBoundaries:

    def test_exactly_40_words_is_class_a(self):
        """A 40-word match is Class A."""
        passage = _make_passage(40)
        # Use unique boundary words that won't match across scenes
        pre_a = _make_passage(10, seed="preA")
        post_a = _make_passage(10, seed="postA")
        pre_b = _make_passage(10, seed="preB")
        post_b = _make_passage(10, seed="postB")
        scene_a = f"{pre_a} {passage} {post_a}"
        scene_b = f"{pre_b} {passage} {post_b}"

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) == 1
        assert "40 words" in class_a[0].description

    def test_exactly_39_words_is_class_b(self):
        """A 39-word match is Class B, not Class A."""
        passage = _make_passage(39)
        pre_a = _make_passage(10, seed="preA")
        post_a = _make_passage(10, seed="postA")
        pre_b = _make_passage(10, seed="preB")
        post_b = _make_passage(10, seed="postB")
        scene_a = f"{pre_a} {passage} {post_a}"
        scene_b = f"{pre_b} {passage} {post_b}"

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        class_b = [f for f in findings if f.severity == "CLASS_B"]
        assert len(class_a) == 0
        assert len(class_b) == 1

    def test_exactly_25_words_is_class_b(self):
        """A 25-word match is Class B (the floor)."""
        passage = _make_passage(25)
        pre_a = _make_passage(10, seed="preA")
        post_a = _make_passage(10, seed="postA")
        pre_b = _make_passage(10, seed="preB")
        post_b = _make_passage(10, seed="postB")
        scene_a = f"{pre_a} {passage} {post_a}"
        scene_b = f"{pre_b} {passage} {post_b}"

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        class_b = [f for f in findings if f.severity == "CLASS_B"]
        assert len(class_b) == 1

    def test_exactly_24_words_not_reported(self):
        """A 24-word match is below threshold and not reported."""
        passage = _make_passage(24)
        pre_a = _make_passage(10, seed="preA")
        post_a = _make_passage(10, seed="postA")
        pre_b = _make_passage(10, seed="preB")
        post_b = _make_passage(10, seed="postB")
        scene_a = f"{pre_a} {passage} {post_a}"
        scene_b = f"{pre_b} {passage} {post_b}"

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        assert len(findings) == 0


# ── Deduplication test (spec §4.2 step 5) ────────────────────────────────

class TestDeduplication:

    def test_222_word_match_reports_once(self):
        """A 222-word match must report as one finding, not 198 sub-matches."""
        passage = _make_passage(222)
        pre_a = _make_passage(10, seed="preA")
        post_a = _make_passage(10, seed="postA")
        pre_b = _make_passage(10, seed="preB")
        post_b = _make_passage(10, seed="postB")
        scene_a = f"{pre_a} {passage} {post_a}"
        scene_b = f"{pre_b} {passage} {post_b}"

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) == 1
        assert "222 words" in class_a[0].description


# ── Pair window test (spec §5) ───────────────────────────────────────────

class TestPairWindow:

    def test_pair_window_2(self):
        """pair_window=2 compares (i,i+1) and (i,i+2) but not (i,i+3)."""
        passage = _make_passage(50)

        # Put the same passage in scene 1 and scene 4 (distance 3)
        scenes = [
            f"Unique scene one. {passage} End scene one.",
            f"Unique scene two content only no match here at all.",
            f"Unique scene three content only no match here at all.",
            f"Unique scene four. {passage} End scene four.",
        ]

        ms = _make_manuscript(scenes)
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        # Distance 3 exceeds window 2, so no findings
        assert len(findings) == 0

        # Now put it in scenes 1 and 3 (distance 2) — should find it
        scenes2 = [
            f"Unique scene one. {passage} End scene one.",
            f"Unique scene two content only no match here at all.",
            f"Unique scene three. {passage} End scene three.",
        ]

        ms2 = _make_manuscript(scenes2)
        findings2 = checker.run(ms2, _make_briefs())
        assert len(findings2) == 1


# ── Input mode tests ─────────────────────────────────────────────────────

class TestInputModes:

    def test_mode_a_per_scene_files(self):
        """Mode A: per-scene files in a directory produce correct findings."""
        passage = _make_passage(50)
        scene_a = f"Unique alpha content. {passage} Unique alpha end."
        scene_b = f"Unique beta content. {passage} Unique beta end."

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        assert len(findings) == 1
        assert findings[0].severity == "CLASS_A"
        assert 1 in findings[0].scene_numbers
        assert 2 in findings[0].scene_numbers

    def test_mode_b_assembled_manuscript(self):
        """Mode B: splitting an assembled manuscript on *** and # Chapter N."""
        passage = _make_passage(50)
        assembled = (
            "# Chapter 1\n\n"
            f"Unique alpha content. {passage} Unique alpha end.\n\n"
            "***\n\n"
            f"Unique beta content. {passage} Unique beta end.\n\n"
            "# Chapter 2\n\n"
            "Unique gamma content only."
        )

        segments = split_assembled_manuscript(assembled)
        assert len(segments) >= 2  # At least 2 scenes from the split

        scenes = []
        for i, (text, start_line) in enumerate(segments, 1):
            scenes.append(SceneText(
                scene_number=i,
                text=text,
                file_path="assembled.md",
            ))

        ms = ManuscriptArtifact(scenes=scenes, manuscript_dir=tempfile.mkdtemp())
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) == 1


# ── Cross-boundary match test ────────────────────────────────────────────

class TestCrossBoundary:

    def test_cross_boundary_match(self):
        """Match spanning trailing portion of A + leading portion of B,
        duplicated together elsewhere, is detected."""
        # Create a passage that appears at the end of scene 1 and start of scene 2
        # and also appears entirely in scene 3
        passage = _make_passage(50)

        # Scene 1: unique content + first 25 words of passage
        words = passage.split()
        half1 = " ".join(words[:25])
        half2 = " ".join(words[25:])

        scene_1 = f"Unique intro scene one. {half1}"
        scene_2 = f"{half2} Unique outro scene two."
        # Scene 3 has the full passage — will match 25 words with scene 1
        # and 25 words with scene 2 (within pair_window=2)
        scene_3 = f"Unique intro scene three. {passage} Unique outro scene three."

        ms = _make_manuscript([scene_1, scene_2, scene_3])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        # Should detect matches between scene 1-3 (25 words = Class B)
        # and scene 2-3 (25 words = Class B)
        assert len(findings) >= 1


# ── Config disabled test ─────────────────────────────────────────────────

class TestConfigDisabled:

    def test_enabled_false_returns_empty(self):
        """enabled: false produces empty findings list."""
        passage = _make_passage(50)
        scene_a = f"Unique alpha. {passage} End alpha."
        scene_b = f"Unique beta. {passage} End beta."

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs(enabled=False))

        assert len(findings) == 0


# ── Multiple matches between same pair ───────────────────────────────────

class TestMultipleMatches:

    def test_three_separate_matches(self):
        """Three separate 50-word matches between scenes A and B produce
        three findings, not one merged."""
        p1 = _make_passage(50, seed="first")
        p2 = _make_passage(50, seed="second")
        p3 = _make_passage(50, seed="third")

        filler_a = _make_passage(30, seed="fillerA")
        filler_b = _make_passage(30, seed="fillerB")

        scene_a = f"Unique alpha start. {p1} {filler_a} {p2} {filler_a} {p3} Unique alpha end."
        scene_b = f"Unique beta start. {p1} {filler_b} {p2} {filler_b} {p3} Unique beta end."

        ms = _make_manuscript([scene_a, scene_b])
        checker = CrossSceneDuplication()
        findings = checker.run(ms, _make_briefs())

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) == 3
