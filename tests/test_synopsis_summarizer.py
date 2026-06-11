"""Tests for synopsis_summarizer — two-report architecture."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from synopsis_summarizer import (
    build_quality_review_prompt,
    build_story_summary_prompt,
    build_beat_summary_prompt,
    parse_json_response,
    main,
    summarize_synopsis,
    MAX_OUTPUT_TOKENS,
)


def _valid_quality_review_json():
    return json.dumps({
        "verdict": "PROCEED",
        "verdict_rationale": "Strong structure.",
        "pacing": "Well balanced.",
        "twists": "Land at correct positions.",
        "action_distribution": "Good spread.",
        "final_battle": "Heavy and satisfying.",
        "resolution": "Clean emotional landing.",
        "gaps": [{"concern": "Test concern", "proposal": "Test proposal"}],
    })


def _valid_story_summary_json():
    return json.dumps({
        "setup": "Protagonist enters the world.",
        "rising_action": "Complications arise.",
        "midpoint": "Everything changes.",
        "crisis": "The lowest point.",
        "climax": "Final confrontation.",
        "resolution": "New equilibrium.",
        "protagonist_arc": "From analyst to operator.",
        "central_conflict": "Can the network be dismantled before it reconstitutes?",
    })


def _valid_beat_summary_json():
    return json.dumps({
        "scenes": [
            {"number": 1, "title": "Opening", "type": "ACTION", "pov": "Hank", "beat": "Hank watches the raid from El Hatillo rooftop."},
            {"number": 2, "title": "Candidates", "type": "NON-ACTION", "pov": "Hank", "beat": "Hank selects Lena from CIA candidates at Langley."},
        ]
    })


class TestPromptBuilders:
    def test_quality_review_prompt_includes_verdict_requirement(self):
        prompt = build_quality_review_prompt("Test synopsis text.")
        assert "PROCEED" in prompt
        assert "DO NOT PROCEED" in prompt

    def test_story_summary_prompt_excludes_quality_assessment(self):
        prompt = build_story_summary_prompt("Test synopsis text.")
        prompt_lower = prompt.lower()
        assert "verdict" not in prompt_lower
        # The summary prompt should not ask for structural analysis output
        assert '"pacing"' not in prompt_lower
        assert '"action_distribution"' not in prompt_lower


class TestParseJsonResponse:
    def test_json_decode_error_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc_info:
            parse_json_response('{"truncated...', "test")
        assert exc_info.value.code == 1

    def test_valid_json_returns_dict(self):
        result = parse_json_response('{"key": "value"}', "test")
        assert result == {"key": "value"}


class TestMainThreeDocx:
    @patch("synopsis_summarizer.call_model")
    def test_three_docx_files_written(self, mock_call, tmp_path, monkeypatch):
        mock_call.side_effect = [
            _valid_quality_review_json(), _valid_story_summary_json(), _valid_beat_summary_json()
        ]
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("# Synopsis\n### Scene 1\n- Beat.")
        intake = tmp_path / "intake.json"
        intake.write_text(json.dumps({"title": "Test"}))
        monkeypatch.setattr(
            sys, "argv",
            ["prog", "--synopsis", str(synopsis), "--intake", str(intake),
             "--output-dir", str(tmp_path)],
        )
        result = main()
        assert result == 0
        docx_files = list(tmp_path.glob("*.docx"))
        assert len(docx_files) == 3
        names = [f.name for f in docx_files]
        assert any("quality_review" in n for n in names)
        assert any("story_summary" in n for n in names)
        assert any("beat_summary" in n for n in names)

    @patch("synopsis_summarizer.call_model")
    @patch("synopsis_summarizer.render_quality_review_docx")
    def test_missing_quality_review_exits_nonzero(self, mock_render_qr, mock_call, tmp_path, monkeypatch):
        mock_call.side_effect = [
            _valid_quality_review_json(), _valid_story_summary_json(), _valid_beat_summary_json()
        ]
        mock_render_qr.return_value = None
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("# Synopsis\n### Scene 1\n- Beat.")
        intake = tmp_path / "intake.json"
        intake.write_text(json.dumps({"title": "Test"}))
        monkeypatch.setattr(
            sys, "argv",
            ["prog", "--synopsis", str(synopsis), "--intake", str(intake),
             "--output-dir", str(tmp_path)],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    @patch("synopsis_summarizer.call_model")
    @patch("synopsis_summarizer.render_story_summary_docx")
    def test_missing_story_summary_exits_nonzero(self, mock_render_ss, mock_call, tmp_path, monkeypatch):
        mock_call.side_effect = [
            _valid_quality_review_json(), _valid_story_summary_json(), _valid_beat_summary_json()
        ]
        mock_render_ss.return_value = None
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("# Synopsis\n### Scene 1\n- Beat.")
        intake = tmp_path / "intake.json"
        intake.write_text(json.dumps({"title": "Test"}))
        monkeypatch.setattr(
            sys, "argv",
            ["prog", "--synopsis", str(synopsis), "--intake", str(intake),
             "--output-dir", str(tmp_path)],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    @patch("synopsis_summarizer.call_model")
    @patch("synopsis_summarizer.render_beat_summary_docx")
    def test_missing_beat_summary_exits_nonzero(self, mock_render_bs, mock_call, tmp_path, monkeypatch):
        mock_call.side_effect = [
            _valid_quality_review_json(), _valid_story_summary_json(), _valid_beat_summary_json()
        ]
        mock_render_bs.return_value = None
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("# Synopsis\n### Scene 1\n- Beat.")
        intake = tmp_path / "intake.json"
        intake.write_text(json.dumps({"title": "Test"}))
        monkeypatch.setattr(
            sys, "argv",
            ["prog", "--synopsis", str(synopsis), "--intake", str(intake),
             "--output-dir", str(tmp_path)],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    @patch("synopsis_summarizer.call_model")
    def test_callable_returns_three_paths(self, mock_call, tmp_path):
        mock_call.side_effect = [
            _valid_quality_review_json(), _valid_story_summary_json(), _valid_beat_summary_json()
        ]
        synopsis = tmp_path / "synopsis.md"
        synopsis.write_text("# Synopsis\n### Scene 1\n- Beat.")
        intake = tmp_path / "intake.json"
        intake.write_text(json.dumps({"title": "Test"}))
        result = summarize_synopsis(
            synopsis_path=synopsis,
            intake_path=intake,
            output_dir=tmp_path,
        )
        assert result["status"] == "success"
        assert result["quality_review_path"].exists()
        assert result["story_summary_path"].exists()
        assert result["beat_summary_path"].exists()

    def test_beat_summary_prompt_specifies_scene_count(self):
        prompt = build_beat_summary_prompt("Test synopsis.")
        assert "one entry per scene" in prompt.lower() or "one line per scene" in prompt.lower()


class TestMaxTokensConfig:
    def test_default_max_tokens_is_8k(self):
        assert MAX_OUTPUT_TOKENS == 8000
