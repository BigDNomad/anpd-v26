#!/usr/bin/env python3
"""
Regression test: S-3 gate enforcement against synthetic CSAR-style failure.

Synthetic 2-chapter, 4-scene manuscript. Scene 4 deliberately produces
over-length output (mocked scene_writer returns 1500 words). Validates
the full orchestrator failure-handling pipeline:

- 3 retry attempts logged
- Attempt 2 feedback contains word-delta directive
- Attempt 3 feedback contains FINAL marker
- manuscript_BLOCKED.md exists
- act1_full.md does NOT exist
- failure_report.json exists with blocked: true
- Orchestrator returns exit code 1 equivalent

Usage:
    cd /anpd/v25 && python3 pipeline/tests/regression_s3_blocked_manuscript.py
"""

from __future__ import annotations

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
    # S-8 provenance fields (required by orchestrator)
    full_user_prompt: str = "mock user prompt"
    system_prompt: str = "mock system prompt"
    model: str = "claude-sonnet-4-6"
    generation_params: dict = field(default_factory=lambda: {"temperature": "model_default", "max_tokens": 8192})


def main() -> int:
    chapters, synopsis = _make_synopsis()

    # Track feedback strings passed to write_scene
    captured_feedback: list[str] = []

    def mock_write_scene(scene, failure_feedback="", target_words=850, **kwargs):
        captured_feedback.append(failure_feedback)
        # Scene (2,2) always produces over-length output
        if scene.chapter_number == 2 and scene.scene_number == 2:
            return MockSceneProse(prose="Overlong word. " * 150)  # 300 words... need 1500
        return MockSceneProse(prose="Good prose here. " * 55)  # ~275 words... too few

    # We need prose that hits the word count gate. Let me make scene (2,2) return 1500 words
    # and all others return 850 words.
    def mock_write_scene_v2(scene, failure_feedback="", target_words=850, **kwargs):
        captured_feedback.append(failure_feedback)
        if scene.chapter_number == 2 and scene.scene_number == 2:
            return MockSceneProse(prose="The soldier marched forward. " * 375)  # 1500 words
        return MockSceneProse(prose="The soldier stood guard. " * 213)  # ~852 words

    # Mock audit_scene to return real deterministic audit results
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

        # Write synopsis in the format parse_synopsis expects:
        # ## Chapter N — Title
        # ### Scene N — Title [TYPE: X] [POV: Y]
        synopsis_text = ""
        for ch in chapters:
            synopsis_text += f"## Chapter {ch.chapter_number} — {ch.title}\n\n"
            for sc in ch.scenes:
                synopsis_text += f"### Scene {sc.scene_number} — {sc.title} [TYPE: {sc.scene_type}] [POV: {sc.pov}]\n\n"
                synopsis_text += f"{sc.body}\n\n"

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

        # Patch write_scene and audit_scene
        with patch("manuscript_orchestrator.write_scene", side_effect=mock_write_scene_v2), \
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

        # 1. manuscript_BLOCKED.md exists
        blocked_path = os.path.join(manuscript_dir, "manuscript_BLOCKED.md")
        if not os.path.exists(blocked_path):
            errors.append("manuscript_BLOCKED.md does NOT exist")
        else:
            print("  OK  manuscript_BLOCKED.md exists")

        # 2. act1_full.md does NOT exist
        canonical_path = os.path.join(manuscript_dir, "act1_full.md")
        if os.path.exists(canonical_path):
            errors.append("act1_full.md exists (should NOT when blocked)")
        else:
            print("  OK  act1_full.md does NOT exist")

        # 3. failure_report.json exists with blocked: true
        report_path = os.path.join(manuscript_dir, "failure_report.json")
        if not os.path.exists(report_path):
            errors.append("failure_report.json does NOT exist")
        else:
            with open(report_path) as f:
                report = json.load(f)
            if not report.get("blocked"):
                errors.append("failure_report.json blocked is not True")
            else:
                print(f"  OK  failure_report.json exists, blocked=true, "
                      f"{report['class_a_failures']} Class A failure(s)")

            # Check failing scenes
            if not report.get("failing_scenes"):
                errors.append("failure_report.json has no failing_scenes")
            else:
                failing = report["failing_scenes"][0]
                if len(failing.get("retry_history", [])) != 3:
                    errors.append(
                        f"Expected 3 retry attempts, got "
                        f"{len(failing.get('retry_history', []))}"
                    )
                else:
                    print(f"  OK  3 retry attempts logged for failing scene")

        # 4. Orchestrator exit code equivalent
        if receipt.get("class_a_failures", 0) == 0:
            errors.append("class_a_failures == 0 (expected > 0)")
        else:
            print(f"  OK  class_a_failures = {receipt['class_a_failures']} (exit 1)")

        # 5. Retry feedback contains expected markers
        # captured_feedback has entries for each write_scene call
        # Scene (2,2) has 3 attempts, so 3 feedback strings for it
        # Find the feedback strings for scene (2,2) — they're the last 3
        # (after the 3 good scenes with 1 attempt each)
        #
        # Layout: 4 scenes total, scene (2,2) retries 3 times
        # Calls: sc(1,1) x1, sc(1,2) x1, sc(2,1) x1, sc(2,2) x3 = 6 calls
        if len(captured_feedback) < 6:
            errors.append(f"Expected at least 6 write_scene calls, got {len(captured_feedback)}")
        else:
            # Scene (2,2) attempts: indices 3, 4, 5
            fb_attempt1 = captured_feedback[3]  # first attempt — no feedback
            fb_attempt2 = captured_feedback[4]  # after attempt 1 failed
            fb_attempt3 = captured_feedback[5]  # after attempt 2 failed

            if fb_attempt1 != "":
                errors.append(f"Attempt 1 feedback should be empty, got: '{fb_attempt1[:50]}'")
            else:
                print("  OK  Attempt 1 feedback is empty")

            if "ATTEMPT 2 of 3" not in fb_attempt2:
                errors.append(f"Attempt 2 feedback missing 'ATTEMPT 2 of 3'")
            elif "cut at least" not in fb_attempt2:
                errors.append(f"Attempt 2 feedback missing word-delta directive")
            else:
                print("  OK  Attempt 2 feedback contains 'ATTEMPT 2 of 3' + word-delta")

            if "ATTEMPT 3 of 3" not in fb_attempt3 or "FINAL" not in fb_attempt3:
                errors.append(f"Attempt 3 feedback missing 'ATTEMPT 3 of 3 — FINAL'")
            else:
                print("  OK  Attempt 3 feedback contains FINAL marker")

            if fb_attempt2 == fb_attempt3:
                errors.append("Attempt 2 and 3 feedback are identical (should differ)")
            else:
                print("  OK  Feedback strings are distinct across attempts")

        # ── Result ────────────────────────────────────────────────────

        if errors:
            print(f"\nFAILURES:")
            for e in errors:
                print(f"  x {e}")
            return 1
        else:
            print(f"\nPASS: All S-3 gate enforcement checks verified.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
