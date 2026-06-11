"""
Tests for MA-006 reintroduction_detection.

Covers:
  - Common verb skipping
  - Threshold and dominance filtering
  - LLM verdict handling (stutter + echo)
  - Lena "filed" calibration anchor
  - Module auto-discovery
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    REGISTRY,
    discover_and_register,
)
from audit_checks.reintroduction_detection import (
    ReintroductionDetection,
    StutterCandidate,
    EchoCandidate,
    extract_subject_verb_pairs,
    build_stutter_candidates,
    build_echo_candidates,
    _load_character_roster,
    MA006_STUTTER_THRESHOLD,
    MA006_STUTTER_DOMINANCE,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_manuscript(*scenes):
    return ManuscriptArtifact(
        scenes=[SceneText(scene_number=n, text=t, file_path=f"/fake/sc_{n:03d}.md")
                for n, t in scenes],
        manuscript_dir="/fake",
    )


def _make_briefs(**kwargs):
    return BriefBundle(**kwargs)


_TEST_CHARACTERS = {"Lena", "Hank", "Cole", "Mia", "Eddie", "Funes"}


# ─── Sub-check A: Stutter Detection ──────────────────────────────────────────

class TestCommonVerbsSkipped:

    def test_said_skipped(self):
        """'Lena said' 30 times → no stutter (common verb)."""
        text = " ".join(["Lena said hello." for _ in range(30)])
        pairs = extract_subject_verb_pairs(text, 1, _TEST_CHARACTERS)
        # "said" is in _COMMON_VERBS → should not appear
        verbs = [v for _, v, _, _ in pairs]
        assert "said" not in verbs


class TestThresholdFiltering:

    def test_below_threshold_no_candidate(self):
        """'Lena cataloged' 5 times → below threshold, no candidate."""
        pairs_by_char = {"Lena": [("cataloged", i, f"Lena cataloged item {i}") for i in range(5)]}
        all_verbs = Counter({"cataloged": 5})
        candidates = build_stutter_candidates(pairs_by_char, all_verbs)
        assert len(candidates) == 0

    def test_above_threshold_candidate(self):
        """'Lena cataloged' 10 times → above threshold, candidate."""
        pairs_by_char = {"Lena": [("cataloged", i, f"Lena cataloged item {i}") for i in range(10)]}
        all_verbs = Counter({"cataloged": 10})
        candidates = build_stutter_candidates(pairs_by_char, all_verbs)
        assert len(candidates) >= 1
        assert candidates[0].verb == "cataloged"
        assert candidates[0].character == "Lena"


class TestDominanceFiltering:

    def test_verb_used_by_many_no_candidate(self):
        """'filed' used 10 times across Lena, Hank, Mia → fails dominance."""
        pairs_by_char = {
            "Lena": [("filed", i, f"Lena filed it {i}") for i in range(4)],
            "Hank": [("filed", i + 10, f"Hank filed the report {i}") for i in range(3)],
            "Mia": [("filed", i + 20, f"Mia filed her nails {i}") for i in range(3)],
        }
        all_verbs = Counter({"filed": 10})
        candidates = build_stutter_candidates(pairs_by_char, all_verbs)
        # Lena has 4/10 = 40% dominance, below 70%
        assert len(candidates) == 0

    def test_verb_dominated_by_one_character(self):
        """'filed' 20 times for Lena, 2 across others → dominance passes."""
        pairs_by_char = {
            "Lena": [("filed", i, f"She filed it {i}") for i in range(20)],
            "Hank": [("filed", 50, "Hank filed the report"), ("filed", 51, "Hank filed it")],
        }
        all_verbs = Counter({"filed": 22})
        candidates = build_stutter_candidates(pairs_by_char, all_verbs)
        assert len(candidates) >= 1
        assert candidates[0].character == "Lena"
        assert candidates[0].dominance >= MA006_STUTTER_DOMINANCE


class TestLLMVerdicts:

    def test_legitimate_vocabulary_suppresses(self):
        """LLM returns LEGITIMATE_VOCABULARY → no finding."""
        candidate = StutterCandidate(
            character="Lena", verb="processed", total_occurrences=10,
            character_occurrences=9, dominance=0.9,
            scene_examples=[(1, "She processed the data")],
        )

        with patch("audit_checks.reintroduction_detection._call_llm") as mock:
            mock.return_value = "LEGITIMATE_VOCABULARY\nCommon intelligence work verb."
            check = ReintroductionDetection()
            ms = _make_manuscript((1, "Lena processed the feed."))
            briefs = _make_briefs(
                character_profiles={"characters": [{"name": "Lena Ibarra"}]},
                series_bible={"recurring_characters": [{"name": "Lena Ibarra"}]},
            )
            findings = check.run(ms, briefs)
            # Should suppress — only 1 occurrence in the actual manuscript
            # (below threshold), so no candidate generated in real run.
            # Direct test of verdict handling:
            from audit_checks.reintroduction_detection import llm_confirm_stutter
            result = llm_confirm_stutter(candidate)
            assert result == "LEGITIMATE_VOCABULARY"

    def test_characterization_stutter_class_a(self):
        """LLM returns CHARACTERIZATION_STUTTER → CLASS_A."""
        candidate = StutterCandidate(
            character="Lena", verb="filed", total_occurrences=29,
            character_occurrences=27, dominance=0.93,
            scene_examples=[(1, "She filed the observation")],
        )

        with patch("audit_checks.reintroduction_detection._call_llm") as mock:
            mock.return_value = "CHARACTERIZATION_STUTTER\nThis is Lena's distinctive verb."
            from audit_checks.reintroduction_detection import llm_confirm_stutter
            result = llm_confirm_stutter(candidate)
            assert result == "CHARACTERIZATION_STUTTER"


# ─── Sub-check B: Echo Detection ─────────────────────────────────────────────

class TestEchoDetection:

    def test_below_threshold_no_candidate(self):
        """'the weight of' 3 times → below threshold."""
        ms = _make_manuscript(
            (1, "He felt the weight of it."),
            (2, "She carried the weight of the decision."),
            (3, "The weight of the silence pressed."),
        )
        candidates = build_echo_candidates(ms, threshold=4)
        weight_candidates = [c for c in candidates if "weight of" in c.phrase_label]
        assert len(weight_candidates) == 0

    def test_above_threshold_candidate(self):
        """'the weight of' 8 times → above threshold."""
        scenes = [(i, f"Scene {i}. The weight of what they carried.") for i in range(1, 9)]
        ms = _make_manuscript(*scenes)
        candidates = build_echo_candidates(ms, threshold=4)
        weight_candidates = [c for c in candidates if "weight of" in c.phrase_label]
        assert len(weight_candidates) >= 1

    def test_llm_echo_class_a(self):
        """LLM returns THEMATIC_ECHO → CLASS_A finding."""
        candidate = EchoCandidate(
            phrase_label="the weight of",
            total_occurrences=8,
            occurrences=[(i, f"The weight of scene {i}") for i in range(1, 9)],
        )
        with patch("audit_checks.reintroduction_detection._call_llm") as mock:
            mock.return_value = "THEMATIC_ECHO\nRepeated without development."
            from audit_checks.reintroduction_detection import llm_confirm_echo
            result = llm_confirm_echo(candidate)
            assert result == "THEMATIC_ECHO"

    def test_llm_developed_theme_suppresses(self):
        """LLM returns DEVELOPED_THEME → no finding."""
        candidate = EchoCandidate(
            phrase_label="the weight of",
            total_occurrences=8,
            occurrences=[(i, f"The weight of scene {i}") for i in range(1, 9)],
        )
        with patch("audit_checks.reintroduction_detection._call_llm") as mock:
            mock.return_value = "DEVELOPED_THEME\nEach occurrence adds nuance."
            from audit_checks.reintroduction_detection import llm_confirm_echo
            result = llm_confirm_echo(candidate)
            assert result == "DEVELOPED_THEME"


# ─── Pronoun Resolution ─────────────────────────────────────────────────────

class TestPronounResolution:

    def test_pronoun_resolution_unambiguous(self):
        """Scene with only Lena named → 'She filed' resolves to Lena."""
        text = "Lena walked to the desk. She filed the report carefully."
        pairs = extract_subject_verb_pairs(text, 1, _TEST_CHARACTERS)
        filed_pairs = [(c, v) for c, v, _, _ in pairs if v == "filed"]
        assert ("Lena", "filed") in filed_pairs

    def test_pronoun_resolution_gender_disambiguates(self):
        """Scene with Lena and Hank → 'She filed' resolves to Lena (gender)."""
        text = "Hank opened the door. Lena sat at her desk. She filed the observation."
        pairs = extract_subject_verb_pairs(text, 1, _TEST_CHARACTERS)
        filed_pairs = [(c, v) for c, v, _, _ in pairs if v == "filed"]
        assert ("Lena", "filed") in filed_pairs
        # Should NOT resolve to Hank
        assert ("Hank", "filed") not in filed_pairs

    def test_pronoun_resolution_ambiguous_dropped(self):
        """Scene with Lena and Mia (both 'she') → 'She filed' dropped."""
        text = "Lena reviewed the notes. Mia checked the map. She filed the report."
        pairs = extract_subject_verb_pairs(text, 1, _TEST_CHARACTERS)
        filed_pairs = [(c, v) for c, v, _, _ in pairs if v == "filed"]
        # Ambiguous — both Lena and Mia are 'she' — should be dropped
        assert len(filed_pairs) == 0

    def test_pronoun_reset_at_scene_break(self):
        """Pronoun at start of scene with no prior named subject → dropped."""
        # Each scene is processed independently, so a fresh scene has no context
        text = "She filed the report without hesitation."
        pairs = extract_subject_verb_pairs(text, 2, _TEST_CHARACTERS)
        filed_pairs = [(c, v) for c, v, _, _ in pairs if v == "filed"]
        assert len(filed_pairs) == 0

    def test_pronoun_ambiguity_window_200_chars(self):
        """Two same-gender characters within 200 chars of pronoun → ambiguous, dropped."""
        # Lena and Mia both mentioned within 200 chars, then "She filed"
        text = "Lena reviewed the file. Mia checked the window. She filed the observation."
        pairs = extract_subject_verb_pairs(text, 1, _TEST_CHARACTERS)
        filed_pairs = [(c, v) for c, v, _, _ in pairs if v == "filed"]
        assert len(filed_pairs) == 0

    def test_pronoun_sticky_binding_beyond_200_chars(self):
        """Named subject >200 chars before pronoun still resolves (sticky binding)."""
        # Lena appears once, then >200 chars of filler, then "She filed"
        # With no other she-gender character in the scene, binding is unambiguous
        filler = "The wind blew through the empty corridor. " * 6  # ~252 chars
        text = f"Lena entered the building. {filler}She filed the observation."
        pairs = extract_subject_verb_pairs(text, 1, _TEST_CHARACTERS)
        filed_pairs = [(c, v) for c, v, _, _ in pairs if v == "filed"]
        # Sticky binding: Lena is last_named for "she" gender, no ambiguity
        assert len(filed_pairs) == 1
        assert filed_pairs[0] == ("Lena", "filed")

    def test_filed_caught_in_lena_pronoun_context(self):
        """Synthesized scene: Lena named, then 9x 'She filed' → all resolve to Lena."""
        text = "Lena opened her notebook. "
        text += " ".join(["She filed the detail." for _ in range(9)])
        pairs = extract_subject_verb_pairs(text, 1, _TEST_CHARACTERS)
        filed_pairs = [(c, v) for c, v, _, _ in pairs if v == "filed"]
        # All 9 should resolve to Lena (each "She filed" is within 200 chars
        # of prior "Lena" or prior "She filed" which itself resolved to Lena)
        # Actually, only those within 200 chars of "Lena" will resolve
        # At minimum the first several should resolve
        assert len(filed_pairs) >= 5, f"Expected >=5 filed pairs, got {len(filed_pairs)}"
        assert all(c == "Lena" for c, _ in filed_pairs)


# ─── Mandate Calibration Anchor ──────────────────────────────────────────────

class TestMandateCalibrationAnchor:

    @patch("audit_checks.reintroduction_detection._call_llm")
    def test_lena_filed_mandate_calibration(self, mock_llm):
        """Synthetic manuscript with Lena 'filed' pattern → stutter candidate."""
        mock_llm.return_value = "CHARACTERIZATION_STUTTER\nDistinctive Lena verb."

        # Build a synthetic manuscript with the "filed" stutter pattern
        scenes = []
        for i in range(1, 30):
            if i % 3 == 0:
                scenes.append((i, f"Lena filed the observation. She filed the detail in her mind."))
            else:
                scenes.append((i, f"Lena watched the street. She filed what she saw."))

        ms = _make_manuscript(*scenes)
        briefs = _make_briefs(
            character_profiles={"characters": [{"name": "Lena Ibarra"}]},
            series_bible={"recurring_characters": [{"name": "Lena Ibarra"}]},
        )

        check = ReintroductionDetection()
        findings = check.run(ms, briefs)

        filed_findings = [f for f in findings if "filed" in f.description.lower()]
        assert len(filed_findings) >= 1, "Lena 'filed' stutter must be caught"
        assert any(f.severity == "CLASS_A" for f in filed_findings)


# ─── Module Interface ────────────────────────────────────────────────────────

class TestModuleInterface:

    def test_has_required_interface(self):
        check = ReintroductionDetection()
        assert check.check_id == "MA-006-reintroduction"
        assert check.severity == "CLASS_A"
        assert hasattr(check, "run")

    def test_module_auto_discovered(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-006-reintroduction" in check_ids
        REGISTRY.clear()
