"""
Tests for MA-002 schema 1.1.0 migration.

Validates:
  - Name-keyed profile format is read correctly
  - Legacy array format still works
  - Series ∪ book profiles are merged into roster
  - Airmen series file in array format = FAIL validation
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import BriefBundle
from audit_checks.character_name_registry import build_canonical_roster


class TestNameKeyedFormat:

    def test_name_keyed_profiles_read(self):
        """Schema 1.1.0 name-keyed profiles add names to roster."""
        briefs = BriefBundle(
            character_profiles={
                "Danny Archer": {"name": "Danny Archer", "character_role": "protagonist"},
                "Tom Coyle": {"name": "Tom Coyle", "character_role": "supporting"},
            },
        )
        roster, _ = build_canonical_roster(briefs)
        assert "danny archer" in roster
        assert "tom coyle" in roster
        assert "archer" in roster
        assert "coyle" in roster

    def test_legacy_array_format_still_works(self):
        """Legacy {characters: [...]} format must still be recognized."""
        briefs = BriefBundle(
            character_profiles={
                "characters": [
                    {"name": "Danny Archer", "role": "protagonist"},
                    {"name": "Tom Coyle", "role": "supporting"},
                ],
            },
        )
        roster, _ = build_canonical_roster(briefs)
        assert "danny archer" in roster
        assert "tom coyle" in roster


class TestSeriesBookMerge:

    def test_roster_merges_series_and_book(self):
        """Roster must include names from both series-level and book-level profiles."""
        briefs = BriefBundle(
            character_profiles={
                "Danny Archer": {"name": "Danny Archer", "character_role": "protagonist"},
            },
            book_character_profiles={
                "Bounmy": {"name": "Bounmy", "character_role": "minor"},
                "Whitfield": {"name": "Whitfield", "character_role": "minor"},
            },
        )
        roster, _ = build_canonical_roster(briefs)
        assert "danny archer" in roster, "Series-level name must be in roster"
        assert "bounmy" in roster, "Book-level name must be in roster"
        assert "whitfield" in roster, "Book-level name must be in roster"

    def test_book_profiles_alone(self):
        """Book-level profiles work even if series-level is empty."""
        briefs = BriefBundle(
            character_profiles={},
            book_character_profiles={
                "Phomma": {"name": "Phomma", "character_role": "minor"},
            },
        )
        roster, _ = build_canonical_roster(briefs)
        assert "phomma" in roster

    def test_relationships_from_name_keyed(self):
        """Relationship names are extracted from name-keyed profiles."""
        briefs = BriefBundle(
            character_profiles={
                "Danny Archer": {
                    "name": "Danny Archer",
                    "relationships": {"Silas Vance": "mentor"},
                },
            },
        )
        roster, _ = build_canonical_roster(briefs)
        assert "silas vance" in roster
        assert "vance" in roster


class TestAirmenSchemaValidation:

    def test_airmen_series_file_is_name_keyed(self):
        """Airmen series character_profiles.json must NOT be in array format."""
        path = "/anpd/v26/series/airmen/character_profiles.json"
        if not os.path.isfile(path):
            pytest.skip("Airmen series profiles not available")

        with open(path) as f:
            data = json.load(f)

        assert "characters" not in data, (
            "Airmen series character_profiles.json must use schema 1.1.0 "
            "(name-keyed), not legacy array format"
        )
        # Verify it's a dict of character dicts
        assert isinstance(data, dict)
        for key, val in data.items():
            assert isinstance(val, dict), f"Value for '{key}' must be a dict"
            assert val.get("name") == key, (
                f"Entry '{key}' must have name == key, got name={val.get('name')}"
            )

    def test_airmen_book_profiles_are_name_keyed(self):
        """Airmen b01 book character_profiles.json must be name-keyed."""
        path = "/anpd/v26/series/airmen/b01/work/character_profiles.json"
        if not os.path.isfile(path):
            pytest.skip("Airmen b01 book profiles not available")

        with open(path) as f:
            data = json.load(f)

        assert "characters" not in data
        assert isinstance(data, dict)

    def test_airmen_full_roster_includes_minors(self):
        """MA-002 roster built from airmen series + b01 book includes all 7 minors."""
        series_path = "/anpd/v26/series/airmen/character_profiles.json"
        book_path = "/anpd/v26/series/airmen/b01/work/character_profiles.json"
        bible_path = "/anpd/v26/series/airmen/series_bible.json"

        for p in [series_path, book_path, bible_path]:
            if not os.path.isfile(p):
                pytest.skip(f"{p} not available")

        with open(series_path) as f:
            series_profiles = json.load(f)
        with open(book_path) as f:
            book_profiles = json.load(f)
        with open(bible_path) as f:
            bible = json.load(f)

        briefs = BriefBundle(
            series_bible=bible,
            character_profiles=series_profiles,
            book_character_profiles=book_profiles,
        )
        roster, _ = build_canonical_roster(briefs)

        minors = ["bounmy", "briggs", "harwick", "paulson", "phomma", "phommasack", "whitfield"]
        for name in minors:
            assert name in roster, f"Minor '{name}' must be in merged roster"
