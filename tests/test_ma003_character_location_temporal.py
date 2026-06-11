"""
Tests for MA-003 character_location_temporal — character location contradiction check.

Covers:
  - Distance classification (same_city, same_country, different_country)
  - Phase 1 regex extraction of location claims
  - Candidate pair generation
  - End-to-end Funes wife Caracas/Maracaibo contradiction
  - Narrative bridge suppression
  - Module interface and auto-discovery
"""

from __future__ import annotations

import os
import sys
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
from audit_checks.character_location_temporal import (
    CharacterLocationTemporal,
    LocationClaim,
    CandidatePair,
    distance_class,
    is_travel_plausible,
    extract_location_claims,
    build_candidate_pairs,
    normalize_entity_key,
)
from audit_checks._lib.timeline_extractor import SceneTimeline


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_manuscript(*scenes):
    return ManuscriptArtifact(
        scenes=[SceneText(scene_number=n, text=t, file_path=f"/fake/sc_{n:03d}.md")
                for n, t in scenes],
        manuscript_dir="/fake",
    )


# ─── Distance Classification ─────────────────────────────────────────────────

class TestDistanceClass:

    def test_same_city(self):
        assert distance_class("Caracas", "Caracas") == "same_city"

    def test_same_country(self):
        assert distance_class("Caracas", "Maracaibo") == "same_country"

    def test_different_country(self):
        assert distance_class("Caracas", "Madrid") == "different_country"

    def test_unknown_pair_defaults_different_country(self):
        assert distance_class("Helsinki", "Tokyo") == "different_country"

    def test_washington_langley_same_city(self):
        assert distance_class("Washington", "Langley") == "same_city"

    def test_case_insensitive(self):
        assert distance_class("caracas", "MARACAIBO") == "same_country"


# ─── Travel Plausibility ─────────────────────────────────────────────────────

class TestTravelPlausibility:

    def test_same_city_always_plausible(self):
        assert is_travel_plausible(0.0, "same_city") is True

    def test_same_country_needs_more_than_one_day(self):
        assert is_travel_plausible(0.5, "same_country") is False
        assert is_travel_plausible(1.0, "same_country") is False  # boundary: not plausible
        assert is_travel_plausible(1.1, "same_country") is True

    def test_different_country_needs_more_than_half_day(self):
        assert is_travel_plausible(0.3, "different_country") is False
        assert is_travel_plausible(0.5, "different_country") is False  # boundary: not plausible
        assert is_travel_plausible(0.6, "different_country") is True

    def test_none_elapsed_is_not_plausible(self):
        assert is_travel_plausible(None, "same_country") is False


# ─── Phase 1: Extraction ─────────────────────────────────────────────────────

class TestPhase1Extraction:

    def test_extracts_simple_present_in_claim(self):
        """'His wife was still in Caracas' → claim with location='caracas'."""
        ms = _make_manuscript(
            (26, 'Funes sat down. His wife was still in Caracas. She knew nothing.'),
        )
        claims = extract_location_claims(ms)
        caracas_claims = [c for c in claims if c.location == "caracas"]
        assert len(caracas_claims) >= 1

    def test_extracts_possessive_relation_claim(self):
        """'His wife in Maracaibo' → claim with entity_key containing 'wife'."""
        ms = _make_manuscript(
            (28, 'Funes had been talking. He had a wife in Maracaibo. She knew nothing.'),
        )
        claims = extract_location_claims(ms)
        wife_claims = [c for c in claims if "wife" in c.entity_key]
        assert len(wife_claims) >= 1
        assert wife_claims[0].location == "maracaibo"

    def test_extracts_name_in_location(self):
        """'Hank was in Caracas' → claim for hank."""
        ms = _make_manuscript(
            (10, 'Hank was in Caracas. He watched the street.'),
        )
        claims = extract_location_claims(ms)
        hank_claims = [c for c in claims if c.entity_key == "hank"]
        assert len(hank_claims) >= 1
        assert hank_claims[0].location == "caracas"


# ─── Phase 1: Candidate Pairing ──────────────────────────────────────────────

class TestCandidatePairing:

    def test_pairs_two_different_locations_in_window(self):
        """Same entity in different locations within window → candidate generated."""
        claims = [
            LocationClaim(26, "funes_wife", "caracas", "wife still in Caracas"),
            LocationClaim(28, "funes_wife", "maracaibo", "wife in Maracaibo"),
        ]
        timelines = [
            SceneTimeline(scene_number=26, estimated_elapsed_days=10.0),
            SceneTimeline(scene_number=28, estimated_elapsed_days=10.5),
        ]
        candidates = build_candidate_pairs(claims, timelines)
        assert len(candidates) >= 1
        assert candidates[0].claim_a.location == "caracas"
        assert candidates[0].claim_b.location == "maracaibo"

    def test_no_pair_when_same_location(self):
        """Same entity in same location → no candidate."""
        claims = [
            LocationClaim(26, "funes_wife", "caracas", "wife still in Caracas"),
            LocationClaim(28, "funes_wife", "caracas", "wife in Caracas"),
        ]
        timelines = [
            SceneTimeline(scene_number=26, estimated_elapsed_days=10.0),
            SceneTimeline(scene_number=28, estimated_elapsed_days=10.5),
        ]
        candidates = build_candidate_pairs(claims, timelines)
        assert len(candidates) == 0

    def test_no_pair_when_outside_window(self):
        """Same entity in different locations but > MA003_WINDOW_SCENES apart → no candidate."""
        claims = [
            LocationClaim(1, "hank", "caracas", "Hank was in Caracas"),
            LocationClaim(50, "hank", "madrid", "Hank arrived in Madrid"),
        ]
        candidates = build_candidate_pairs(claims, None)
        assert len(candidates) == 0

    def test_no_pair_when_travel_plausible(self):
        """Different locations but enough elapsed time → no candidate."""
        claims = [
            LocationClaim(26, "funes_wife", "caracas", "wife still in Caracas"),
            LocationClaim(28, "funes_wife", "maracaibo", "wife in Maracaibo"),
        ]
        # Enough time for same_country travel (>1 day)
        timelines = [
            SceneTimeline(scene_number=26, estimated_elapsed_days=10.0),
            SceneTimeline(scene_number=28, estimated_elapsed_days=12.0),
        ]
        candidates = build_candidate_pairs(claims, timelines)
        assert len(candidates) == 0


# ─── End-to-End ──────────────────────────────────────────────────────────────

class TestEndToEnd:

    @patch("audit_checks.character_location_temporal._call_llm")
    def test_funes_wife_caracas_maracaibo(self, mock_llm):
        """Funes wife in Caracas (sc 26) vs Maracaibo (sc 28) → CLASS_A."""
        mock_llm.return_value = "CONTRADICTION_CONFIRMED\nThe wife is described in two different cities with no travel bridge."

        ms = _make_manuscript(
            (26, 'He was building the picture. His wife is still in Caracas. They have been separated for years.'),
            (28, 'Funes had been Prada\'s financial liaison. He had a wife in Maracaibo. She knew nothing about the work.'),
        )
        briefs = BriefBundle()
        check = CharacterLocationTemporal()
        findings = check.run(ms, briefs)

        location_findings = [f for f in findings if f.severity == "CLASS_A"]
        assert len(location_findings) >= 1
        assert any("caracas" in f.description.lower() and "maracaibo" in f.description.lower()
                    for f in location_findings)

    @patch("audit_checks.character_location_temporal._call_llm")
    def test_narrative_bridge_suppresses_finding(self, mock_llm):
        """When LLM returns NARRATIVE_BRIDGE_PRESENT → no finding."""
        mock_llm.return_value = "NARRATIVE_BRIDGE_PRESENT\nThe second scene mentions the flight to Madrid."

        ms = _make_manuscript(
            (10, 'Hank was in Caracas. He watched the street.'),
            (12, 'After the flight to Madrid, Hank settled in. He was now in Madrid.'),
        )
        briefs = BriefBundle()
        check = CharacterLocationTemporal()
        findings = check.run(ms, briefs)

        # Narrative bridge should suppress the finding
        contradiction_findings = [f for f in findings if f.severity == "CLASS_A"]
        assert len(contradiction_findings) == 0


# ─── Module Interface ────────────────────────────────────────────────────────

class TestModuleInterface:

    def test_has_required_interface(self):
        check = CharacterLocationTemporal()
        assert check.check_id == "MA-003-character-location-temporal"
        assert check.severity == "CLASS_A"
        assert hasattr(check, "run")
        assert hasattr(check, "description")

    def test_discover_registers_ma003(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-003-character-location-temporal" in check_ids
        REGISTRY.clear()
