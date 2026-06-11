"""
Tests for anpd_export — V25 filename convention export utility.

Covers:
  - Series-level file renaming (series_slug)
  - Book-level file renaming (book_slug)
  - Scene file renaming
  - Timestamped file renaming
  - Unmapped basename fallback
  - Error conditions (source not under series/, missing slug)
  - Destination directory creation
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anpd_export as exp


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def toy_series(tmp_path):
    """Create a minimal toy_book series tree for testing."""
    series_root = tmp_path / "series"
    toy_dir = series_root / "toy_book"
    toy_dir.mkdir(parents=True)

    # series_config.json
    config = {
        "genre": "test",
        "series_name": "Toy Series",
        "series_slug": "tby",
        "book_slugs": {"b01": "tby001"},
    }
    (toy_dir / "series_config.json").write_text(json.dumps(config))
    (toy_dir / "series_bible.json").write_text(json.dumps({"key": "value"}))
    (toy_dir / "banned_phrases.json").write_text(json.dumps({"names": []}))

    # Book-level files
    work_dir = toy_dir / "b01" / "work"
    work_dir.mkdir(parents=True)
    (work_dir / "intake.json").write_text(json.dumps({"intake": True}))
    (work_dir / "outline.md").write_text("# Outline\n")
    (work_dir / "sc_001.md").write_text("Scene one prose.")
    (work_dir / "synopsis_20260515_0253.md").write_text("# Synopsis\n")
    (work_dir / "weird_artifact.txt").write_text("unknown file")

    return str(series_root), str(toy_dir)


@pytest.fixture
def btd_series(tmp_path):
    """Create a minimal black_tide series tree for testing."""
    series_root = tmp_path / "series"
    btd_dir = series_root / "black_tide"
    btd_dir.mkdir(parents=True)

    config = {
        "genre": "thriller",
        "series_name": "Black Tide",
        "series_slug": "btd",
        "book_slugs": {"b01": "btd001"},
    }
    (btd_dir / "series_config.json").write_text(json.dumps(config))

    work_dir = btd_dir / "b01" / "work"
    work_dir.mkdir(parents=True)
    (work_dir / "intake.json").write_text(json.dumps({"intake": True}))

    return str(series_root), str(btd_dir)


def _export(source, dest, series_root, **kwargs):
    """Run export_file with patched SERIES_ROOT."""
    with patch.object(exp, "SERIES_ROOT", series_root):
        return exp.export_file(source, dest, **kwargs)


# ─── Test Cases ───────────────────────────────────────────────────────────────

class TestSeriesLevelRename:

    def test_series_level_intake_rename(self, toy_series, tmp_path):
        """series/toy_book/series_bible.json → series_bible_tby.json"""
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "series_bible.json")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "series_bible_tby.json"))


class TestBookLevelRename:

    def test_book_level_intake_rename(self, toy_series, tmp_path):
        """series/toy_book/b01/work/intake.json → intake_tby001.json"""
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "b01", "work", "intake.json")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "intake_tby001.json"))

    def test_book_level_outline_rename(self, toy_series, tmp_path):
        """series/toy_book/b01/work/outline.md → outline_tby001.md"""
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "b01", "work", "outline.md")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "outline_tby001.md"))


class TestSceneFileRename:

    def test_scene_file_rename(self, toy_series, tmp_path):
        """series/toy_book/b01/work/sc_001.md → sc_001_tby001.md"""
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "b01", "work", "sc_001.md")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "sc_001_tby001.md"))


class TestTimestampedRename:

    def test_timestamped_synopsis_rename(self, toy_series, tmp_path):
        """synopsis_20260515_0253.md → synopsis_tby001_20260515_0253.md"""
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "b01", "work", "synopsis_20260515_0253.md")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "synopsis_tby001_20260515_0253.md"))


class TestUnmappedBasename:

    def test_unmapped_basename_copied_as_is(self, toy_series, tmp_path, capsys):
        """Unknown basename → copied unchanged, warning printed."""
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "b01", "work", "weird_artifact.txt")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "weird_artifact.txt"))
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "weird_artifact.txt" in captured.err


class TestErrorConditions:

    def test_source_not_under_series_errors(self, tmp_path):
        """Source outside /anpd/v25/series/ → exit 1."""
        random_file = tmp_path / "random.json"
        random_file.write_text("{}")
        dest = str(tmp_path / "out")

        # Use a series_root that doesn't contain the file
        rc = _export(str(random_file), dest, str(tmp_path / "nonexistent_series"))
        assert rc == 1

    def test_missing_series_slug_errors(self, tmp_path):
        """series_config.json without series_slug → exit 1."""
        series_root = tmp_path / "series"
        bad_dir = series_root / "bad_series"
        bad_dir.mkdir(parents=True)

        # Config without series_slug
        config = {"genre": "test", "series_name": "Bad"}
        (bad_dir / "series_config.json").write_text(json.dumps(config))
        (bad_dir / "somefile.json").write_text("{}")

        source = os.path.join(str(bad_dir), "somefile.json")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, str(series_root))
        assert rc == 1


class TestCrossSeries:

    def test_btd_intake_rename(self, btd_series, tmp_path):
        """series/black_tide/b01/work/intake.json → intake_btd001.json"""
        series_root, btd_dir = btd_series
        source = os.path.join(btd_dir, "b01", "work", "intake.json")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "intake_btd001.json"))


class TestDestinationDirectory:

    def test_destination_directory_created(self, toy_series, tmp_path):
        """Destination path that doesn't exist → created, file copied."""
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "series_bible.json")
        dest = str(tmp_path / "deeply" / "nested" / "output")

        assert not os.path.isdir(dest)
        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isdir(dest)
        assert os.path.isfile(os.path.join(dest, "series_bible_tby.json"))


# ─── Publish-gate integration (Dispatch 2) ──────────────────────────────────

def _clean_report(class_a=0, class_b=2, class_c=5):
    """Build a minimal audit report with the given CLASS_A count."""
    findings = []
    for i in range(class_a):
        findings.append({
            "check_id": f"MA-{i+1:03d}",
            "severity": "CLASS_A",
            "description": f"class-a finding {i+1}",
            "evidence": ["x"],
        })
    for i in range(class_b):
        findings.append({
            "check_id": f"MA-{100+i:03d}",
            "severity": "CLASS_B",
            "description": f"class-b finding {i+1}",
            "evidence": [],
        })
    for i in range(class_c):
        findings.append({
            "check_id": f"MA-{200+i:03d}",
            "severity": "CLASS_C",
            "description": f"class-c finding {i+1}",
            "evidence": [],
        })
    return {
        "header": {},
        "summary": {
            "class_a": class_a,
            "class_b": class_b,
            "class_c": class_c,
            "total": class_a + class_b + class_c,
        },
        "findings_by_check": {},
        "all_findings": findings,
    }


@pytest.fixture
def manuscript_series(tmp_path):
    """Create a toy series with a manuscript file for gate testing."""
    series_root = tmp_path / "series"
    toy_dir = series_root / "toy_book"
    toy_dir.mkdir(parents=True)

    config = {
        "genre": "test",
        "series_name": "Toy Series",
        "series_slug": "tby",
        "book_slugs": {"b01": "tby001"},
    }
    (toy_dir / "series_config.json").write_text(json.dumps(config))

    ms_dir = toy_dir / "b01" / "work" / "manuscript_20260527_0953"
    ms_dir.mkdir(parents=True)
    ms_file = ms_dir / "act1_full.md"
    ms_file.write_text("# Chapter 1\n\nProse here.\n")

    return str(series_root), str(toy_dir), str(ms_file), ms_dir


class TestGateCleared:
    """Test 1: manuscript + clean report → CLEARED, byte-identical copy."""

    def test_manuscript_cleared_exports(self, manuscript_series, tmp_path):
        series_root, toy_dir, ms_file, ms_dir = manuscript_series
        dest = str(tmp_path / "out")

        # Write a clean audit report
        report_path = tmp_path / "report.json"
        report_path.write_text(json.dumps(_clean_report(class_a=0)))

        rc = _export(ms_file, dest, series_root, audit_report=str(report_path))
        assert rc == 0

        # File was copied with the renamed convention
        exported = os.path.join(dest, "manuscript_tby001.md")
        assert os.path.isfile(exported)

        # Byte-identical to source
        with open(ms_file, "rb") as f:
            src_bytes = f.read()
        with open(exported, "rb") as f:
            dst_bytes = f.read()
        assert src_bytes == dst_bytes

        # No refusal or override records
        gate_files = [f for f in os.listdir(dest) if f.startswith("export_refused") or f.startswith("export_override")]
        assert gate_files == []


class TestGateBlockedCSAR:
    """Test 2: manuscript + CLASS_A report → BLOCKED, refusal record, no copy."""

    def test_manuscript_blocked_by_audit(self, manuscript_series, tmp_path):
        series_root, toy_dir, ms_file, ms_dir = manuscript_series
        dest = str(tmp_path / "out")

        # Use the real CSAR dry-run report
        csar_report = "/tmp/csar_audit_dryrun/manuscript_audit_REPORT.json"
        if not os.path.isfile(csar_report):
            pytest.skip("CSAR dry-run report not available")

        rc = _export(ms_file, dest, series_root, audit_report=csar_report)
        assert rc == 3

        # Manuscript was NOT copied
        exported = os.path.join(dest, "manuscript_tby001.md")
        assert not os.path.isfile(exported)

        # Refusal record was written
        refusal_files = [f for f in os.listdir(dest) if f.startswith("export_refused")]
        assert len(refusal_files) == 1

        # Refusal record contains MA-011 and MA-047
        with open(os.path.join(dest, refusal_files[0])) as f:
            record = json.load(f)
        check_ids = {f["check_id"] for f in record["clearance_result"]["findings"]}
        assert any("MA-011" in cid for cid in check_ids), f"MA-011 not in {check_ids}"
        assert any("MA-047" in cid for cid in check_ids), f"MA-047 not in {check_ids}"


class TestGateOverride:
    """Test 3: manuscript + CLASS_A + override → exports, override record."""

    def test_manuscript_override_exports(self, manuscript_series, tmp_path):
        series_root, toy_dir, ms_file, ms_dir = manuscript_series
        dest = str(tmp_path / "out")

        csar_report = "/tmp/csar_audit_dryrun/manuscript_audit_REPORT.json"
        if not os.path.isfile(csar_report):
            pytest.skip("CSAR dry-run report not available")

        rc = _export(
            ms_file, dest, series_root,
            audit_report=csar_report,
            override_clearance="test override",
        )
        assert rc == 0

        # File WAS copied
        exported = os.path.join(dest, "manuscript_tby001.md")
        assert os.path.isfile(exported)

        # Override record was written
        override_files = [f for f in os.listdir(dest) if f.startswith("export_override")]
        assert len(override_files) == 1

        with open(os.path.join(dest, override_files[0])) as f:
            record = json.load(f)
        assert record["override_reason"] == "test override"
        assert record["clearance_result"]["reason"] == "audit"


class TestGateMissingFlags:
    """Test 4: manuscript + no flags → error, no copy, no refusal record."""

    def test_manuscript_no_flags_errors(self, manuscript_series, tmp_path):
        series_root, toy_dir, ms_file, ms_dir = manuscript_series
        dest = str(tmp_path / "out")

        rc = _export(ms_file, dest, series_root)
        assert rc == 1

        # Nothing copied, no gate records (gate didn't run)
        if os.path.isdir(dest):
            assert os.listdir(dest) == []


class TestGateNonManuscriptWithAuditReport:
    """Test 5: non-manuscript + CLASS_A report → copies normally, gate silent."""

    def test_synopsis_ignores_gate(self, toy_series, tmp_path):
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "b01", "work", "synopsis_20260515_0253.md")
        dest = str(tmp_path / "out")

        csar_report = "/tmp/csar_audit_dryrun/manuscript_audit_REPORT.json"
        if not os.path.isfile(csar_report):
            # Use a synthetic CLASS_A report
            report_path = tmp_path / "report.json"
            report_path.write_text(json.dumps(_clean_report(class_a=3)))
            csar_report = str(report_path)

        rc = _export(source, dest, series_root, audit_report=csar_report)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "synopsis_tby001_20260515_0253.md"))

        # No gate records
        gate_files = [f for f in os.listdir(dest) if f.startswith("export_refused") or f.startswith("export_override")]
        assert gate_files == []


class TestGateNonManuscriptNoFlags:
    """Test 6: non-manuscript + no flags → copies normally (regression)."""

    def test_intake_no_flags_unchanged(self, toy_series, tmp_path):
        series_root, toy_dir = toy_series
        source = os.path.join(toy_dir, "b01", "work", "intake.json")
        dest = str(tmp_path / "out")

        rc = _export(source, dest, series_root)
        assert rc == 0
        assert os.path.isfile(os.path.join(dest, "intake_tby001.json"))


class TestGateActMultiAct:
    """act2_full.md is manuscript-class — gate fires."""

    def test_act2_gate_fires(self, tmp_path):
        series_root = tmp_path / "series"
        toy_dir = series_root / "toy_book"
        toy_dir.mkdir(parents=True)
        config = {
            "genre": "test",
            "series_name": "Toy Series",
            "series_slug": "tby",
            "book_slugs": {"b01": "tby001"},
        }
        (toy_dir / "series_config.json").write_text(json.dumps(config))

        ms_dir = toy_dir / "b01" / "work" / "manuscript_20260527_0953"
        ms_dir.mkdir(parents=True)
        ms_file = ms_dir / "act2_full.md"
        ms_file.write_text("# Act 2\n\nProse.\n")

        dest = str(tmp_path / "out")

        csar_report = "/tmp/csar_audit_dryrun/manuscript_audit_REPORT.json"
        if not os.path.isfile(csar_report):
            pytest.skip("CSAR dry-run report not available")

        rc = _export(str(ms_file), dest, str(series_root), audit_report=csar_report)
        assert rc == 3

        # Not copied
        assert not os.path.isfile(os.path.join(dest, "manuscript_act2_full_tby001.md"))

        # Refusal record written
        refusal_files = [f for f in os.listdir(dest) if f.startswith("export_refused")]
        assert len(refusal_files) == 1


class TestGateBlockedFilename:
    """manuscript_BLOCKED.md triggers gate → generation_filename (branch 2)."""

    def test_blocked_filename_gate_fires(self, tmp_path):
        series_root = tmp_path / "series"
        toy_dir = series_root / "toy_book"
        toy_dir.mkdir(parents=True)
        config = {
            "genre": "test",
            "series_name": "Toy Series",
            "series_slug": "tby",
            "book_slugs": {"b01": "tby001"},
        }
        (toy_dir / "series_config.json").write_text(json.dumps(config))

        ms_dir = toy_dir / "b01" / "work" / "manuscript_20260527_0953"
        ms_dir.mkdir(parents=True)
        ms_file = ms_dir / "manuscript_BLOCKED.md"
        ms_file.write_text("BLOCKED content\n")

        dest = str(tmp_path / "out")

        # Provide a clean audit report — but generation_filename should win
        report_path = tmp_path / "report.json"
        report_path.write_text(json.dumps(_clean_report(class_a=0)))

        rc = _export(str(ms_file), dest, str(series_root), audit_report=str(report_path))
        assert rc == 3

        # Not copied
        assert not os.path.isfile(os.path.join(dest, "manuscript_BLOCKED_tby001.md"))

        # Refusal record written with reason=generation_filename
        refusal_files = [f for f in os.listdir(dest) if f.startswith("export_refused")]
        assert len(refusal_files) == 1
        with open(os.path.join(dest, refusal_files[0])) as f:
            record = json.load(f)
        assert record["clearance_result"]["reason"] == "generation_filename"
