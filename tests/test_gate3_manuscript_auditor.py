"""
Tests for Gate 3 wiring: handle_manuscript_gate invokes manuscript_auditor_v25
and fails the phase on CLASS_A findings.

Three tests:
1. Gate 3 invokes real auditor (not stubbed) and passes on clean manuscript
2. Gate 3 fails on CLASS_A findings
3. Gate 3 passes through CLASS_B/C findings without blocking
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phase_handlers import handle_manuscript_gate
import master_controller as mc


def _make_args(book_dir, **kwargs):
    """Build a minimal args namespace for handle_manuscript_gate."""
    defaults = {
        "book_dir": book_dir,
        "max_retries_per_gate": 0,
        "series_bible": None,
        "character_profiles": None,
        "synopsis": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_pipeline_state():
    """Build a minimal pipeline_state dict."""
    return {
        "gate_verdicts": {},
        "class_b_violations": 0,
        "invocation_timeline": [],
        "components_called": {name: False for name in mc.COMPONENTS},
    }


class TestGate3InvokesRealAuditor:

    def test_gate3_not_stubbed(self):
        """Gate 3 no longer returns 'stubbed' — it invokes the real auditor."""
        # Mock run_component_subprocess to simulate a clean audit
        clean_report = {"findings": [], "summary": {"total": 0}}
        with patch.object(mc, "run_component_subprocess") as mock_sub:
            mock_sub.return_value = {
                "exit_code": 0,
                "stdout": json.dumps(clean_report),
                "stderr": "",
                "duration_seconds": 1.0,
                "stubbed": False,
                "stop_report_written_during_call": False,
                "stop_report_payload": None,
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                os.makedirs(os.path.join(tmpdir, "out", "chapters"), exist_ok=True)
                args = _make_args(tmpdir)
                state = _make_pipeline_state()

                result = handle_manuscript_gate(args, state, {})

                # Must NOT be stubbed
                assert result.get("via") != "stubbed", \
                    "Gate 3 should invoke real auditor, not return stubbed"
                assert result["verdict"] == "pass"

                # Verify it called run_component_subprocess with "manuscript_auditor"
                mock_sub.assert_called_once()
                call_args = mock_sub.call_args
                assert call_args[0][0] == "manuscript_auditor"
                # Verify --manuscript-dir is in the args
                cli_args = call_args[0][1]
                assert "--manuscript-dir" in cli_args


class TestGate3FailsOnClassA:

    def test_class_a_findings_halt_gate(self):
        """CLASS_A findings cause Gate 3 to fail."""
        report_with_class_a = {
            "findings": [
                {
                    "check_id": "MA-009",
                    "severity": "CLASS_A",
                    "scene_number": 5,
                    "description": "Word count 1800 exceeds maximum 1100",
                    "suggested_fix": "Reduce word count",
                }
            ],
            "summary": {"total": 1, "class_a": 1},
        }
        with patch.object(mc, "run_component_subprocess") as mock_sub:
            mock_sub.return_value = {
                "exit_code": 1,
                "stdout": json.dumps(report_with_class_a),
                "stderr": "",
                "duration_seconds": 2.0,
                "stubbed": False,
                "stop_report_written_during_call": False,
                "stop_report_payload": None,
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                os.makedirs(os.path.join(tmpdir, "out", "chapters"), exist_ok=True)
                args = _make_args(tmpdir)
                state = _make_pipeline_state()

                result = handle_manuscript_gate(args, state, {})

                # Gate must fail
                assert state["gate_verdicts"]["manuscript"] == "fail"


class TestGate3PassesThroughClassBC:

    def test_class_b_c_findings_pass(self):
        """CLASS_B/C findings allow the gate to pass."""
        report_with_class_b = {
            "findings": [
                {
                    "check_id": "MA-005",
                    "severity": "CLASS_B",
                    "scene_number": 3,
                    "description": "Minor style issue",
                    "suggested_fix": "Review",
                }
            ],
            "summary": {"total": 1, "class_a": 0},
        }
        with patch.object(mc, "run_component_subprocess") as mock_sub:
            mock_sub.return_value = {
                "exit_code": 0,
                "stdout": json.dumps(report_with_class_b),
                "stderr": "",
                "duration_seconds": 1.0,
                "stubbed": False,
                "stop_report_written_during_call": False,
                "stop_report_payload": None,
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                os.makedirs(os.path.join(tmpdir, "out", "chapters"), exist_ok=True)
                args = _make_args(tmpdir)
                state = _make_pipeline_state()

                result = handle_manuscript_gate(args, state, {})

                assert result["verdict"] == "pass"
                assert state["gate_verdicts"]["manuscript"] == "pass"
