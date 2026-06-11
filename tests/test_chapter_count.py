"""
Tests for MA-C11 chapter_count (25-chapter rule).

Covers:
  - exactly 25 chapters -> 0 findings
  - 24 chapters -> 1 CLASS_A finding
  - 26 chapters -> 1 CLASS_A finding
  - 0 chapters -> 1 CLASS_A finding
  - finding has suggested_fix populated
  - auto-discovery registers the module
  - interface conformance
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    REGISTRY,
    discover_and_register,
)
from audit_checks.chapter_count import ChapterCount, REQUIRED_CHAPTER_COUNT


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_synopsis(n_chapters: int) -> str:
    """Build a minimal chapter-organized synopsis with n chapter headers.

    Each chapter gets 2 scene headers so chapter_count != scene_count,
    ensuring these fixtures are never mistaken for scene-organized (1:1).
    """
    lines = ["# Synopsis — Test\n"]
    scene_num = 0
    for i in range(1, n_chapters + 1):
        lines.append(f"## Chapter {i} — Title {i}\n")
        scene_num += 1
        lines.append(f"### Scene {scene_num} — A Scene [TYPE: ACTION]\n")
        lines.append(f"- beat\n\n")
        scene_num += 1
        lines.append(f"### Scene {scene_num} — B Scene [TYPE: NON-ACTION]\n")
        lines.append(f"- beat\n\n")
    return "\n".join(lines)


def _make_manuscript(n=1):
    return ManuscriptArtifact(
        scenes=[
            SceneText(scene_number=i, text=f"Scene {i}.", file_path=f"/fake/sc_{i:03d}.md")
            for i in range(1, n + 1)
        ],
        manuscript_dir="/fake",
    )


def _make_briefs():
    return BriefBundle()


def _run_with_chapters(n_chapters: int, tmp_path: Path | None = None):
    """Run the check with a synthetic synopsis containing n chapters."""
    import tempfile
    synopsis_text = _make_synopsis(n_chapters)
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    synopsis_file = tmp_path / "synopsis.md"
    synopsis_file.write_text(synopsis_text, encoding="utf-8")
    check = ChapterCount()
    ms = _make_manuscript()
    briefs = BriefBundle(synopsis_path=str(synopsis_file))
    return check.run(ms, briefs)


# ─── Tests ──────────────────────────────────────────────────────────────────

class TestExactly25:

    def test_25_chapters_no_findings(self):
        findings = _run_with_chapters(25)
        assert len(findings) == 0


class Test24Chapters:

    def test_24_chapters_one_class_a(self):
        findings = _run_with_chapters(24)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_A"
        assert "24" in findings[0].description
        assert "25" in findings[0].description


class Test26Chapters:

    def test_26_chapters_one_class_a(self):
        findings = _run_with_chapters(26)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_A"
        assert "26" in findings[0].description


class TestZeroChapters:

    def test_no_chapters_one_class_a(self):
        """Degenerate input: no chapter headers at all -> CLASS_A, never silent-pass."""
        findings = _run_with_chapters(0)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_A"
        assert "0" in findings[0].description


class TestSuggestedFix:

    def test_finding_has_suggested_fix(self):
        findings = _run_with_chapters(30)
        assert len(findings) == 1
        assert findings[0].suggested_fix != ""
        assert "25" in findings[0].suggested_fix


class TestModuleInterface:

    def test_has_required_interface(self):
        check = ChapterCount()
        assert check.check_id == "MA-C11-chapter-count"
        assert check.severity == "CLASS_A"
        assert hasattr(check, "run")
        assert callable(check.run)

    def test_module_auto_discovered(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-C11-chapter-count" in check_ids
        REGISTRY.clear()
