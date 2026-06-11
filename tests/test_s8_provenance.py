"""
Tests for S-8 per-scene provenance trail.

Six tests per spec §5.3:
1. SceneProse carries full provenance
2. Full prompt not truncated
3. Per-scene provenance file written for a passing scene
4. Per-attempt provenance for a failing scene
5. System prompt stored once with matching hash
6. Provenance written on pass AND fail
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scene_writer import SceneProse, write_scene
from synopsis_parser import SceneEntry, ChapterEntry, SynopsisStructure


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_scene(**overrides):
    defaults = dict(
        chapter_number=1,
        scene_number=1,
        title="Test Scene",
        scene_type="MIXED",
        pov="Archer",
        body="Synopsis body. Beat one. Beat two.",
        position_in_chapter=1,
    )
    defaults.update(overrides)
    return SceneEntry(**defaults)


def _make_synopsis(n_chapters=1, scenes_per_chapter=1):
    chapters = []
    for ch in range(1, n_chapters + 1):
        scenes = []
        for sc in range(1, scenes_per_chapter + 1):
            scenes.append(_make_scene(
                chapter_number=ch, scene_number=sc,
                title=f"Scene {sc}",
            ))
        chapters.append(ChapterEntry(
            chapter_number=ch, title=f"Chapter {ch}", scenes=scenes,
        ))
    return chapters, SynopsisStructure(chapters=chapters)


@dataclass
class MockSceneProse:
    """Mock that mirrors SceneProse with S-8 fields."""
    prose: str
    tokens_used: dict = field(default_factory=lambda: {"input_tokens": 100, "output_tokens": 200})
    prompt_excerpt: str = ""
    full_user_prompt: str = "full user prompt text"
    system_prompt: str = "system prompt text"
    model: str = "claude-sonnet-4-6"
    generation_params: dict = field(default_factory=lambda: {"temperature": "model_default", "max_tokens": 8192})


def _setup_orchestrator_inputs(tmpdir, chapters):
    """Write the minimal input files the orchestrator needs."""
    synopsis_text = ""
    for ch in chapters:
        synopsis_text += f"## Chapter {ch.chapter_number} — {ch.title}\n\n"
        for sc in ch.scenes:
            synopsis_text += (
                f"### Scene {sc.scene_number} — {sc.title} "
                f"[TYPE: {sc.scene_type}] [POV: {sc.pov}]\n\n"
                f"{sc.body}\n\n"
            )

    paths = {}
    for name, content in [
        ("synopsis.md", synopsis_text),
        ("intake.json", json.dumps({"target_word_count": 85000, "total_chapter_count": len(chapters)})),
        ("series_bible.json", json.dumps({})),
        ("character_profiles.json", json.dumps({})),
        ("principles.json", json.dumps({"principles": []})),
    ]:
        p = os.path.join(tmpdir, name)
        with open(p, "w") as f:
            f.write(content if isinstance(content, str) else content)
        paths[name.split(".")[0]] = p

    paths["output_dir"] = os.path.join(tmpdir, "output")
    return paths


# ── Test 1: SceneProse carries full provenance ────────────────────────────


class TestSceneProseProvenance:

    def test_sceneprose_carries_provenance(self):
        """write_scene() returns SceneProse with non-empty S-8 fields."""
        with patch("scene_writer._call_api") as mock_api:
            mock_api.return_value = (
                "Generated prose text here.",
                {"input_tokens": 500, "output_tokens": 300},
            )

            scene = _make_scene()
            result = write_scene(
                scene=scene,
                adjacent={"prior": None, "next": None},
                series_bible={},
                character_profiles={},
                craft_principles=[],
                target_words=850,
            )

            assert result.full_user_prompt != "", "full_user_prompt should be non-empty"
            assert result.system_prompt != "", "system_prompt should be non-empty"
            assert result.model != "", "model should be non-empty"
            assert result.generation_params, "generation_params should be non-empty"
            assert "temperature" in result.generation_params
            assert "max_tokens" in result.generation_params


# ── Test 2: Full prompt not truncated ─────────────────────────────────────


class TestFullPromptNotTruncated:

    def test_full_prompt_not_truncated(self):
        """full_user_prompt length equals actual prompt, not 500."""
        with patch("scene_writer._call_api") as mock_api:
            mock_api.return_value = (
                "Generated prose.",
                {"input_tokens": 500, "output_tokens": 300},
            )

            # Build a scene with a long body to ensure prompt > 500 chars
            long_body = "Beat description. " * 100  # ~1800 chars
            scene = _make_scene(body=long_body)
            result = write_scene(
                scene=scene,
                adjacent={"prior": None, "next": None},
                series_bible={},
                character_profiles={},
                craft_principles=[],
                target_words=850,
            )

            assert len(result.full_user_prompt) > 500, "full prompt should exceed 500 chars"
            # prompt_excerpt should still be truncated at 500
            assert len(result.prompt_excerpt) == 500
            # full_user_prompt should be the actual full prompt
            assert len(result.full_user_prompt) > len(result.prompt_excerpt)


# ── Test 3: Per-scene provenance file for a passing scene ─────────────────


class TestProvenanceFilePassingScene:

    def test_provenance_file_written_for_passing_scene(self):
        """Orchestrator writes provenance JSON for a 1-scene passing run."""
        chapters, synopsis = _make_synopsis(n_chapters=1, scenes_per_chapter=1)

        def mock_write(scene, failure_feedback="", **kwargs):
            return MockSceneProse(prose="The soldier stood guard. " * 213)

        from scene_auditor import audit_scene as real_audit

        def mock_audit(prose, scene, use_llm=False, **kwargs):
            return real_audit(prose=prose, scene=scene, use_llm=False, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _setup_orchestrator_inputs(tmpdir, chapters)

            with patch("manuscript_orchestrator.write_scene", side_effect=mock_write), \
                 patch("manuscript_orchestrator.audit_scene", side_effect=mock_audit):
                from manuscript_orchestrator import generate_manuscript
                receipt = generate_manuscript(
                    synopsis_path=paths["synopsis"],
                    intake_path=paths["intake"],
                    series_bible_path=paths["series_bible"],
                    character_profiles_path=paths["character_profiles"],
                    principles_path=paths["principles"],
                    output_dir=paths["output_dir"],
                    max_attempts_per_scene=3,
                    skip_llm_audit=True,
                )

            manuscript_dir = receipt["output_paths"]["manuscript_dir"]
            prov_dir = os.path.join(manuscript_dir, "scene_provenance")
            assert os.path.isdir(prov_dir), "scene_provenance/ dir should exist"

            prov_file = os.path.join(prov_dir, "sc_ch01_sc01_provenance.json")
            assert os.path.exists(prov_file), "provenance file should exist"

            with open(prov_file) as f:
                prov = json.load(f)

            assert prov["total_attempts"] == 1
            assert len(prov["attempts"]) == 1
            assert prov["final_passed"] is True
            assert prov["attempts"][0]["user_prompt"] != ""
            assert prov["attempts"][0]["output"] != ""
            assert prov["attempts"][0]["audit_passed"] is True


# ── Test 4: Per-attempt provenance for a failing scene ────────────────────


class TestPerAttemptProvenance:

    def test_three_retry_attempts_captured(self):
        """Failing scene with 3 retries produces 3 attempt records with distinct prompts."""
        chapters, synopsis = _make_synopsis(n_chapters=1, scenes_per_chapter=1)

        call_count = [0]

        def mock_write(scene, failure_feedback="", **kwargs):
            call_count[0] += 1
            # Always return over-length prose to force failure
            prompt = f"prompt for attempt {call_count[0]} feedback={failure_feedback}"
            return MockSceneProse(
                prose="The soldier marched forward. " * 375,  # 1500 words
                full_user_prompt=prompt,
            )

        from scene_auditor import audit_scene as real_audit

        def mock_audit(prose, scene, use_llm=False, **kwargs):
            return real_audit(prose=prose, scene=scene, use_llm=False, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _setup_orchestrator_inputs(tmpdir, chapters)

            with patch("manuscript_orchestrator.write_scene", side_effect=mock_write), \
                 patch("manuscript_orchestrator.audit_scene", side_effect=mock_audit):
                from manuscript_orchestrator import generate_manuscript
                receipt = generate_manuscript(
                    synopsis_path=paths["synopsis"],
                    intake_path=paths["intake"],
                    series_bible_path=paths["series_bible"],
                    character_profiles_path=paths["character_profiles"],
                    principles_path=paths["principles"],
                    output_dir=paths["output_dir"],
                    max_attempts_per_scene=3,
                    skip_llm_audit=True,
                )

            manuscript_dir = receipt["output_paths"]["manuscript_dir"]
            prov_file = os.path.join(
                manuscript_dir, "scene_provenance", "sc_ch01_sc01_provenance.json"
            )
            with open(prov_file) as f:
                prov = json.load(f)

            assert prov["total_attempts"] == 3
            assert len(prov["attempts"]) == 3
            # Each attempt has its own user_prompt
            prompts = [a["user_prompt"] for a in prov["attempts"]]
            assert prompts[0] != prompts[1], "Attempt 2 prompt should differ from attempt 1 (S-3 augmentation)"
            # All attempts should have failed
            assert all(not a["audit_passed"] for a in prov["attempts"])
            # Gates fired should be non-empty for all
            assert all(len(a["gates_fired"]) > 0 for a in prov["attempts"])


# ── Test 5: System prompt stored once with matching hash ──────────────────


class TestSystemPromptStoredOnce:

    def test_system_prompt_stored_once_hash_matches(self):
        """3-scene run stores system_prompt.txt once; all provenance files share the same hash."""
        chapters, synopsis = _make_synopsis(n_chapters=1, scenes_per_chapter=3)

        def mock_write(scene, **kwargs):
            return MockSceneProse(prose="The soldier stood guard. " * 213)

        from scene_auditor import audit_scene as real_audit

        def mock_audit(prose, scene, use_llm=False, **kwargs):
            return real_audit(prose=prose, scene=scene, use_llm=False, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _setup_orchestrator_inputs(tmpdir, chapters)

            with patch("manuscript_orchestrator.write_scene", side_effect=mock_write), \
                 patch("manuscript_orchestrator.audit_scene", side_effect=mock_audit):
                from manuscript_orchestrator import generate_manuscript
                receipt = generate_manuscript(
                    synopsis_path=paths["synopsis"],
                    intake_path=paths["intake"],
                    series_bible_path=paths["series_bible"],
                    character_profiles_path=paths["character_profiles"],
                    principles_path=paths["principles"],
                    output_dir=paths["output_dir"],
                    max_attempts_per_scene=3,
                    skip_llm_audit=True,
                )

            manuscript_dir = receipt["output_paths"]["manuscript_dir"]

            # system_prompt.txt should exist once
            sp_path = os.path.join(manuscript_dir, "run_provenance", "system_prompt.txt")
            assert os.path.exists(sp_path), "system_prompt.txt should exist"

            with open(sp_path) as f:
                sp_text = f.read()
            expected_hash = hashlib.sha256(sp_text.encode("utf-8")).hexdigest()

            # All 3 scene provenance files should reference the same hash
            prov_dir = os.path.join(manuscript_dir, "scene_provenance")
            hashes = []
            refs = []
            for sc in range(1, 4):
                prov_file = os.path.join(prov_dir, f"sc_ch01_sc{sc:02d}_provenance.json")
                assert os.path.exists(prov_file), f"provenance file for scene {sc} should exist"
                with open(prov_file) as f:
                    prov = json.load(f)
                hashes.append(prov["system_prompt_sha256"])
                refs.append(prov["system_prompt_ref"])

            # All hashes match the stored system prompt
            for h in hashes:
                assert h == expected_hash, "All scenes should reference the same system prompt hash"
            for r in refs:
                assert r == "run_provenance/system_prompt.txt"


# ── Test 6: Provenance written on pass AND fail ───────────────────────────


class TestProvenancePassAndFail:

    def test_provenance_exists_for_both_passing_and_failing(self):
        """Both passing and failing scenes get provenance files."""
        chapters, synopsis = _make_synopsis(n_chapters=1, scenes_per_chapter=2)

        def mock_write(scene, failure_feedback="", **kwargs):
            if scene.scene_number == 2:
                # Over-length — will fail all 3 attempts
                return MockSceneProse(prose="The soldier marched forward. " * 375)
            return MockSceneProse(prose="The soldier stood guard. " * 213)

        from scene_auditor import audit_scene as real_audit

        def mock_audit(prose, scene, use_llm=False, **kwargs):
            return real_audit(prose=prose, scene=scene, use_llm=False, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _setup_orchestrator_inputs(tmpdir, chapters)

            with patch("manuscript_orchestrator.write_scene", side_effect=mock_write), \
                 patch("manuscript_orchestrator.audit_scene", side_effect=mock_audit):
                from manuscript_orchestrator import generate_manuscript
                receipt = generate_manuscript(
                    synopsis_path=paths["synopsis"],
                    intake_path=paths["intake"],
                    series_bible_path=paths["series_bible"],
                    character_profiles_path=paths["character_profiles"],
                    principles_path=paths["principles"],
                    output_dir=paths["output_dir"],
                    max_attempts_per_scene=3,
                    skip_llm_audit=True,
                )

            manuscript_dir = receipt["output_paths"]["manuscript_dir"]
            prov_dir = os.path.join(manuscript_dir, "scene_provenance")

            # Scene 1 (pass) provenance
            prov1 = os.path.join(prov_dir, "sc_ch01_sc01_provenance.json")
            assert os.path.exists(prov1), "Passing scene provenance should exist"
            with open(prov1) as f:
                p1 = json.load(f)
            assert p1["final_passed"] is True

            # Scene 2 (fail) provenance
            prov2 = os.path.join(prov_dir, "sc_ch01_sc02_provenance.json")
            assert os.path.exists(prov2), "Failing scene provenance should exist"
            with open(prov2) as f:
                p2 = json.load(f)
            assert p2["final_passed"] is False
            assert p2["total_attempts"] == 3
