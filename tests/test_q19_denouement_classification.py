"""
Tests for Q19 denouement scene counting logic.

Amended bands (2026-06-12):
1. 0 DENOUEMENT → FAIL (deficit)
2. 1 DENOUEMENT → FAIL (deficit)
3. 2 DENOUEMENT → PASS (ideal)
4. 3 DENOUEMENT → WEAK (advisory)
5. 4 DENOUEMENT → FAIL (excess)
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_auditor import _count_denouement_scenes, consolidate


def _make_q19_item(scenes):
    """Build a Q19 item with post_climax_scenes data."""
    return {
        "id": "Q19",
        "verdict": "FAIL",
        "note": "",
        "post_climax_scenes": scenes,
    }


def _make_scene(num, classification, justification="test"):
    return {"scene": num, "classification": classification, "justification": justification}


class TestCountDenouementScenes:

    def test_zero_denouement(self):
        item = _make_q19_item([
            _make_scene(97, "AFTERMATH"),
            _make_scene(98, "AFTERMATH"),
        ])
        count, table = _count_denouement_scenes(item)
        assert count == 0
        assert "0 DENOUEMENT" in table

    def test_one_denouement(self):
        item = _make_q19_item([
            _make_scene(97, "AFTERMATH"),
            _make_scene(98, "AFTERMATH"),
            _make_scene(99, "DENOUEMENT"),
        ])
        count, table = _count_denouement_scenes(item)
        assert count == 1

    def test_two_denouement(self):
        item = _make_q19_item([
            _make_scene(97, "AFTERMATH"),
            _make_scene(98, "AFTERMATH"),
            _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        count, table = _count_denouement_scenes(item)
        assert count == 2

    def test_three_denouement(self):
        item = _make_q19_item([
            _make_scene(98, "DENOUEMENT"),
            _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        count, table = _count_denouement_scenes(item)
        assert count == 3

    def test_four_denouement(self):
        item = _make_q19_item([
            _make_scene(97, "DENOUEMENT"),
            _make_scene(98, "DENOUEMENT"),
            _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        count, table = _count_denouement_scenes(item)
        assert count == 4


def _effective_config():
    return {
        "target_synopsis_word_min": 18000,
        "target_synopsis_word_max": 28000,
        "action_scene_percentage_min": 0.65,
    }


def _make_calls_with_q19(scenes):
    """Build call_1/call_2 data with a Q19 item in call_1."""
    q19 = _make_q19_item(scenes)
    call_1 = {
        "items": [q19],
        "total_scenes": 100, "action_scenes": 67,
        "action_scene_percentage": 67.0, "resolution_scenes": len(scenes),
    }
    call_2 = {"items": [{"id": "Q8", "verdict": "PASS", "note": "in range"}]}
    return call_1, call_2


class TestQ19ConsolidateVerdict:

    def test_two_denouement_passes_gate(self):
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(97, "AFTERMATH"),
            _make_scene(98, "AFTERMATH"),
            _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "PASS"
        assert "Q19" not in output_json["fails"]

    def test_three_denouement_weak_advisory(self):
        """3 DENOUEMENT → WEAK (advisory, not a gate-blocker)."""
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(98, "DENOUEMENT"),
            _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "WEAK"
        assert "Q19" not in output_json["fails"]

    def test_four_denouement_fails_gate(self):
        """4+ DENOUEMENT → FAIL (excess)."""
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(97, "DENOUEMENT"),
            _make_scene(98, "DENOUEMENT"),
            _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "FAIL"
        assert "Q19" in output_json["fails"]

    def test_zero_denouement_fails_gate(self):
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(97, "AFTERMATH"),
            _make_scene(98, "AFTERMATH"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "FAIL"
        assert "deficit" in q19["note"]

    def test_one_denouement_fails_gate(self):
        """1 DENOUEMENT → FAIL (deficit)."""
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(97, "AFTERMATH"),
            _make_scene(98, "AFTERMATH"),
            _make_scene(99, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "FAIL"
        assert "deficit" in q19["note"]
