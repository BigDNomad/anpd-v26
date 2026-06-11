"""
Tests for Q19 denouement scene counting + multi-pass majority logic.

Amended bands (2026-06-12):
  0–1 DENOUEMENT → FAIL (deficit)
  2   DENOUEMENT → PASS (ideal)
  3   DENOUEMENT → WEAK (advisory)
  4+  DENOUEMENT → FAIL (excess)

Multi-pass (2026-06-12 T1400):
  3 passes, per-Q majority verdict, per-scene majority classification.
  Missing structured data → ValueError (never fallback to note text).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_auditor import (
    _count_denouement_scenes,
    _majority_verdict,
    _majority_q19_scenes,
    consolidate,
)


def _make_q19_item(scenes):
    return {
        "id": "Q19",
        "verdict": "FAIL",
        "note": "",
        "post_climax_scenes": scenes,
    }


def _make_scene(num, classification, justification="test"):
    return {"scene": num, "classification": classification, "justification": justification}


# ── _count_denouement_scenes ──────────────────────────────────────────────────

class TestCountDenouementScenes:

    def test_zero_denouement(self):
        item = _make_q19_item([_make_scene(97, "AFTERMATH"), _make_scene(98, "AFTERMATH")])
        count, table = _count_denouement_scenes(item)
        assert count == 0
        assert "0 DENOUEMENT" in table

    def test_one_denouement(self):
        item = _make_q19_item([_make_scene(97, "AFTERMATH"), _make_scene(99, "DENOUEMENT")])
        count, _ = _count_denouement_scenes(item)
        assert count == 1

    def test_two_denouement(self):
        item = _make_q19_item([
            _make_scene(97, "AFTERMATH"), _make_scene(98, "AFTERMATH"),
            _make_scene(99, "DENOUEMENT"), _make_scene(100, "DENOUEMENT"),
        ])
        count, _ = _count_denouement_scenes(item)
        assert count == 2

    def test_three_denouement(self):
        item = _make_q19_item([
            _make_scene(98, "DENOUEMENT"), _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        count, _ = _count_denouement_scenes(item)
        assert count == 3

    def test_four_denouement(self):
        item = _make_q19_item([
            _make_scene(97, "DENOUEMENT"), _make_scene(98, "DENOUEMENT"),
            _make_scene(99, "DENOUEMENT"), _make_scene(100, "DENOUEMENT"),
        ])
        count, _ = _count_denouement_scenes(item)
        assert count == 4

    def test_missing_structured_data_raises(self):
        """No post_climax_scenes → ValueError, never fallback to note text."""
        item = {"id": "Q19", "verdict": "FAIL", "note": "DENOUEMENT DENOUEMENT DENOUEMENT"}
        with pytest.raises(ValueError, match="missing structured post_climax_scenes"):
            _count_denouement_scenes(item)

    def test_empty_list_raises(self):
        item = _make_q19_item([])
        with pytest.raises(ValueError, match="missing structured post_climax_scenes"):
            _count_denouement_scenes(item)


# ── _majority_verdict ─────────────────────────────────────────────────────────

class TestMajorityVerdict:

    def test_three_agree(self):
        v, stable = _majority_verdict(["PASS", "PASS", "PASS"])
        assert v == "PASS"
        assert stable is True

    def test_two_of_three(self):
        v, stable = _majority_verdict(["PASS", "FAIL", "PASS"])
        assert v == "PASS"
        assert stable is True

    def test_two_of_three_fail(self):
        v, stable = _majority_verdict(["FAIL", "PASS", "FAIL"])
        assert v == "FAIL"
        assert stable is True

    def test_three_way_split_takes_worst(self):
        v, stable = _majority_verdict(["PASS", "WEAK", "FAIL"])
        assert v == "FAIL"
        assert stable is False

    def test_two_of_three_weak(self):
        v, stable = _majority_verdict(["WEAK", "PASS", "WEAK"])
        assert v == "WEAK"
        assert stable is True


# ── _majority_q19_scenes ──────────────────────────────────────────────────────

class TestMajorityQ19Scenes:

    def test_all_agree(self):
        items = [
            _make_q19_item([_make_scene(97, "AFTERMATH"), _make_scene(98, "DENOUEMENT")]),
            _make_q19_item([_make_scene(97, "AFTERMATH"), _make_scene(98, "DENOUEMENT")]),
            _make_q19_item([_make_scene(97, "AFTERMATH"), _make_scene(98, "DENOUEMENT")]),
        ]
        merged = _majority_q19_scenes(items)
        assert len(merged) == 2
        assert merged[0]["classification"] == "AFTERMATH"
        assert merged[1]["classification"] == "DENOUEMENT"

    def test_split_takes_majority(self):
        items = [
            _make_q19_item([_make_scene(98, "AFTERMATH")]),
            _make_q19_item([_make_scene(98, "DENOUEMENT")]),
            _make_q19_item([_make_scene(98, "AFTERMATH")]),
        ]
        merged = _majority_q19_scenes(items)
        assert merged[0]["classification"] == "AFTERMATH"
        assert "votes:" in merged[0]["justification"]


# ── consolidate verdict bands ─────────────────────────────────────────────────

def _effective_config():
    return {
        "target_synopsis_word_min": 18000,
        "target_synopsis_word_max": 28000,
        "action_scene_percentage_min": 0.65,
    }


def _make_calls_with_q19(scenes):
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
            _make_scene(97, "AFTERMATH"), _make_scene(98, "AFTERMATH"),
            _make_scene(99, "DENOUEMENT"), _make_scene(100, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "PASS"
        assert "Q19" not in output_json["fails"]

    def test_three_denouement_weak_advisory(self):
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(98, "DENOUEMENT"), _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "WEAK"
        assert "Q19" not in output_json["fails"]

    def test_four_denouement_fails_gate(self):
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(97, "DENOUEMENT"), _make_scene(98, "DENOUEMENT"),
            _make_scene(99, "DENOUEMENT"), _make_scene(100, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "FAIL"
        assert "Q19" in output_json["fails"]

    def test_zero_denouement_fails_gate(self):
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(97, "AFTERMATH"), _make_scene(98, "AFTERMATH"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "FAIL"
        assert "deficit" in q19["note"]

    def test_one_denouement_fails_gate(self):
        call_1, call_2 = _make_calls_with_q19([
            _make_scene(97, "AFTERMATH"), _make_scene(99, "DENOUEMENT"),
        ])
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 20000, _effective_config())
        q19 = [i for i in output_json["items"] if i["id"] == "Q19"][0]
        assert q19["verdict"] == "FAIL"
        assert "deficit" in q19["note"]

    def test_missing_structured_data_raises_in_consolidate(self):
        """consolidate must raise ValueError when Q19 has no structured data."""
        call_1 = {
            "items": [{"id": "Q19", "verdict": "FAIL", "note": "DENOUEMENT x3"}],
            "total_scenes": 100, "action_scenes": 67,
            "action_scene_percentage": 67.0, "resolution_scenes": 3,
        }
        call_2 = {"items": [{"id": "Q8", "verdict": "PASS", "note": "ok"}]}
        with pytest.raises(ValueError):
            consolidate(call_1, call_2, "Test", 20000, _effective_config())
