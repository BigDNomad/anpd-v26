"""
Tests for targeted chapter regeneration in synopsis_generator.

Two tests:
(a) _mark_chapters_for_regen marks correct chapters incomplete, leaves others
(b) build_chapter_prompt includes correction text for targeted chapters, omits for others
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_generator import _mark_chapters_for_regen, build_chapter_prompt


class TestMarkChaptersForRegen:

    def test_targeted_chapters_marked_incomplete(self):
        """Specified chapters are marked incomplete; others stay completed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            chapters_dir = os.path.join(tmpdir, "synopsis_chapters")
            os.makedirs(chapters_dir)

            # Create state with 3 completed chapters
            state = {
                "chapters": {
                    "097": {"status": "completed", "attempts": 1},
                    "098": {"status": "completed", "attempts": 1},
                    "099": {"status": "completed", "attempts": 1},
                },
                "totals": {},
            }
            state_path = os.path.join(tmpdir, "synopsis_generation_state.json")
            with open(state_path, "w") as f:
                json.dump(state, f)

            # Create chapter files
            for ch in [97, 98, 99]:
                with open(os.path.join(chapters_dir, f"sc_{ch:03d}.md"), "w") as f:
                    f.write(f"Chapter {ch} content")

            # Mark only 98 and 99 for regen
            _mark_chapters_for_regen(tmpdir, [98, 99])

            # Reload state
            with open(state_path) as f:
                updated = json.load(f)

            assert updated["chapters"]["097"]["status"] == "completed"
            assert updated["chapters"]["098"]["status"] == "incomplete"
            assert updated["chapters"]["099"]["status"] == "incomplete"

            # Chapter files deleted for targeted chapters
            assert os.path.exists(os.path.join(chapters_dir, "sc_097.md"))
            assert not os.path.exists(os.path.join(chapters_dir, "sc_098.md"))
            assert not os.path.exists(os.path.join(chapters_dir, "sc_099.md"))


class _FakeChapter:
    def __init__(self, num):
        self.chapter_number = num
        self.content = f"Chapter {num} outline beats"
        self.beats = ["beat1", "beat2"]
        self.annotations = {}


class TestCorrectionInPrompt:

    def test_correction_present_for_targeted_chapter(self):
        """Correction text appears in prompt for targeted chapter."""
        prompt = build_chapter_prompt(
            chapter=_FakeChapter(99),
            intake={"title": "Test"},
            series_bible={},
            character_profiles={},
            principles_text="",
            correction="Compress to exactly 2 denouement scenes",
        )
        assert "AUDITOR-DRIVEN CORRECTION" in prompt
        assert "Compress to exactly 2 denouement scenes" in prompt

    def test_correction_absent_when_none(self):
        """No correction block when correction is None."""
        prompt = build_chapter_prompt(
            chapter=_FakeChapter(99),
            intake={"title": "Test"},
            series_bible={},
            character_profiles={},
            principles_text="",
            correction=None,
        )
        assert "AUDITOR-DRIVEN CORRECTION" not in prompt
