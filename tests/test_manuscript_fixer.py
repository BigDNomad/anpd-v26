"""Tests for ManuscriptFixer — Tier 1 surgical fixes + Tier 2 scene regeneration."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from audit_checks import Finding, BriefBundle
from manuscript_fixer import (
    ManuscriptFixer,
    FixerResult,
    classify_tier,
    load_synopsis_subscene,
    _group_tier_2_by_scene,
    _BANNED_NAME_REPLACEMENTS,
)


def _minimal_briefs() -> BriefBundle:
    return BriefBundle(
        series_bible={},
        character_profiles={"characters": []},
        book_config={},
        scene_map={},
        entity_ledger={},
    )


# ── Helpers ──────────────────────────────────────────────────────────────

def _finding(
    check_id: str = "MA-001-character-detail-consistency",
    severity: str = "CLASS_A",
    scene_number: int | None = 1,
    description: str = "test finding",
    evidence: list[str] | None = None,
    **kwargs,
) -> Finding:
    f = Finding(
        check_id=check_id,
        severity=severity,
        scene_number=scene_number,
        description=description,
        evidence=evidence or [],
    )
    # Attach extra attrs (e.g. suggested_tier)
    for k, v in kwargs.items():
        object.__setattr__(f, k, v)
    return f


def _setup_book(tmp_path: Path, scenes: dict[int, str] | None = None) -> Path:
    """Create a fake book directory with manuscript scenes."""
    book_dir = tmp_path / "series" / "test_series" / "b01"
    ms_dir = book_dir / "out" / "manuscript"
    ms_dir.mkdir(parents=True)
    if scenes:
        for sn, text in scenes.items():
            (ms_dir / f"sc_{sn:03d}.md").write_text(text, encoding="utf-8")
    return book_dir


# ═══════════════════════════════════════════════════════════════════════════
# Tier classification tests (no workspace needed)
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyTier:
    def test_classify_ma001_is_tier_2(self):
        f = _finding(check_id="MA-001-character-detail-consistency")
        assert classify_tier(f) == 2

    def test_classify_ma005_is_tier_1(self):
        f = _finding(check_id="MA-005-pipeline-note-leak")
        assert classify_tier(f) == 1

    def test_classify_ma008_is_tier_3(self):
        f = _finding(check_id="MA-008-pillar-position-verification")
        assert classify_tier(f) == 3

    def test_classify_ma002_banned_subtype_is_tier_1(self):
        f = _finding(
            check_id="MA-002-character-name-registry",
            description="Banned name 'Marcus Webb' appears in manuscript",
        )
        assert classify_tier(f) == 1

    def test_classify_ma002_invented_subtype_is_tier_2(self):
        f = _finding(
            check_id="MA-002-character-name-registry",
            description="Invented character 'John Doe' speaks dialogue",
        )
        assert classify_tier(f) == 2

    def test_classify_ma007_anaphora_is_tier_1(self):
        f = _finding(
            check_id="MA-007-voice-register-adherence",
            description="Anaphora detected in scene 5",
        )
        assert classify_tier(f) == 1

    def test_classify_ma007_intrusion_breach_is_tier_2(self):
        f = _finding(
            check_id="MA-007-voice-register-adherence",
            description="Intrusion-allocation breach in scene 5 — TYPE=ACTION shows 12.3%",
        )
        assert classify_tier(f) == 2

    def test_classify_suggested_tier_override(self):
        f = _finding(
            check_id="MA-001-character-detail-consistency",
            description="Some finding",
            suggested_tier=1,
        )
        assert classify_tier(f) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Workspace setup tests
# ═══════════════════════════════════════════════════════════════════════════

class TestWorkspaceSetup:
    def test_setup_workspace_creates_directories(self, tmp_path):
        book_dir = _setup_book(tmp_path, {1: "Scene one text."})
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        assert (book_dir / "_fixer_workspace" / "manuscript").is_dir()
        assert (book_dir / "_fixer_workspace" / "patches").is_dir()
        assert (book_dir / "_fixer_workspace" / "audit_runs").is_dir()
        assert (book_dir / "_fixer_workspace" / "fixer_log.md").is_file()

    def test_setup_workspace_copies_manuscript_scenes(self, tmp_path):
        book_dir = _setup_book(tmp_path, {1: "Scene one.", 2: "Scene two."})
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        ws_ms = book_dir / "_fixer_workspace" / "manuscript"
        assert (ws_ms / "sc_001.md").read_text() == "Scene one."
        assert (ws_ms / "sc_002.md").read_text() == "Scene two."

    def test_setup_workspace_overwrites_existing(self, tmp_path):
        book_dir = _setup_book(tmp_path, {1: "Scene one."})
        ws_dir = book_dir / "_fixer_workspace"
        ws_dir.mkdir(parents=True)
        sentinel = ws_dir / "old_sentinel.txt"
        sentinel.write_text("should be deleted")

        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        assert not sentinel.exists()
        assert (ws_dir / "manuscript" / "sc_001.md").is_file()


# ═══════════════════════════════════════════════════════════════════════════
# Tier 1 operation tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTier1Operations:
    def test_tier_1_delete_span_pipeline_note(self, tmp_path):
        scene_text = 'He walked to the door. [NOTE: revise] She waited outside.'
        book_dir = _setup_book(tmp_path, {63: scene_text})
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-005-pipeline-note-leak",
            scene_number=63,
            description="Pipeline note leak (bracketed_editorial_marker, sub-check A) in scene 63",
            evidence=["Scene 63: ...He walked to the door. [NOTE: revise] She waited outside...."],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is True
        assert entry["operation"] == "delete_span"

        result_text = (book_dir / "_fixer_workspace" / "manuscript" / "sc_063.md").read_text()
        assert "[NOTE: revise]" not in result_text
        assert "He walked to the door." in result_text
        assert "She waited outside." in result_text

    def test_tier_1_delete_sentence_book_two_leak(self, tmp_path):
        scene_text = (
            'No elaboration. "This is Book Two\'s problem," he said. He meant it.'
        )
        book_dir = _setup_book(tmp_path, {63: scene_text})
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-005-pipeline-note-leak",
            scene_number=63,
            description="Pipeline note leak (meta_narrative_reference, sub-check B) in scene 63",
            evidence=[
                'Scene 63: ...No elaboration. "This is Book Two\'s problem," he said. He meant it....'
            ],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is True
        assert entry["operation"] == "delete_sentence"

        result_text = (book_dir / "_fixer_workspace" / "manuscript" / "sc_063.md").read_text()
        assert "Book Two" not in result_text
        assert "No elaboration." in result_text

    def test_tier_1_replace_span_banned_name(self, tmp_path):
        scene_text = 'Marcus Webb stepped into the light. Webb nodded.'
        book_dir = _setup_book(tmp_path, {22: scene_text})
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-002-character-name-registry",
            scene_number=22,
            scene_numbers=[22],
            description="Banned name 'Marcus Webb' appears in manuscript",
            evidence=['Scene 22: "Marcus Webb stepped into the light."'],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is True
        assert entry["operation"] == "replace_span"

        result_text = (book_dir / "_fixer_workspace" / "manuscript" / "sc_022.md").read_text()
        assert "Anton Reyes" in result_text
        assert "Marcus Webb" not in result_text

    def test_tier_1_unknown_banned_name_skipped(self, tmp_path):
        scene_text = 'Unknown Banned Name walked in.'
        book_dir = _setup_book(tmp_path, {45: scene_text})
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-002-character-name-registry",
            scene_number=45,
            description="Banned name 'Unknown Banned Name' appears in manuscript",
            evidence=['Scene 45: "Unknown Banned Name walked in."'],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is False
        assert "no replacement registered" in entry.get("skip_reason", "")


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end tests
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEnd:
    def _make_mixed_findings(self) -> list[Finding]:
        """2 Tier 1, 3 Tier 2, 1 Tier 3."""
        return [
            # Tier 1: MA-005
            _finding(
                check_id="MA-005-pipeline-note-leak", scene_number=1,
                description="Pipeline note leak (bracketed_editorial_marker, sub-check A) in scene 1",
                evidence=["Scene 1: ...text [NOTE: fix] more text..."],
            ),
            # Tier 1: MA-007 forbidden
            _finding(
                check_id="MA-007-voice-register-adherence", scene_number=2,
                description="Anaphora detected in scene 2",
                evidence=["Scene 2: ...He knew. He understood. He waited...."],
            ),
            # Tier 2: MA-001
            _finding(check_id="MA-001-character-detail-consistency", scene_number=3,
                     description="Character detail inconsistency"),
            _finding(check_id="MA-003-character-location-temporal", scene_number=4,
                     description="Location inconsistency"),
            _finding(check_id="MA-004-object-state-continuity", scene_number=5,
                     description="Object state error"),
            # Tier 3: MA-008
            _finding(check_id="MA-008-pillar-position-verification", scene_number=1,
                     description="Pillar violation"),
        ]

    def test_run_mixed_findings_correct_counts(self, tmp_path):
        scenes = {
            1: "Some text [NOTE: fix] more text here.",
            2: "He knew. He understood. He waited. The end.",
            3: "Scene three text.",
            4: "Scene four text.",
            5: "Scene five text.",
        }
        book_dir = _setup_book(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        findings = self._make_mixed_findings()
        result = fixer.run(findings)

        assert result.tier_1_applied + result.tier_1_skipped == 2
        # Tier 2 findings skip because no synopsis exists in test env
        assert result.tier_2_applied + result.tier_2_skipped == 3
        assert result.tier_3_escalated == 1

    def test_run_writes_patch_files(self, tmp_path):
        scenes = {
            1: "Some text [NOTE: fix] more text here.",
            2: "He knew. He understood. He waited. The end.",
            3: "Scene three text.",
            4: "Scene four text.",
            5: "Scene five text.",
        }
        book_dir = _setup_book(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        result = fixer.run(self._make_mixed_findings())

        assert len(result.patch_files) > 0
        for pf in result.patch_files:
            assert pf.exists()
            data = json.loads(pf.read_text())
            assert "scene_number" in data
            assert "patches" in data

    def test_run_writes_fixer_log(self, tmp_path):
        scenes = {
            1: "Some text [NOTE: fix] more text here.",
            2: "He knew. He understood. He waited. The end.",
            3: "Scene three text.",
            4: "Scene four text.",
            5: "Scene five text.",
        }
        book_dir = _setup_book(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        result = fixer.run(self._make_mixed_findings())

        assert result.fixer_log_path is not None
        assert result.fixer_log_path.exists()
        log_text = result.fixer_log_path.read_text()
        assert "# Fixer Run" in log_text
        assert "Tier 1" in log_text
        assert "Tier 2" in log_text
        assert "Tier 3" in log_text


# ═══════════════════════════════════════════════════════════════════════════
# Tier 2 helpers
# ═══════════════════════════════════════════════════════════════════════════

_SAMPLE_SYNOPSIS = """\
# Synopsis — Test Book
Generated: 20260516

## Chapter 1 — Opening

### Scene 1 — First [TYPE: ACTION] [POV: Hank Reyes]

- Hank enters the building. Clears the room.
- Finds the target. Engages.

---

### Scene 2 — Second [TYPE: NON-ACTION] [POV: Lena Ibarra]

- Lena reviews the intel. Notes discrepancies.
- Contacts Hank via secure channel.

## Chapter 2 — Middle

### Scene 1 — Third [TYPE: MIXED] [POV: Hank Reyes]

- Hank meets the asset. Tense exchange.
- Hank decides to proceed.

---

### Scene 2 — Fourth [TYPE: ACTION] [POV: Lena Ibarra]

- Pursuit through the market. Lena tracks from overwatch.
"""


def _setup_book_with_synopsis(
    tmp_path: Path,
    scenes: dict[int, str],
    synopsis_text: str = _SAMPLE_SYNOPSIS,
) -> Path:
    """Create a book directory with manuscript scenes AND a synopsis."""
    book_dir = _setup_book(tmp_path, scenes)
    work_dir = book_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "synopsis.md").write_text(synopsis_text, encoding="utf-8")
    # Create minimal series-level files
    series_dir = book_dir.parent  # test_series/
    (series_dir / "series_bible.json").write_text("{}", encoding="utf-8")
    (series_dir / "character_profiles.json").write_text("{}", encoding="utf-8")
    return book_dir


def _make_scene_prose(word_count: int = 500) -> str:
    """Generate fake prose of approximately word_count words."""
    sentence = "The operative moved through the corridor without sound. "
    words_per = len(sentence.split())
    repeats = max(1, word_count // words_per)
    return sentence * repeats


def _mock_write_scene(prose_text: str = ""):
    """Return a mock for scene_writer.write_scene that returns given prose."""
    from scene_writer import SceneProse
    text = prose_text or _make_scene_prose(500)
    mock = MagicMock(return_value=SceneProse(
        prose=text,
        tokens_used={"input_tokens": 5000, "output_tokens": 2000},
        prompt_excerpt="test",
    ))
    return mock


# ═══════════════════════════════════════════════════════════════════════════
# Synopsis subscene lookup tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSynopsisSubscene:
    def test_load_synopsis_subscene_first(self, tmp_path):
        syn = tmp_path / "synopsis.md"
        syn.write_text(_SAMPLE_SYNOPSIS, encoding="utf-8")
        result = load_synopsis_subscene(syn, 1)
        assert result is not None
        assert "### Scene 1" in result
        assert "Hank enters the building" in result

    def test_load_synopsis_subscene_middle(self, tmp_path):
        syn = tmp_path / "synopsis.md"
        syn.write_text(_SAMPLE_SYNOPSIS, encoding="utf-8")
        # Scene 3 = Chapter 2, Scene 1 in flat numbering
        result = load_synopsis_subscene(syn, 3)
        assert result is not None
        assert "Hank meets the asset" in result

    def test_load_synopsis_subscene_missing_returns_none(self, tmp_path):
        syn = tmp_path / "synopsis.md"
        syn.write_text(_SAMPLE_SYNOPSIS, encoding="utf-8")
        result = load_synopsis_subscene(syn, 999)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Constraint block construction tests (no LLM)
# ═══════════════════════════════════════════════════════════════════════════

class TestConstraintBlock:
    def test_constraint_block_single_finding(self, tmp_path):
        scenes = {5: _make_scene_prose(500)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-001-character-detail-consistency",
            scene_number=5,
            description="Funes's wife is in Maracaibo, not Caracas.",
            evidence=["sc 26: His wife is still in Caracas."],
        )
        block = fixer._build_constraint_block(5, [f])
        assert "CORRECTIONS REQUIRED:" in block
        assert "Funes's wife is in Maracaibo, not Caracas." in block
        assert "NARRATIVE CONTINUITY:" in block
        assert "VOICE CONTINUITY:" in block

    def test_constraint_block_multiple_findings_stacked(self, tmp_path):
        scenes = {5: _make_scene_prose(500)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f1 = _finding(
            check_id="MA-001-character-detail-consistency",
            scene_number=5,
            description="Wife location wrong.",
        )
        f2 = _finding(
            check_id="MA-007-voice-register-adherence",
            scene_number=5,
            description="Intrusion-allocation breach.",
        )
        block = fixer._build_constraint_block(5, [f1, f2])
        assert "Wife location wrong." in block
        assert "Intrusion-allocation breach." in block
        assert block.count("CORRECTIONS REQUIRED:") == 1

    def test_constraint_block_includes_narrative_continuity(self, tmp_path):
        scenes = {4: "Prior scene ending text.", 5: _make_scene_prose(500),
                  6: "Following scene opening text."}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=5, description="test")
        block = fixer._build_constraint_block(5, [f])
        assert "sc_004" in block
        assert "sc_006" in block
        assert "Prior scene ending text." in block
        assert "Following scene opening text." in block

    def test_constraint_block_first_scene_no_prior_window(self, tmp_path):
        scenes = {1: _make_scene_prose(500), 2: "Next scene text."}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=1, description="test")
        block = fixer._build_constraint_block(1, [f])
        assert "BOOK OPENING" in block

    def test_constraint_block_last_scene_no_subsequent_window(self, tmp_path):
        # Only 2 scenes in workspace — scene 2 is last
        scenes = {1: "Prior.", 2: _make_scene_prose(500)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=2, description="test")
        block = fixer._build_constraint_block(2, [f])
        assert "BOOK ENDING" in block


# ═══════════════════════════════════════════════════════════════════════════
# Tier 2 execution tests (mocked LLM)
# ═══════════════════════════════════════════════════════════════════════════

class TestTier2Execution:
    def test_tier_2_groups_findings_by_scene(self):
        """3 Tier-2 findings, 2 on scene 26, 1 on scene 28 → 2 groups."""
        f1 = _finding(check_id="MA-001-character-detail-consistency",
                      scene_number=26, description="Finding A")
        f2 = _finding(check_id="MA-007-voice-register-adherence",
                      scene_number=26, description="Finding B")
        f3 = _finding(check_id="MA-001-character-detail-consistency",
                      scene_number=28, description="Finding C")
        grouped = _group_tier_2_by_scene([f1, f2, f3])
        assert set(grouped.keys()) == {26, 28}
        assert len(grouped[26]) == 2
        assert len(grouped[28]) == 1

    def test_tier_2_skips_findings_without_scene_number(self):
        """Finding with scene_number=None → not grouped."""
        f = _finding(check_id="MA-001-character-detail-consistency",
                     scene_number=None, description="No scene")
        grouped = _group_tier_2_by_scene([f])
        assert len(grouped) == 0

    @patch("manuscript_fixer.write_scene")
    def test_tier_2_writes_regenerated_scene_to_workspace(self, mock_ws, tmp_path):
        new_text = _make_scene_prose(500)
        from scene_writer import SceneProse
        mock_ws.return_value = SceneProse(prose=new_text, tokens_used={}, prompt_excerpt="")

        scenes = {1: _make_scene_prose(500)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=1, description="test finding")
        result = FixerResult(book_dir=book_dir, workspace_dir=fixer.workspace_dir)
        fixer._apply_tier_2([f], result)

        ws_text = (fixer.workspace_manuscript / "sc_001.md").read_text()
        assert ws_text == new_text
        assert result.tier_2_applied == 1

    @patch("manuscript_fixer.write_scene")
    def test_tier_2_original_manuscript_unchanged(self, mock_ws, tmp_path):
        original_text = _make_scene_prose(500)
        new_text = _make_scene_prose(600)
        from scene_writer import SceneProse
        mock_ws.return_value = SceneProse(prose=new_text, tokens_used={}, prompt_excerpt="")

        scenes = {1: original_text}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())

        f = _finding(scene_number=1, description="test finding")
        fixer.run([f])

        # Original manuscript untouched
        orig = (book_dir / "out" / "manuscript" / "sc_001.md").read_text()
        assert orig == original_text

    @patch("manuscript_fixer.write_scene")
    def test_tier_2_too_short_skips_and_preserves_original(self, mock_ws, tmp_path):
        original_text = _make_scene_prose(500)
        from scene_writer import SceneProse
        mock_ws.return_value = SceneProse(prose="hi", tokens_used={}, prompt_excerpt="")

        scenes = {1: original_text}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=1, description="test finding")
        result = FixerResult(book_dir=book_dir, workspace_dir=fixer.workspace_dir)
        entries = fixer._apply_tier_2([f], result)

        assert entries[0]["success"] is False
        assert entries[0]["failure_reason"] == "regeneration_too_short"
        # Workspace scene unchanged
        ws_text = (fixer.workspace_manuscript / "sc_001.md").read_text()
        assert ws_text == original_text
        assert result.tier_2_skipped == 1

    @patch("manuscript_fixer.write_scene")
    def test_tier_2_runaway_skips_and_preserves_original(self, mock_ws, tmp_path):
        original_text = _make_scene_prose(500)
        huge_text = _make_scene_prose(5000)  # 10x original
        from scene_writer import SceneProse
        mock_ws.return_value = SceneProse(prose=huge_text, tokens_used={}, prompt_excerpt="")

        scenes = {1: original_text}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=1, description="test finding")
        result = FixerResult(book_dir=book_dir, workspace_dir=fixer.workspace_dir)
        entries = fixer._apply_tier_2([f], result)

        assert entries[0]["success"] is False
        assert entries[0]["failure_reason"] == "regeneration_runaway"
        ws_text = (fixer.workspace_manuscript / "sc_001.md").read_text()
        assert ws_text == original_text

    @patch("manuscript_fixer.write_scene")
    def test_tier_2_api_error_retries_once_then_skips(self, mock_ws, tmp_path):
        # Both calls raise → skip
        mock_ws.side_effect = [RuntimeError("API fail"), RuntimeError("API fail again")]

        scenes = {1: _make_scene_prose(500)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=1, description="test finding")
        result = FixerResult(book_dir=book_dir, workspace_dir=fixer.workspace_dir)
        entries = fixer._apply_tier_2([f], result)

        assert entries[0]["success"] is False
        assert entries[0]["failure_reason"] == "llm_api_error"
        assert mock_ws.call_count == 2  # retried once
        assert result.tier_2_skipped == 1

    @patch("manuscript_fixer.write_scene")
    def test_tier_2_api_error_retry_succeeds(self, mock_ws, tmp_path):
        new_text = _make_scene_prose(500)
        from scene_writer import SceneProse
        good = SceneProse(prose=new_text, tokens_used={}, prompt_excerpt="")
        mock_ws.side_effect = [RuntimeError("API fail"), good]

        scenes = {1: _make_scene_prose(500)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=1, description="test finding")
        result = FixerResult(book_dir=book_dir, workspace_dir=fixer.workspace_dir)
        entries = fixer._apply_tier_2([f], result)

        assert entries[0]["success"] is True
        assert result.tier_2_applied == 1

    @patch("manuscript_fixer.write_scene")
    def test_tier_2_patch_log_includes_regeneration_metadata(self, mock_ws, tmp_path):
        new_text = _make_scene_prose(500)
        from scene_writer import SceneProse
        mock_ws.return_value = SceneProse(
            prose=new_text,
            tokens_used={"input_tokens": 5000, "output_tokens": 2000},
            prompt_excerpt="",
        )

        scenes = {1: _make_scene_prose(400)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())
        fixer._setup_workspace()

        f = _finding(scene_number=1, description="test finding")
        result = FixerResult(book_dir=book_dir, workspace_dir=fixer.workspace_dir)
        entries = fixer._apply_tier_2([f], result)

        entry = entries[0]
        assert entry["success"] is True
        assert "original_word_count" in entry
        assert "new_word_count" in entry
        assert "findings_addressed" in entry
        assert isinstance(entry["findings_addressed"], list)
        assert entry["findings_addressed"][0]["check_id"] == "MA-001-character-detail-consistency"


# ═══════════════════════════════════════════════════════════════════════════
# Tier 2 end-to-end (mocked LLM)
# ═══════════════════════════════════════════════════════════════════════════

class TestTier2EndToEnd:
    @patch("manuscript_fixer.write_scene")
    def test_run_tier_2_applies_and_logs(self, mock_ws, tmp_path):
        new_text = _make_scene_prose(500)
        from scene_writer import SceneProse
        mock_ws.return_value = SceneProse(
            prose=new_text,
            tokens_used={"input_tokens": 5000, "output_tokens": 2000},
            prompt_excerpt="",
        )

        scenes = {1: _make_scene_prose(500), 2: _make_scene_prose(500),
                  3: _make_scene_prose(500)}
        book_dir = _setup_book_with_synopsis(tmp_path, scenes)
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs())

        findings = [
            _finding(check_id="MA-001-character-detail-consistency",
                     scene_number=1, description="Finding on scene 1"),
            _finding(check_id="MA-003-character-location-temporal",
                     scene_number=1, description="Stacked finding on scene 1"),
            _finding(check_id="MA-001-character-detail-consistency",
                     scene_number=2, description="Finding on scene 2"),
        ]
        result = fixer.run(findings)

        assert result.tier_2_applied == 2  # 2 scenes, not 3 findings
        assert 1 in result.scenes_regenerated
        assert 2 in result.scenes_regenerated
        # Only 2 write_scene calls (findings on scene 1 batched)
        assert mock_ws.call_count == 2

        # Check fixer log
        log_text = fixer.fixer_log_path.read_text()
        assert "Scene regeneration applied: 2" in log_text
        assert "Tier 2 totals:" in log_text


# ═══════════════════════════════════════════════════════════════════════════
# Tier 1 with pre-flight integration tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTier1WithPreflight:
    def test_tier_1_replace_span_with_safe_replacement_still_applies(self, tmp_path):
        """Banned-name swap where replacement does NOT appear elsewhere and
        is NOT a known character. Pre-flight passes; fix applies."""
        scene_text = "Marcus Webb stepped into the light."
        book_dir = _setup_book(tmp_path, {22: scene_text})
        briefs = _minimal_briefs()
        fixer = ManuscriptFixer(book_dir, briefs=briefs)
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-002-character-name-registry",
            scene_number=22,
            description="Banned name 'Marcus Webb' appears in manuscript",
            evidence=['Scene 22: "Marcus Webb stepped into the light."'],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is True
        assert entry["preflight_decision"] == "APPLY"

        result_text = (fixer.workspace_manuscript / "sc_022.md").read_text()
        assert "Anton Reyes" in result_text
        assert "Marcus Webb" not in result_text

    def test_tier_1_replace_span_with_collision_escalates_tier_3(self, tmp_path):
        """Banned-name swap where the manuscript has another scene mentioning
        the replacement. Pre-flight escalates; scene file unchanged."""
        book_dir = _setup_book(tmp_path, {
            22: "Marcus Webb stepped into the light.",
            47: "Anton Reyes was already there.",
        })
        briefs = _minimal_briefs()
        fixer = ManuscriptFixer(book_dir, briefs=briefs)
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-002-character-name-registry",
            scene_number=22,
            description="Banned name 'Marcus Webb' appears in manuscript",
            evidence=['Scene 22: "Marcus Webb stepped into the light."'],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is False
        assert entry["preflight_decision"] == "ESCALATE_TIER_3"
        assert entry["escalated_to"] == "tier_3"

        # Scene unchanged
        result_text = (fixer.workspace_manuscript / "sc_022.md").read_text()
        assert "Marcus Webb" in result_text

    def test_tier_1_replace_span_target_is_canonical_character_escalates_tier_3(self, tmp_path):
        """Banned-name swap where the target IS in character_profiles.
        Pre-flight escalates; scene file unchanged."""
        book_dir = _setup_book(tmp_path, {22: "Marcus Webb stepped in."})
        briefs = BriefBundle(
            series_bible={},
            character_profiles={"characters": [{"name": "Marcus Webb"}]},
            book_config={},
            scene_map={},
            entity_ledger={},
        )
        fixer = ManuscriptFixer(book_dir, briefs=briefs)
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-002-character-name-registry",
            scene_number=22,
            description="Banned name 'Marcus Webb' appears in manuscript",
            evidence=['Scene 22: "Marcus Webb stepped in."'],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is False
        assert entry["preflight_decision"] == "ESCALATE_TIER_3"

    def test_tier_1_delete_span_bracketed_marker_still_applies(self, tmp_path):
        """MA-005 fix where target is [NOTE: revise]. Well-formed marker;
        pre-flight passes (no LLM call); fix applies."""
        scene_text = "He walked to the door. [NOTE: revise] She waited outside."
        book_dir = _setup_book(tmp_path, {63: scene_text})
        mock_llm = MagicMock()
        fixer = ManuscriptFixer(book_dir, briefs=_minimal_briefs(), llm_client=mock_llm)
        fixer._setup_workspace()

        f = _finding(
            check_id="MA-005-pipeline-note-leak",
            scene_number=63,
            description="Pipeline note leak (bracketed_editorial_marker, sub-check A) in scene 63",
            evidence=["Scene 63: ...He walked to the door. [NOTE: revise] She waited outside...."],
        )
        entry = fixer._apply_tier_1(f)
        assert entry["success"] is True
        assert entry["preflight_decision"] == "APPLY"

        # LLM was NOT called (well-formed marker skips LLM)
        mock_llm.assert_not_called()

        result_text = (fixer.workspace_manuscript / "sc_063.md").read_text()
        assert "[NOTE: revise]" not in result_text

    def test_tier_1_escalation_routes_to_tier_2_in_same_run(self, tmp_path):
        """Pre-flight escalates a Tier 1 finding to Tier 2; verify it appears
        in Tier 2 patch entries after run() completes."""
        book_dir = _setup_book(tmp_path, {
            22: "Webb met Webb at the Webb bar.",  # 3 occurrences → T2 escalation
        })
        briefs = _minimal_briefs()
        fixer = ManuscriptFixer(book_dir, briefs=briefs)

        f = _finding(
            check_id="MA-002-character-name-registry",
            scene_number=22,
            description="Banned name 'Webb' appears in manuscript",
            evidence=['Scene 22: "Webb met Webb at the Webb bar."'],
        )
        result = fixer.run([f])

        # The finding should have been escalated from Tier 1 via pre-flight
        # and routed to Tier 2 (which skips because no synopsis)
        assert result.tier_1_applied == 0
        assert result.tier_1_skipped == 1  # pre-flight escalated it
        # Tier 2 should have received the escalated finding
        assert result.tier_2_skipped >= 1  # skips because no synopsis
