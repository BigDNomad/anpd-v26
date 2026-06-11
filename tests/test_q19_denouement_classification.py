"""
Tests for Q19 denouement scene counting + multi-pass majority logic.

Amended bands (2026-06-12):
  0–1 DENOUEMENT → FAIL (deficit)
  2   DENOUEMENT → PASS (ideal)
  3   DENOUEMENT → WEAK (advisory)
  4+  DENOUEMENT → FAIL (excess)

V26 T1800: deterministic bands are applied in _merge_multipass_results
(not consolidate).  LLM verdicts for Q19 are ignored; only per-scene
classifications (majority-voted) feed the bands.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_auditor import (
    _count_denouement_scenes,
    _q19_band_verdict,
    _majority_verdict,
    _majority_q19_scenes,
    _merge_multipass_results,
    consolidate,
)


def _make_q19_item(scenes, verdict="FAIL"):
    return {
        "id": "Q19",
        "verdict": verdict,
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


# ── _q19_band_verdict ────────────────────────────────────────────────────────

class TestQ19BandVerdict:
    def test_zero(self):
        assert _q19_band_verdict(0) == 'FAIL'

    def test_one(self):
        assert _q19_band_verdict(1) == 'FAIL'

    def test_two(self):
        assert _q19_band_verdict(2) == 'PASS'

    def test_three(self):
        assert _q19_band_verdict(3) == 'WEAK'

    def test_four(self):
        assert _q19_band_verdict(4) == 'FAIL'

    def test_five(self):
        assert _q19_band_verdict(5) == 'FAIL'


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


# ── Dispatch-required test (b): stable mocked scene classifications across
#    passes → identical Q19 verdict every time ─────────────────────────────────

def _effective_config():
    return {
        "target_synopsis_word_min": 18000,
        "target_synopsis_word_max": 28000,
        "action_scene_percentage_min": 0.65,
    }


class TestQ19DeterministicThroughMerge:
    """Stable per-scene classifications → same Q19 verdict regardless of LLM Q19 verdicts."""

    def _make_pass_data(self, q19_llm_verdict, scenes):
        """Build one pass of (call_1, call_2) with injected Q19 LLM verdict and scenes."""
        q19 = _make_q19_item(scenes, verdict=q19_llm_verdict)
        c1 = {"items": [q19], "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": len(scenes)}
        c2 = {"items": [{"id": "Q8", "verdict": "PASS", "note": "in range"}]}
        return c1, c2

    def _standard_scenes(self):
        return [
            _make_scene(97, "AFTERMATH"), _make_scene(98, "AFTERMATH"),
            _make_scene(99, "DENOUEMENT"), _make_scene(100, "DENOUEMENT"),
        ]

    def test_stable_scenes_all_fail_verdicts(self):
        """3 LLM passes say FAIL, but 2 DENOUEMENT → merge produces PASS."""
        scenes = self._standard_scenes()
        passes = [self._make_pass_data("FAIL", scenes) for _ in range(3)]
        c1, c2 = _merge_multipass_results(passes, "Test", 20000, _effective_config())
        q19 = next(i for i in c1['items'] if i['id'] == 'Q19')
        assert q19['verdict'] == 'PASS'

    def test_stable_scenes_all_pass_verdicts(self):
        """3 LLM passes say PASS, 2 DENOUEMENT → merge produces PASS."""
        scenes = self._standard_scenes()
        passes = [self._make_pass_data("PASS", scenes) for _ in range(3)]
        c1, c2 = _merge_multipass_results(passes, "Test", 20000, _effective_config())
        q19 = next(i for i in c1['items'] if i['id'] == 'Q19')
        assert q19['verdict'] == 'PASS'

    def test_stable_scenes_mixed_verdicts(self):
        """Mixed LLM votes (FAIL/PASS/FAIL), 2 DENOUEMENT → merge produces PASS."""
        scenes = self._standard_scenes()
        passes = [
            self._make_pass_data("FAIL", scenes),
            self._make_pass_data("PASS", scenes),
            self._make_pass_data("FAIL", scenes),
        ]
        c1, c2 = _merge_multipass_results(passes, "Test", 20000, _effective_config())
        q19 = next(i for i in c1['items'] if i['id'] == 'Q19')
        assert q19['verdict'] == 'PASS'

    def test_three_denouement_always_weak(self):
        """3 DENOUEMENT scenes → WEAK regardless of LLM verdict."""
        scenes = [
            _make_scene(98, "DENOUEMENT"), _make_scene(99, "DENOUEMENT"),
            _make_scene(100, "DENOUEMENT"),
        ]
        passes = [
            self._make_pass_data("PASS", scenes),
            self._make_pass_data("FAIL", scenes),
            self._make_pass_data("PASS", scenes),
        ]
        c1, c2 = _merge_multipass_results(passes, "Test", 20000, _effective_config())
        q19 = next(i for i in c1['items'] if i['id'] == 'Q19')
        assert q19['verdict'] == 'WEAK'

    def test_four_denouement_always_fail(self):
        """4 DENOUEMENT scenes → FAIL regardless of LLM verdict."""
        scenes = [
            _make_scene(97, "DENOUEMENT"), _make_scene(98, "DENOUEMENT"),
            _make_scene(99, "DENOUEMENT"), _make_scene(100, "DENOUEMENT"),
        ]
        passes = [self._make_pass_data("PASS", scenes) for _ in range(3)]
        c1, c2 = _merge_multipass_results(passes, "Test", 20000, _effective_config())
        q19 = next(i for i in c1['items'] if i['id'] == 'Q19')
        assert q19['verdict'] == 'FAIL'


# ── Dispatch-required test (c): overall=PASS impossible while any final
#    FAIL exists ──────────────────────────────────────────────────────────────

class TestOverallVerdictConsistency:
    """Overall verdict MUST be FAIL if any final check is FAIL."""

    def test_one_fail_means_overall_fail(self):
        c1 = {"items": [{"id": "Q1", "verdict": "FAIL", "note": "missing"}],
              "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 2}
        c2 = {"items": [{"id": "Q8", "verdict": "PASS", "note": "ok"}]}
        output, _, verdict = consolidate(c1, c2, "Test", 20000, _effective_config())
        assert verdict == "FAIL"
        assert output["verdict"] == "FAIL"

    def test_all_pass_means_overall_pass(self):
        c1 = {"items": [{"id": "Q1", "verdict": "PASS", "note": ""}],
              "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 2}
        c2 = {"items": [{"id": "Q8", "verdict": "PASS", "note": "ok"}]}
        output, _, verdict = consolidate(c1, c2, "Test", 20000, _effective_config())
        assert verdict == "PASS"

    def test_weak_does_not_block_pass(self):
        c1 = {"items": [{"id": "Q1", "verdict": "PASS", "note": ""}],
              "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 2}
        c2 = {"items": [{"id": "Q8", "verdict": "WEAK", "note": "advisory"}]}
        output, _, verdict = consolidate(c1, c2, "Test", 20000, _effective_config())
        assert verdict == "PASS"

    def test_multiple_fails_still_fail(self):
        c1 = {"items": [
            {"id": "Q1", "verdict": "FAIL", "note": "missing"},
            {"id": "Q19", "verdict": "FAIL", "note": "deficit",
             "post_climax_scenes": [_make_scene(97, "AFTERMATH")]},
        ], "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 1}
        c2 = {"items": [{"id": "Q8", "verdict": "FAIL", "note": "hard floor"}]}
        output, _, verdict = consolidate(c1, c2, "Test", 12000, _effective_config())
        assert verdict == "FAIL"
        assert "Q1" in output["fails"]
        assert "Q8" in output["fails"]
