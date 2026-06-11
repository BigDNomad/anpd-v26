"""Tests for V25 scene_auditor."""
import pytest
from scene_auditor import (
    check_word_count, check_no_smell_openers, check_no_relative_time_refs,
    check_no_team_age_refs, check_no_base_language, check_no_metadata_in_output,
    check_character_state, check_balaclava_ops, check_no_reintroduction,
    check_reflexive_tautology, count_reflexive_tautologies, audit_scene,
)
from synopsis_parser import SceneEntry


@pytest.fixture
def action_scene():
    return SceneEntry(
        chapter_number=7, scene_number=2, title="The Ambush",
        scene_type="ACTION", pov="Hadeon Kovalenko",
        body="The unit ambushes the truck.", position_in_chapter=2,
    )


@pytest.fixture
def non_action_scene():
    return SceneEntry(
        chapter_number=4, scene_number=1, title="The Burial",
        scene_type="NON_ACTION", pov="Hadeon Kovalenko",
        body="They bury their parents.", position_in_chapter=1,
    )


# ── Word count ──

def test_word_count_pass():
    prose = " ".join(["word"] * 850)
    assert check_word_count(prose) == []


def test_word_count_too_low():
    prose = " ".join(["word"] * 500)
    findings = check_word_count(prose)
    assert len(findings) == 1
    assert findings[0].severity == "CLASS_A"


def test_word_count_too_high():
    prose = " ".join(["word"] * 1200)
    findings = check_word_count(prose)
    assert len(findings) == 1
    assert findings[0].severity == "CLASS_A"


# ── Smell openers ──

def test_smell_opener_detected():
    prose = "The smell of diesel filled the corridor. Hadeon moved forward."
    findings = check_no_smell_openers(prose)
    assert len(findings) == 1


def test_no_smell_opener_clean():
    prose = "Hadeon moved through the corridor. The light was grey."
    assert check_no_smell_openers(prose) == []


# ── Time refs ──

def test_time_ref_detected():
    prose = "Three hours later, the unit reached the barn."
    findings = check_no_relative_time_refs(prose)
    assert len(findings) >= 1


def test_time_ref_clean():
    prose = "The unit reached the barn. The light was failing."
    assert check_no_relative_time_refs(prose) == []


# ── Age refs ──

def test_age_ref_detected():
    prose = "Hadeon was sixteen years old when the war began."
    findings = check_no_team_age_refs(prose)
    assert len(findings) >= 1


def test_age_ref_clean():
    prose = "Hadeon stood at the table. He held the saber."
    assert check_no_team_age_refs(prose) == []


# ── Base language ──

def test_base_language_detected():
    prose = "They returned to their base and prepared for the operation."
    findings = check_no_base_language(prose)
    assert len(findings) >= 1


def test_base_language_clean():
    prose = "They made camp in the birch stand and prepared weapons."
    assert check_no_base_language(prose) == []


# ── Metadata leak ──

def test_metadata_detected():
    prose = "### Scene 3 — Test [TYPE: ACTION]\nThe battle began."
    findings = check_no_metadata_in_output(prose)
    assert len(findings) >= 1


def test_metadata_clean():
    prose = "The battle began at the road's edge. Hadeon moved left."
    assert check_no_metadata_in_output(prose) == []


# ── Character state ──

def test_dead_char_acting_detected():
    prose = "Taras walks to the fire and sits down."
    scene = SceneEntry(8, 1, "Test", "NON_ACTION", "Hadeon", "", 1)
    findings = check_character_state(prose, scene, 8)
    assert len(findings) >= 1
    assert findings[0].severity == "CLASS_A"


def test_dead_char_in_memory_ok():
    prose = "He remembered what Taras had said about the uncle."
    scene = SceneEntry(8, 1, "Test", "NON_ACTION", "Hadeon", "", 1)
    findings = check_character_state(prose, scene, 8)
    assert len(findings) == 0


def test_alive_char_no_finding():
    prose = "Symon walks to the radio and begins scanning."
    scene = SceneEntry(8, 1, "Test", "NON_ACTION", "Hadeon", "", 1)
    findings = check_character_state(prose, scene, 8)
    assert len(findings) == 0


# ── Balaclava ──

def test_balaclava_missing_in_combat(action_scene):
    prose = "They fired from the treeline. The ambush was precise. Shots rang out."
    findings = check_balaclava_ops(prose, action_scene)
    assert len(findings) == 1
    assert findings[0].severity == "CLASS_B"


def test_balaclava_present(action_scene):
    prose = "They pulled balaclavas down and fired from the treeline."
    findings = check_balaclava_ops(prose, action_scene)
    assert len(findings) == 0


# ── Full audit (deterministic only) ──

def test_audit_clean_prose(non_action_scene):
    prose = " ".join(["word"] * 850)
    result = audit_scene(prose, non_action_scene, use_llm=False)
    assert result.passed


def test_audit_short_prose(non_action_scene):
    prose = "Too short."
    result = audit_scene(prose, non_action_scene, use_llm=False)
    assert not result.passed
    class_a = [f for f in result.findings if f.severity == "CLASS_A"]
    assert len(class_a) >= 1


# ── Reintroduction check (no LLM — unit-level) ──

def test_reintroduction_empty_prior_returns_empty(non_action_scene):
    prose = "New content here."
    findings = check_no_reintroduction(prose, non_action_scene, prior_prose_in_chapter=[], use_llm=False)
    assert findings == []


def test_reintroduction_none_prior_returns_empty(non_action_scene):
    findings = check_no_reintroduction("Some prose.", non_action_scene, prior_prose_in_chapter=None, use_llm=False)
    assert findings == []


def test_reintroduction_no_llm_returns_empty(non_action_scene):
    findings = check_no_reintroduction("Some prose.", non_action_scene,
                                        prior_prose_in_chapter=["Prior scene text."], use_llm=False)
    assert findings == []


# ── audit_scene with prior_prose parameter ──

def test_audit_accepts_prior_prose_param(non_action_scene):
    prose = " ".join(["word"] * 850)
    result = audit_scene(prose, non_action_scene, use_llm=False, prior_prose_in_chapter=["prior scene"])
    assert result.passed  # deterministic checks only, should pass


# ── Reflexive tautology ──

def test_tautology_count_zero():
    prose = "He walked to the door. She opened it."
    assert count_reflexive_tautologies(prose) == 0


def test_tautology_count_detected():
    prose = "He watched the way he always did, which was what he was."
    assert count_reflexive_tautologies(prose) >= 1


def test_tautology_over_budget():
    prose = "The way he always did. Which was what it was. As they had always been."
    findings = check_reflexive_tautology(prose, budget_remaining=0)
    assert any(f.severity == "CLASS_A" for f in findings)


def test_tautology_within_budget():
    prose = "The way he always did something about it."
    findings = check_reflexive_tautology(prose, budget_remaining=5)
    assert len(findings) == 0 or all(f.severity != "CLASS_A" for f in findings)


def test_tautology_no_budget_advisory():
    prose = "The way he always did something."
    findings = check_reflexive_tautology(prose, budget_remaining=None)
    # Advisory only when no budget tracking
    for f in findings:
        assert f.severity == "CLASS_B"
