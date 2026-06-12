"""
Tests for MA-047 F-INT-9 Part 2: LLM-extracted, ledger-judged fact types.

Covers:
  - 3-pass majority vote mechanism
  - LLM-gated count/side severity (CLASS_A on 2/3, CLASS_B on 1/3)
  - Planted-defect acceptance test (violations caught, clean passes)
  - Module interface unchanged
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import ManuscriptArtifact, BriefBundle, SceneText, Finding
from audit_checks.entity_consistency import (
    EntityConsistencyCheck,
    _majority_vote_extract,
    _llm_extract_entity_facts,
    _SEVERITY_BY_FACT,
    _LLM_GATED_FACTS,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_manuscript(*scene_texts):
    """Create a manuscript from (scene_number, text) tuples."""
    scenes = [
        SceneText(scene_number=sn, text=text, file_path=f"/fake/sc_{sn:03d}.md")
        for sn, text in scene_texts
    ]
    return ManuscriptArtifact(scenes=scenes, manuscript_dir="/fake")


def _make_ledger(entities):
    """Create a minimal sealed ledger."""
    return {
        "ledger_meta": {"sealed": True, "schema_version": "1.0.0"},
        "entities": entities,
        "provenance": {},
    }


def _make_briefs(ledger):
    return BriefBundle(entity_ledger=ledger)


# ── Severity map tests ──────────────────────────────────────────────────

class TestSeverityMap:

    def test_count_is_class_a(self):
        assert _SEVERITY_BY_FACT["count"] == "CLASS_A"

    def test_side_is_class_a(self):
        assert _SEVERITY_BY_FACT["side"] == "CLASS_A"

    def test_forbidden_state_is_class_a(self):
        assert _SEVERITY_BY_FACT["forbidden_state"] == "CLASS_A"

    def test_role_violation_is_class_a(self):
        assert _SEVERITY_BY_FACT["role_violation"] == "CLASS_A"

    def test_llm_gated_facts_set(self):
        assert "count" in _LLM_GATED_FACTS
        assert "side" in _LLM_GATED_FACTS
        assert "designation" not in _LLM_GATED_FACTS


# ── Majority vote tests ─────────────────────────────────────────────────

class TestMajorityVote:

    @patch("audit_checks.entity_consistency._llm_extract_entity_facts")
    def test_2_of_3_passes_yields_majority(self, mock_extract):
        """2/3 passes agreeing on same value → vote_count >= 2."""
        mock_extract.side_effect = [
            [{"fact_type": "count", "asserted_value": 9, "excerpt": "nine rotors"}],
            [{"fact_type": "count", "asserted_value": 9, "excerpt": "nine rotors"}],
            [{"fact_type": "count", "asserted_value": 8, "excerpt": "eight rotors"}],
        ]
        entity = {
            "canonical_name": "KL-7 cipher rotors",
            "aliases": ["rotors"],
            "invariants": {"count": 8},
        }
        results = _majority_vote_extract(entity, "scene text", n_passes=3)
        nine_results = [r for r in results if r["asserted_value"] == 9]
        assert len(nine_results) == 1
        assert nine_results[0]["vote_count"] == 2

    @patch("audit_checks.entity_consistency._llm_extract_entity_facts")
    def test_1_of_3_no_majority(self, mock_extract):
        """1/3 agreement → vote_count == 1 (no majority)."""
        mock_extract.side_effect = [
            [{"fact_type": "count", "asserted_value": 7, "excerpt": "seven"}],
            [{"fact_type": "count", "asserted_value": 8, "excerpt": "eight"}],
            [{"fact_type": "count", "asserted_value": 9, "excerpt": "nine"}],
        ]
        entity = {
            "canonical_name": "KL-7 cipher rotors",
            "aliases": ["rotors"],
            "invariants": {"count": 8},
        }
        results = _majority_vote_extract(entity, "scene text", n_passes=3)
        for r in results:
            assert r["vote_count"] == 1

    @patch("audit_checks.entity_consistency._llm_extract_entity_facts")
    def test_no_assertions_yields_empty(self, mock_extract):
        """All passes return no assertions → empty results."""
        mock_extract.return_value = []
        entity = {
            "canonical_name": "KL-7 cipher rotors",
            "aliases": [],
            "invariants": {"count": 8},
        }
        results = _majority_vote_extract(entity, "scene text", n_passes=3)
        assert results == []


# ── Planted-defect acceptance tests ──────────────────────────────────────

class TestPlantedDefects:
    """Fixture manuscript with one known violation per fact type.
    Clean fixture must produce zero findings."""

    LEDGER = _make_ledger([
        {
            "id": "test_rotors",
            "canonical_name": "KL-7 cipher rotors",
            "aliases": ["cipher rotors", "rotors"],
            "entity_class": "scalar",
            "invariants": {"count": 8, "designation": "KL-7"},
        },
        {
            "id": "test_burn",
            "canonical_name": "Coyle burn damage",
            "aliases": [],
            "entity_class": "scalar",
            "invariants": {"side": "right"},
        },
    ])

    def test_clean_fixture_zero_findings(self):
        """Manuscript with correct facts → 0 findings."""
        ms = _make_manuscript(
            (1, "He counted the eight KL-7 rotors in the pouch. All eight present."),
            (2, "The burns down his right arm and right side were getting worse."),
        )
        briefs = _make_briefs(self.LEDGER)
        check = EntityConsistencyCheck()
        findings = check.run(ms, briefs)
        # Designation + count should match → no findings
        assert len(findings) == 0, f"Expected 0 findings, got: {[f.description for f in findings]}"

    def test_designation_violation_caught(self):
        """Manuscript with wrong designation → CLASS_A finding."""
        ms = _make_manuscript(
            (1, "He examined the KL-47 rotors. The cipher machine variant was unfamiliar."),
        )
        briefs = _make_briefs(self.LEDGER)
        check = EntityConsistencyCheck()
        findings = check.run(ms, briefs)
        designation_findings = [f for f in findings if "Designation" in f.description]
        assert len(designation_findings) >= 1
        assert designation_findings[0].severity == "CLASS_A"
        assert designation_findings[0].scene_number == 1

    @patch("audit_checks.entity_consistency._majority_vote_extract")
    def test_count_violation_llm_confirmed_class_a(self, mock_vote):
        """Count mismatch confirmed by 2/3 LLM passes → CLASS_A."""
        mock_vote.return_value = [
            {"fact_type": "count", "asserted_value": 9, "excerpt": "nine rotors",
             "vote_count": 2, "total_passes": 3},
        ]
        ms = _make_manuscript(
            (1, "He counted nine cipher rotors in the pouch."),
        )
        briefs = _make_briefs(self.LEDGER)
        check = EntityConsistencyCheck()
        findings = check.run(ms, briefs)
        count_findings = [f for f in findings if "Count" in f.description]
        assert len(count_findings) >= 1
        assert count_findings[0].severity == "CLASS_A"
        assert "LLM-confirmed" in count_findings[0].description

    @patch("audit_checks.entity_consistency._majority_vote_extract")
    def test_count_violation_llm_unconfirmed_class_b(self, mock_vote):
        """Count mismatch with only 1/3 LLM agreement → CLASS_B."""
        mock_vote.return_value = [
            {"fact_type": "count", "asserted_value": 9, "excerpt": "nine rotors",
             "vote_count": 1, "total_passes": 3},
        ]
        ms = _make_manuscript(
            (1, "He counted nine cipher rotors in the pouch."),
        )
        briefs = _make_briefs(self.LEDGER)
        check = EntityConsistencyCheck()
        findings = check.run(ms, briefs)
        count_findings = [f for f in findings if "Count" in f.description]
        assert len(count_findings) >= 1
        assert count_findings[0].severity == "CLASS_B"
        assert "LLM-unconfirmed" in count_findings[0].description

    @patch("audit_checks.entity_consistency._majority_vote_extract")
    def test_side_violation_llm_confirmed_class_a(self, mock_vote):
        """Side mismatch confirmed by LLM majority → CLASS_A."""
        mock_vote.return_value = [
            {"fact_type": "side", "asserted_value": "left", "excerpt": "left side burns",
             "vote_count": 3, "total_passes": 3},
        ]
        ms = _make_manuscript(
            # Include character name "Coyle" in proximity for the side scanner
            (1, "Coyle winced as the medic checked the burns on his left arm. The left side wound was bleeding again."),
        )
        briefs = _make_briefs(self.LEDGER)
        check = EntityConsistencyCheck()
        findings = check.run(ms, briefs)
        side_findings = [f for f in findings if "Side" in f.description]
        assert len(side_findings) >= 1
        assert side_findings[0].severity == "CLASS_A"


# ── Module interface ─────────────────────────────────────────────────────

class TestModuleInterface:

    def test_check_id_and_severity(self):
        check = EntityConsistencyCheck()
        assert check.check_id == "MA-047-entity-consistency"
        assert check.severity == "CLASS_A"

    def test_empty_ledger_returns_empty(self):
        ms = _make_manuscript((1, "Some text."))
        briefs = BriefBundle()
        check = EntityConsistencyCheck()
        assert check.run(ms, briefs) == []

    def test_unsealed_ledger_skipped(self):
        ledger = {"ledger_meta": {"sealed": False}, "entities": []}
        briefs = BriefBundle(entity_ledger=ledger)
        ms = _make_manuscript((1, "Some text."))
        check = EntityConsistencyCheck()
        assert check.run(ms, briefs) == []
