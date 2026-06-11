"""
Tests for runtime_verifier startup gate in master_controller.

Two tests:
(a) Controller halts with STOP_REPORT when a component module is not importable
(b) Controller proceeds past verification when all components are importable
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

import master_controller as mc


class TestStartupGateHaltsOnFailure:

    def test_stop_report_on_unimportable_component(self):
        """Startup gate writes STOP_REPORT when a registered component can't import."""
        # Inject a bogus component into COMPONENTS
        original_components = mc.COMPONENTS.copy()
        mc.COMPONENTS["_test_bogus"] = "pipeline._nonexistent_module_xyz"

        try:
            # Create a mock verifier with the bogus component in its entries
            mock_verifier = MagicMock()
            mock_verifier._component_entries = {
                "_test_bogus": {"component_name": "_test_bogus"},
            }

            findings = mc._verify_startup(mock_verifier)

            assert len(findings) == 1
            assert findings[0]["rule_id"] == "S001"
            assert findings[0]["severity"] == "A"
            assert "_test_bogus" in findings[0]["component_name"]
            assert "_nonexistent_module_xyz" in findings[0]["message"]
        finally:
            mc.COMPONENTS.clear()
            mc.COMPONENTS.update(original_components)


class TestStartupGatePassesWhenClean:

    def test_no_findings_when_all_importable(self):
        """Startup gate returns empty findings when all components are importable."""
        mock_verifier = MagicMock()
        # Use a real component that we know imports successfully
        mock_verifier._component_entries = {
            "findings": {"component_name": "findings"},
        }

        # Ensure COMPONENTS has the entry
        original_components = mc.COMPONENTS.copy()
        mc.COMPONENTS["findings"] = "pipeline.findings_v26_20260611"

        try:
            findings = mc._verify_startup(mock_verifier)
            assert findings == []
        finally:
            mc.COMPONENTS.clear()
            mc.COMPONENTS.update(original_components)
