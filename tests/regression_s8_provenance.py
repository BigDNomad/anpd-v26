#!/usr/bin/env python3
"""
Regression test: S-8 provenance trail composes with S-3 gate enforcement.

Synthetic 2-chapter, 4-scene manuscript. Scene (2,2) deliberately produces
over-length output (1500 words). Validates:

- scene_provenance/ directory exists
- Provenance file for the failing scene has 3 distinct attempt prompts
- Attempt 2 prompt differs from attempt 1 (S-3 augmentation captured in S-8)
- System prompt stored once in run_provenance/
- Provenance files exist for both passing and failing scenes

Usage:
    cd /anpd/v25 && python3 pipeline/tests/regression_s8_provenance.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_parser import SceneEntry, ChapterEntry, SynopsisStructure


def _make_synopsis():
    """Build a 2-chapter, 4-scene synopsis."""
    chapters = []
    for ch_num in range(1, 3):
        scenes = []
        for sc_num in range(1, 3):
            scenes.append(SceneEntry(
                chapter_number=ch_num,
                scene_number=sc_num,
                title=f"Scene {sc_num}",
                scene_type="MIXED",
                pov="Archer",
                body=f"Synopsis body for ch{ch_num} sc{sc_num}.\n\nBeat one.\n\nBeat two.",
                position_in_chapter=sc_num,
            ))
        chapters.append(ChapterEntry(
            chapter_number=ch_num,
            title=f"Chapter {ch_num}",
            scenes=scenes,
        ))
    return chapters, SynopsisStructure(chapters=chapters)


@dataclass
class MockSceneProse:
    prose: str
    tokens_used: dict = field(default_factory=lambda: {"input_tokens": 100, "output_tokens": 200})
    prompt_excerpt: str = ""
    full_user_prompt: str = ""
    system_prompt: str = "system prompt for test run"
    model: str = "claude-sonnet-4-6"
    generation_params: dict = field(default_factory=lambda: {"temperature": "model_default", "max_tokens": 8192})


def main() -> int:
    chapters, synopsis = _make_synopsis()

    call_count = [0]

    def mock_write_scene(scene, failure_feedback="", target_words=850, **kwargs):
        call_count[0] += 1
        prompt = f"prompt for call {call_count[0]} scene=({scene.chapter_number},{scene.scene_number}) feedback='{failure_feedback[:60]}'"
        if scene.chapter_number == 2 and scene.scene_number == 2:
            return MockSceneProse(
                prose="The soldier marched forward. " * 375,  # 1500 words
                full_user_prompt=prompt,
            )
        return MockSceneProse(
            prose="The soldier stood guard. " * 213,  # ~852 words
            full_user_prompt=prompt,
        )

    from scene_auditor import audit_scene as real_audit

    def mock_audit_scene(prose, scene, use_llm=False, **kwargs):
        return real_audit(prose=prose, scene=scene, use_llm=False, **kwargs)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create required input files
        synopsis_path = os.path.join(tmpdir, "synopsis.md")
        intake_path = os.path.join(tmpdir, "intake.json")
        bible_path = os.path.join(tmpdir, "series_bible.json")
        profiles_path = os.path.join(tmpdir, "character_profiles.json")
        principles_path = os.path.join(tmpdir, "principles.json")
        output_dir = os.path.join(tmpdir, "output")

        synopsis_text = ""
        for ch in chapters:
            synopsis_text += f"## Chapter {ch.chapter_number} — {ch.title}\n\n"
            for sc in ch.scenes:
                synopsis_text += (
                    f"### Scene {sc.scene_number} — {sc.title} "
                    f"[TYPE: {sc.scene_type}] [POV: {sc.pov}]\n\n"
                    f"{sc.body}\n\n"
                )

        with open(synopsis_path, 'w') as f:
            f.write(synopsis_text)
        with open(intake_path, 'w') as f:
            json.dump({"target_word_count": 85000, "total_chapter_count": 2}, f)
        with open(bible_path, 'w') as f:
            json.dump({}, f)
        with open(profiles_path, 'w') as f:
            json.dump({}, f)
        with open(principles_path, 'w') as f:
            json.dump({"principles": []}, f)

        with patch("manuscript_orchestrator.write_scene", side_effect=mock_write_scene), \
             patch("manuscript_orchestrator.audit_scene", side_effect=mock_audit_scene):

            from manuscript_orchestrator import generate_manuscript

            receipt = generate_manuscript(
                synopsis_path=synopsis_path,
                intake_path=intake_path,
                series_bible_path=bible_path,
                character_profiles_path=profiles_path,
                principles_path=principles_path,
                output_dir=output_dir,
                max_attempts_per_scene=3,
                skip_llm_audit=True,
            )

        manuscript_dir = receipt["output_paths"]["manuscript_dir"]

        # ── Validations ──────────────────────────────────────────────

        errors = []

        # 1. scene_provenance/ directory exists
        prov_dir = os.path.join(manuscript_dir, "scene_provenance")
        if not os.path.isdir(prov_dir):
            errors.append("scene_provenance/ directory does NOT exist")
        else:
            print("  OK  scene_provenance/ directory exists")

        # 2. Provenance file for the failing scene (ch2, sc2) has 3 attempts
        fail_prov_path = os.path.join(prov_dir, "sc_ch02_sc02_provenance.json")
        if not os.path.exists(fail_prov_path):
            errors.append("Provenance file for failing scene (ch2 sc2) does NOT exist")
        else:
            with open(fail_prov_path) as f:
                fail_prov = json.load(f)

            if fail_prov["total_attempts"] != 3:
                errors.append(f"Expected 3 attempts, got {fail_prov['total_attempts']}")
            else:
                print(f"  OK  Failing scene has 3 attempts")

            if len(fail_prov["attempts"]) != 3:
                errors.append(f"Expected 3 attempt records, got {len(fail_prov['attempts'])}")
            else:
                print(f"  OK  3 attempt records present")

            # 3. Attempt prompts are distinct (S-3 augmentation captured)
            prompts = [a["user_prompt"] for a in fail_prov["attempts"]]
            if prompts[0] == prompts[1]:
                errors.append("Attempt 1 and 2 prompts are identical (S-3 augmentation NOT captured)")
            else:
                print(f"  OK  Attempt 1 and 2 prompts are distinct (S-3 augmentation captured)")
                print(f"      Attempt 1 prompt: {prompts[0][:80]}...")
                print(f"      Attempt 2 prompt: {prompts[1][:80]}...")
                print(f"      Attempt 3 prompt: {prompts[2][:80]}...")

            if not fail_prov.get("final_passed") is False:
                errors.append("Failing scene should have final_passed=false")
            else:
                print(f"  OK  final_passed=false for failing scene")

            # Check gates_fired
            for i, att in enumerate(fail_prov["attempts"]):
                if len(att["gates_fired"]) == 0:
                    errors.append(f"Attempt {i+1} has no gates_fired")
            if not any("gates_fired" in str(e) for e in errors):
                print(f"  OK  All attempts have gates_fired records")

        # 4. system_prompt.txt stored once in run_provenance/
        sp_path = os.path.join(manuscript_dir, "run_provenance", "system_prompt.txt")
        if not os.path.exists(sp_path):
            errors.append("run_provenance/system_prompt.txt does NOT exist")
        else:
            with open(sp_path) as f:
                sp_text = f.read()
            expected_hash = hashlib.sha256(sp_text.encode("utf-8")).hexdigest()
            print(f"  OK  system_prompt.txt exists (SHA-256: {expected_hash[:16]}...)")

        # 5. Provenance files exist for passing scenes too
        pass_prov_path = os.path.join(prov_dir, "sc_ch01_sc01_provenance.json")
        if not os.path.exists(pass_prov_path):
            errors.append("Provenance file for passing scene (ch1 sc1) does NOT exist")
        else:
            with open(pass_prov_path) as f:
                pass_prov = json.load(f)
            if pass_prov["final_passed"] is not True:
                errors.append("Passing scene should have final_passed=true")
            else:
                print(f"  OK  Passing scene provenance exists with final_passed=true")

        # 6. Count total provenance files — should be 4 (one per scene)
        prov_files = [f for f in os.listdir(prov_dir) if f.endswith("_provenance.json")]
        if len(prov_files) != 4:
            errors.append(f"Expected 4 provenance files, got {len(prov_files)}: {prov_files}")
        else:
            print(f"  OK  4 provenance files (one per scene)")

        # ── Result ────────────────────────────────────────────────────

        if errors:
            print(f"\nFAILURES:")
            for e in errors:
                print(f"  x {e}")
            return 1
        else:
            print(f"\nPASS: All S-8 provenance trail checks verified.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
