"""Test: synopsis gate uses contract path work/synopsis.md, not glob.

Regression for the file-selection defect: find_latest_file("synopsis_*.md")
could match synopsis_audit_report.md (or any synopsis_<derived>.md) if it
was newer.  The fix uses the deterministic contract path work/synopsis.md.
"""
import json
import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch
from argparse import Namespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import phase_handlers_v26_20260612_T2200 as ph


def _make_args(tmp_path, **overrides):
    book_dir = str(tmp_path)
    work_dir = os.path.join(book_dir, "work")
    os.makedirs(work_dir, exist_ok=True)
    intake_path = os.path.join(work_dir, "intake.json")
    with open(intake_path, "w") as f:
        json.dump({"outline_path": "outline.md"}, f)
    with open(os.path.join(work_dir, "outline.md"), "w") as f:
        f.write("# Outline\n")
    series_dir = os.path.join(str(tmp_path), "series")
    os.makedirs(series_dir, exist_ok=True)
    for fn in ("character_profiles.json", "series_bible.json", "twist_library.md"):
        with open(os.path.join(series_dir, fn), "w") as f:
            f.write("{}" if fn.endswith(".json") else "")
    defaults = dict(
        book_dir=book_dir,
        series_dir=series_dir,
        series_config=os.path.join(series_dir, "series_bible.json"),
        intake=intake_path,
        max_retries_per_gate=0,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def _gen_side_effect_that_creates_synopsis(work_dir):
    """Returns a side_effect that writes synopsis.md on first call (generator)
    and returns success dict on all calls."""
    call_count = [0]

    def side_effect(component_name, args_list, book_dir, pipeline_state):
        call_count[0] += 1
        if component_name == "synopsis_generator":
            # Simulate generator writing the contract path
            path = os.path.join(work_dir, "synopsis.md")
            if not os.path.isfile(path):
                with open(path, "w") as f:
                    f.write("# Synopsis\nScene content here.\n")
        return {
            "stubbed": False,
            "exit_code": 0,
            "stderr": "",
            "stop_report_written_during_call": False,
        }
    return side_effect


class TestSynopsisContractPath:
    """The handler must select work/synopsis.md, not a newer glob match."""

    @patch.object(ph, "mc")
    def test_selects_contract_path_over_newer_glob_match(self, mock_mc, tmp_path):
        """Fixture: synopsis.md + NEWER synopsis_audit_report.md.
        Handler must pass work/synopsis.md to the auditor, not the report."""
        args = _make_args(tmp_path)
        work_dir = os.path.join(str(tmp_path), "work")

        # Create the canonical synopsis.md FIRST
        synopsis_path = os.path.join(work_dir, "synopsis.md")
        with open(synopsis_path, "w") as f:
            f.write("# Synopsis\nScene content here.\n")

        # Create a NEWER file that the old glob would have matched
        time.sleep(0.05)
        decoy_path = os.path.join(work_dir, "synopsis_audit_report.md")
        with open(decoy_path, "w") as f:
            f.write("# Audit Report — NOT the synopsis\n")

        mock_mc.run_component_subprocess = MagicMock(return_value={
            "stubbed": False,
            "exit_code": 0,
            "stderr": "",
            "stdout": "[]",
            "stop_report_written_during_call": False,
        })
        pipeline_state = {
            "phases_completed": [],
            "gate_verdicts": {"synopsis": "not_yet_run"},
            "components_called": {},
            "invocation_timeline": [],
            "class_a_failures": 0,
            "class_b_violations": 0,
            "hard_stop": False,
        }

        ph.handle_synopsis_gate(args, pipeline_state, {})

        # The auditor subprocess must receive the contract path
        calls = mock_mc.run_component_subprocess.call_args_list
        auditor_calls = [c for c in calls if c[0][0] == "synopsis_auditor"]
        assert len(auditor_calls) >= 1, "synopsis_auditor was never called"
        auditor_args = auditor_calls[0][0][1]
        synopsis_idx = auditor_args.index("--synopsis") + 1
        actual_path = auditor_args[synopsis_idx]
        assert actual_path == synopsis_path, (
            f"Expected contract path {synopsis_path}, got {actual_path}"
        )

    @patch.object(ph, "mc")
    def test_contract_path_missing_is_hard_error(self, mock_mc, tmp_path):
        """If work/synopsis.md is absent after generation, return class-A halt."""
        args = _make_args(tmp_path)
        work_dir = os.path.join(str(tmp_path), "work")

        # Only the decoy exists — no synopsis.md
        decoy = os.path.join(work_dir, "synopsis_20260612.md")
        with open(decoy, "w") as f:
            f.write("# Decoy synopsis\n")

        mock_mc.run_component_subprocess = MagicMock(return_value={
            "stubbed": False,
            "exit_code": 0,
            "stderr": "",
            "stop_report_written_during_call": False,
        })
        pipeline_state = {"phases_completed": []}

        result = ph.handle_synopsis_gate(args, pipeline_state, {})
        assert result["verdict"] == "halt"
        findings_text = str(result.get("findings", []))
        assert "contract path" in findings_text, (
            f"Expected 'contract path' in findings, got: {findings_text}"
        )
