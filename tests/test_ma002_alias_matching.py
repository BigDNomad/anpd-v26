"""
Tests for MA-002 alias matching improvement (2026-06-13).

Roster matching must recognize first-name-only and surname-only references
to rostered full names before flagging "invented character."

1. "Silas" matches roster entry "silas vance" (first-name reference).
2. "Kowalski" matches roster entry "meat kowalski" (surname reference).
3. Genuinely novel name still flags CLASS_A.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks.character_name_registry import (
    check_names_against_roster,
    CharacterAppearance,
    _build_roster_parts_index,
)


def _make_roster() -> set[str]:
    """Roster containing multi-word names (full names with callsigns)."""
    return {
        "silas vance",
        "meat kowalski",
        "sparky miller",
        "animal motherway",
        "danny archer",
        "tom coyle",
    }


def _make_banned() -> set[str]:
    return set()


class TestFirstNameMatch:

    def test_silas_matches_silas_vance(self):
        """'Silas' (first name only) must not be flagged when 'silas vance' is in roster."""
        apps = [CharacterAppearance("Silas", 26, True, '"Silas," he said.')]
        findings = check_names_against_roster(apps, _make_roster(), _make_banned())
        assert findings == [], (
            f"Expected no findings for 'Silas' (matches 'silas vance'), "
            f"got: {[f.description for f in findings]}"
        )


class TestSurnameMatch:

    def test_kowalski_matches_meat_kowalski(self):
        """'Kowalski' (surname only) must not be flagged when 'meat kowalski' is in roster."""
        apps = [CharacterAppearance("Kowalski", 2, True, "Kowalski is your other PJ")]
        findings = check_names_against_roster(apps, _make_roster(), _make_banned())
        assert findings == [], (
            f"Expected no findings for 'Kowalski' (matches 'meat kowalski'), "
            f"got: {[f.description for f in findings]}"
        )

    def test_miller_matches_sparky_miller(self):
        """'Miller' (surname only) must not be flagged."""
        apps = [CharacterAppearance("Miller", 2, True, "Miller handles the guns")]
        findings = check_names_against_roster(apps, _make_roster(), _make_banned())
        assert findings == []

    def test_motherway_matches_animal_motherway(self):
        """'Motherway' (surname only) must not be flagged."""
        apps = [CharacterAppearance("Motherway", 2, True, "Motherway is your copilot")]
        findings = check_names_against_roster(apps, _make_roster(), _make_banned())
        assert findings == []


class TestNovelNameStillFlags:

    def test_genuinely_novel_name_flags_class_a(self):
        """A name not matching any roster entry must still flag CLASS_A."""
        apps = [CharacterAppearance("Briggs", 4, True, "sensor operator named Briggs")]
        findings = check_names_against_roster(apps, _make_roster(), _make_banned())
        assert len(findings) == 1
        assert findings[0].severity == "CLASS_A"
        assert "Briggs" in findings[0].description


class TestRosterPartsIndex:

    def test_index_maps_parts_to_full_names(self):
        """The reverse index maps individual name parts to their full roster entry."""
        roster = {"silas vance", "meat kowalski", "danny archer"}
        index = _build_roster_parts_index(roster)
        assert "silas" in index
        assert "vance" in index
        assert "kowalski" in index
        assert "meat" in index
        assert "archer" in index
        assert "danny" in index

    def test_single_word_roster_entry_not_indexed(self):
        """Single-word roster entries don't produce reverse index entries."""
        roster = {"archer", "coyle"}
        index = _build_roster_parts_index(roster)
        assert len(index) == 0

    def test_short_parts_excluded(self):
        """Parts with 2 or fewer chars are excluded from the index."""
        roster = {"jo smith"}
        index = _build_roster_parts_index(roster)
        assert "jo" not in index  # len <= 2
        assert "smith" in index
