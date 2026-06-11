"""
Tests for Q8 word-count severity bands after recalibration.

Four bands:
1. <13,000 → FAIL (hard floor)
2. 13,000–17,999 → WEAK (advisory, above published-book floor)
3. 18,000–28,000 → PASS (target range)
4. >28,000 → FAIL (over-padded, keeps LLM verdict)
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synopsis_auditor import consolidate


def _make_call_data_with_q8(verdict="FAIL", note=""):
    """Build minimal call_1/call_2 data with a Q8 item in call_2."""
    call_1 = {"items": [], "total_scenes": 100, "action_scenes": 67,
              "action_scene_percentage": 67.0, "resolution_scenes": 2}
    call_2 = {"items": [{"id": "Q8", "verdict": verdict, "note": note}]}
    return call_1, call_2


def _effective_config():
    return {
        "target_synopsis_word_min": 18000,
        "target_synopsis_word_max": 28000,
        "action_scene_percentage_min": 0.65,
    }


class TestQ8HardFloor:
    def test_below_13000_fails(self):
        """Word count below 13,000 → FAIL."""
        call_1, call_2 = _make_call_data_with_q8("FAIL", "below min")
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 12999, _effective_config())
        q8 = [i for i in output_json["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "FAIL"
        assert "Hard floor" in q8["note"]
        assert "Q8" in output_json["fails"]


class TestQ8AdvisoryBand:
    def test_13000_weak(self):
        """Word count at 13,000 → WEAK."""
        call_1, call_2 = _make_call_data_with_q8("FAIL", "below min")
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 13000, _effective_config())
        q8 = [i for i in output_json["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "WEAK"
        assert "advisory" in q8["note"]
        assert "Q8" not in output_json["fails"]
        assert "Q8" in output_json["weaks"]

    def test_14487_weak(self):
        """Published-book word count (14,487) → WEAK, not FAIL."""
        call_1, call_2 = _make_call_data_with_q8("FAIL", "below min")
        output_json, _, _ = consolidate(call_1, call_2, "Test", 14487, _effective_config())
        q8 = [i for i in output_json["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "WEAK"

    def test_17999_weak(self):
        """Word count at 17,999 → WEAK (just below target range)."""
        call_1, call_2 = _make_call_data_with_q8("FAIL", "below min")
        output_json, _, _ = consolidate(call_1, call_2, "Test", 17999, _effective_config())
        q8 = [i for i in output_json["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "WEAK"


class TestQ8TargetRange:
    def test_18000_passes(self):
        """Word count at 18,000 → PASS (bottom of target range)."""
        call_1, call_2 = _make_call_data_with_q8("PASS", "in range")
        output_json, _, verdict = consolidate(call_1, call_2, "Test", 18000, _effective_config())
        q8 = [i for i in output_json["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "PASS"
        assert verdict != "FAIL"

    def test_28000_passes(self):
        """Word count at 28,000 → PASS (top of target range)."""
        call_1, call_2 = _make_call_data_with_q8("PASS", "in range")
        output_json, _, _ = consolidate(call_1, call_2, "Test", 28000, _effective_config())
        q8 = [i for i in output_json["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "PASS"


class TestQ8OverPadded:
    def test_above_28000_keeps_llm_fail(self):
        """Word count above 28,000 → keeps LLM FAIL verdict."""
        call_1, call_2 = _make_call_data_with_q8("FAIL", "over max")
        output_json, _, _ = consolidate(call_1, call_2, "Test", 28001, _effective_config())
        q8 = [i for i in output_json["items"] if i["id"] == "Q8"][0]
        assert q8["verdict"] == "FAIL"
        assert "Q8" in output_json["fails"]
