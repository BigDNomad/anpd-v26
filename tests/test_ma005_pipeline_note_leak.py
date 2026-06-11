"""
Tests for MA-005 pipeline_note_leak_detection.

Covers:
  - Sub-check A: bracketed editorial markers
  - Sub-check B: meta-narrative references (including Book Two calibration anchor)
  - Sub-check C: synopsis scaffolding phrases
  - Sub-check D: stage directions
  - Sub-check E: LLM artifact leaks and tics
  - False-positive guards (in-world book references, dialogue tics)
  - Mandate calibration anchor
  - Module auto-discovery
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
from audit_checks.pipeline_note_leak import (
    PipelineNoteLeak,
    scan_scene,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_manuscript(*scenes):
    return ManuscriptArtifact(
        scenes=[SceneText(scene_number=n, text=t, file_path=f"/fake/sc_{n:03d}.md")
                for n, t in scenes],
        manuscript_dir="/fake",
    )


def _scan(text, sn=1):
    """Convenience: scan text, return hits."""
    return scan_scene(text, sn)


# ─── Sub-check A: Bracketed Markers ──────────────────────────────────────────

class TestSubcheckA:

    def test_bracketed_note_marker_caught(self):
        hits = _scan("He walked in. [NOTE: revise this paragraph] She looked up.")
        assert len(hits) >= 1
        assert hits[0].severity == "CLASS_A"
        assert hits[0].subcheck == "A"

    def test_bracketed_todo_caught(self):
        hits = _scan("The team moved. [TODO] He waited.")
        assert len(hits) >= 1
        assert hits[0].subcheck == "A"

    def test_bracketed_pov_marker_caught(self):
        hits = _scan("The city was dark. [POV: Hank Reyes] He walked.")
        assert len(hits) >= 1
        assert hits[0].subcheck == "A"


# ─── Sub-check B: Meta-narrative References ──────────────────────────────────

class TestSubcheckB:

    def test_book_two_possessive_caught(self):
        hits = _scan('"This is Book Two\'s problem," he said.')
        assert len(hits) >= 1
        assert hits[0].severity == "CLASS_A"
        assert hits[0].subcheck == "B"

    def test_in_book_two_caught(self):
        hits = _scan('"It happens in Book Two."')
        assert len(hits) >= 1
        assert hits[0].subcheck == "B"

    def test_next_chapter_caught(self):
        hits = _scan("She would deal with it in the next chapter.")
        assert len(hits) >= 1
        assert hits[0].subcheck == "B"

    def test_to_be_continued_caught(self):
        hits = _scan("He turned and walked away. To be continued.")
        assert len(hits) >= 1
        assert hits[0].subcheck == "B"


# ─── Sub-check C: Synopsis Scaffolding ────────────────────────────────────────

class TestSubcheckC:

    def test_synopsis_phrase_caught(self):
        hits = _scan("The team regrouped as described in the synopsis and moved forward.")
        assert len(hits) >= 1
        assert hits[0].subcheck == "C"
        assert hits[0].severity == "CLASS_A"


# ─── Sub-check D: Stage Directions ───────────────────────────────────────────

class TestSubcheckD:

    def test_end_of_scene_caught(self):
        hits = _scan("He closed the door. [end of scene]")
        assert len(hits) >= 1
        assert hits[0].subcheck == "D"
        assert hits[0].severity == "CLASS_A"


# ─── Sub-check E: LLM Artifacts ──────────────────────────────────────────────

class TestSubcheckE:

    def test_as_an_ai_caught(self):
        hits = _scan("As an AI, I cannot produce violent content.")
        assert len(hits) >= 1
        assert hits[0].subcheck == "E"
        assert hits[0].severity == "CLASS_A"

    def test_llm_tic_sentence_initial_class_b(self):
        hits = _scan("He looked at the sky.\n\nOf course, the answer was simple.")
        class_b = [h for h in hits if h.severity == "CLASS_B"]
        assert len(class_b) >= 1
        assert class_b[0].pattern_label == "llm_tic_narration"

    def test_llm_tic_in_dialogue_not_flagged(self):
        """'Of course,' inside dialogue quotes should NOT flag."""
        hits = _scan('"Of course," she said. "I knew that."')
        tic_hits = [h for h in hits if h.pattern_label == "llm_tic_narration"]
        assert len(tic_hits) == 0


# ─── False-Positive Guards ────────────────────────────────────────────────────

class TestFalsePositiveGuards:

    def test_the_novel_not_flagged(self):
        hits = _scan("He picked up the novel from the table.")
        assert len(hits) == 0

    def test_a_book_not_flagged(self):
        hits = _scan("She was reading a book. The book was thick and worn.")
        assert len(hits) == 0


# ─── Mandate Calibration Anchor ──────────────────────────────────────────────

class TestMandateCalibrationAnchor:

    def test_mandate_calibration_anchor_caught(self):
        """The exact text from Mandate sc_063 line 37 must be caught."""
        mandate_text = '''\u201cThis is Book Two\u2019s problem,\u201d he said.'''
        hits = _scan(mandate_text, sn=63)
        book_hits = [h for h in hits if h.subcheck == "B"]
        assert len(book_hits) >= 1, (
            "Calibration anchor 'Book Two's problem' must be caught by sub-check B"
        )
        assert book_hits[0].severity == "CLASS_A"


# ─── Module Interface ────────────────────────────────────────────────────────

class TestModuleInterface:

    def test_has_required_interface(self):
        check = PipelineNoteLeak()
        assert check.check_id == "MA-005-pipeline-note-leak"
        assert check.severity == "CLASS_A"
        assert hasattr(check, "run")

    def test_module_auto_discovered(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-005-pipeline-note-leak" in check_ids
        REGISTRY.clear()
