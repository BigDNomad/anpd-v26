"""
Tests for V25 manuscript_auditor — check-module architecture.

Covers:
  - ManuscriptArtifact / BriefBundle / Finding data structures
  - Scene-per-file and chapter-based loading
  - Check module registry and discovery
  - Report generation (JSON + markdown)
  - Orchestration (run_audit)
  - character_detail_consistency check module (unit tests with synthetic data)
  - Ported V24 tests (41 tests, adapted for V25 structures)
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Ensure pipeline is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manuscript_auditor_v25 as ma
from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    Finding,
    REGISTRY,
    register,
    discover_and_register,
)
from audit_checks.character_detail_consistency import (
    CharacterDetailConsistency,
    Claim,
    _deterministic_checks,
    _group_claims_by_character,
    _deduplicate_findings,
    _is_plausible_progression,
    detect_contradictions_llm,
    AGE_MIN_DAYS,
    PHYSICAL_MIN_DAYS,
)
from audit_checks._lib.timeline_extractor import (
    extract_timeline,
    elapsed_days_between,
    SceneTimeline,
    _deterministic_time_extract,
)
from audit_checks.character_name_registry import (
    CharacterNameRegistry,
    CharacterAppearance,
    build_canonical_roster,
    check_names_against_roster,
    _normalize_name,
    _group_appearances_by_name,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_manuscript_dir(tmp_path):
    """Create a temporary manuscript directory with scene files."""
    d = tmp_path / "manuscript"
    d.mkdir()
    return str(d)


@pytest.fixture
def tmp_chapters_dir(tmp_path):
    """Create a temporary chapters directory with chapter files."""
    d = tmp_path / "chapters"
    d.mkdir()
    return str(d)


@pytest.fixture
def tmp_output_dir(tmp_path):
    d = tmp_path / "audit_output"
    d.mkdir()
    return str(d)


def _write_scene(manuscript_dir, scene_num, text):
    path = os.path.join(manuscript_dir, f"sc_{scene_num:03d}.md")
    with open(path, "w") as f:
        f.write(text)
    return path


def _write_chapter(chapters_dir, ch_num, slug, text):
    path = os.path.join(chapters_dir, f"ch{ch_num:02d}_{slug}.md")
    with open(path, "w") as f:
        f.write(text)
    return path


def _make_manuscript(*scenes):
    """Build a ManuscriptArtifact from (scene_num, text) tuples."""
    return ManuscriptArtifact(
        scenes=[SceneText(scene_number=n, text=t, file_path=f"/fake/sc_{n:03d}.md")
                for n, t in scenes],
        manuscript_dir="/fake",
    )


def _make_briefs(**kwargs):
    return BriefBundle(**kwargs)


# ─── Data Structures ──────────────────────────────────────────────────────

class TestSceneText:

    def test_word_count_computed(self):
        s = SceneText(scene_number=1, text="one two three four five", file_path="/x")
        assert s.word_count == 5

    def test_explicit_word_count(self):
        s = SceneText(scene_number=1, text="one two", file_path="/x", word_count=99)
        assert s.word_count == 99


class TestManuscriptArtifact:

    def test_full_text(self):
        ms = _make_manuscript((1, "Scene one."), (2, "Scene two."))
        assert "Scene one." in ms.full_text()
        assert "Scene two." in ms.full_text()

    def test_scene_by_number(self):
        ms = _make_manuscript((1, "A"), (5, "B"))
        assert ms.scene_by_number(5).text == "B"
        assert ms.scene_by_number(3) is None

    def test_total_words(self):
        ms = _make_manuscript((1, "one two three"), (2, "four five"))
        assert ms.total_words() == 5


class TestFinding:

    def test_to_dict_minimal(self):
        f = Finding(check_id="X", severity="CLASS_A", scene_number=1, description="test")
        d = f.to_dict()
        assert d["check_id"] == "X"
        assert d["severity"] == "CLASS_A"
        assert d["scene_number"] == 1
        assert "line_number" not in d  # None omitted

    def test_to_dict_full(self):
        f = Finding(
            check_id="X", severity="CLASS_B", scene_number=None,
            description="test", evidence=["a", "b"],
            scene_numbers=[1, 2], suggested_fix="fix it",
        )
        d = f.to_dict()
        assert d["scene_numbers"] == [1, 2]
        assert d["evidence"] == ["a", "b"]
        assert d["suggested_fix"] == "fix it"


# ─── Scene-per-file Loading ───────────────────────────────────────────────

class TestLoadManuscriptScenes:

    def test_loads_scene_files(self, tmp_manuscript_dir):
        _write_scene(tmp_manuscript_dir, 1, "Scene one prose.")
        _write_scene(tmp_manuscript_dir, 2, "Scene two prose.")
        ms = ma.load_manuscript_scenes(tmp_manuscript_dir)
        assert len(ms.scenes) == 2
        assert ms.scenes[0].scene_number == 1
        assert ms.scenes[1].scene_number == 2

    def test_empty_dir(self, tmp_manuscript_dir):
        ms = ma.load_manuscript_scenes(tmp_manuscript_dir)
        assert ms.scenes == []

    def test_ignores_receipt_files(self, tmp_manuscript_dir):
        _write_scene(tmp_manuscript_dir, 1, "Prose.")
        # Write a receipt file that shouldn't be loaded
        with open(os.path.join(tmp_manuscript_dir, "sc_001_receipt.json"), "w") as f:
            f.write("{}")
        ms = ma.load_manuscript_scenes(tmp_manuscript_dir)
        assert len(ms.scenes) == 1

    def test_scene_numbers_parsed_correctly(self, tmp_manuscript_dir):
        _write_scene(tmp_manuscript_dir, 99, "Scene 99.")
        _write_scene(tmp_manuscript_dir, 100, "Scene 100.")
        ms = ma.load_manuscript_scenes(tmp_manuscript_dir)
        assert [s.scene_number for s in ms.scenes] == [99, 100]


# ─── Chapter-based Loading ────────────────────────────────────────────────

class TestLoadManuscriptChapters:

    def test_loads_chapters(self, tmp_chapters_dir):
        _write_chapter(tmp_chapters_dir, 1, "intro",
                       "## Scene 1 — A\nFirst scene.\n## Scene 2 — B\nSecond scene.")
        ms = ma.load_manuscript_chapters(tmp_chapters_dir)
        assert len(ms.scenes) == 2
        assert ms.scenes[0].scene_number == 1

    def test_no_scene_headings_uses_chapter_number(self, tmp_chapters_dir):
        _write_chapter(tmp_chapters_dir, 3, "interlude", "Just prose without headings.")
        ms = ma.load_manuscript_chapters(tmp_chapters_dir)
        assert len(ms.scenes) == 1
        assert ms.scenes[0].scene_number == 3

    def test_empty_dir(self, tmp_chapters_dir):
        ms = ma.load_manuscript_chapters(tmp_chapters_dir)
        assert ms.scenes == []


# ─── Auto-detect Loading ──────────────────────────────────────────────────

class TestLoadManuscript:

    def test_auto_detects_scenes(self, tmp_manuscript_dir):
        _write_scene(tmp_manuscript_dir, 1, "Prose.")
        ms = ma.load_manuscript(tmp_manuscript_dir)
        assert len(ms.scenes) == 1

    def test_auto_detects_chapters(self, tmp_chapters_dir):
        _write_chapter(tmp_chapters_dir, 1, "x", "## Scene 1\nProse.")
        ms = ma.load_manuscript(tmp_chapters_dir)
        assert len(ms.scenes) == 1

    def test_empty_dir(self, tmp_path):
        ms = ma.load_manuscript(str(tmp_path))
        assert ms.scenes == []


# ─── Brief Loading ─────────────────────────────────────────────────────────

class TestLoadBriefs:

    def test_loads_json_files(self, tmp_path):
        bible = tmp_path / "bible.json"
        bible.write_text(json.dumps({"key": "value"}))
        briefs = ma.load_briefs(series_bible_path=str(bible))
        assert briefs.series_bible == {"key": "value"}

    def test_missing_file_ok(self):
        briefs = ma.load_briefs(series_bible_path="/nonexistent/file.json")
        assert briefs.series_bible == {}

    def test_none_path_ok(self):
        briefs = ma.load_briefs()
        assert briefs.series_bible == {}


# ─── Check Module Registry ────────────────────────────────────────────────

class TestRegistry:

    def test_register_adds_to_registry(self):
        initial_len = len(REGISTRY)

        class FakeCheck:
            check_id = "FAKE-001"
            severity = "CLASS_C"
            description = "test"
            def run(self, manuscript, briefs):
                return []

        obj = FakeCheck()
        register(obj)
        assert obj in REGISTRY
        # Cleanup
        REGISTRY.remove(obj)

    def test_discover_finds_character_detail_consistency(self):
        # Clear registry
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-001-character-detail-consistency" in check_ids
        # Cleanup for other tests
        REGISTRY.clear()


# ─── Report Generation ────────────────────────────────────────────────────

class TestReportGeneration:

    def test_json_report_structure(self):
        ms = _make_manuscript((1, "text"))
        findings = [
            Finding(check_id="X", severity="CLASS_A", scene_number=1, description="bad"),
            Finding(check_id="X", severity="CLASS_B", scene_number=2, description="meh"),
        ]
        report = ma.generate_json_report(findings, ms, BriefBundle(), ["X"], 1.5)
        assert report["header"]["total_scenes"] == 1
        assert report["summary"]["class_a"] == 1
        assert report["summary"]["class_b"] == 1
        assert report["summary"]["blocks_publication"] is True
        assert len(report["all_findings"]) == 2
        assert "X" in report["findings_by_check"]

    def test_json_report_clean(self):
        ms = _make_manuscript((1, "text"))
        report = ma.generate_json_report([], ms, BriefBundle(), ["X"], 0.5)
        assert report["summary"]["total_findings"] == 0
        assert report["summary"]["blocks_publication"] is False

    def test_markdown_report_contains_findings(self):
        ms = _make_manuscript((1, "text"))
        findings = [
            Finding(check_id="X", severity="CLASS_A", scene_number=1,
                    description="bad thing", evidence=["ev1", "ev2"],
                    suggested_fix="fix it"),
        ]
        report = ma.generate_json_report(findings, ms, BriefBundle(), ["X"], 1.0)
        md = ma.generate_markdown_report(report)
        assert "# Manuscript Audit Report" in md
        assert "bad thing" in md
        assert "ev1" in md
        assert "PUBLICATION BLOCKED" in md

    def test_markdown_report_no_findings(self):
        ms = _make_manuscript((1, "text"))
        report = ma.generate_json_report([], ms, BriefBundle(), [], 0.1)
        md = ma.generate_markdown_report(report)
        assert "PUBLICATION BLOCKED" not in md


# ─── Orchestration ─────────────────────────────────────────────────────────

class TestRunAudit:

    def test_empty_manuscript_returns_error(self):
        ms = ManuscriptArtifact(scenes=[], manuscript_dir="/fake")
        briefs = BriefBundle()
        rc, findings = ma.run_audit(ms, briefs)
        assert rc == 2

    def test_no_checks_returns_clean(self):
        ms = _make_manuscript((1, "text"))
        briefs = BriefBundle()
        rc, findings = ma.run_audit(ms, briefs, checks=[])
        assert rc == 0
        assert findings == []

    def test_check_module_failure_produces_class_b(self):
        ms = _make_manuscript((1, "text"))
        briefs = BriefBundle()

        class FailingCheck:
            check_id = "FAIL-001"
            severity = "CLASS_A"
            description = "always fails"
            def run(self, manuscript, briefs):
                raise RuntimeError("boom")

        rc, findings = ma.run_audit(ms, briefs, checks=[FailingCheck()])
        assert rc == 0  # Module failure → CLASS_B, not CLASS_A
        assert any(f.severity == "CLASS_B" and "boom" in f.description for f in findings)

    def test_class_a_finding_returns_exit_1(self):
        ms = _make_manuscript((1, "text"))
        briefs = BriefBundle()

        class AlwaysFinds:
            check_id = "FIND-001"
            severity = "CLASS_A"
            description = "always finds"
            def run(self, manuscript, briefs):
                return [Finding(check_id="FIND-001", severity="CLASS_A",
                               scene_number=1, description="issue")]

        rc, findings = ma.run_audit(ms, briefs, checks=[AlwaysFinds()])
        assert rc == 1

    def test_writes_reports_to_output_dir(self, tmp_output_dir):
        ms = _make_manuscript((1, "text"))
        briefs = BriefBundle()

        class CleanCheck:
            check_id = "CLEAN-001"
            severity = "CLASS_C"
            description = "clean"
            def run(self, manuscript, briefs):
                return []

        rc, _ = ma.run_audit(ms, briefs, output_dir=tmp_output_dir, checks=[CleanCheck()])
        assert os.path.isfile(os.path.join(tmp_output_dir, "manuscript_audit_REPORT.json"))
        assert os.path.isfile(os.path.join(tmp_output_dir, "manuscript_audit_REPORT.md"))


# ─── Character Detail Consistency: Deterministic Checks ────────────────────

class TestDeterministicChecks:

    def test_detects_device_brand_contradiction(self):
        ms = _make_manuscript(
            (49, "He opened the ThinkPad and began to type."),
            (50, "She glanced at his MacBook screen."),
        )
        findings = _deterministic_checks(ms)
        assert len(findings) >= 1
        assert any("ThinkPad" in f.description and "MacBook" in f.description
                    for f in findings)

    def test_no_contradiction_single_brand(self):
        ms = _make_manuscript(
            (1, "He opened the ThinkPad."),
            (2, "He closed the ThinkPad."),
        )
        findings = _deterministic_checks(ms)
        device_findings = [f for f in findings if "brand" in f.description.lower()
                          or "device" in f.description.lower()]
        assert len(device_findings) == 0

    def test_no_contradiction_distant_scenes(self):
        ms = _make_manuscript(
            (1, "He opened the ThinkPad."),
            (50, "She used her MacBook."),  # distant, likely different devices
        )
        findings = _deterministic_checks(ms)
        # Distant scenes (>5 apart) should not flag
        device_findings = [f for f in findings if "ThinkPad" in f.description
                          and "MacBook" in f.description]
        assert len(device_findings) == 0

    def test_detects_rank_contradiction(self):
        ms = _make_manuscript(
            (21, "Capitán Vera surveyed the scene."),
            (45, "Major Vera gave the order."),
        )
        findings = _deterministic_checks(ms)
        rank_findings = [f for f in findings if "rank" in f.description.lower()
                        or "Rank" in f.description]
        assert len(rank_findings) >= 1

    def test_no_rank_contradiction_different_people(self):
        ms = _make_manuscript(
            (21, "Capitán Rodriguez stood at attention."),
            (22, "Major Williams entered the room."),
        )
        findings = _deterministic_checks(ms)
        # Different names → no contradiction
        rank_findings = [f for f in findings if "rank" in f.description.lower()
                        or "Rank" in f.description]
        assert len(rank_findings) == 0


# ─── Character Detail Consistency: LLM-based (mocked) ─────────────────────

class TestClaimExtraction:

    def test_group_claims_by_character(self):
        claims = [
            Claim("Hank Reyes", "PHYSICAL", "hair", "short", 1, "short hair"),
            Claim("hank reyes", "MATERIAL", "laptop", "ThinkPad", 2, "ThinkPad"),
            Claim("Mia Navarro", "PHYSICAL", "hair", "black", 3, "black hair"),
        ]
        groups = _group_claims_by_character(claims)
        assert len(groups) == 2
        assert len(groups["hank reyes"]) == 2
        assert len(groups["mia navarro"]) == 1


class TestDeduplication:

    def test_dedup_same_scenes(self):
        f1 = Finding(check_id="X", severity="CLASS_A", scene_number=None,
                     scene_numbers=[49, 50], description="device brand A")
        f2 = Finding(check_id="X", severity="CLASS_A", scene_number=None,
                     scene_numbers=[49, 50], description="device brand B")
        result = _deduplicate_findings([f1, f2])
        # Both kept because descriptions differ
        assert len(result) == 2

    def test_dedup_preserves_unique(self):
        f1 = Finding(check_id="X", severity="CLASS_A", scene_number=1,
                     description="issue one")
        f2 = Finding(check_id="X", severity="CLASS_A", scene_number=2,
                     description="issue two")
        result = _deduplicate_findings([f1, f2])
        assert len(result) == 2


class TestCharacterDetailConsistencyModule:

    def test_module_has_required_interface(self):
        check = CharacterDetailConsistency()
        assert hasattr(check, "check_id")
        assert hasattr(check, "severity")
        assert hasattr(check, "description")
        assert hasattr(check, "run")
        assert check.check_id == "MA-001-character-detail-consistency"
        assert check.severity == "CLASS_B"  # demoted 20260612 D-A(1)

    @patch("audit_checks.character_detail_consistency._call_llm")
    def test_run_with_no_claims(self, mock_llm):
        """When LLM returns no claims, only deterministic findings returned."""
        mock_llm.return_value = ""  # No claims extracted
        ms = _make_manuscript((1, "Clean prose with no character details."))
        briefs = BriefBundle()
        check = CharacterDetailConsistency()
        findings = check.run(ms, briefs)
        # Should return only deterministic findings (likely 0)
        assert all(f.check_id == "MA-001-character-detail-consistency" for f in findings)

    @patch("audit_checks.character_detail_consistency._call_llm")
    def test_run_catches_hair_contradiction(self, mock_llm):
        """Test full pipeline with mocked LLM responses."""
        # First call: extraction
        extraction_response = (
            '{"character": "Mia", "category": "PHYSICAL", "detail_key": "hair", '
            '"value": "short black hair", "scene_number": 7, "excerpt": "her short black hair"}\n'
            '{"character": "Mia", "category": "PHYSICAL", "detail_key": "hair", '
            '"value": "long dark hair", "scene_number": 39, "excerpt": "her long dark hair"}'
        )
        # Second call: contradiction detection
        contradiction_response = (
            '{"character": "Mia", "detail_key": "hair", '
            '"claim_a": {"value": "short black hair", "scene_number": 7, "excerpt": "her short black hair"}, '
            '"claim_b": {"value": "long dark hair", "scene_number": 39, "excerpt": "her long dark hair"}, '
            '"explanation": "hair described as short black in scene 7 but long dark in scene 39"}'
        )
        mock_llm.side_effect = [extraction_response, contradiction_response]

        ms = _make_manuscript(
            (7, "Mia ran a hand through her short black hair."),
            (39, "Her long dark hair fell across her shoulders."),
        )
        briefs = BriefBundle()
        check = CharacterDetailConsistency()
        findings = check.run(ms, briefs)

        hair_findings = [f for f in findings if "hair" in f.description.lower()]
        assert len(hair_findings) >= 1
        assert hair_findings[0].severity == "CLASS_B"  # demoted 20260612 D-A(1)
        assert 7 in hair_findings[0].scene_numbers or 39 in hair_findings[0].scene_numbers

    @patch("audit_checks.character_detail_consistency._call_llm")
    def test_run_catches_age_contradiction(self, mock_llm):
        extraction_response = (
            '{"character": "Funes", "category": "BIOGRAPHICAL", "detail_key": "daughters_ages", '
            '"value": "older daughter at university, younger in school", "scene_number": 12, '
            '"excerpt": "older daughter at university"}\n'
            '{"character": "Funes", "category": "BIOGRAPHICAL", "detail_key": "daughters_ages", '
            '"value": "both daughters at university", "scene_number": 55, '
            '"excerpt": "both daughters at university"}'
        )
        contradiction_response = (
            '{"character": "Funes", "detail_key": "daughters_ages", '
            '"claim_a": {"value": "older at university, younger in school", "scene_number": 12, '
            '"excerpt": "older daughter at university"}, '
            '"claim_b": {"value": "both at university", "scene_number": 55, '
            '"excerpt": "both daughters at university"}, '
            '"explanation": "younger daughter changes from school-age to university"}'
        )
        mock_llm.side_effect = [extraction_response, contradiction_response]

        ms = _make_manuscript(
            (12, "Funes thought of his daughters — the older one at university, the younger still in school."),
            (55, "Both daughters were at university now."),
        )
        briefs = BriefBundle()
        check = CharacterDetailConsistency()
        findings = check.run(ms, briefs)

        age_findings = [f for f in findings if "daughter" in f.description.lower()]
        assert len(age_findings) >= 1

    @patch("audit_checks.character_detail_consistency._call_llm")
    def test_run_no_contradictions(self, mock_llm):
        extraction_response = (
            '{"character": "Hank", "category": "PHYSICAL", "detail_key": "build", '
            '"value": "compact", "scene_number": 1, "excerpt": "compact build"}'
        )
        mock_llm.side_effect = [extraction_response, "NO_CONTRADICTIONS"]

        ms = _make_manuscript((1, "Hank's compact build made him hard to spot."))
        briefs = BriefBundle()
        check = CharacterDetailConsistency()
        findings = check.run(ms, briefs)

        # Only deterministic findings (should be 0 for clean text)
        llm_findings = [f for f in findings if "contradiction" in f.description.lower()]
        assert len(llm_findings) == 0

    @patch("audit_checks.character_detail_consistency._call_llm")
    def test_llm_failure_graceful(self, mock_llm):
        """If LLM fails entirely, check still returns deterministic findings."""
        mock_llm.side_effect = RuntimeError("API down")
        ms = _make_manuscript(
            (49, "He opened the ThinkPad."),
            (50, "He opened the MacBook."),
        )
        briefs = BriefBundle()
        check = CharacterDetailConsistency()
        findings = check.run(ms, briefs)
        # Deterministic check should still catch ThinkPad vs MacBook
        assert any("ThinkPad" in f.description for f in findings)


# ─── Ported V24 Tests (adapted for V25 structures) ────────────────────────
# These validate that V24's core patterns still work through V25's loaders.

class TestV24Compat_ChapterLoading:

    def test_empty_dir(self, tmp_chapters_dir):
        ms = ma.load_manuscript_chapters(tmp_chapters_dir)
        assert ms.scenes == []

    def test_basic_listing(self, tmp_chapters_dir):
        _write_chapter(tmp_chapters_dir, 1, "first", "## Scene 1\nx")
        _write_chapter(tmp_chapters_dir, 3, "third", "## Scene 3\nx")
        _write_chapter(tmp_chapters_dir, 2, "second", "## Scene 2\nx")
        ms = ma.load_manuscript_chapters(tmp_chapters_dir)
        assert sorted(s.scene_number for s in ms.scenes) == [1, 2, 3]

    def test_extracts_numbered_scenes(self, tmp_chapters_dir):
        _write_chapter(tmp_chapters_dir, 1, "x",
                       "## Scene 1 — A\nbody\n## Scene 5 — B\nbody")
        ms = ma.load_manuscript_chapters(tmp_chapters_dir)
        assert sorted(s.scene_number for s in ms.scenes) == [1, 5]

    def test_dedup_scene_numbers(self, tmp_chapters_dir):
        _write_chapter(tmp_chapters_dir, 1, "x",
                       "## Scene 1\nfirst\n## Scene 1 — Repeat\nsecond")
        ms = ma.load_manuscript_chapters(tmp_chapters_dir)
        # Both headings produce a scene entry (V25 doesn't dedup — different text)
        assert len(ms.scenes) == 2


class TestV24Compat_ReportFormat:
    """V24 outputs JSON to stdout. V25 does the same + files."""

    def test_json_report_is_valid(self):
        ms = _make_manuscript((1, "text"))
        report = ma.generate_json_report([], ms, BriefBundle(), ["test"], 0.1)
        # Should be JSON-serializable
        json_str = json.dumps(report)
        parsed = json.loads(json_str)
        assert "header" in parsed
        assert "summary" in parsed
        assert "all_findings" in parsed

    def test_findings_include_check_id_and_severity(self):
        findings = [Finding(check_id="X-001", severity="CLASS_A",
                           scene_number=1, description="test")]
        ms = _make_manuscript((1, "text"))
        report = ma.generate_json_report(findings, ms, BriefBundle(), ["X-001"], 0.1)
        f = report["all_findings"][0]
        assert f["check_id"] == "X-001"
        assert f["severity"] == "CLASS_A"

    def test_exit_code_1_on_class_a(self):
        ms = _make_manuscript((1, "text"))
        briefs = BriefBundle()

        class ClassACheck:
            check_id = "A"
            severity = "CLASS_A"
            description = "x"
            def run(self, manuscript, briefs):
                return [Finding(check_id="A", severity="CLASS_A",
                               scene_number=1, description="x")]

        rc, _ = ma.run_audit(ms, briefs, checks=[ClassACheck()])
        assert rc == 1

    def test_exit_code_0_on_clean(self):
        ms = _make_manuscript((1, "text"))
        briefs = BriefBundle()

        class CleanCheck:
            check_id = "C"
            severity = "CLASS_C"
            description = "x"
            def run(self, manuscript, briefs):
                return []

        rc, _ = ma.run_audit(ms, briefs, checks=[CleanCheck()])
        assert rc == 0


# ══════════════════════════════════════════════════════════════════════════════
# NEW TESTS — Dispatch 30
# ══════════════════════════════════════════════════════════════════════════════


# ─── Timeline Extractor ──────────────────────────────────────────────────────

class TestTimelineExtractorDeterministic:
    """Test deterministic (regex-based) time extraction."""

    def test_next_morning(self):
        scene = SceneText(scene_number=1, text="The next morning he woke early.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["relation_to_previous"] == "next_day"
        assert result["estimated_hours_since_previous"] == 12.0
        assert len(result["anchors"]) >= 1

    def test_two_days_later(self):
        scene = SceneText(scene_number=2, text="Two days later the rain stopped.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["relation_to_previous"] == "days_later"
        assert result["estimated_hours_since_previous"] == 48.0

    def test_that_evening(self):
        scene = SceneText(scene_number=3, text="That evening they met at the bar.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["relation_to_previous"] == "same_day"

    def test_midnight(self):
        scene = SceneText(scene_number=4, text="After midnight the city was quiet.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["time_of_day"] == "night"

    def test_afternoon_light(self):
        scene = SceneText(scene_number=5, text="The afternoon light came through the windows.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["time_of_day"] == "afternoon"

    def test_no_time_anchors(self):
        scene = SceneText(scene_number=6, text="He walked into the room and sat down.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["relation_to_previous"] == "unknown"
        assert result["estimated_hours_since_previous"] is None

    def test_a_week_later(self):
        scene = SceneText(scene_number=7, text="A week later the team regrouped.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["relation_to_previous"] == "weeks_later"
        assert result["estimated_hours_since_previous"] == 168.0

    def test_forty_eight_hours(self):
        scene = SceneText(scene_number=8, text="He said he needed forty-eight hours.", file_path="/x")
        result = _deterministic_time_extract(scene)
        assert result["estimated_hours_since_previous"] == 48.0


class TestTimelineExtraction:
    """Test the full timeline extraction pipeline (deterministic only)."""

    def test_basic_timeline(self):
        ms = _make_manuscript(
            (1, "Dawn broke over the city."),
            (2, "That evening they gathered."),
            (3, "The next morning she left."),
        )
        timelines = extract_timeline(ms, use_llm=False)
        assert len(timelines) == 3
        assert timelines[0].estimated_elapsed_days == 0.0
        assert timelines[0].scene_number == 1
        # Scene 2 is same day
        assert timelines[1].estimated_elapsed_days < 1.0
        # Scene 3 is next morning
        assert timelines[2].estimated_elapsed_days >= 0.5

    def test_empty_manuscript(self):
        ms = ManuscriptArtifact(scenes=[], manuscript_dir="/fake")
        timelines = extract_timeline(ms, use_llm=False)
        assert timelines == []

    def test_single_scene(self):
        ms = _make_manuscript((1, "Just one scene."))
        timelines = extract_timeline(ms, use_llm=False)
        assert len(timelines) == 1
        assert timelines[0].estimated_elapsed_days == 0.0

    def test_elapsed_days_between(self):
        timelines = [
            SceneTimeline(scene_number=1, estimated_elapsed_days=0.0),
            SceneTimeline(scene_number=2, estimated_elapsed_days=1.0),
            SceneTimeline(scene_number=3, estimated_elapsed_days=5.0),
        ]
        assert elapsed_days_between(timelines, 1, 3) == 5.0
        assert elapsed_days_between(timelines, 2, 3) == 4.0
        assert elapsed_days_between(timelines, 1, 2) == 1.0

    def test_elapsed_days_between_missing(self):
        timelines = [
            SceneTimeline(scene_number=1, estimated_elapsed_days=0.0),
        ]
        assert elapsed_days_between(timelines, 1, 99) is None

    def test_consecutive_scenes_short_elapsed(self):
        """Consecutive scenes without time anchors should be close together."""
        ms = _make_manuscript(
            (25, "He thought about his daughters in Madrid."),
            (26, "She told him about the financial details."),
        )
        timelines = extract_timeline(ms, use_llm=False)
        elapsed = elapsed_days_between(timelines, 25, 26)
        assert elapsed is not None
        assert elapsed < 30  # should be well under a month


# ─── MA-001 Timeline-Aware Contradiction Logic ──────────────────────────────

class TestPlausibleProgression:
    """Test _is_plausible_progression for different contradiction types."""

    def _make_timelines(self, scene_a, days_a, scene_b, days_b):
        return [
            SceneTimeline(scene_number=scene_a, estimated_elapsed_days=days_a),
            SceneTimeline(scene_number=scene_b, estimated_elapsed_days=days_b),
        ]

    def test_age_contradiction_short_window_not_plausible(self):
        """Age contradiction in 1-day window: NOT plausible → should flag."""
        timelines = self._make_timelines(25, 10.0, 26, 10.5)
        contradiction = {
            "character": "Funes",
            "detail_key": "daughters_ages",
            "claim_a": {"scene_number": 25, "value": "older at university, younger in school"},
            "claim_b": {"scene_number": 26, "value": "both at university"},
        }
        assert _is_plausible_progression(contradiction, timelines) is False

    def test_age_contradiction_long_window_plausible(self):
        """Age contradiction over 2 years: plausible → should NOT flag."""
        timelines = self._make_timelines(25, 0.0, 26, 730.0)
        contradiction = {
            "character": "Funes",
            "detail_key": "daughters_ages",
            "claim_a": {"scene_number": 25, "value": "older at university, younger in school"},
            "claim_b": {"scene_number": 26, "value": "both at university"},
        }
        assert _is_plausible_progression(contradiction, timelines) is True

    def test_location_always_flagged(self):
        """Location contradictions should always be flagged regardless of time."""
        timelines = self._make_timelines(10, 0.0, 50, 500.0)
        contradiction = {
            "detail_key": "location",
            "claim_a": {"scene_number": 10},
            "claim_b": {"scene_number": 50},
        }
        assert _is_plausible_progression(contradiction, timelines) is False

    def test_hair_short_window_not_plausible(self):
        """Hair change in 1 week: NOT plausible."""
        timelines = self._make_timelines(7, 5.0, 39, 12.0)
        contradiction = {
            "detail_key": "hair",
            "claim_a": {"scene_number": 7},
            "claim_b": {"scene_number": 39},
        }
        assert _is_plausible_progression(contradiction, timelines) is False

    def test_hair_long_window_plausible(self):
        """Hair change over 6 months: plausible."""
        timelines = self._make_timelines(7, 0.0, 39, 180.0)
        contradiction = {
            "detail_key": "hair",
            "claim_a": {"scene_number": 7},
            "claim_b": {"scene_number": 39},
        }
        assert _is_plausible_progression(contradiction, timelines) is True

    def test_no_timelines_flags_everything(self):
        """Without timelines, all contradictions should be flagged."""
        contradiction = {
            "detail_key": "daughters_ages",
            "claim_a": {"scene_number": 25},
            "claim_b": {"scene_number": 26},
        }
        assert _is_plausible_progression(contradiction, None) is False

    def test_unknown_detail_key_flags(self):
        """Unknown detail keys should be flagged (conservative)."""
        timelines = self._make_timelines(1, 0.0, 2, 500.0)
        contradiction = {
            "detail_key": "favorite_color",
            "claim_a": {"scene_number": 1},
            "claim_b": {"scene_number": 2},
        }
        assert _is_plausible_progression(contradiction, timelines) is False

    def test_daughter_keyword_in_detail_key(self):
        """'daughter' in detail_key should trigger age-based bounds."""
        timelines = self._make_timelines(25, 0.0, 26, 0.5)
        contradiction = {
            "detail_key": "funes_daughter_status",
            "claim_a": {"scene_number": 25},
            "claim_b": {"scene_number": 26},
        }
        assert _is_plausible_progression(contradiction, timelines) is False


class TestMA001TimelineAware:
    """Integration test: MA-001 with timeline-aware contradiction detection."""

    @patch("audit_checks.character_detail_consistency._call_llm")
    @patch("audit_checks._lib.timeline_extractor._call_llm")
    def test_funes_daughters_caught_with_timeline(self, mock_timeline_llm, mock_cdc_llm):
        """The Funes daughter age contradiction should be caught because scenes
        are close together (< 365 days elapsed)."""
        # Timeline LLM returns scene data (but deterministic will also work)
        mock_timeline_llm.return_value = ""

        # CDC extraction returns both claims
        extraction_response = (
            '{"character": "Funes", "category": "BIOGRAPHICAL", "detail_key": "daughters_ages", '
            '"value": "older daughter at university, younger in school", "scene_number": 25, '
            '"excerpt": "The older one had started at the university. The younger one was still in school"}\n'
            '{"character": "Funes", "category": "BIOGRAPHICAL", "detail_key": "daughters_ages", '
            '"value": "both daughters at university", "scene_number": 26, '
            '"excerpt": "He has two daughters in Madrid. Both in university."}'
        )
        # CDC contradiction detection flags it
        contradiction_response = (
            '{"character": "Funes", "detail_key": "daughters_ages", '
            '"claim_a": {"value": "older at university, younger in school", "scene_number": 25, '
            '"excerpt": "The older one had started at the university"}, '
            '"claim_b": {"value": "both at university", "scene_number": 26, '
            '"excerpt": "Both in university"}, '
            '"explanation": "younger daughter changes from school-age to university between consecutive scenes"}'
        )
        mock_cdc_llm.side_effect = [extraction_response, contradiction_response]

        ms = _make_manuscript(
            (25, "He thought about Madrid. His daughters were there. The older one had started at the university. The younger one was still in school."),
            (26, "He has two daughters in Madrid. Both in university. He's been moving money to a Spanish account."),
        )
        briefs = BriefBundle()
        check = CharacterDetailConsistency()
        findings = check.run(ms, briefs)

        # The timeline filter should NOT dismiss this because the elapsed time
        # between scenes 25 and 26 is far less than AGE_MIN_DAYS (365)
        daughter_findings = [f for f in findings if "daughter" in f.description.lower()]
        assert len(daughter_findings) >= 1, "Funes daughter ages contradiction should be caught"
        assert daughter_findings[0].severity == "CLASS_B"  # demoted 20260612 D-A(1)
        assert 25 in daughter_findings[0].scene_numbers
        assert 26 in daughter_findings[0].scene_numbers


# ─── MA-002 Character Name Registry ─────────────────────────────────────────

class TestNormalizeName:

    def test_basic(self):
        assert _normalize_name("Hank Reyes") == "hank reyes"

    def test_with_rank(self):
        assert _normalize_name("Capitán Vera") == "vera"

    def test_with_title(self):
        assert _normalize_name("Dr. Smith") == "smith"


class TestBuildCanonicalRoster:

    def test_builds_from_series_bible(self):
        briefs = BriefBundle(
            series_bible={
                "recurring_characters": [
                    {"name": "Hank Reyes", "role": "leader"},
                    {"name": "Lena Ibarra", "role": "operator"},
                ],
                "banned_names": ["Sarah", "Chen"],
            },
        )
        roster, banned = build_canonical_roster(briefs)
        assert "hank reyes" in roster
        assert "hank" in roster
        assert "reyes" in roster
        assert "lena ibarra" in roster
        assert "sarah" in banned
        assert "chen" in banned

    def test_builds_from_character_profiles(self):
        briefs = BriefBundle(
            character_profiles={
                "characters": [
                    {"name": "Rodrigo Funes", "role": "asset",
                     "relationships": {"Hank Reyes": "traded him"}},
                ],
            },
        )
        roster, banned = build_canonical_roster(briefs)
        assert "rodrigo funes" in roster
        assert "funes" in roster
        assert "hank reyes" in roster  # from relationships

    def test_builds_from_banned_phrases(self):
        briefs = BriefBundle(
            book_config={"names": ["Marcus Webb"]},
        )
        roster, banned = build_canonical_roster(briefs)
        assert "marcus webb" in banned
        assert "marcus" in banned
        assert "webb" in banned

    def test_builds_from_synopsis(self, tmp_path):
        synopsis_file = tmp_path / "synopsis.md"
        synopsis_file.write_text(
            "### Scene 1 [TYPE: ACTION] [POV: Lena Ibarra]\n- Hank briefs the team.\n",
            encoding="utf-8",
        )
        briefs = BriefBundle(synopsis_path=str(synopsis_file))
        roster, banned = build_canonical_roster(briefs)
        assert "lena ibarra" in roster


class TestGroupAppearancesByName:

    def test_groups_correctly(self):
        apps = [
            CharacterAppearance("Hank Reyes", 1, True, "Hank said"),
            CharacterAppearance("hank reyes", 2, True, "Hank walked"),
            CharacterAppearance("Mia", 3, True, "Mia watched"),
        ]
        groups = _group_appearances_by_name(apps)
        assert len(groups) == 2
        assert len(groups["hank reyes"]) == 2
        assert len(groups["mia"]) == 1


class TestCheckNamesAgainstRoster:

    def test_banned_name_flagged(self):
        apps = [CharacterAppearance("Sarah", 5, True, "Sarah said hello")]
        roster = {"hank", "lena"}
        banned = {"sarah"}
        findings = check_names_against_roster(apps, roster, banned)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_A"
        assert "Banned" in findings[0].description or "banned" in findings[0].description.lower()

    def test_invented_character_with_dialogue(self):
        apps = [CharacterAppearance("Torres", 8, True, "Torres shook his head")]
        roster = {"hank", "lena", "cole", "eddie", "mia"}
        banned = set()
        findings = check_names_against_roster(apps, roster, banned)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_A"
        assert "Invented" in findings[0].description or "invented" in findings[0].description.lower()

    def test_mentioned_only_is_class_b(self):
        apps = [CharacterAppearance("Torres", 8, False, "they mentioned Torres")]
        roster = {"hank", "lena"}
        banned = set()
        findings = check_names_against_roster(apps, roster, banned)
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_B"

    def test_roster_name_not_flagged(self):
        apps = [CharacterAppearance("Hank Reyes", 1, True, "Hank walked in")]
        roster = {"hank reyes", "hank", "reyes"}
        banned = set()
        findings = check_names_against_roster(apps, roster, banned)
        assert len(findings) == 0

    def test_partial_name_match(self):
        """First name match to roster should not flag."""
        apps = [CharacterAppearance("Hank", 1, True, "Hank said")]
        roster = {"hank reyes", "hank"}
        banned = set()
        findings = check_names_against_roster(apps, roster, banned)
        assert len(findings) == 0

    def test_multiple_invented_characters(self):
        apps = [
            CharacterAppearance("Torres", 8, True, "Torres shook his head"),
            CharacterAppearance("Mico", 8, True, "Mico was watching"),
            CharacterAppearance("Castillo", 8, True, "Castillo looked at the map"),
        ]
        roster = {"hank", "lena", "cole", "eddie"}
        banned = set()
        findings = check_names_against_roster(apps, roster, banned)
        assert len(findings) == 3
        names_flagged = {f.description.split("'")[1] for f in findings}
        assert "Torres" in names_flagged
        assert "Mico" in names_flagged
        assert "Castillo" in names_flagged


class TestCharacterNameRegistryModule:

    def test_module_has_required_interface(self):
        check = CharacterNameRegistry()
        assert hasattr(check, "check_id")
        assert hasattr(check, "severity")
        assert hasattr(check, "description")
        assert hasattr(check, "run")
        assert check.check_id == "MA-002-character-name-registry"
        assert check.severity == "CLASS_A"

    @patch("audit_checks.character_name_registry._call_llm")
    def test_run_catches_invented_character(self, mock_llm):
        """Invented character speaking dialogue should be CLASS_A."""
        mock_llm.return_value = (
            '{"name": "Torres", "scene_number": 8, "appears_directly": true, '
            '"evidence": "Torres shook his head"}\n'
            '{"name": "Hank", "scene_number": 8, "appears_directly": true, '
            '"evidence": "Hank walked the team through"}'
        )
        ms = _make_manuscript(
            (8, "Hank walked the team through the plan. Torres shook his head."),
        )
        briefs = BriefBundle(
            series_bible={
                "recurring_characters": [{"name": "Hank Reyes"}],
                "banned_names": [],
            },
            character_profiles={"characters": [{"name": "Hank Reyes"}]},
        )
        check = CharacterNameRegistry()
        findings = check.run(ms, briefs)
        torres_findings = [f for f in findings if "Torres" in f.description]
        assert len(torres_findings) >= 1
        assert torres_findings[0].severity == "CLASS_A"

    @patch("audit_checks.character_name_registry._call_llm")
    def test_run_catches_banned_name(self, mock_llm):
        """Banned name should be CLASS_A."""
        mock_llm.return_value = (
            '{"name": "Sarah", "scene_number": 10, "appears_directly": true, '
            '"evidence": "Sarah entered the room"}'
        )
        ms = _make_manuscript((10, "Sarah entered the room."))
        briefs = BriefBundle(
            series_bible={"recurring_characters": [], "banned_names": ["Sarah"]},
        )
        check = CharacterNameRegistry()
        findings = check.run(ms, briefs)
        sarah_findings = [f for f in findings if "Sarah" in f.description]
        assert len(sarah_findings) >= 1
        assert sarah_findings[0].severity == "CLASS_A"
        assert "banned" in sarah_findings[0].description.lower()

    @patch("audit_checks.character_name_registry._call_llm")
    def test_run_clean_manuscript(self, mock_llm):
        """All known characters → no findings."""
        mock_llm.return_value = (
            '{"name": "Hank", "scene_number": 1, "appears_directly": true, '
            '"evidence": "Hank said"}'
        )
        ms = _make_manuscript((1, "Hank said nothing."))
        briefs = BriefBundle(
            series_bible={"recurring_characters": [{"name": "Hank Reyes"}]},
            character_profiles={"characters": [{"name": "Hank Reyes"}]},
        )
        check = CharacterNameRegistry()
        findings = check.run(ms, briefs)
        assert len(findings) == 0

    def test_discover_registers_ma002(self):
        """MA-002 should be auto-discovered."""
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-002-character-name-registry" in check_ids
        REGISTRY.clear()


# ══════════════════════════════════════════════════════════════════════════════
# F-INT-7 — Synopsis Input Plumbing
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadBriefsSynopsis:
    """F-INT-7 test 1-2: load_briefs synopsis plumbing."""

    def test_synopsis_populates_fields(self, tmp_path):
        """load_briefs with valid synopsis populates text, path, sha256."""
        import hashlib
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("## Chapter 1\n### Scene 1 [TYPE: ACTION]\n- beat\n", encoding="utf-8")
        briefs = ma.load_briefs(synopsis_path=str(synopsis))
        assert briefs.synopsis_text.startswith("## Chapter 1")
        assert briefs.synopsis_path == str(synopsis)
        expected_sha = hashlib.sha256(briefs.synopsis_text.encode("utf-8")).hexdigest()
        assert briefs.synopsis_sha256 == expected_sha

    def test_synopsis_missing_raises(self, tmp_path):
        """load_briefs with missing synopsis raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="synopsis not found"):
            ma.load_briefs(synopsis_path=str(tmp_path / "nonexistent.md"))


class TestResolveSynopsis:
    """F-INT-7 test 3: _resolve_synopsis and main() exit 2."""

    def test_resolve_under_work_tree(self, tmp_path):
        """Manuscript under work/ with synopsis.md resolves correctly."""
        work = tmp_path / "work"
        ms_dir = work / "manuscript" / "manuscript_20260530_0133"
        ms_dir.mkdir(parents=True)
        synopsis = work / "synopsis.md"
        synopsis.write_text("synopsis content", encoding="utf-8")
        result = ma._resolve_synopsis(str(ms_dir))
        assert result == str(synopsis)

    def test_resolve_no_work_tree(self, tmp_path):
        """Path without work/ ancestor returns None."""
        result = ma._resolve_synopsis(str(tmp_path))
        assert result is None

    def test_main_exits_2_no_synopsis(self, tmp_path):
        """main() returns 2 when synopsis cannot be resolved."""
        ms_dir = tmp_path / "manuscript"
        ms_dir.mkdir()
        sc = ms_dir / "sc_001.md"
        sc.write_text("Scene 1.", encoding="utf-8")
        rc = ma.main(["--manuscript-dir", str(ms_dir)])
        assert rc == 2


class TestReportSynopsisProvenance:
    """F-INT-7 test 4: generate_json_report includes synopsis provenance."""

    def test_header_contains_synopsis_fields(self, tmp_path):
        import hashlib
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("test synopsis", encoding="utf-8")
        briefs = ma.load_briefs(synopsis_path=str(synopsis))
        ms = _make_manuscript((1, "text"))
        report = ma.generate_json_report([], ms, briefs, ["X"], 0.1)
        assert report["header"]["synopsis_path"] == str(synopsis)
        expected_sha = hashlib.sha256(b"test synopsis").hexdigest()
        assert report["header"]["synopsis_sha256"] == expected_sha

    def test_header_none_when_no_synopsis(self):
        briefs = BriefBundle()
        ms = _make_manuscript((1, "text"))
        report = ma.generate_json_report([], ms, briefs, ["X"], 0.1)
        assert report["header"]["synopsis_path"] is None
        assert report["header"]["synopsis_sha256"] is None


class TestSynopsisFidelityUsesCorrectSynopsis:
    """F-INT-7 test 5: regression — check uses briefs.synopsis_path, not hardcoded."""

    def test_airmen_synopsis_returns_airmen_specs(self, tmp_path):
        """A tiny 2-scene synopsis via briefs.synopsis_path returns matching specs."""
        from audit_checks._lib.synopsis_scene_types import load_scene_specs
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text(
            "## Chapter 1\n"
            "### Scene 1 — Airmen Opening [TYPE: ACTION] [FOCUS: Hank]\n"
            "- Hank lands in Caracas.\n"
            "### Scene 2 — Briefing [TYPE: NON-ACTION] [FOCUS: Lena]\n"
            "- Lena delivers intel.\n",
            encoding="utf-8",
        )
        specs = load_scene_specs(str(synopsis))
        assert len(specs) == 2
        assert specs[1].title == "Airmen Opening"
        assert specs[2].title == "Briefing"
        # NOT Black Tide titles
        for spec in specs.values():
            assert "Mandate" not in spec.title
            assert "Black Tide" not in spec.title
