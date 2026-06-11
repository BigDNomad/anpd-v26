"""Tests for V25 intake_validator."""
import json
import os
import tempfile
import pytest
from intake_validator import validate_intake


@pytest.fixture
def valid_intake_data():
    return {
        "book_number": 1,
        "title": "Broken Sabers",
        "series": "Hadeon's Cossacks",
        "total_chapter_count": 8,
        "target_word_count": 85000,
        "outline_path": "",  # Will be set per test
        "historical_window": {"start_date": "2018-12-01", "end_date": "2022-04-15"},
        "historical_anchors_in_scope": ["February 24 2022 invasion"],
        "historical_anchors_out_of_scope": ["Bucha massacre"],
    }


@pytest.fixture
def valid_intake_file(valid_intake_data, tmp_path):
    outline = tmp_path / "outline.md"
    outline.write_text("# Chapter 1\nSome content.")
    valid_intake_data["outline_path"] = str(outline)
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(valid_intake_data))
    return str(intake_path)


def test_valid_intake_passes(valid_intake_file):
    result = validate_intake(valid_intake_file)
    assert result.passed
    assert len(result.errors) == 0


def test_missing_file_fails():
    result = validate_intake("/nonexistent/intake.json")
    assert not result.passed
    assert any("not found" in e for e in result.errors)


def test_missing_required_field(valid_intake_data, tmp_path):
    del valid_intake_data["title"]
    outline = tmp_path / "outline.md"
    outline.write_text("# Chapter 1\nContent.")
    valid_intake_data["outline_path"] = str(outline)
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(valid_intake_data))
    result = validate_intake(str(path))
    assert not result.passed
    assert any("title" in e for e in result.errors)


def test_type_mismatch(valid_intake_data, tmp_path):
    valid_intake_data["book_number"] = "one"  # should be int
    outline = tmp_path / "outline.md"
    outline.write_text("# Chapter 1\nContent.")
    valid_intake_data["outline_path"] = str(outline)
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(valid_intake_data))
    result = validate_intake(str(path))
    assert not result.passed
    assert any("Type mismatch" in e for e in result.errors)


def test_outline_path_must_exist(valid_intake_data, tmp_path):
    valid_intake_data["outline_path"] = "/nonexistent/outline.pdf"
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(valid_intake_data))
    result = validate_intake(str(path))
    assert not result.passed
    assert any("outline_path" in e for e in result.errors)


def test_optional_fields_produce_warnings(valid_intake_file):
    result = validate_intake(valid_intake_file)
    assert result.passed
    assert len(result.warnings) > 0  # missing optional fields


def test_intake_without_ratios_passes(valid_intake_data, tmp_path):
    """Intake without scene_type_ratio and pov_balance validates successfully.
    Operator decision 2026-05-09: outline-fidelity is the sole structural test."""
    outline = tmp_path / "outline.md"
    outline.write_text("# Chapter 1\nContent.")
    valid_intake_data["outline_path"] = str(outline)
    assert "scene_type_ratio" not in valid_intake_data
    assert "pov_balance" not in valid_intake_data
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(valid_intake_data))
    result = validate_intake(str(path))
    assert result.passed
