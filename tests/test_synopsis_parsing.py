"""Tests for the shared canonical scene-header parser (synopsis_parsing.py).

Covers:
  - [TYPE:] only headers
  - [TYPE:] + [PILLAR:] headers
  - [TYPE:] + [PILLAR:] + [POV:] headers
  - V24 fallback
  - Body extraction
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "pipeline"))

from synopsis_parsing import parse_scene_headers, ParsedScene


def test_type_only():
    text = "### Scene 1 — Title [TYPE: ACTION]\n\nBody text here.\n"
    scenes = parse_scene_headers(text)
    assert len(scenes) == 1
    assert scenes[0].number == 1
    assert scenes[0].title == "Title"
    assert scenes[0].scene_type == "ACTION"
    assert scenes[0].pillar == ""
    assert scenes[0].pov == ""
    assert "Body text here." in scenes[0].body


def test_type_and_pillar():
    text = "### Scene 24 — The Fall [TYPE: ACTION] [PILLAR: TWIST1]\n\nFalling.\n"
    scenes = parse_scene_headers(text)
    assert len(scenes) == 1
    assert scenes[0].number == 24
    assert scenes[0].title == "The Fall"
    assert scenes[0].scene_type == "ACTION"
    assert scenes[0].pillar == "TWIST1"
    assert scenes[0].pov == ""


def test_type_pillar_and_pov():
    text = "### Scene 56 — Kill Order [TYPE: SUSPENSE] [PILLAR: TWIST2] [POV: Archer]\n\nTense.\n"
    scenes = parse_scene_headers(text)
    assert len(scenes) == 1
    assert scenes[0].number == 56
    assert scenes[0].title == "Kill Order"
    assert scenes[0].scene_type == "SUSPENSE"
    assert scenes[0].pillar == "TWIST2"
    assert scenes[0].pov == "Archer"


def test_type_and_pov_no_pillar():
    text = "### Scene 10 — Alone Among the Dead [TYPE: SUSPENSE] [POV: Vance]\n\nQuiet.\n"
    scenes = parse_scene_headers(text)
    assert len(scenes) == 1
    assert scenes[0].scene_type == "SUSPENSE"
    assert scenes[0].pillar == ""
    assert scenes[0].pov == "Vance"


def test_em_dash_and_en_dash():
    """Both em-dash (—) and en-dash (–) are accepted."""
    for dash in ["—", "–", "-"]:
        text = f"### Scene 1 {dash} Title [TYPE: ACTION]\n\nBody.\n"
        scenes = parse_scene_headers(text)
        assert len(scenes) == 1, f"Failed for dash: {dash!r}"
        assert scenes[0].title == "Title"


def test_v24_fallback():
    text = "## SCENE 1: Old Format Title\n\nBody.\n## SCENE 2: Second\n\nMore.\n"
    scenes = parse_scene_headers(text)
    assert len(scenes) == 2
    assert scenes[0].number == 1
    assert scenes[0].title == "Old Format Title"
    assert scenes[1].number == 2


def test_multiple_scenes_with_mixed_tags():
    text = (
        "## Chapter 1\n\n"
        "### Scene 1 — Start [TYPE: NON-ACTION]\n\nOpening.\n\n"
        "### Scene 2 — Middle [TYPE: ACTION] [PILLAR: TWIST1]\n\nTurning point.\n\n"
        "### Scene 3 — End [TYPE: SUSPENSE] [POV: Hero]\n\nClosing.\n"
    )
    scenes = parse_scene_headers(text)
    assert len(scenes) == 3
    assert scenes[0].scene_type == "NON_ACTION"
    assert scenes[0].pillar == ""
    assert scenes[1].scene_type == "ACTION"
    assert scenes[1].pillar == "TWIST1"
    assert scenes[2].scene_type == "SUSPENSE"
    assert scenes[2].pov == "Hero"


def test_empty_input():
    assert parse_scene_headers("") == []
    assert parse_scene_headers("   ") == []


def test_no_headers():
    assert parse_scene_headers("Just some text without headers.") == []


def test_body_between_scenes():
    text = (
        "### Scene 1 — First [TYPE: ACTION]\n\nBody one.\n\n"
        "### Scene 2 — Second [TYPE: ACTION]\n\nBody two.\n"
    )
    scenes = parse_scene_headers(text)
    assert len(scenes) == 2
    assert "Body one." in scenes[0].body
    assert "Body two." in scenes[1].body
    # Body one should NOT contain body two
    assert "Body two." not in scenes[0].body


def test_lowest_point_pillar():
    text = "### Scene 77 — Slide Locks Back [TYPE: ACTION] [PILLAR: LOWEST_POINT]\n\nDespair.\n"
    scenes = parse_scene_headers(text)
    assert scenes[0].pillar == "LOWEST_POINT"


def test_final_battle_pillar():
    text = "### Scene 86 — Sandy's Run [TYPE: ACTION] [PILLAR: FINAL_BATTLE]\n\nClimax.\n"
    scenes = parse_scene_headers(text)
    assert scenes[0].pillar == "FINAL_BATTLE"
