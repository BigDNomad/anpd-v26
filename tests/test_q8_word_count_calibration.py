"""
Tests for Q8 word-count severity bands after recalibration.

Four bands:
1. <13,000 → FAIL (hard floor)
2. 13,000–17,999 → WEAK (advisory, above published-book floor)
3. 18,000–28,000 → PASS (target range)
4. >28,000 → FAIL (over-padded)

V26 T1800: deterministic bands are applied in _merge_multipass_results
(not consolidate).  Tests verify band helpers and the merge path.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_auditor import (
    _q8_band_verdict,
    _q8_band_note,
    _merge_multipass_results,
    consolidate,
)


def _effective_config():
    return {
        "target_synopsis_word_min": 18000,
        "target_synopsis_word_max": 28000,
        "action_scene_percentage_min": 0.65,
    }


# ── Band helper unit tests ──────────────────────────────────────────────────

class TestQ8BandVerdict:
    def test_below_hard_floor(self):
        assert _q8_band_verdict(12999, 18000, 28000) == 'FAIL'

    def test_at_hard_floor(self):
        assert _q8_band_verdict(13000, 18000, 28000) == 'WEAK'

    def test_advisory_band(self):
        assert _q8_band_verdict(14811, 18000, 28000) == 'WEAK'

    def test_at_target_min(self):
        assert _q8_band_verdict(18000, 18000, 28000) == 'PASS'

    def test_at_target_max(self):
        assert _q8_band_verdict(28000, 18000, 28000) == 'PASS'

    def test_above_target_max(self):
        assert _q8_band_verdict(28001, 18000, 28000) == 'FAIL'

    def test_published_book_calibration(self):
        """14,487 (published CSAR) → WEAK, not FAIL."""
        assert _q8_band_verdict(14487, 18000, 28000) == 'WEAK'


# ── Dispatch-required test (a): word_count=14811 → Q8=WEAK regardless of
#    injected LLM votes ──────────────────────────────────────────────────────

class TestQ8DeterministicThroughMerge:
    """word_count=14811 → Q8=WEAK no matter what the LLM voted."""

    def _make_pass_data(self, q8_llm_verdict):
        """Build one pass worth of (call_1, call_2) with an injected Q8 LLM verdict."""
        c1 = {"items": [], "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 2}
        c2 = {"items": [{"id": "Q8", "verdict": q8_llm_verdict, "note": "LLM said this"}]}
        return c1, c2

    def test_all_passes_fail_still_weak(self):
        """3 LLM passes all say FAIL → merge produces WEAK (band override)."""
        passes = [self._make_pass_data("FAIL") for _ in range(3)]
        c1, c2 = _merge_multipass_results(passes, "Test", 14811, _effective_config())
        q8 = next(i for items in [c1['items'], c2['items']] for i in items if i['id'] == 'Q8')
        assert q8['verdict'] == 'WEAK'

    def test_all_passes_pass_still_weak(self):
        """3 LLM passes all say PASS → merge produces WEAK (band override)."""
        passes = [self._make_pass_data("PASS") for _ in range(3)]
        c1, c2 = _merge_multipass_results(passes, "Test", 14811, _effective_config())
        q8 = next(i for items in [c1['items'], c2['items']] for i in items if i['id'] == 'Q8')
        assert q8['verdict'] == 'WEAK'

    def test_mixed_llm_votes_still_weak(self):
        """Mixed LLM votes (FAIL/PASS/WEAK) → merge produces WEAK (band override)."""
        passes = [
            self._make_pass_data("FAIL"),
            self._make_pass_data("PASS"),
            self._make_pass_data("WEAK"),
        ]
        c1, c2 = _merge_multipass_results(passes, "Test", 14811, _effective_config())
        q8 = next(i for items in [c1['items'], c2['items']] for i in items if i['id'] == 'Q8')
        assert q8['verdict'] == 'WEAK'


# ── consolidate no longer overrides Q8 — verify pass-through ──────────────

class TestConsolidateQ8PassThrough:
    """consolidate must preserve Q8 verdict as-is (no override)."""

    def test_weak_stays_weak(self):
        c1 = {"items": [], "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 2}
        c2 = {"items": [{"id": "Q8", "verdict": "WEAK", "note": "already set"}]}
        output, _, verdict = consolidate(c1, c2, "Test", 14811, _effective_config())
        q8 = [i for i in output["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "WEAK"
        assert verdict == "PASS"  # WEAK does not block PASS

    def test_fail_stays_fail(self):
        c1 = {"items": [], "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 2}
        c2 = {"items": [{"id": "Q8", "verdict": "FAIL", "note": "hard floor"}]}
        output, _, verdict = consolidate(c1, c2, "Test", 12000, _effective_config())
        q8 = [i for i in output["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "FAIL"
        assert verdict == "FAIL"
