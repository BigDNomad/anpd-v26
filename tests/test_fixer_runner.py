"""Tests for fixer_runner — convergence loop unit tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from audit_checks import Finding, BriefBundle
from manuscript_fixer import FixerResult
from fixer_runner import (
    run_fixer_loop,
    RunnerResult,
    _load_audit_summary,
    _extract_class_a_findings,
    _bookslug_from_book_dir,
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _minimal_briefs() -> BriefBundle:
    return BriefBundle(
        series_bible={}, character_profiles={"characters": []},
        book_config={}, scene_map={}, entity_ledger={},
    )


def _write_report(path: Path, class_a: int, class_b: int = 0, class_c: int = 0):
    """Write a minimal audit report JSON."""
    findings = [
        {"check_id": "MA-001-character-detail-consistency", "severity": "CLASS_A",
         "description": f"finding {i}", "evidence": [f"ev {i}"], "scene_number": i + 1}
        for i in range(class_a)
    ]
    report = {
        "summary": {"class_a": class_a, "class_b": class_b, "class_c": class_c,
                     "total_findings": class_a + class_b + class_c},
        "all_findings": findings,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))


def _fake_fix_result(book_dir: Path, **overrides) -> FixerResult:
    defaults = dict(
        book_dir=book_dir,
        workspace_dir=book_dir / "_fixer_workspace",
        tier_1_applied=1, tier_1_skipped=0,
        tier_2_applied=2, tier_2_skipped=0,
        regeneration_cost_usd=0.5,
        scenes_regenerated=[1, 2],
        tier_3_escalated=0,
    )
    defaults.update(overrides)
    return FixerResult(**defaults)


# ── Unit tests: helpers ─────────────────────────────────────────────────

class TestHelpers:
    def test_bookslug_from_intake(self, tmp_path):
        book_dir = tmp_path / "b01"
        (book_dir / "work").mkdir(parents=True)
        (book_dir / "work" / "intake.json").write_text(json.dumps({"book_slug": "arm001"}))
        assert _bookslug_from_book_dir(book_dir) == "arm001"

    def test_bookslug_fallback(self, tmp_path):
        book_dir = tmp_path / "b01"
        book_dir.mkdir()
        assert _bookslug_from_book_dir(book_dir) == "b01"

    def test_load_audit_summary(self, tmp_path):
        report = tmp_path / "report.json"
        _write_report(report, class_a=5, class_b=10, class_c=2)
        a, b, c, total = _load_audit_summary(report)
        assert (a, b, c, total) == (5, 10, 2, 17)

    def test_extract_class_a_findings(self, tmp_path):
        report = tmp_path / "report.json"
        _write_report(report, class_a=3, class_b=2)
        findings = _extract_class_a_findings(report)
        assert len(findings) == 3
        assert all(f.severity == "CLASS_A" for f in findings)


# ── Unit tests: convergence loop ────────────────────────────────────────

class TestFixerRunner:
    def test_single_iteration_converges(self, tmp_path):
        """First audit returns CLASS_A=0 → immediate convergence."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms = tmp_path / "ms.md"; ms.write_text("test")

        def fake_audit(manuscript, briefs, output_dir):
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json", class_a=0)
            return (0, [])

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        assert result.termination_reason == "converged"
        assert result.final_class_a == 0
        assert len(result.iterations) == 1
        assert result.iterations[0].fixer_summary is None

    def test_multiple_iterations_converge(self, tmp_path):
        """CLASS_A drops 10 → 5 → 0 over three audit passes (2 fix passes)."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms_dir = book_dir / "out" / "manuscript"; ms_dir.mkdir(parents=True)
        (ms_dir / "sc_001.md").write_text("scene 1")
        ms = tmp_path / "ms.md"; ms.write_text("test")

        audit_call = {"n": 0}
        sequence = [10, 5, 0]

        def fake_audit(manuscript, briefs, output_dir):
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json",
                          class_a=sequence[audit_call["n"]])
            audit_call["n"] += 1
            return (0 if sequence[audit_call["n"] - 1] == 0 else 1, [])

        fake_fixer = MagicMock()
        fake_fixer.run.return_value = _fake_fix_result(book_dir)

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])), \
             patch("fixer_runner.ManuscriptFixer", return_value=fake_fixer):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        assert result.termination_reason == "converged"
        assert result.final_class_a == 0
        assert len(result.iterations) == 3
        assert fake_fixer.run.call_count == 2

    def test_no_progress_halt(self, tmp_path):
        """CLASS_A stays at 10 → no_progress on iteration 2."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms_dir = book_dir / "out" / "manuscript"; ms_dir.mkdir(parents=True)
        (ms_dir / "sc_001.md").write_text("scene 1")
        ms = tmp_path / "ms.md"; ms.write_text("test")

        audit_call = {"n": 0}

        def fake_audit(manuscript, briefs, output_dir):
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json", class_a=10)
            audit_call["n"] += 1
            return (1, [])

        fake_fixer = MagicMock()
        fake_fixer.run.return_value = _fake_fix_result(book_dir)

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])), \
             patch("fixer_runner.ManuscriptFixer", return_value=fake_fixer):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        assert result.termination_reason == "no_progress"
        assert result.final_class_a == 10
        assert fake_fixer.run.call_count == 1  # only iter 1 fixes

    def test_max_iterations_reached(self, tmp_path):
        """CLASS_A decreases by 1 each iteration but never hits 0."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms_dir = book_dir / "out" / "manuscript"; ms_dir.mkdir(parents=True)
        (ms_dir / "sc_001.md").write_text("scene 1")
        ms = tmp_path / "ms.md"; ms.write_text("test")

        audit_call = {"n": 0}
        # 3 iters with max_iterations=3: audit calls yield 6, 5, 4, (no more fix)
        sequence = [6, 5, 4]

        def fake_audit(manuscript, briefs, output_dir):
            idx = min(audit_call["n"], len(sequence) - 1)
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json",
                          class_a=sequence[idx])
            audit_call["n"] += 1
            return (1, [])

        fake_fixer = MagicMock()
        fake_fixer.run.return_value = _fake_fix_result(book_dir)

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])), \
             patch("fixer_runner.ManuscriptFixer", return_value=fake_fixer):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms,
                                    max_iterations=3, briefs=_minimal_briefs())

        assert result.termination_reason == "max_iterations"
        assert result.final_class_a == 4

    def test_audit_failure_halts_with_error(self, tmp_path):
        """run_audit raises → error."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms = tmp_path / "ms.md"; ms.write_text("test")

        with patch("fixer_runner.run_audit", side_effect=RuntimeError("API down")), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        assert result.termination_reason == "error"
        assert len(result.iterations) == 1

    def test_fixer_failure_halts_with_error(self, tmp_path):
        """ManuscriptFixer.run raises → error."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms_dir = book_dir / "out" / "manuscript"; ms_dir.mkdir(parents=True)
        (ms_dir / "sc_001.md").write_text("scene 1")
        ms = tmp_path / "ms.md"; ms.write_text("test")

        def fake_audit(manuscript, briefs, output_dir):
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json", class_a=5)
            return (1, [])

        fake_fixer = MagicMock()
        fake_fixer.run.side_effect = RuntimeError("Fixer crash")

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])), \
             patch("fixer_runner.ManuscriptFixer", return_value=fake_fixer):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        assert result.termination_reason == "error"

    def test_consolidated_log_written(self, tmp_path):
        """Verify consolidated JSON log structure."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms = tmp_path / "ms.md"; ms.write_text("test")

        def fake_audit(manuscript, briefs, output_dir):
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json", class_a=0)
            return (0, [])

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        assert result.log_path is not None
        log_data = json.loads(Path(result.log_path).read_text())
        assert "runner_meta" in log_data
        assert "iterations" in log_data
        assert "final_state" in log_data
        assert log_data["runner_meta"]["termination_reason"] == "converged"
        assert log_data["final_state"]["publish_gate_clearable"] is True

    def test_skip_workspace_setup_on_iter_2(self, tmp_path):
        """Iter 2 instantiates ManuscriptFixer with skip_workspace_setup=True."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms_dir = book_dir / "out" / "manuscript"; ms_dir.mkdir(parents=True)
        (ms_dir / "sc_001.md").write_text("scene 1")
        ms = tmp_path / "ms.md"; ms.write_text("test")

        audit_call = {"n": 0}
        sequence = [5, 3, 0]

        def fake_audit(manuscript, briefs, output_dir):
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json",
                          class_a=sequence[audit_call["n"]])
            audit_call["n"] += 1
            return (0, [])

        fixer_cls = MagicMock()
        fixer_cls.return_value.run.return_value = _fake_fix_result(book_dir)

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])), \
             patch("fixer_runner.ManuscriptFixer", fixer_cls):
            result = run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        assert result.termination_reason == "converged"
        # Check constructor calls
        calls = fixer_cls.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs.get("skip_workspace_setup") is False
        assert calls[1].kwargs.get("skip_workspace_setup") is True

    def test_iteration_number_propagates(self, tmp_path):
        """Iter N passes iteration_number=N to ManuscriptFixer."""
        book_dir = tmp_path / "book"; book_dir.mkdir()
        ms_dir = book_dir / "out" / "manuscript"; ms_dir.mkdir(parents=True)
        (ms_dir / "sc_001.md").write_text("scene 1")
        ms = tmp_path / "ms.md"; ms.write_text("test")

        audit_call = {"n": 0}
        sequence = [5, 3, 0]

        def fake_audit(manuscript, briefs, output_dir):
            _write_report(Path(output_dir) / "manuscript_audit_REPORT.json",
                          class_a=sequence[audit_call["n"]])
            audit_call["n"] += 1
            return (0, [])

        fixer_cls = MagicMock()
        fixer_cls.return_value.run.return_value = _fake_fix_result(book_dir)

        with patch("fixer_runner.run_audit", side_effect=fake_audit), \
             patch("fixer_runner.load_manuscript", return_value=MagicMock(scenes=[])), \
             patch("fixer_runner.ManuscriptFixer", fixer_cls):
            run_fixer_loop(book_dir=book_dir, manuscript_path=ms, briefs=_minimal_briefs())

        calls = fixer_cls.call_args_list
        assert calls[0].kwargs.get("iteration_number") == 1
        assert calls[1].kwargs.get("iteration_number") == 2


def test_fint8_load_briefs_passes_synopsis_when_present(tmp_path):
    """F-INT-8: when work/synopsis.md exists, the fixer brief-loader must pass
    synopsis_path to load_briefs (so the fixer's audits aren't synopsis-blind)."""
    book_dir = tmp_path / "book"
    (book_dir / "work").mkdir(parents=True)
    (book_dir / "work" / "synopsis.md").write_text("# Synopsis\n", encoding="utf-8")

    import fixer_runner
    with patch.object(fixer_runner, "load_briefs") as mock_load:
        mock_load.return_value = BriefBundle()
        fixer_runner._load_briefs_from_paths(book_dir, None)

    kwargs = mock_load.call_args.kwargs
    assert kwargs.get("synopsis_path") is not None
    assert kwargs["synopsis_path"].endswith("synopsis.md")


def test_fint8_load_briefs_synopsis_none_when_absent(tmp_path):
    """F-INT-8: when no synopsis.md exists, synopsis_path is None (non-fatal,
    preserves prior behavior — no crash)."""
    book_dir = tmp_path / "book"
    (book_dir / "work").mkdir(parents=True)

    import fixer_runner
    with patch.object(fixer_runner, "load_briefs") as mock_load:
        mock_load.return_value = BriefBundle()
        fixer_runner._load_briefs_from_paths(book_dir, None)

    assert mock_load.call_args.kwargs.get("synopsis_path") is None
