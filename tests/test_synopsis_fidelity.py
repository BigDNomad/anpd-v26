"""
Tests for MA-035 synopsis_fidelity.

Covers:
  1. All beats PRESENT -> 0 findings.
  2. One beat MISSING -> 1 CLASS_B finding naming that beat.
  3. stop_reason="max_tokens" on every attempt -> 1 "unverifiable" CLASS_B finding.
  4. invented_major non-empty -> 1 CLASS_B finding.
  5. load_scene_type_map regression guard.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    REGISTRY,
    discover_and_register,
)
from pathlib import Path

from audit_checks._lib.synopsis_scene_types import (
    SceneSpec,
    load_scene_type_map,
    load_scene_specs,
)
from audit_checks.synopsis_fidelity import SynopsisFidelity, _compare_scene


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_manuscript(n_scenes=3):
    return ManuscriptArtifact(
        scenes=[
            SceneText(scene_number=i, text=f"Scene {i} prose content.", file_path=f"/fake/sc_{i:03d}.md")
            for i in range(1, n_scenes + 1)
        ],
        manuscript_dir="/fake",
    )


def _make_briefs():
    return BriefBundle(series_bible={}, character_profiles={"characters": []})


def _make_specs(n=3, beats_per=2):
    return {
        i: SceneSpec(
            number=i,
            title=f"Scene {i} Title",
            type="ACTION",
            focus="Character",
            beats=[f"Beat {i}.{j}" for j in range(beats_per)],
        )
        for i in range(1, n + 1)
    }


def _mock_llm_response(verdicts_payload, invented=None, stop_reason="end_turn"):
    """Build a mock LLMResponse matching llm_client.LLMResponse."""
    payload = {
        "verdicts": verdicts_payload,
        "invented_major": invented or [],
    }
    resp = MagicMock()
    resp.text = json.dumps(payload)
    resp.input_tokens = 100
    resp.output_tokens = 50
    resp.stop_reason = stop_reason
    return resp


# ─── Test 1: All beats PRESENT -> 0 findings ────────────────────────────────

class TestAllPresent:

    def test_all_present_no_findings(self):
        specs = _make_specs(n=2, beats_per=3)
        ms = _make_manuscript(2)
        briefs = _make_briefs()

        all_present = [{"beat_index": i, "verdict": "PRESENT", "note": ""} for i in range(3)]
        mock_resp = _mock_llm_response(all_present)

        check = SynopsisFidelity()
        with patch("audit_checks.synopsis_fidelity.load_scene_specs", return_value=specs), \
             patch("audit_checks.synopsis_fidelity._get_call_llm", return_value=lambda **kw: mock_resp):
            findings = check.run(ms, briefs)

        ma035 = [f for f in findings if f.check_id == "MA-035-synopsis-fidelity"]
        assert len(ma035) == 0


# ─── Test 2: One beat MISSING -> 1 CLASS_B finding ──────────────────────────

class TestMissingBeat:

    def test_missing_beat_produces_finding(self):
        specs = _make_specs(n=1, beats_per=3)
        ms = _make_manuscript(1)
        briefs = _make_briefs()

        verdicts = [
            {"beat_index": 0, "verdict": "PRESENT", "note": ""},
            {"beat_index": 1, "verdict": "MISSING", "note": "beat not found in prose"},
            {"beat_index": 2, "verdict": "PRESENT", "note": ""},
        ]
        mock_resp = _mock_llm_response(verdicts)

        check = SynopsisFidelity()
        with patch("audit_checks.synopsis_fidelity.load_scene_specs", return_value=specs), \
             patch("audit_checks.synopsis_fidelity._get_call_llm", return_value=lambda **kw: mock_resp):
            findings = check.run(ms, briefs)

        missing = [f for f in findings if "missing" in f.description.lower()]
        assert len(missing) == 1
        assert missing[0].severity == "CLASS_B"
        assert "Beat 1.1" in missing[0].description


# ─── Test 3: Truncation -> unverifiable (load-bearing test) ─────────────────

class TestTruncationGuard:

    def test_max_tokens_all_retries_produces_unverifiable(self):
        """stop_reason='max_tokens' on every attempt -> 'unverifiable' finding."""
        specs = _make_specs(n=1, beats_per=2)
        ms = _make_manuscript(1)
        briefs = _make_briefs()

        truncated_resp = _mock_llm_response([], stop_reason="max_tokens")

        check = SynopsisFidelity()
        with patch("audit_checks.synopsis_fidelity.load_scene_specs", return_value=specs), \
             patch("audit_checks.synopsis_fidelity._get_call_llm", return_value=lambda **kw: truncated_resp), \
             patch("audit_checks.synopsis_fidelity.time.sleep"):
            findings = check.run(ms, briefs)

        unverifiable = [f for f in findings if "unverifiable" in f.description]
        assert len(unverifiable) == 1
        assert unverifiable[0].severity == "CLASS_B"
        # Must NOT silently pass
        assert len(findings) > 0


# ─── Test 4: Invented major event -> 1 CLASS_B finding ──────────────────────

class TestInventedMajor:

    def test_invented_major_produces_finding(self):
        specs = _make_specs(n=1, beats_per=2)
        ms = _make_manuscript(1)
        briefs = _make_briefs()

        all_present = [{"beat_index": i, "verdict": "PRESENT", "note": ""} for i in range(2)]
        invented = [{"event": "Character dies unexpectedly", "evidence": "He fell and did not rise"}]
        mock_resp = _mock_llm_response(all_present, invented=invented)

        check = SynopsisFidelity()
        with patch("audit_checks.synopsis_fidelity.load_scene_specs", return_value=specs), \
             patch("audit_checks.synopsis_fidelity._get_call_llm", return_value=lambda **kw: mock_resp):
            findings = check.run(ms, briefs)

        inv = [f for f in findings if "invented" in f.description.lower()]
        assert len(inv) == 1
        assert inv[0].severity == "CLASS_B"
        assert "Character dies unexpectedly" in inv[0].description


# ─── Test 5: load_scene_type_map regression guard ───────────────────────────

class TestSceneTypeMapRegression:

    def test_load_scene_type_map_unchanged(self):
        """load_scene_type_map still returns the same output after _lib extension."""
        _SYNOPSIS_PATH = Path("/anpd/v25/series/black_tide/b01/work/synopsis.md")
        if not _SYNOPSIS_PATH.exists():
            pytest.skip("Synopsis not available")

        type_map = load_scene_type_map(_SYNOPSIS_PATH)
        spec_map = load_scene_specs(_SYNOPSIS_PATH)

        # Both should have the same keys (flat scene numbers)
        assert set(type_map.keys()) == set(spec_map.keys()), (
            f"Key mismatch: type_map has {len(type_map)} entries, "
            f"spec_map has {len(spec_map)} entries"
        )
        # Types should match
        for n in type_map:
            assert type_map[n] == spec_map[n].type, (
                f"Scene {n}: type_map={type_map[n]}, spec_map={spec_map[n].type}"
            )


# ─── Module interface ───────────────────────────────────────────────────────

class TestModuleInterface:

    def test_has_required_interface(self):
        check = SynopsisFidelity()
        assert check.check_id == "MA-035-synopsis-fidelity"
        assert check.severity == "CLASS_B"
        assert hasattr(check, "run")

    def test_module_auto_discovered(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-035-synopsis-fidelity" in check_ids
        REGISTRY.clear()
