"""Tests for V25 manuscript_assembler."""
import os
import pytest
from manuscript_assembler import assemble_manuscript
from synopsis_parser import SynopsisStructure, ChapterEntry, SceneEntry


@pytest.fixture
def sample_synopsis():
    return SynopsisStructure(chapters=[
        ChapterEntry(chapter_number=1, title="Prologue", scenes=[
            SceneEntry(1, 1, "Scene A", "ACTION", "Narrator", "body", 1),
            SceneEntry(1, 2, "Scene B", "NON_ACTION", "Narrator", "body", 2),
        ]),
        ChapterEntry(chapter_number=2, title="", scenes=[
            SceneEntry(2, 1, "Scene C", "MIXED", "Hadeon", "body", 1),
        ]),
    ])


@pytest.fixture
def sample_scene_results():
    return {
        (1, 1): "The battle raged across the frozen field.",
        (1, 2): "The Cossacks withdrew to the tree line.",
        (2, 1): "Hadeon sat at the piano and placed his hands.",
    }


def test_assemble_creates_chapter_files(tmp_path, sample_synopsis, sample_scene_results):
    result = assemble_manuscript(sample_scene_results, str(tmp_path), sample_synopsis)
    assert len(result["chapters"]) == 2
    for path in result["chapters"]:
        assert os.path.exists(path)


def test_assemble_creates_full_manuscript(tmp_path, sample_synopsis, sample_scene_results):
    result = assemble_manuscript(sample_scene_results, str(tmp_path), sample_synopsis)
    assert os.path.exists(result["full"])
    with open(result["full"]) as f:
        text = f.read()
    assert "frozen field" in text
    assert "piano" in text


def test_assemble_creates_scene_files(tmp_path, sample_synopsis, sample_scene_results):
    result = assemble_manuscript(sample_scene_results, str(tmp_path), sample_synopsis)
    assert len(result["scene_files"]) == 3


def test_chapter_header_includes_title(tmp_path, sample_synopsis, sample_scene_results):
    result = assemble_manuscript(sample_scene_results, str(tmp_path), sample_synopsis)
    with open(result["chapters"][0]) as f:
        text = f.read()
    assert "# Chapter 1 — Prologue" in text


def test_chapter_without_title(tmp_path, sample_synopsis, sample_scene_results):
    result = assemble_manuscript(sample_scene_results, str(tmp_path), sample_synopsis)
    with open(result["chapters"][1]) as f:
        text = f.read()
    assert "# Chapter 2\n" in text


def test_scene_break_markers(tmp_path, sample_synopsis, sample_scene_results):
    result = assemble_manuscript(sample_scene_results, str(tmp_path), sample_synopsis)
    with open(result["chapters"][0]) as f:
        text = f.read()
    assert "***" in text  # Scene break between scenes 1 and 2


def test_full_manuscript_is_sum_of_chapters(tmp_path, sample_synopsis, sample_scene_results):
    result = assemble_manuscript(sample_scene_results, str(tmp_path), sample_synopsis)
    with open(result["full"]) as f:
        full = f.read()
    total_ch_words = 0
    for path in result["chapters"]:
        with open(path) as f:
            total_ch_words += len(f.read().split())
    full_words = len(full.split())
    assert abs(full_words - total_ch_words) < 5  # small tolerance for whitespace
