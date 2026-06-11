"""ANPD V24 — character_profile_auditor

Gate 2 character profile auditor. Validates character profile files against
Character Profile Schema v1.1.0 and emits V24 findings (per White Paper §3.8)
that the master controller (Phase 4) routes through the auto-fix tiers.

Built fresh in V24 with V20 `story_seed/auditors/character_auditor.py` as
reference per White Paper §2.8. Reference audit at
/anpd/v26/docs/character_auditor_v20_reference_audit_20260427_2330.md
classifies V20 logic as INHERIT / EXTEND / RELOCATE / EXCLUDE.

Architecture per White Paper §2.12:

- Auditor reads its inputs and produces findings. It does not orchestrate,
  does not retry, does not write STOP_REPORT.json. The caller (master
  controller in Phase 4) handles those concerns.
- Auditor reads thresholds and identifiers from config_resolver; no
  hardcoded values that duplicate canonical sources (§2.1).
- Each check is implemented as a focused function returning a list of
  findings. Aggregation happens in audit_character_profiles().

Build status (this commit):

- File skeleton + finding emission scaffolding.
- One deterministic check implemented: envelope-key violation per
  Schema §3.4. The auditor reports envelope keys as Class A findings
  rather than raising (which is what character_profile_merge does),
  because the auditor's job is to surface findings for downstream auto-fix
  tier routing, not to halt processing.
- Subsequent commits add one check per commit until all V20-INHERIT and
  V24-NEW checks are present. The Haiku LLM check (V20 EXTEND) lands
  late as a separate commit.

CLI surface:

- --series-config (required) — drives effective_config resolution and
  Haiku model identifier (when LLM check lands later).
- --series-profiles (required) — path to series-level character_profiles.json.
- --book-profiles (required) — path to book-specific character_profiles.json.

The auditor consumes both files because Schema §3.3 mandates merged-cast
validation: relationship symmetry crosses the file boundary, name collision
detection requires comparing both sets, etc. Components that need only one
file's validation can use specific check functions directly without going
through the CLI / main() path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from character_profile_merge import (
    FORBIDDEN_TOP_LEVEL_KEYS,
    ProfileMergeError,
    merge_character_profiles,
)
from config_resolver import resolve_config
from findings import create_finding


# ── Auditor identity ──────────────────────────────────────────────────────────

AUDITOR_NAME = "character_profile_auditor"
GATE = "character_profile"


# ── Schema-derived constants ──────────────────────────────────────────────────
#
# These constants encode rules from Character Profile Schema v1.1.0 in
# machine-readable form. The schema document is the canonical narrative source;
# this module's constants are the canonical machine-readable source for the
# same rules. They drift together — if the schema doc changes, these constants
# update in lockstep. Comments below cite the relevant §-numbers.

# §12.5 — global banned names. Series-specific bans live in banned_phrases.json
# at /anpd/v26/series/{series}/banned_phrases.json under the 'names' key.
BANNED_NAMES_GLOBAL: frozenset = frozenset({
    "Sarah",
    "Chen",
    "Marcus",
    "Webb",
})


# §3.2 — character_role must be one of these four values.
CHARACTER_ROLES: frozenset = frozenset({
    "protagonist",
    "antagonist",
    "recurring",
    "supporting",
})

# §5 — voice_specification has these four core sub-fields. dialogue_constraints
# is also defined in §5 but is optional and validated separately when present.
VOICE_SPEC_CORE_FIELDS: frozenset = frozenset({
    "vocabulary_register",
    "sentence_structure_tendencies",
    "signature_expressions",
    "stress_state_shifts",
})

# §5 (universal fields, all roles) plus §6/§7/§8/§9 (role-specific fields).
# Tuples are concatenations: universal first, then role-specific. Order within
# each role is preserved for documentation; iteration treats the tuple as a set.
_UNIVERSAL_REQUIRED: tuple = (
    "name",
    "character_role",
    "primary_trait",
    "secondary_trait",
    "psychological_wound",
    "voice_specification",
    "physical_description",
    "gender",
    # Per Schema §10 — cross-reference fields are required for every character.
    "series_bible_match",
    "relationships",
)

REQUIRED_FIELDS_BY_ROLE: dict = {
    # §6 — protagonist adds:
    "protagonist": _UNIVERSAL_REQUIRED + (
        "defining_image",
        "character_purpose",
        "what_they_want",
        "what_they_will_not_or_cannot_do",
        "plot_flaw_connection",
    ),
    # §7 — antagonist adds:
    "antagonist": _UNIVERSAL_REQUIRED + (
        "defining_image",
        "justification",
        "specific_threat",
        "escalation_capacity",
        "what_they_want",
        "relationship_to_protagonist",
    ),
    # §8 — supporting characters; defining_image OPTIONAL per §5/§8.
    "supporting": _UNIVERSAL_REQUIRED + (
        "narrative_function",
        "relationship_to_protagonist",
    ),
    # §9 — recurring (cross-book continuity); defining_image required.
    "recurring": _UNIVERSAL_REQUIRED + (
        "defining_image",
        "narrative_function",
        "relationship_to_protagonist",
        "recurrence_pattern",
        "what_they_want",
        "what_they_will_not_or_cannot_do",
    ),
}


# ── LLM check configuration ───────────────────────────────────────────────────

# The qualitative check covers five judgment-class concerns in a single Haiku
# call. Each concern is a rubric the model evaluates against the merged cast.
# Mapping from rubric_key → (description, schema_section_reference). Used
# both in the prompt the model sees and in finding emission.

LLM_RUBRIC_CONCERNS: dict = {
    "trait_opposition": (
        "Protagonist and antagonist primary/secondary traits oppose each "
        "other and create genuine inner conflict (per Schema §5).",
        "§5",
    ),
    "defining_image_observable": (
        "defining_image (where present) describes an observable physical "
        "moment — something a camera could capture — not a thought, feeling, "
        "or backstory exposition (per Schema §12.2).",
        "§12.2",
    ),
    "voice_spec_distinctness": (
        "Voice specifications are distinct across characters in the merged "
        "cast — no two characters share identical vocabulary register, "
        "sentence structure, AND signature expressions (per Schema §12.3).",
        "§12.3",
    ),
    "escalation_capacity_nontrivial": (
        "Antagonist's escalation_capacity describes capability beyond the "
        "specific_threat itself — it is not a restatement (per Schema §12.6).",
        "§12.6",
    ),
    "antagonist_moral_complexity": (
        "Antagonist's justification is internally consistent and "
        "understandable from the antagonist's perspective — no cartoon "
        "villains (per Schema §7).",
        "§7",
    ),
}

# Default model, used when effective_config doesn't carry a model identifier.
# Matches synopsis_auditor.call_haiku default; flows through unchanged when
# effective_config provides 'model_character_audit'.
DEFAULT_HAIKU_MODEL = "claude-haiku-4-5"
DEFAULT_HAIKU_MAX_TOKENS = 4000
LLM_API_RETRY_COUNT = 3
LLM_API_RETRY_BACKOFF_SECONDS = (30, 60, 90)


# ── Top-level error class ─────────────────────────────────────────────────────

class CharacterProfileAuditorError(Exception):
    """Raised when the auditor cannot proceed with audit (e.g., file load
    failure, malformed input that prevents any check from running). Distinct
    from in-band findings — findings are produced for problems that the
    auditor *can* identify; this exception is for problems that prevent the
    auditor from identifying anything.

    The caller (Phase 4 master_controller; for now, main()) catches and
    decides routing.
    """
    pass


# ── Deterministic checks ──────────────────────────────────────────────────────

def check_no_envelope_keys(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check that the top-level dict contains no forbidden envelope keys.

    Per Schema v1.1.0 §3.4, character profile files contain only per-character
    entries at the top level. Envelope fields (`series`, `book_number`,
    `title`, `version`, `copyright`, `do_not_appear`, `series_engine`,
    `book_subtext`, `emotional_core`, `characters`) belong in their canonical
    homes (intake, book_config, series_config, series_bible) and must not
    appear in character_profiles.json.

    The forbidden list is sourced from `character_profile_merge.FORBIDDEN_TOP_LEVEL_KEYS`,
    which is the single canonical source for both the merge utility and this
    auditor (§2.1).

    Args:
        profile_data: The parsed character profile dict (top-level).
        file_path: Path to the source file, for inclusion in finding location.
        file_label: 'series-level' or 'book-level', for finding description.

    Returns:
        List of findings — one Class A finding per forbidden key found, or
        empty list if none.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    forbidden_present = sorted(
        key for key in profile_data.keys()
        if key in FORBIDDEN_TOP_LEVEL_KEYS
    )

    for counter, forbidden_key in enumerate(forbidden_present, start=1):
        finding = create_finding(
            finding_id=f"character_profile_envelope_key_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="envelope_key_check",
            class_="A",
            tier="1",
            category="schema_violation",
            location={
                "type": "field_path",
                "file_path": file_path,
                "field_path": forbidden_key,
            },
            description=(
                f"Forbidden envelope key '{forbidden_key}' found at top level of "
                f"{file_label} character profiles file. Per Schema v1.1.0 §3.4, "
                f"character_profiles.json contains only per-character entries; "
                f"envelope fields belong in their canonical homes (intake, "
                f"book_config, series_config, or series_bible)."
            ),
            evidence=None,
            confidence="HIGH",
            fix_action="auto_fix",
            suggested_fix=(
                f"Remove the top-level '{forbidden_key}' field from "
                f"{file_path}. If the field's content is needed elsewhere, "
                f"move it to its canonical home per Schema §3.4."
            ),
            timestamp=timestamp,
        )
        findings.append(finding)

    return findings


def check_value_is_dict(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check that every top-level value in the profile dict is a JSON object.

    Per Schema v1.1.0 §3.1 / §3.2, every top-level entry maps a character
    canonical name (key) to a character profile object (value). A non-object
    value (string, number, list, null) at a top-level key indicates either a
    severely malformed file or accidental flattening. Either way, no
    downstream check can extract character data from a non-object value.

    Findings produced here are Class A. They also short-circuit per-character
    checks (name-key-match, required-fields, etc.) for the same key — those
    checks defensively skip non-dict values to avoid TypeErrors. The
    'value-is-dict' finding is the durable record of the malformation.

    Skips entries whose key is in FORBIDDEN_TOP_LEVEL_KEYS — those are
    already reported by check_no_envelope_keys, no need to double-report.

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path, for finding location.
        file_label: 'series-level' or 'book-level'.

    Returns:
        List of Class A findings, one per non-dict value found.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    bad_keys = sorted(
        key for key, value in profile_data.items()
        if key not in FORBIDDEN_TOP_LEVEL_KEYS
        and not isinstance(value, dict)
    )

    for counter, bad_key in enumerate(bad_keys, start=1):
        actual_type = type(profile_data[bad_key]).__name__
        finding = create_finding(
            finding_id=f"character_profile_value_not_dict_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="value_is_dict_check",
            class_="A",
            tier="1",
            category="schema_violation",
            location={
                "type": "field_path",
                "file_path": file_path,
                "field_path": bad_key,
            },
            description=(
                f"Top-level value at key '{bad_key}' in {file_label} "
                f"character profiles file is not a JSON object — got "
                f"{actual_type}. Per Schema v1.1.0 §3.1/§3.2, every "
                f"top-level entry must be a character profile object."
            ),
            evidence=None,
            confidence="HIGH",
            fix_action="route_to_human",
            suggested_fix=(
                f"Inspect {file_path} at top-level key '{bad_key}'. "
                f"Either replace the {actual_type} value with a complete "
                f"character profile object, or remove the entry entirely "
                f"if the key is spurious."
            ),
            timestamp=timestamp,
        )
        findings.append(finding)

    return findings


def check_name_key_match(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check that each character's top-level dict key equals its 'name' field.

    Per Schema v1.1.0 §3.2, the canonical character name is duplicated at
    two locations: the dict key and the character object's 'name' field.
    The duplication exists for clarity — readers see the name immediately
    when scanning the file — and the auditor enforces consistency.

    Mismatches indicate either a typo, a partial rename (one location updated
    but not the other), or copy-paste corruption.

    Skips entries that fail upstream checks: forbidden envelope keys
    (already reported by check_no_envelope_keys) and non-dict values
    (already reported by check_value_is_dict). Also skips entries whose
    value is a dict but doesn't have a 'name' field — that's a
    required-field violation and will be reported by a future check.

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.

    Returns:
        List of Class A findings, one per mismatch.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if 'name' not in value:
            continue
        if value['name'] == key:
            continue

        counter += 1
        finding = create_finding(
            finding_id=f"character_profile_name_key_mismatch_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="name_key_match_check",
            class_="A",
            tier="1",
            category="schema_violation",
            location={
                "type": "field_path",
                "file_path": file_path,
                "field_path": f"{key}.name",
            },
            description=(
                f"Character entry at top-level key '{key}' in {file_label} "
                f"file has 'name' field set to '{value['name']}'. "
                f"Per Schema v1.1.0 §3.2, the dict key and the character's "
                f"'name' field must match exactly."
            ),
            evidence=f"key={key!r}, name={value['name']!r}",
            confidence="HIGH",
            fix_action="route_to_human",
            suggested_fix=(
                f"Inspect {file_path}. If '{key}' is the canonical name, "
                f"update the character's 'name' field to match. If "
                f"'{value['name']}' is the canonical name, update the "
                f"dict key. Both locations must agree."
            ),
            timestamp=timestamp,
        )
        findings.append(finding)

    return findings


def check_character_role_enum(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check that each character's character_role is in the allowed enum.

    Per Schema v1.1.0 §3.2, character_role is one of:
      - protagonist
      - antagonist
      - recurring
      - supporting

    Defensively skips envelope keys and non-dict values (already reported
    by upstream checks). Skips entries whose value is a dict but doesn't
    have a 'character_role' key — that's a required-field violation
    reported separately by check_required_fields.

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.

    Returns:
        List of Class A findings, one per invalid character_role value.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if 'character_role' not in value:
            continue
        if value['character_role'] in CHARACTER_ROLES:
            continue

        counter += 1
        actual_role = value['character_role']
        finding = create_finding(
            finding_id=f"character_profile_role_invalid_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="character_role_enum_check",
            class_="A",
            tier="1",
            category="schema_violation",
            location={
                "type": "field_path",
                "file_path": file_path,
                "field_path": f"{key}.character_role",
            },
            description=(
                f"Character '{key}' in {file_label} file has "
                f"character_role='{actual_role}'. Per Schema v1.1.0 §3.2, "
                f"character_role must be one of: "
                f"{sorted(CHARACTER_ROLES)}."
            ),
            evidence=f"character_role={actual_role!r}",
            confidence="HIGH",
            fix_action="route_to_human",
            suggested_fix=(
                f"Inspect {file_path} at {key}.character_role. Replace "
                f"'{actual_role}' with one of the four valid roles. The "
                f"correct role depends on the character's narrative function "
                f"in this book/series."
            ),
            timestamp=timestamp,
        )
        findings.append(finding)

    return findings


def check_required_fields(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check that each character has all required fields for its role.

    Per Schema v1.1.0 §5-§9, the required field set varies by character_role.
    REQUIRED_FIELDS_BY_ROLE encodes this mapping. The check produces one
    finding per missing field per character (so a character missing three
    required fields produces three findings).

    Defensively skips upstream-flagged entries: envelope keys, non-dict
    values, missing character_role, invalid character_role.

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.

    Returns:
        List of Class A findings, one per missing required field.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if 'character_role' not in value:
            # character_role itself is required — report it here since we
            # need it to determine which required-field set applies.
            counter += 1
            findings.append(create_finding(
                finding_id=f"character_profile_missing_field_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="required_fields_check",
                class_="A",
                tier="1",
                category="schema_violation",
                location={
                    "type": "field_path",
                    "file_path": file_path,
                    "field_path": f"{key}.character_role",
                },
                description=(
                    f"Character '{key}' in {file_label} file is missing "
                    f"required field 'character_role'. Per Schema v1.1.0 "
                    f"§3.2, every character entry must have character_role."
                ),
                evidence=None,
                confidence="HIGH",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Add 'character_role' field to {key} in {file_path}. "
                    f"Choose one of: {sorted(CHARACTER_ROLES)}."
                ),
                timestamp=timestamp,
            ))
            continue
        role = value['character_role']
        if role not in REQUIRED_FIELDS_BY_ROLE:
            # Invalid role; check_character_role_enum will report it.
            # We can't determine which required-field set applies, so skip.
            continue

        for required_field in REQUIRED_FIELDS_BY_ROLE[role]:
            if required_field in value:
                continue

            counter += 1
            section = {"protagonist": 6, "antagonist": 7, "supporting": 8, "recurring": 9}
            finding = create_finding(
                finding_id=f"character_profile_missing_field_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="required_fields_check",
                class_="A",
                tier="1",
                category="schema_violation",
                location={
                    "type": "field_path",
                    "file_path": file_path,
                    "field_path": f"{key}.{required_field}",
                },
                description=(
                    f"Character '{key}' (role={role}) in {file_label} file "
                    f"is missing required field '{required_field}'. Per "
                    f"Schema v1.1.0 §5-§{section.get(role, '?')}, this "
                    f"field is required for {role} characters."
                ),
                evidence=None,
                confidence="HIGH",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Add the '{required_field}' field to {key} in "
                    f"{file_path}. See Schema §5 (universal fields) and "
                    f"§{section.get(role, '?')} for the {role}-specific "
                    f"requirements."
                ),
                timestamp=timestamp,
            )
            findings.append(finding)

    return findings


def check_voice_specification_core(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check that voice_specification has all four required core sub-fields.

    Per Schema v1.1.0 §5, every character's voice_specification object must
    contain:
      - vocabulary_register
      - sentence_structure_tendencies
      - signature_expressions
      - stress_state_shifts

    The optional dialogue_constraints sub-object is NOT checked here (it's
    explicitly optional per §5 and validated by a separate concern when
    present).

    Defensively skips upstream-flagged entries. Skips characters whose
    voice_specification value isn't a dict — that's a separate type
    violation that required-fields plus an eventual type-shape check
    will surface.

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.

    Returns:
        List of Class A findings, one per missing voice_spec core sub-field.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if 'voice_specification' not in value:
            # required-fields check reports this for all roles since
            # voice_specification is universal. Skip here to avoid
            # double-report.
            continue
        voice_spec = value['voice_specification']
        if not isinstance(voice_spec, dict):
            # Type violation; skip to avoid TypeError on key lookup.
            continue

        for core_field in VOICE_SPEC_CORE_FIELDS:
            if core_field in voice_spec:
                continue

            counter += 1
            finding = create_finding(
                finding_id=f"character_profile_voice_spec_missing_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="voice_specification_core_check",
                class_="A",
                tier="1",
                category="schema_violation",
                location={
                    "type": "field_path",
                    "file_path": file_path,
                    "field_path": f"{key}.voice_specification.{core_field}",
                },
                description=(
                    f"Character '{key}' in {file_label} file has "
                    f"voice_specification missing required core sub-field "
                    f"'{core_field}'. Per Schema v1.1.0 §5, all four core "
                    f"sub-fields ({sorted(VOICE_SPEC_CORE_FIELDS)}) are "
                    f"required."
                ),
                evidence=None,
                confidence="HIGH",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Add '{core_field}' to "
                    f"{key}.voice_specification in {file_path}. See "
                    f"Schema §5 for guidance on what each sub-field "
                    f"contains."
                ),
                timestamp=timestamp,
            )
            findings.append(finding)

    return findings


def check_skills_shape(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check that skills (if present) is an array of non-empty strings.

    Per Schema v1.1.0 §12.4, the skills field is optional. If present, it must
    be an array of plain strings, each non-empty and non-whitespace-only.
    Empty array is valid (means "no relevant skills"). Field omitted entirely
    is valid.

    Defensively skips upstream-flagged entries: envelope keys, non-dict values.
    Skips entries where skills is omitted (legitimate per §5).

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.

    Returns:
        List of Class A findings. One finding per shape violation per
        character — separate findings for "not an array", "contains empty
        string", "contains non-string", etc.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if 'skills' not in value:
            continue

        skills = value['skills']

        # Top-level shape: must be a list.
        if not isinstance(skills, list):
            counter += 1
            findings.append(create_finding(
                finding_id=f"character_profile_skills_not_list_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="skills_shape_check",
                class_="A",
                tier="1",
                category="schema_violation",
                location={
                    "type": "field_path",
                    "file_path": file_path,
                    "field_path": f"{key}.skills",
                },
                description=(
                    f"Character '{key}' in {file_label} file has skills "
                    f"field of type {type(skills).__name__}. Per Schema "
                    f"v1.1.0 §12.4, skills must be an array of strings."
                ),
                evidence=None,
                confidence="HIGH",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Replace skills value at {key}.skills in {file_path} "
                    f"with a JSON array of strings. Use [] if no skills are "
                    f"relevant."
                ),
                timestamp=timestamp,
            ))
            continue

        # Element-by-element validation.
        for index, element in enumerate(skills):
            if not isinstance(element, str):
                counter += 1
                findings.append(create_finding(
                    finding_id=f"character_profile_skills_non_string_{counter:04d}",
                    auditor=AUDITOR_NAME,
                    gate=GATE,
                    pass_name="skills_shape_check",
                    class_="A",
                    tier="1",
                    category="schema_violation",
                    location={
                        "type": "field_path",
                        "file_path": file_path,
                        "field_path": f"{key}.skills[{index}]",
                    },
                    description=(
                        f"Character '{key}' in {file_label} file has "
                        f"skills[{index}] of type {type(element).__name__}, "
                        f"not string. Per Schema v1.1.0 §12.4, every skills "
                        f"entry must be a string."
                    ),
                    evidence=f"element={element!r}",
                    confidence="HIGH",
                    fix_action="route_to_human",
                    suggested_fix=(
                        f"Replace {key}.skills[{index}] in {file_path} with "
                        f"a string description of the skill."
                    ),
                    timestamp=timestamp,
                ))
                continue

            # String element — must be non-empty and non-whitespace.
            if not element.strip():
                counter += 1
                findings.append(create_finding(
                    finding_id=f"character_profile_skills_empty_string_{counter:04d}",
                    auditor=AUDITOR_NAME,
                    gate=GATE,
                    pass_name="skills_shape_check",
                    class_="A",
                    tier="1",
                    category="schema_violation",
                    location={
                        "type": "field_path",
                        "file_path": file_path,
                        "field_path": f"{key}.skills[{index}]",
                    },
                    description=(
                        f"Character '{key}' in {file_label} file has "
                        f"skills[{index}] as empty or whitespace-only string. "
                        f"Per Schema v1.1.0 §12.4, every skills entry must "
                        f"be a non-empty string describing one skill."
                    ),
                    evidence=f"element={element!r}",
                    confidence="HIGH",
                    fix_action="route_to_human",
                    suggested_fix=(
                        f"Either populate {key}.skills[{index}] in {file_path} "
                        f"with a real skill description, or remove the empty "
                        f"entry from the array."
                    ),
                    timestamp=timestamp,
                ))

    return findings


def check_banned_names(
    profile_data: dict,
    file_path: str,
    file_label: str,
    banned_names_extra: List[str] = None,
) -> List[dict]:
    """Check character canonical names against the banned list.

    Per Schema v1.1.0 §12.5, no character canonical name may match the
    global banned list (BANNED_NAMES_GLOBAL: Sarah, Chen, Marcus, Webb)
    or any name in series-specific banned_phrases.json names array
    (passed via banned_names_extra parameter).

    Caller is responsible for loading banned_phrases.json and passing the
    names array. If the file is absent or the names array is empty, caller
    passes None or [] — the check then validates against the global list
    only.

    Defensively skips envelope keys and non-dict values.

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.
        banned_names_extra: Optional list of additional banned names from
            series-specific banned_phrases.json. None or [] means use only
            the global list.

    Returns:
        List of Class A findings, one per banned-name match.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    extra = banned_names_extra or []
    full_banned = set(BANNED_NAMES_GLOBAL) | set(extra)

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue

        # Check the dict key (canonical name).
        if key not in full_banned:
            continue

        source = "global banned list" if key in BANNED_NAMES_GLOBAL else (
            "series-specific banned_phrases.json"
        )

        counter += 1
        findings.append(create_finding(
            finding_id=f"character_profile_banned_name_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="banned_names_check",
            class_="A",
            tier="1",
            category="schema_violation",
            location={
                "type": "field_path",
                "file_path": file_path,
                "field_path": key,
            },
            description=(
                f"Character canonical name '{key}' in {file_label} file "
                f"matches a banned name from the {source}. Per Schema "
                f"v1.1.0 §12.5, banned names must not be used as character "
                f"canonical names."
            ),
            evidence=f"name={key!r}, source={source}",
            confidence="HIGH",
            fix_action="route_to_human",
            suggested_fix=(
                f"Rename character '{key}' in {file_path} to a name not on "
                f"the banned list. Update both the dict key and the "
                f"character's 'name' field. If the character appears in "
                f"other files (relationships, name_registry), update those "
                f"references too."
            ),
            timestamp=timestamp,
        ))

    return findings


def check_name_registry(
    profile_data: dict,
    file_path: str,
    file_label: str,
    name_registry: dict = None,
) -> List[dict]:
    """Check character canonical names match the name registry.

    Per Schema v1.1.0 §12.5, every character canonical name must match the
    canonical form in name_registry.json. Caller is responsible for loading
    name_registry.json and passing it.

    Soft-skip behavior: if name_registry is None (file absent or not
    provided), the check produces no findings. This is intentional — new
    series have no registry until the first book establishes one. Per V20
    reference audit, banned-name and name-registry checks are best-effort
    when their reference files are absent.

    The exact registry shape is the canonical name → variants mapping from
    name_registry.json. The check verifies each character's canonical name
    appears as a top-level key in the registry. Variants stored under a
    different canonical name (e.g., "Joe" appearing under "Joseph") would
    flag the character as not matching canonical form.

    Defensively skips envelope keys and non-dict values.

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.
        name_registry: Optional dict loaded from name_registry.json. None
            triggers soft-skip behavior — no findings produced.

    Returns:
        List of Class B findings (not Class A — name-registry mismatches
        are reportable but not gate-blocking; the registry may be
        out-of-date or missing canonical entries that the auditor cannot
        distinguish from real violations). One finding per name not found.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    if name_registry is None:
        return findings  # Soft-skip when registry absent.

    if not isinstance(name_registry, dict):
        # Registry is malformed; can't meaningfully check. One finding for
        # the registry shape, no per-character checks.
        return [create_finding(
            finding_id="character_profile_name_registry_malformed_0001",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="name_registry_check",
            class_="B",
            tier="2",
            category="schema_violation",
            location={
                "type": "whole_artifact",
                "file_path": "name_registry.json",
            },
            description=(
                f"name_registry.json content is not a JSON object — got "
                f"{type(name_registry).__name__}. Cannot validate "
                f"character names against the registry."
            ),
            evidence=None,
            confidence="HIGH",
            fix_action="route_to_human",
            suggested_fix=(
                "Inspect name_registry.json. The top-level value must be a "
                "JSON object mapping canonical names to variant arrays."
            ),
            timestamp=timestamp,
        )]

    canonical_names = set(name_registry.keys())

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if key in canonical_names:
            continue

        counter += 1
        findings.append(create_finding(
            finding_id=f"character_profile_name_registry_mismatch_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="name_registry_check",
            class_="B",
            tier="2",
            category="schema_violation",
            location={
                "type": "field_path",
                "file_path": file_path,
                "field_path": key,
            },
            description=(
                f"Character canonical name '{key}' in {file_label} file does "
                f"not appear as a top-level key in name_registry.json. Per "
                f"Schema v1.1.0 §12.5, character names must match canonical "
                f"form in the registry."
            ),
            evidence=f"name={key!r}",
            confidence="MEDIUM",
            fix_action="route_to_human",
            suggested_fix=(
                f"Either add '{key}' as a canonical entry in name_registry.json, "
                f"or rename the character in {file_path} to match an existing "
                f"canonical name. If '{key}' is a variant of an existing "
                f"canonical name, the character profile should use the "
                f"canonical form."
            ),
            timestamp=timestamp,
        ))

    return findings


def check_trait_distinctness(
    profile_data: dict,
    file_path: str,
    file_label: str,
) -> List[dict]:
    """Check primary_trait != secondary_trait for every character.

    Per Schema v1.1.0 §5, §8, §9: every character has two traits, and the
    two traits must be distinct. The opposition requirement (which applies
    to protagonist and antagonist per §5) is judgment-class and is checked
    by the LLM qualitative pass; this check only verifies the deterministic
    requirement that the two trait strings are not identical.

    Comparison is case-insensitive and whitespace-insensitive: traits that
    differ only in capitalization or surrounding whitespace are still
    considered the same. Genuinely-distinct traits ("calm under pressure"
    vs "deeply loyal") pass; trivially-non-distinct traits ("brave" vs
    "Brave" or "brave " vs "brave") fail.

    Defensively skips upstream-flagged entries: envelope keys, non-dict
    values, characters missing primary_trait or secondary_trait (those
    failures are reported by check_required_fields).

    Args:
        profile_data: Top-level character profile dict.
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.

    Returns:
        List of Class A findings, one per character whose two traits are
        not distinct.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if 'primary_trait' not in value or 'secondary_trait' not in value:
            continue

        primary = value['primary_trait']
        secondary = value['secondary_trait']

        # Both must be strings to compare. If either isn't, skip — that's a
        # type violation a future shape check may report.
        if not isinstance(primary, str) or not isinstance(secondary, str):
            continue

        if primary.strip().lower() != secondary.strip().lower():
            continue

        counter += 1
        findings.append(create_finding(
            finding_id=f"character_profile_traits_not_distinct_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="trait_distinctness_check",
            class_="A",
            tier="1",
            category="schema_violation",
            location={
                "type": "field_path",
                "file_path": file_path,
                "field_path": f"{key}.secondary_trait",
            },
            description=(
                f"Character '{key}' in {file_label} file has identical "
                f"primary_trait and secondary_trait (case-insensitive, "
                f"whitespace-insensitive comparison). Per Schema v1.1.0 §5, "
                f"§8, and §9, every character has two distinct traits."
            ),
            evidence=(
                f"primary_trait={primary!r}, secondary_trait={secondary!r}"
            ),
            confidence="HIGH",
            fix_action="route_to_human",
            suggested_fix=(
                f"Replace {key}.secondary_trait in {file_path} with a trait "
                f"that is genuinely distinct from primary_trait. For "
                f"protagonist and antagonist roles, per §5, the secondary "
                f"trait should also oppose the primary to create inner "
                f"conflict (this is judgment-class and reported separately)."
            ),
            timestamp=timestamp,
        ))

    return findings


def check_relationship_symmetry(
    merged_cast: dict,
    series_file_path: str,
    book_file_path: str,
) -> List[dict]:
    """Check that relationships are symmetric across the merged cast.

    Per Schema v1.1.0 §10 and §12.6: if character A's profile lists
    character B in its relationships object, then character B's profile
    must list character A in its relationships object. The relationship
    descriptions need not match (A may describe B as 'estranged brother'
    while B describes A as 'younger sister' — both reference each other,
    that's symmetry). The check verifies presence of the back-reference,
    not content equality.

    Operates on the merged cast (both files combined). Cross-file: a
    character in the series-level file referencing a book-level character
    must have a back-reference; same in the other direction.

    Defensively skips:
    - Characters not in the merged cast (their absence is reported by
      file-loading errors before this check runs).
    - Relationships that point to characters not in the merged cast
      (those are reported as a separate dangling-reference finding —
      not implemented here).

    Args:
        merged_cast: The result of merge_character_profiles(series, book).
            All character names from both files, name-keyed.
        series_file_path: Path to series-level profile file (for finding
            location when the asymmetric reference originates there).
        book_file_path: Path to book-specific profile file (same).

    Returns:
        List of Class A findings, one per asymmetric reference. If A
        references B but B doesn't reference A, one finding is produced
        (located at A's relationships entry — that's where the unilateral
        reference exists). The reverse asymmetry produces its own finding
        when iteration reaches B.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    counter = 0
    for char_a, profile_a in merged_cast.items():
        if not isinstance(profile_a, dict):
            continue
        relationships_a = profile_a.get('relationships', {})
        if not isinstance(relationships_a, dict):
            continue

        for char_b in relationships_a.keys():
            # Look up B in the merged cast.
            profile_b = merged_cast.get(char_b)
            if not isinstance(profile_b, dict):
                # B not in cast or not a valid profile — skip; dangling
                # reference is a separate concern.
                continue

            relationships_b = profile_b.get('relationships', {})
            if not isinstance(relationships_b, dict):
                # B has no relationships object or it's malformed — that's
                # a required-field violation reported elsewhere.
                continue

            if char_a in relationships_b:
                # Symmetric — both reference each other.
                continue

            # Asymmetric: A references B, B does not reference A.
            counter += 1
            findings.append(create_finding(
                finding_id=f"character_profile_relationship_asymmetric_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="relationship_symmetry_check",
                class_="A",
                tier="1",
                category="schema_violation",
                location={
                    "type": "field_path",
                    "file_path": book_file_path,
                    "field_path": f"{char_a}.relationships.{char_b}",
                },
                description=(
                    f"Asymmetric relationship: '{char_a}' references "
                    f"'{char_b}' in their relationships object, but "
                    f"'{char_b}' does not reference '{char_a}' back. Per "
                    f"Schema v1.1.0 §10 and §12.6, relationships must be "
                    f"symmetric across the merged cast."
                ),
                evidence=(
                    f"{char_a}.relationships keys={list(relationships_a.keys())}, "
                    f"{char_b}.relationships keys={list(relationships_b.keys())}"
                ),
                confidence="HIGH",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Either add '{char_a}' to {char_b}.relationships with "
                    f"an appropriate description, or remove '{char_b}' "
                    f"from {char_a}.relationships. Both characters' files "
                    f"should agree on whether they have a relationship."
                ),
                timestamp=timestamp,
            ))

    return findings


def check_shared_first_letter(
    merged_cast: dict,
    series_file_path: str,
    book_file_path: str,
) -> List[dict]:
    """Check for characters sharing the same first letter (WARN, not FAIL).

    Per Schema v1.1.0 §12.5: the V20 character_auditor's shared-first-letter
    check is RELAXED from FAIL (Class A) to WARN (Class C, Tier 3). Reader
    confusion from same-letter names is a real concern, but international
    casts (Cambodian, Filipino, Khmer) routinely produce names that share
    first letters yet are clearly distinct. The check still flags the
    situation; it does not block.

    Operates on the merged cast. Compares only character canonical names;
    aliases are not considered (a character whose canonical name is
    "Sokha" with alias "S." is fine — the alias isn't a separate name).

    First-letter comparison is case-insensitive. Single-character names
    (rare in practice) compare as their full content.

    One finding per group of 2+ characters sharing a first letter, not one
    per pair. Description lists the affected characters.

    Args:
        merged_cast: Result of merge_character_profiles(series, book).
        series_file_path: For location reference.
        book_file_path: For location reference.

    Returns:
        List of Class C findings, one per first-letter group with 2+
        characters.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Group character names by first letter (case-insensitive).
    by_first_letter: dict = {}
    for name in merged_cast.keys():
        if not isinstance(name, str) or not name:
            continue
        first = name[0].upper()
        by_first_letter.setdefault(first, []).append(name)

    counter = 0
    for first_letter, names in sorted(by_first_letter.items()):
        if len(names) < 2:
            continue

        counter += 1
        findings.append(create_finding(
            finding_id=f"character_profile_shared_first_letter_{counter:04d}",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="shared_first_letter_check",
            class_="C",
            tier="3",
            category="readability_concern",
            location={
                "type": "whole_artifact",
                "file_path": book_file_path,
            },
            description=(
                f"Multiple characters share first letter '{first_letter}': "
                f"{sorted(names)}. Per Schema v1.1.0 §12.5, this can cause "
                f"reader confusion but is not a blocking issue. Reviewer "
                f"should confirm names are distinct enough in context."
            ),
            evidence=f"first_letter={first_letter!r}, names={sorted(names)}",
            confidence="LOW",
            fix_action="route_to_human",
            suggested_fix=(
                f"Review whether characters {sorted(names)} are sufficiently "
                f"distinct in dialogue and narration. International casts "
                f"often produce same-first-letter names that work fine in "
                f"context (e.g., Cambodian 'Sokha' and 'Sothy'). If "
                f"confusion is likely, consider renaming one character."
            ),
            timestamp=timestamp,
        ))

    return findings


def check_series_bible_match(
    profile_data: dict,
    file_path: str,
    file_label: str,
    series_bible: dict = None,
) -> List[dict]:
    """Check that series_bible_match: true characters align with series_bible.json.

    Per Schema v1.1.0 §10 and §12.6: characters with `series_bible_match: true`
    (typically protagonist and recurring characters) must have field content
    consistent with their corresponding entry in series_bible.json. The
    check verifies high-level alignment of fields that the bible also
    defines: name, character_role, traits, wound, defining_image. Field
    content is compared as strings; the check does not perform semantic
    equivalence (that's judgment-class).

    Soft-skip behavior: if series_bible is None (file absent or not provided),
    the check produces no findings. New series may not have a series_bible
    until series-bible authoring lands.

    The exact bible shape per series_bible.json convention has top-level
    keys for `protagonist` and `recurring_characters`. Protagonist match is
    looked up by character name; recurring match is looked up by the
    canonical name in the recurring_characters list.

    Defensively skips:
    - Characters with series_bible_match: false (book-specific antagonists,
      supporting; correctly not in the bible).
    - Characters missing series_bible_match field (required-fields reports).
    - Envelope keys and non-dict values.

    Args:
        profile_data: Top-level character profile dict (series-level OR
            book-level — the check applies to either, but typically only
            series-level characters have series_bible_match: true).
        file_path: Source file path.
        file_label: 'series-level' or 'book-level'.
        series_bible: Optional dict loaded from series_bible.json. None
            triggers soft-skip.

    Returns:
        List of Class B findings (not Class A — bible drift can be
        legitimate during active development; a strict block would impede
        iteration). One finding per significant field mismatch.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    if series_bible is None:
        return findings  # Soft-skip when bible absent.

    if not isinstance(series_bible, dict):
        return findings  # Malformed; can't meaningfully cross-reference.

    # Build a lookup of bible entries by character name.
    # The protagonist lives at series_bible['protagonist'] (the character's
    # canonical name is in series_bible['protagonist']['name']).
    # Recurring characters live at series_bible['recurring_characters'][i]['name'].
    bible_lookup: dict = {}
    protagonist_entry = series_bible.get('protagonist')
    if isinstance(protagonist_entry, dict):
        prot_name = protagonist_entry.get('name')
        if isinstance(prot_name, str):
            bible_lookup[prot_name] = protagonist_entry
    recurring_list = series_bible.get('recurring_characters', [])
    if isinstance(recurring_list, list):
        for entry in recurring_list:
            if not isinstance(entry, dict):
                continue
            rec_name = entry.get('name')
            if isinstance(rec_name, str):
                bible_lookup[rec_name] = entry

    # Fields to compare. Bible may use different field names; this is the
    # intersection of fields the bible commonly defines and the profile
    # schema requires. See Schema §12.6.
    COMPARABLE_FIELDS = ['character_role', 'primary_trait', 'secondary_trait',
                         'psychological_wound', 'defining_image']

    counter = 0
    for key, value in profile_data.items():
        if key in FORBIDDEN_TOP_LEVEL_KEYS:
            continue
        if not isinstance(value, dict):
            continue
        if value.get('series_bible_match') is not True:
            continue

        bible_entry = bible_lookup.get(key)
        if bible_entry is None:
            counter += 1
            findings.append(create_finding(
                finding_id=f"character_profile_bible_no_entry_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="series_bible_match_check",
                class_="B",
                tier="2",
                category="schema_violation",
                location={
                    "type": "field_path",
                    "file_path": file_path,
                    "field_path": f"{key}.series_bible_match",
                },
                description=(
                    f"Character '{key}' in {file_label} file has "
                    f"series_bible_match: true but no matching entry exists "
                    f"in series_bible.json (neither as protagonist nor in "
                    f"recurring_characters)."
                ),
                evidence=f"name={key!r}",
                confidence="HIGH",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Either add an entry for '{key}' in series_bible.json "
                    f"(under protagonist or recurring_characters as "
                    f"appropriate), or set series_bible_match: false in "
                    f"{file_path}. The flag should reflect actual bible "
                    f"presence."
                ),
                timestamp=timestamp,
            ))
            continue

        # Field-by-field content comparison. Mismatches produce one finding
        # each.
        for field in COMPARABLE_FIELDS:
            profile_value = value.get(field)
            bible_value = bible_entry.get(field)
            if profile_value is None or bible_value is None:
                continue
            if not isinstance(profile_value, str) or not isinstance(bible_value, str):
                continue
            if profile_value.strip() == bible_value.strip():
                continue

            counter += 1
            findings.append(create_finding(
                finding_id=f"character_profile_bible_field_mismatch_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="series_bible_match_check",
                class_="B",
                tier="2",
                category="schema_violation",
                location={
                    "type": "field_path",
                    "file_path": file_path,
                    "field_path": f"{key}.{field}",
                },
                description=(
                    f"Character '{key}' field '{field}' in {file_label} "
                    f"file does not match series_bible.json. Per Schema "
                    f"v1.1.0 §12.6, characters with series_bible_match: "
                    f"true must have field content consistent with their "
                    f"bible entry."
                ),
                evidence=(
                    f"profile={profile_value!r}, bible={bible_value!r}"
                ),
                confidence="MEDIUM",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Reconcile {key}.{field} between {file_path} and "
                    f"series_bible.json. Determine which is canonical and "
                    f"update the other to match."
                ),
                timestamp=timestamp,
            ))

    return findings


def _call_haiku_for_audit(
    user_prompt: str,
    system_prompt: str,
    model: str = DEFAULT_HAIKU_MODEL,
    max_tokens: int = DEFAULT_HAIKU_MAX_TOKENS,
) -> str:
    """Call Anthropic API via llm_client with retry on transient errors.

    Mirrors synopsis_generator.call_api's 3-retry pattern with exponential
    backoff. Distinct from synopsis_auditor.call_haiku (which has no retry)
    because the character profile auditor runs as a pre-generation gate —
    transient API failures should retry rather than fail the gate run.

    Args:
        user_prompt: User message content.
        system_prompt: System prompt content.
        model: Model identifier (e.g., 'claude-haiku-4-5').
        max_tokens: Response token cap.

    Returns:
        Response text content.

    Raises:
        CharacterProfileAuditorError: After LLM_API_RETRY_COUNT transient
            failures, or on first non-transient error.
    """
    import time
    from llm_client import call_llm

    last_error = None
    for attempt in range(LLM_API_RETRY_COUNT):
        try:
            response = call_llm(
                provider="anthropic",
                model=model,
                system=system_prompt,
                user=user_prompt,
                max_tokens=max_tokens,
            )
            return response.text
        except Exception as e:
            error_str = str(e).lower()
            transient_markers = ["rate_limit", "429", "529", "overloaded",
                                 "timeout", "connect", "502", "503", "504",
                                 "connection reset", "broken pipe"]
            if any(t in error_str for t in transient_markers):
                last_error = e
                if attempt < LLM_API_RETRY_COUNT - 1:
                    backoff = LLM_API_RETRY_BACKOFF_SECONDS[attempt]
                    time.sleep(backoff)
                    continue
            # Non-transient — raise immediately.
            raise CharacterProfileAuditorError(
                f"Haiku API non-transient error: {e}"
            ) from e

    # Exhausted retries.
    raise CharacterProfileAuditorError(
        f"Haiku API exhausted {LLM_API_RETRY_COUNT} retries: {last_error}"
    ) from last_error


def _build_haiku_prompt(merged_cast: dict) -> tuple:
    """Build the system prompt and user prompt for the qualitative check.

    Returns (system_prompt, user_prompt) tuple. The model is asked to
    evaluate the merged cast against the five LLM_RUBRIC_CONCERNS and
    return a JSON object with one entry per concern. Each entry is either
    null (no issue found) or a list of finding objects.

    Output schema the model is instructed to produce:
      {
        "trait_opposition": null | [
          {"character_name": str, "explanation": str}, ...
        ],
        "defining_image_observable": null | [...],
        "voice_spec_distinctness": null | [...],
        "escalation_capacity_nontrivial": null | [...],
        "antagonist_moral_complexity": null | [...]
      }

    Each finding object has 'character_name' (the canonical name from the
    merged cast) and 'explanation' (one-sentence rationale).
    """
    system_prompt = (
        "You are a character profile auditor for novel production. You "
        "evaluate character profiles against five qualitative rubrics and "
        "return findings as JSON. You return ONLY the JSON object, with no "
        "preamble, explanation, or markdown fencing."
    )

    rubric_lines = []
    for key, (description, _section) in LLM_RUBRIC_CONCERNS.items():
        rubric_lines.append(f"- {key}: {description}")
    rubric_text = "\n".join(rubric_lines)

    user_prompt = f"""Evaluate the following character profiles against five rubrics. For each rubric, return either null (no issues found) or a list of finding objects identifying specific characters with issues.

RUBRICS:
{rubric_text}

CHARACTER PROFILES (merged cast):
```json
{json.dumps(merged_cast, indent=2)}
```

Return a JSON object with exactly these five keys, each with value null or a list:

{{
  "trait_opposition": null | [{{"character_name": "...", "explanation": "..."}}, ...],
  "defining_image_observable": null | [...],
  "voice_spec_distinctness": null | [...],
  "escalation_capacity_nontrivial": null | [...],
  "antagonist_moral_complexity": null | [...]
}}

Each finding's "character_name" must match a key from the character profiles above. Each "explanation" is one sentence stating the specific issue.

Return ONLY the JSON object. No preamble. No markdown fencing."""

    return system_prompt, user_prompt


def _parse_haiku_response(response_text: str) -> dict:
    """Parse Haiku's JSON response. Strict — raises on malformed output.

    Strips common LLM artifacts before parsing: leading/trailing whitespace,
    markdown code fences (```json ... ```), preamble like "Here is the
    JSON:".

    Raises:
        ValueError: If the response can't be parsed as a JSON object with
            the expected five-key structure.
    """
    text = response_text.strip()

    # Strip markdown code fences if present.
    if text.startswith("```"):
        # Find closing fence
        lines = text.split("\n")
        if len(lines) >= 3 and lines[-1].strip() == "```":
            # Drop first line (```json or ```) and last line (```)
            text = "\n".join(lines[1:-1])

    # Parse JSON.
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Haiku response is not valid JSON: {e}. "
            f"First 500 chars: {text[:500]!r}"
        ) from e

    if not isinstance(data, dict):
        raise ValueError(
            f"Haiku response top-level value is not an object — "
            f"got {type(data).__name__}"
        )

    # Verify all five rubric keys present.
    missing = [k for k in LLM_RUBRIC_CONCERNS.keys() if k not in data]
    if missing:
        raise ValueError(
            f"Haiku response missing required rubric keys: {missing}"
        )

    return data


def check_haiku_qualitative(
    merged_cast: dict,
    effective_config: dict,
    series_file_path: str,
    book_file_path: str,
) -> List[dict]:
    """Qualitative LLM check covering five judgment-class concerns.

    One Haiku API call evaluates the merged cast against five rubrics
    (LLM_RUBRIC_CONCERNS):
      - Trait opposition (protagonist/antagonist)
      - defining_image is observable physical
      - Voice spec distinctness across cast
      - escalation_capacity non-trivial
      - Antagonist moral complexity

    Per V20 reference audit, this fixes V20 character_auditor's
    silent-pass-on-parse-failure bug. If the response can't be parsed as
    valid JSON with the expected structure, this function tries one retry
    with a stricter instruction; if the retry also fails to parse, it
    raises CharacterProfileAuditorError. No silent pass.

    Soft-skip behavior: if effective_config doesn't carry
    'model_character_audit' (e.g., test environments without genre
    template loaded), the check returns no findings rather than failing.
    Logs the soft-skip as a single Class C informational finding so the
    skip is auditable.

    Per Schema §3.3 and consistent with deterministic checks: this check
    operates on the merged cast (both files combined). Findings cite the
    appropriate file_path based on whether the named character originates
    series-level or book-level.

    Args:
        merged_cast: Result of merge_character_profiles(series, book).
        effective_config: Loaded effective_config dict; required parameter.
            'model_character_audit' key is read; missing key triggers
            soft-skip with a Class C informational finding.
        series_file_path: Series-level profile path (for finding location).
        book_file_path: Book-level profile path (for finding location).

    Returns:
        List of findings. Class A for issues identified by the model
        against the four core rubrics; Class B for moral complexity (more
        subjective, lower-confidence findings); Class C for the soft-skip
        case if model identifier is missing.

    Raises:
        CharacterProfileAuditorError: If the API call fails after retries
            or if the response cannot be parsed after one retry.
    """
    findings = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Soft-skip if model identifier absent.
    model = effective_config.get('model_character_audit')
    if not model:
        return [create_finding(
            finding_id="character_profile_haiku_skipped_0001",
            auditor=AUDITOR_NAME,
            gate=GATE,
            pass_name="haiku_qualitative_check",
            class_="C",
            tier="3",
            category="audit_coverage_gap",
            location={
                "type": "whole_artifact",
                "file_path": book_file_path,
            },
            description=(
                "Haiku qualitative check skipped: effective_config does "
                "not carry 'model_character_audit'. Genre template may not "
                "be loaded. The deterministic checks ran normally; only "
                "the LLM-based judgment-class checks were skipped."
            ),
            evidence=None,
            confidence="HIGH",
            fix_action="route_to_human",
            suggested_fix=(
                "Ensure series_config.json references a genre_template "
                "that defines 'model_character_audit', or set the key "
                "directly in series_config.structural_overrides."
            ),
            timestamp=timestamp,
        )]

    # Build prompt and call Haiku. _call_haiku_for_audit handles transient
    # retry; CharacterProfileAuditorError propagates to caller.
    system_prompt, user_prompt = _build_haiku_prompt(merged_cast)

    response_text = _call_haiku_for_audit(
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        model=model,
    )

    # Parse — one retry with stricter instruction if first parse fails.
    try:
        parsed = _parse_haiku_response(response_text)
    except ValueError:
        # Retry once with a stricter instruction prepended.
        retry_user_prompt = (
            "Your previous response was not valid JSON. Return ONLY the "
            "JSON object — no preamble, no markdown fencing, no "
            "explanation outside the JSON.\n\n" + user_prompt
        )
        retry_response = _call_haiku_for_audit(
            user_prompt=retry_user_prompt,
            system_prompt=system_prompt,
            model=model,
        )
        try:
            parsed = _parse_haiku_response(retry_response)
        except ValueError as e:
            raise CharacterProfileAuditorError(
                f"Haiku qualitative check: response unparseable after "
                f"retry. {e}"
            ) from e

    # Convert parsed concerns into V24 findings.
    counter = 0
    for rubric_key, rubric_value in parsed.items():
        if rubric_value is None:
            continue
        if not isinstance(rubric_value, list):
            # Malformed concern — should be null or list. Skip with caution.
            continue

        # Antagonist moral complexity findings are Class B (lower confidence
        # than the structural concerns). Others are Class A.
        finding_class = "B" if rubric_key == "antagonist_moral_complexity" else "A"
        finding_tier = "2" if finding_class == "B" else "1"

        rubric_description, schema_section = LLM_RUBRIC_CONCERNS[rubric_key]

        for issue in rubric_value:
            if not isinstance(issue, dict):
                continue
            char_name = issue.get('character_name')
            explanation = issue.get('explanation')
            if not isinstance(char_name, str) or not isinstance(explanation, str):
                continue

            counter += 1
            findings.append(create_finding(
                finding_id=f"character_profile_haiku_{rubric_key}_{counter:04d}",
                auditor=AUDITOR_NAME,
                gate=GATE,
                pass_name="haiku_qualitative_check",
                class_=finding_class,
                tier=finding_tier,
                category="judgment_class_concern",
                location={
                    "type": "field_path",
                    "file_path": book_file_path,
                    "field_path": char_name,
                },
                description=(
                    f"[{rubric_key}] {explanation} "
                    f"(Per Schema {schema_section}: {rubric_description})"
                ),
                evidence=f"character_name={char_name!r}",
                confidence="MEDIUM",
                fix_action="route_to_human",
                suggested_fix=(
                    f"Review character '{char_name}' and the "
                    f"{rubric_key} rubric. Operator judgment required: "
                    f"the LLM has flagged this as a potential issue but "
                    f"qualitative concerns benefit from human review."
                ),
                timestamp=timestamp,
            ))

    return findings


# ── File loading ──────────────────────────────────────────────────────────────

def _load_profile_file(path: Path, label: str) -> dict:
    """Load a character profile JSON file with auditor-appropriate error
    handling.

    Distinct from character_profile_merge._load_profile_file because the
    auditor's response to load failure is to raise CharacterProfileAuditorError
    (caller decides routing), not ProfileMergeError. Both functions read JSON
    and validate top-level-is-an-object, but they differ in what they raise.
    The auditor also does NOT short-circuit on envelope keys — it loads the
    file and lets the envelope-key check produce findings on the data.
    """
    if not path.exists():
        raise CharacterProfileAuditorError(
            f"Character profile file not found ({label}): {path}"
        )

    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise CharacterProfileAuditorError(
            f"Character profile file is not valid JSON ({label}): {path} — "
            f"line {e.lineno} column {e.colno}: {e.msg}"
        ) from e

    if not isinstance(data, dict):
        raise CharacterProfileAuditorError(
            f"Character profile file top-level value must be a JSON object "
            f"({label}): {path} — got {type(data).__name__}"
        )

    # Normalize: file may use {"characters": [list of dicts]} envelope
    # (Schema v1.1.0 §3.4 alternate representation). Convert to
    # {name: profile_dict} so downstream checks operate on per-character
    # entries. This is NOT a bypass — envelope-key and all other checks
    # still run against the normalized data.
    if ('characters' in data
            and isinstance(data['characters'], list)):
        normalized = {
            p['name']: p for p in data['characters']
            if isinstance(p, dict) and 'name' in p
        }
        return normalized

    return data


# ── Top-level audit orchestration ─────────────────────────────────────────────

def audit_character_profiles(
    series_profiles_path: Path,
    book_profiles_path: Path,
    effective_config: dict,
) -> List[dict]:
    """Run all character profile checks and return aggregated findings.

    Loads both profile files, runs each check function, accumulates findings,
    and returns the list. No I/O beyond file reads — no STOP_REPORT writing,
    no report formatting, no exit-code semantics. Caller decides what to do
    with findings.

    Args:
        series_profiles_path: Path to series-level character_profiles.json.
        book_profiles_path: Path to book-specific character_profiles.json.
        effective_config: Loaded effective_config dict from
            resolve_config(). Required parameter; no None fallback.
            Per the discipline established in synopsis_auditor commits
            e598a6b and 509b94c.

    Returns:
        List of finding dicts conforming to the V24 finding schema (per
        findings.py / White Paper §3.8). Empty list if no findings.

    Raises:
        CharacterProfileAuditorError: If files cannot be loaded or are not
            structurally parseable as objects. Schema-level violations
            (envelope keys, missing required fields, etc.) produce findings
            instead of exceptions.
    """
    series_data = _load_profile_file(series_profiles_path, "series-level")
    book_data = _load_profile_file(book_profiles_path, "book-level")

    findings: List[dict] = []

    # Schema §3.4 envelope-key check (per file, both files).
    findings.extend(check_no_envelope_keys(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_no_envelope_keys(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Schema §3.1/§3.2 value-is-dict check (per file).
    findings.extend(check_value_is_dict(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_value_is_dict(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Schema §3.2 name-key match check (per file).
    findings.extend(check_name_key_match(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_name_key_match(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Schema §3.2 character_role enum check (per file).
    findings.extend(check_character_role_enum(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_character_role_enum(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Schema §5–§9 required-field completeness check (per file).
    findings.extend(check_required_fields(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_required_fields(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Schema §5 voice_specification core fields check (per file).
    findings.extend(check_voice_specification_core(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_voice_specification_core(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Schema §12.4 skills shape check (per file).
    findings.extend(check_skills_shape(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_skills_shape(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Schema §12.5 banned names check (per file).
    # Load series-specific banned names from banned_phrases.json if present.
    # Soft-skip on absence — series may not have a banned_phrases.json yet.
    banned_names_extra: List[str] = []
    series_dir = series_profiles_path.parent
    banned_phrases_path = series_dir / "banned_phrases.json"
    if banned_phrases_path.exists():
        try:
            with banned_phrases_path.open(encoding="utf-8") as f:
                banned_phrases_data = json.load(f)
            if isinstance(banned_phrases_data, dict):
                names = banned_phrases_data.get("names", [])
                if isinstance(names, list):
                    banned_names_extra = [n for n in names if isinstance(n, str)]
        except (json.JSONDecodeError, OSError):
            # Malformed or unreadable; treat as absent.
            pass

    findings.extend(check_banned_names(
        series_data,
        str(series_profiles_path),
        "series-level",
        banned_names_extra,
    ))
    findings.extend(check_banned_names(
        book_data,
        str(book_profiles_path),
        "book-level",
        banned_names_extra,
    ))

    # Schema §12.5 name registry check (per file).
    # Load name_registry.json from the series directory if present.
    # Soft-skip on absence — new series have no registry until first book.
    name_registry: dict = None
    name_registry_path = series_dir / "name_registry.json"
    if name_registry_path.exists():
        try:
            with name_registry_path.open(encoding="utf-8") as f:
                name_registry = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Malformed or unreadable; treat as absent (soft-skip).
            name_registry = None

    findings.extend(check_name_registry(
        series_data,
        str(series_profiles_path),
        "series-level",
        name_registry,
    ))
    findings.extend(check_name_registry(
        book_data,
        str(book_profiles_path),
        "book-level",
        name_registry,
    ))

    # Schema §5/§8/§9 trait distinctness check (per file).
    findings.extend(check_trait_distinctness(
        series_data,
        str(series_profiles_path),
        "series-level",
    ))
    findings.extend(check_trait_distinctness(
        book_data,
        str(book_profiles_path),
        "book-level",
    ))

    # Build merged cast for cross-file checks.
    # If merge fails (envelope keys, name collision), per-file checks
    # already produced findings; skip merged-cast checks defensively.
    merged_cast: dict = None
    try:
        merged_cast = merge_character_profiles(
            series_profiles_path, book_profiles_path,
        )
    except ProfileMergeError:
        merged_cast = None

    if merged_cast is not None:
        # Schema §10/§12.6 relationship symmetry (merged cast).
        findings.extend(check_relationship_symmetry(
            merged_cast,
            str(series_profiles_path),
            str(book_profiles_path),
        ))

        # Schema §12.5 shared-first-letter WARN (merged cast).
        findings.extend(check_shared_first_letter(
            merged_cast,
            str(series_profiles_path),
            str(book_profiles_path),
        ))

    # Schema §10/§12.6 series_bible_match alignment (per file).
    # Load series_bible.json from the series directory if present.
    # Soft-skip on absence — series may not have a bible authored yet.
    series_bible: dict = None
    series_bible_path = series_dir / "series_bible.json"
    if series_bible_path.exists():
        try:
            with series_bible_path.open(encoding="utf-8") as f:
                series_bible = json.load(f)
        except (json.JSONDecodeError, OSError):
            series_bible = None

    findings.extend(check_series_bible_match(
        series_data,
        str(series_profiles_path),
        "series-level",
        series_bible,
    ))
    findings.extend(check_series_bible_match(
        book_data,
        str(book_profiles_path),
        "book-level",
        series_bible,
    ))

    # Schema §12.2/§12.3/§12.6/§7 Haiku LLM qualitative check (merged cast).
    # Single API call covers five judgment-class concerns. Soft-skip if
    # model_character_audit absent from config_resolver.
    if merged_cast is not None:
        findings.extend(check_haiku_qualitative(
            merged_cast,
            effective_config,
            str(series_profiles_path),
            str(book_profiles_path),
        ))

    return findings


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ANPD V24 character profile auditor (Gate 2). "
            "Validates series-level and book-specific character_profiles.json "
            "against Schema v1.1.0 and emits V24 findings."
        ),
    )
    parser.add_argument(
        '--series-config',
        type=Path,
        required=True,
        help='Path to series_config.json (drives effective_config resolution).',
    )
    parser.add_argument(
        '--series-profiles',
        type=Path,
        required=True,
        help='Path to series-level character_profiles.json.',
    )
    parser.add_argument(
        '--book-profiles',
        type=Path,
        required=True,
        help='Path to book-specific character_profiles.json.',
    )
    args = parser.parse_args()

    # Load effective_config — required, no None fallback per the synopsis_auditor
    # discipline from commit 509b94c.
    try:
        effective_config = resolve_config(args.series_config)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        print(
            f"  FATAL: Failed to load effective config: {e}",
            file=sys.stderr,
        )
        return 1

    # Run audit. Findings come back as a list; CLI mode prints summary.
    try:
        findings = audit_character_profiles(
            args.series_profiles,
            args.book_profiles,
            effective_config,
        )
    except CharacterProfileAuditorError as e:
        print(
            f"  FATAL: Auditor cannot proceed: {e}",
            file=sys.stderr,
        )
        return 1

    # Brief stdout summary. Full finding emission to a structured output is
    # the master_controller's concern (Phase 4); CLI mode is for direct
    # inspection.
    if not findings:
        print(f"\n{'='*70}")
        print(f"  Character profile audit: PASS — no findings")
        print(f"  Series profiles: {args.series_profiles}")
        print(f"  Book profiles:   {args.book_profiles}")
        print(f"{'='*70}")
        return 0

    class_a = sum(1 for f in findings if f.get('class_') == 'A')
    class_b = sum(1 for f in findings if f.get('class_') == 'B')
    class_c = sum(1 for f in findings if f.get('class_') == 'C')

    print(f"\n{'='*70}")
    print(f"  Character profile audit: FAIL — {len(findings)} finding(s)")
    print(f"  Class A: {class_a}  Class B: {class_b}  Class C: {class_c}")
    print(f"{'='*70}")
    for finding in findings:
        print(f"\n[{finding.get('class_', '?')}/{finding.get('tier', '?')}] "
              f"{finding.get('finding_id', '?')}")
        print(f"  {finding.get('description', '')}")
        if finding.get('suggested_fix'):
            print(f"  Fix: {finding['suggested_fix']}")

    # Exit non-zero only on Class A. Class B / C are reportable but not
    # gate-blocking from CLI mode's perspective. Master controller will set
    # its own gate semantics in Phase 4.
    return 1 if class_a > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
