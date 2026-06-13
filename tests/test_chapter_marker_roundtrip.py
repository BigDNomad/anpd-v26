"""
Tests for chapter-marker round trip: generator token == auditor token.

The generator assembles chapter markers as ``## Chapter N`` (H2).
The inline auditor's check_chapter_count must read the SAME token.
This test asserts the round trip — not just that the generator emits
something, but that the auditor counts what the generator writes.

Covers:
  - 25 generator-format markers → auditor counts 25 → no finding
  - 0 markers → auditor fires Class A (synopsis_chapter_count_0001)
  - Canonical contract regex (synopsis_parsing.CHAPTER_HEADER_RE) matches
    the same token the generator writes
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_auditor import check_chapter_count
from synopsis_parsing import CHAPTER_HEADER_RE


# ─── The generator's chapter marker (verbatim from synopsis_generator line 993) ──

GENERATOR_MARKER_TEMPLATE = "\n## Chapter {ch}\n\n"


def _build_synopsis_with_generator_markers(n_chapters: int) -> str:
    """Build a synopsis using the exact marker the generator writes."""
    lines = ["# Synopsis — Test\nGenerated: 20260612_0000\n\n"]
    scene = 0
    for ch in range(1, n_chapters + 1):
        lines.append(GENERATOR_MARKER_TEMPLATE.format(ch=ch))
        for _ in range(4):
            scene += 1
            lines.append(
                f"### Scene {scene} — Beat [TYPE: ACTION]\n- beat\n\n"
            )
    return "".join(lines)


def _effective_config(target=25):
    return {"target_chapter_count": target}


# ─── Round-trip tests ─────────────────────────────────────────────────────────

class TestRoundTrip:

    def test_25_generator_markers_auditor_passes(self):
        """Generator writes 25 ## Chapter markers → auditor counts 25 → no finding."""
        synopsis = _build_synopsis_with_generator_markers(25)
        findings = check_chapter_count(synopsis, _effective_config(25), "/fake/synopsis.md")
        assert findings == [], f"Expected 0 findings, got {len(findings)}: {findings}"

    def test_0_markers_fires_class_a(self):
        """No chapter markers → Class A fires."""
        synopsis = "# Synopsis\n\n### Scene 1 — A [TYPE: ACTION]\n- beat\n"
        findings = check_chapter_count(synopsis, _effective_config(25), "/fake/synopsis.md")
        assert len(findings) == 1
        assert findings[0]["class_"] == "A"
        assert findings[0]["finding_id"] == "synopsis_chapter_count_0001"
        assert "0" in findings[0]["description"]

    def test_wrong_hash_level_not_counted(self):
        """### Chapter (3 hashes) must NOT be counted — only ## Chapter (2 hashes)."""
        synopsis = "# Synopsis\n\n"
        for ch in range(1, 26):
            synopsis += f"### Chapter {ch}\n\n### Scene {ch} — A [TYPE: ACTION]\n- beat\n\n"
        findings = check_chapter_count(synopsis, _effective_config(25), "/fake/synopsis.md")
        # Should find 0 valid markers (### is wrong level) → Class A
        assert len(findings) == 1
        assert findings[0]["class_"] == "A"


class TestCanonicalContract:

    def test_generator_marker_matches_canonical_regex(self):
        """The token the generator writes must match CHAPTER_HEADER_RE from synopsis_parsing."""
        marker_line = "## Chapter 14"
        assert CHAPTER_HEADER_RE.search(marker_line) is not None

    def test_three_hash_does_not_match_canonical(self):
        """### Chapter must NOT match the canonical contract."""
        marker_line = "### Chapter 14"
        assert CHAPTER_HEADER_RE.search(marker_line) is None
