"""Tests for V25 scene_writer — unit tests (no API calls)."""
import pytest
from unittest.mock import patch, MagicMock
from scene_writer import (
    _build_system_prompt, _build_user_prompt, write_scene,
    _format_entity_invariants,
)
from synopsis_parser import SceneEntry


@pytest.fixture
def sample_scene():
    return SceneEntry(
        chapter_number=2,
        scene_number=1,
        title="The Concert",
        scene_type="MIXED",
        pov="Hadeon Kovalenko",
        body="Hadeon plays piano at the conservatory. The seizure comes during the Scriabin.",
        position_in_chapter=1,
    )


@pytest.fixture
def sample_series_bible():
    return {
        "voice_register": {
            "base_voice": "Leonard-style short declarative",
            "intrusion_voice": "McCarthy-style extended",
            "intrusion_allocation": "ACTION: 0-5%, NON-ACTION: 10-20%",
            "forbidden_patterns": ["smell-of-room openings"],
        },
        "operational_doctrine": ["No prisoners", "Balaclavas during raids"],
    }


@pytest.fixture
def sample_principles():
    return [
        {
            "id": "TIME-REFS-AVOID",
            "category": "PROSE",
            "description": "No relative time references.",
            "components_inject_into_prompt": ["scene_writer"],
        },
    ]


@pytest.fixture
def sample_characters():
    return {
        "characters": [
            {"name": "Hadeon Kovalenko", "role": "protagonist",
             "voice_characteristics": "Musical vocabulary.", "arc_tags": {"ch2": "recital"}},
        ],
    }


def test_system_prompt_contains_voice(sample_series_bible, sample_principles):
    prompt = _build_system_prompt(sample_series_bible, sample_principles)
    assert "Leonard" in prompt
    assert "McCarthy" in prompt


def test_system_prompt_contains_doctrine(sample_series_bible, sample_principles):
    prompt = _build_system_prompt(sample_series_bible, sample_principles)
    assert "No prisoners" in prompt


def test_system_prompt_contains_anti_patterns(sample_series_bible, sample_principles):
    prompt = _build_system_prompt(sample_series_bible, sample_principles)
    assert "TIME-REFS-AVOID" in prompt


def test_user_prompt_contains_synopsis(sample_scene, sample_characters):
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "")
    assert "piano" in prompt.lower()
    assert "seizure" in prompt.lower()


def test_user_prompt_contains_target_words(sample_scene, sample_characters):
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "")
    assert "850" in prompt


def test_user_prompt_contains_pov(sample_scene, sample_characters):
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "")
    assert "Hadeon Kovalenko" in prompt


def test_user_prompt_contains_failure_feedback(sample_scene, sample_characters):
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "Beat 2 was not covered")
    assert "PREVIOUS ATTEMPT FAILED" in prompt
    assert "Beat 2 was not covered" in prompt


def test_user_prompt_no_feedback_when_empty(sample_scene, sample_characters):
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "")
    assert "PREVIOUS ATTEMPT FAILED" not in prompt


def test_user_prompt_no_prior_prose_when_empty(sample_scene, sample_characters):
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "", prior_prose_in_chapter=None)
    assert "PRIOR SCENES IN THIS CHAPTER" not in prompt


def test_user_prompt_no_prior_prose_when_empty_list(sample_scene, sample_characters):
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "", prior_prose_in_chapter=[])
    assert "PRIOR SCENES IN THIS CHAPTER" not in prompt


def test_user_prompt_with_two_prior_scenes(sample_scene, sample_characters):
    prior = ["First scene prose about Napoleon.", "Second scene prose about Cossack heritage."]
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "", prior_prose_in_chapter=prior)
    assert "PRIOR SCENES IN THIS CHAPTER" in prompt
    assert "First scene prose about Napoleon" in prompt
    assert "Second scene prose about Cossack heritage" in prompt
    assert "***" in prompt  # delimiter between scenes
    assert "DO NOT restate" in prompt


def test_user_prompt_with_three_prior_scenes(sample_scene, sample_characters):
    prior = ["Scene one.", "Scene two.", "Scene three."]
    prompt = _build_user_prompt(sample_scene, {}, sample_characters, 850, "", prior_prose_in_chapter=prior)
    assert prompt.count("***") >= 2  # at least 2 delimiters for 3 scenes


# ─── F1: Corrections parameter ──────────────────────────────────────────────

def test_corrections_none_prompt_unchanged(sample_series_bible, sample_principles):
    """corrections=None -> system prompt does not contain CORRECTIONS REQUIRED."""
    prompt = _build_system_prompt(sample_series_bible, sample_principles, corrections=None)
    assert "CORRECTIONS REQUIRED" not in prompt


def test_corrections_empty_string_prompt_unchanged(sample_series_bible, sample_principles):
    """corrections="" -> system prompt does not contain CORRECTIONS REQUIRED."""
    prompt = _build_system_prompt(sample_series_bible, sample_principles, corrections="")
    assert "CORRECTIONS REQUIRED" not in prompt


def test_corrections_present_prompt_includes_block(sample_series_bible, sample_principles):
    """corrections="Test correction text." -> prompt contains block and text."""
    prompt = _build_system_prompt(sample_series_bible, sample_principles,
                                  corrections="Test correction text.")
    assert "CORRECTIONS REQUIRED" in prompt
    assert "Test correction text." in prompt


def test_corrections_present_prompt_includes_rules(sample_series_bible, sample_principles):
    """Non-empty corrections -> prompt contains CONSTRAINT RESOLUTION RULES."""
    prompt = _build_system_prompt(sample_series_bible, sample_principles,
                                  corrections="Fix the name.")
    assert "CONSTRAINT RESOLUTION RULES" in prompt


def test_corrections_verbatim_passthrough(sample_series_bible, sample_principles):
    """Multi-line corrections with special chars appear verbatim in prompt."""
    corrections = "Line 1: Change 'Marie' to 'Maria'.\nLine 2: Fix the date to 1943.\n\t[Special: café]"
    prompt = _build_system_prompt(sample_series_bible, sample_principles,
                                  corrections=corrections)
    assert "Line 1: Change 'Marie' to 'Maria'." in prompt
    assert "Line 2: Fix the date to 1943." in prompt
    assert "[Special: café]" in prompt


def test_corrections_unchanged_byte_identical_call_path(sample_series_bible, sample_principles):
    """Two calls with corrections=None produce identical system prompts."""
    prompt1 = _build_system_prompt(sample_series_bible, sample_principles, corrections=None)
    prompt2 = _build_system_prompt(sample_series_bible, sample_principles, corrections=None)
    assert prompt1 == prompt2


def test_corrections_does_not_affect_other_parameters(sample_scene, sample_series_bible,
                                                       sample_principles, sample_characters):
    """Corrections parameter doesn't affect user prompt construction."""
    with patch("scene_writer._call_api") as mock_api:
        mock_api.return_value = ("Prose output.", {"input_tokens": 10, "output_tokens": 20})

        result_none = write_scene(
            scene=sample_scene, adjacent={}, series_bible=sample_series_bible,
            character_profiles=sample_characters, craft_principles=sample_principles,
            corrections=None,
        )
        call_none = mock_api.call_args

        result_with = write_scene(
            scene=sample_scene, adjacent={}, series_bible=sample_series_bible,
            character_profiles=sample_characters, craft_principles=sample_principles,
            corrections="Fix the name.",
        )
        call_with = mock_api.call_args

        # User prompts (second positional arg) should be identical
        assert call_none[0][1] == call_with[0][1]
        # System prompts should differ (corrections added)
        assert "CORRECTIONS REQUIRED" not in call_none[0][0]
        assert "CORRECTIONS REQUIRED" in call_with[0][0]


# ─── S-2 Phase 2c: Entity invariants ──────────────────────────────────────────

class TestFormatEntityInvariants:
    def test_none_returns_empty(self):
        assert _format_entity_invariants(None) == ""

    def test_empty_dict_returns_empty(self):
        assert _format_entity_invariants({}) == ""

    def test_empty_entities_returns_empty(self):
        assert _format_entity_invariants({"entities": []}) == ""

    def test_scalar_entity(self):
        ledger = {"entities": [
            {"id": "cipher_rotors", "canonical_name": "KL-7 cipher rotors",
             "entity_class": "scalar", "invariants": {"count": 8, "designation": "KL-7"}}
        ]}
        result = _format_entity_invariants(ledger)
        assert "Scalar invariants:" in result
        assert "KL-7 cipher rotors" in result
        assert "count: 8" in result
        assert "designation: KL-7" in result

    def test_stateful_entity(self):
        ledger = {"entities": [
            {"id": "coyle", "canonical_name": "Coyle's wound",
             "entity_class": "stateful",
             "state_track": {
                 "initial_state": "pristine",
                 "allowed_transitions": [
                     {"from": "pristine", "to": "thigh_shrapnel", "occurs_at_scene": 8}
                 ],
                 "forbidden_states": ["eye_socket_injury"]
             }}
        ]}
        result = _format_entity_invariants(ledger)
        assert "Stateful entities" in result
        assert "Coyle's wound" in result
        assert "pristine" in result
        assert "Scene 8" in result
        assert "eye_socket_injury" in result

    def test_lifecycle_entity(self):
        ledger = {"entities": [
            {"id": "bounchanh_vorasak", "canonical_name": "Bounchanh Vorasak",
             "entity_class": "lifecycle_role",
             "lifecycle": {"alive_at_end_of_book": True, "source": "series_bible"}}
        ]}
        result = _format_entity_invariants(ledger)
        assert "Lifecycle and role invariants:" in result
        assert "Bounchanh Vorasak" in result
        assert "survives the book" in result
        assert "dying or dead" in result

    def test_role_binding_entity(self):
        ledger = {"entities": [
            {"id": "black_widow_crew", "canonical_name": "Black Widow Crew",
             "entity_class": "lifecycle_role",
             "role_bindings": [
                 {"context": "aboard the gunship", "required_form": "role_only",
                  "forbidden_references": ["Dalton", "Vance"],
                  "permitted_roles": ["co-pilot", "gunner"]}
             ]}
        ]}
        result = _format_entity_invariants(ledger)
        assert "Lifecycle and role invariants:" in result
        assert "aboard the gunship" in result
        assert "Dalton" in result
        assert "co-pilot" in result

    def test_mixed_ledger_all_sections(self):
        ledger = {"entities": [
            {"id": "x", "canonical_name": "X", "entity_class": "scalar",
             "invariants": {"v": 1}},
            {"id": "y", "canonical_name": "Y", "entity_class": "stateful",
             "state_track": {"initial_state": "off", "allowed_transitions": [],
                             "forbidden_states": []}},
            {"id": "z", "canonical_name": "Z", "entity_class": "lifecycle_role",
             "lifecycle": {"alive_at_end_of_book": True}},
        ]}
        result = _format_entity_invariants(ledger)
        assert "Scalar invariants:" in result
        assert "Stateful entities" in result
        assert "Lifecycle and role invariants:" in result

    def test_real_csar_ledger(self):
        """Load actual CSAR entity_ledger.json and verify formatting succeeds."""
        import json
        from pathlib import Path
        path = Path("/anpd/v25/series/airmen/b01/work/entity_ledger.json")
        if not path.exists():
            pytest.skip("CSAR entity_ledger.json not present")
        ledger = json.loads(path.read_text())
        result = _format_entity_invariants(ledger)
        assert len(result) > 300
        assert "cipher_rotors" in result.lower() or "KL-7" in result


class TestBuildSystemPromptWithLedger:
    def test_no_ledger_no_invariants_block(self, sample_series_bible, sample_principles):
        prompt = _build_system_prompt(sample_series_bible, sample_principles,
                                      entity_ledger=None)
        assert "ENTITY INVARIANTS" not in prompt

    def test_with_ledger_includes_block(self, sample_series_bible, sample_principles):
        ledger = {"entities": [
            {"id": "x", "canonical_name": "TestEntity", "entity_class": "scalar",
             "invariants": {"count": 42}}
        ]}
        prompt = _build_system_prompt(sample_series_bible, sample_principles,
                                      entity_ledger=ledger)
        assert "ENTITY INVARIANTS" in prompt
        assert "TestEntity" in prompt
        assert "42" in prompt

    def test_invariants_before_corrections(self, sample_series_bible, sample_principles):
        ledger = {"entities": [
            {"id": "x", "canonical_name": "X", "entity_class": "scalar",
             "invariants": {"v": "marker_inv"}}
        ]}
        prompt = _build_system_prompt(sample_series_bible, sample_principles,
                                      entity_ledger=ledger,
                                      corrections="marker_corr")
        inv_pos = prompt.find("ENTITY INVARIANTS")
        corr_pos = prompt.find("CORRECTIONS REQUIRED")
        assert inv_pos < corr_pos


class TestWriteScenePassesLedger:
    @patch("scene_writer._call_api")
    def test_ledger_appears_in_system_prompt(self, mock_api, sample_scene,
                                              sample_series_bible, sample_principles,
                                              sample_characters):
        mock_api.return_value = ("Prose.", {"input_tokens": 10, "output_tokens": 20})
        ledger = {"entities": [
            {"id": "x", "canonical_name": "X", "entity_class": "scalar",
             "invariants": {"v": "ledger_marker_xyz"}}
        ]}
        result = write_scene(
            scene=sample_scene, adjacent={}, series_bible=sample_series_bible,
            character_profiles=sample_characters, craft_principles=sample_principles,
            entity_ledger=ledger,
        )
        assert "ledger_marker_xyz" in result.system_prompt
