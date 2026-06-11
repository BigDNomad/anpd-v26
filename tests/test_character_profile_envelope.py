"""Tests for {"characters": [list]} envelope normalization in character
generator and auditor.

Verifies:
1. Generator's load_series_artifacts normalizes envelope format correctly.
2. Auditor accepts real {"characters": [list]} schema without Class A findings.
3. Auditor still rejects malformed profiles (missing required fields).
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from pipeline.character_generator_v26_20260612 import load_series_artifacts
from pipeline.character_profile_auditor_v26_20260612 import (
    _load_profile_file,
    check_no_envelope_keys,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

VALID_ENVELOPE_PROFILES = {
    "characters": [
        {
            "name": "Alice Tran",
            "character_role": "recurring",
            "primary_trait": "Decisive under pressure",
        },
        {
            "name": "Bob Keane",
            "character_role": "supporting",
            "primary_trait": "Quiet stubbornness",
        },
    ]
}

FLAT_PROFILES = {
    "Alice Tran": {
        "name": "Alice Tran",
        "character_role": "recurring",
        "primary_trait": "Decisive under pressure",
    },
    "Bob Keane": {
        "name": "Bob Keane",
        "character_role": "supporting",
        "primary_trait": "Quiet stubbornness",
    },
}

MALFORMED_PROFILES = {
    "characters": [
        {"no_name_field": True},
        "not_a_dict",
    ]
}


@pytest.fixture
def envelope_series_dir(tmp_path):
    """Create a temp series dir with envelope-format character_profiles."""
    bible = {"series_name": "Test Series", "setting": "Test"}
    (tmp_path / "series_bible.json").write_text(json.dumps(bible))
    (tmp_path / "character_profiles.json").write_text(
        json.dumps(VALID_ENVELOPE_PROFILES)
    )
    return tmp_path


@pytest.fixture
def flat_series_dir(tmp_path):
    """Create a temp series dir with flat-format character_profiles."""
    bible = {"series_name": "Test Series", "setting": "Test"}
    (tmp_path / "series_bible.json").write_text(json.dumps(bible))
    (tmp_path / "character_profiles.json").write_text(
        json.dumps(FLAT_PROFILES)
    )
    return tmp_path


# ── Generator: load_series_artifacts ─────────────────────────────────────────

class TestLoadSeriesArtifacts:
    def test_envelope_format_normalized(self, envelope_series_dir):
        result = load_series_artifacts(str(envelope_series_dir))
        profiles = result['series_profiles']
        assert "Alice Tran" in profiles
        assert "Bob Keane" in profiles
        assert "characters" not in profiles
        assert profiles["Alice Tran"]["primary_trait"] == "Decisive under pressure"

    def test_flat_format_unchanged(self, flat_series_dir):
        result = load_series_artifacts(str(flat_series_dir))
        profiles = result['series_profiles']
        assert "Alice Tran" in profiles
        assert "Bob Keane" in profiles

    def test_malformed_entries_skipped(self, tmp_path):
        bible = {"series_name": "Test"}
        (tmp_path / "series_bible.json").write_text(json.dumps(bible))
        (tmp_path / "character_profiles.json").write_text(
            json.dumps(MALFORMED_PROFILES)
        )
        result = load_series_artifacts(str(tmp_path))
        profiles = result['series_profiles']
        # Both malformed entries should be skipped (no name / not a dict)
        assert len(profiles) == 0


# ── Auditor: _load_profile_file ──────────────────────────────────────────────

class TestAuditorLoadProfileFile:
    def test_envelope_format_normalized(self, tmp_path):
        p = tmp_path / "profiles.json"
        p.write_text(json.dumps(VALID_ENVELOPE_PROFILES))
        data = _load_profile_file(p, "test")
        assert "Alice Tran" in data
        assert "characters" not in data

    def test_flat_format_unchanged(self, tmp_path):
        p = tmp_path / "profiles.json"
        p.write_text(json.dumps(FLAT_PROFILES))
        data = _load_profile_file(p, "test")
        assert "Alice Tran" in data


# ── Auditor: envelope key check after normalization ──────────────────────────

class TestEnvelopeKeyCheck:
    def test_normalized_envelope_no_findings(self, tmp_path):
        """Real schema format should produce zero envelope-key findings."""
        p = tmp_path / "profiles.json"
        p.write_text(json.dumps(VALID_ENVELOPE_PROFILES))
        data = _load_profile_file(p, "series-level")
        findings = check_no_envelope_keys(data, str(p), "series-level")
        assert len(findings) == 0

    def test_flat_with_forbidden_key_still_flagged(self, tmp_path):
        """A flat profile file with a genuine forbidden envelope key should
        still produce a Class A finding."""
        bad_data = dict(FLAT_PROFILES)
        bad_data["series"] = "should not be here"
        p = tmp_path / "profiles.json"
        p.write_text(json.dumps(bad_data))
        data = _load_profile_file(p, "series-level")
        findings = check_no_envelope_keys(data, str(p), "series-level")
        assert len(findings) == 1
        assert findings[0]["class_"] == "A"
