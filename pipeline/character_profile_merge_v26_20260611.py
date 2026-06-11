"""ANPD V24 — character_profile_merge

Shared utility that merges series-level and book-specific character profile files
into a single name-keyed dict representing the complete cast for a given book.

Per Character Profile Schema v1.1.0 §3.3: the runner (auditor, generator, scene
writer) merges series-level and book-specific profiles at runtime to produce the
complete cast. Master controller (Phase 4) will eventually own this responsibility
per White Paper §2.12. Until master_controller exists, this utility is the
canonical home — components that need merged cast either import this function
directly or delegate to a calling layer that does.

The merge is structural, not validating. It enforces only the rules that prevent
the merged dict from being corrupt:

- Both files must parse as valid JSON.
- Both files must have name-keyed top-level structure (no envelope fields per §3.4
  — a top-level key that isn't a character canonical name is a Class A schema
  violation that would silently corrupt the merged dict).
- The two files must not contain the same character name (§3.3 — duplicate
  character definition is Class A).

Other schema validation (field completeness, role-specific requirements, voice
spec checks, relationship symmetry, etc.) is the character_profile_auditor's
responsibility, not this utility's.

Per White Paper §2.12 (Generators-generate, separation of concerns): this module
does one thing — merges two files into a dict. It does not validate field-level
content, does not emit V24 findings, does not write STOP_REPORT.json. Failures
raise ProfileMergeError; the caller decides what to do.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Set


# ── Forbidden top-level keys (per Schema §3.4 envelope prohibition) ────────────
#
# These keys, if present at the top level of a character_profiles file, indicate
# the file pre-dates the v1.1.0 schema (likely V20-era) and contains envelope
# metadata that belongs in other canonical homes (intake, book_config,
# series_config, series_bible). Their presence at the top level would silently
# appear as fake "character" entries in the merged dict.
#
# Source of truth: Character Profile Schema v1.1.0 §3.4. If §3.4 changes, this
# constant updates in lockstep — there is no other canonical list.
FORBIDDEN_TOP_LEVEL_KEYS: frozenset = frozenset({
    "series",
    "book_number",
    "title",
    "version",
    "copyright",
    "do_not_appear",
    "series_engine",
    "book_subtext",
    "emotional_core",
    "characters",
})


class ProfileMergeError(Exception):
    """Raised when character profile merge cannot produce a valid merged dict.

    Caller catches this; this module does not write STOP_REPORT.json or emit
    findings. Per §2.12, response routing is the caller's responsibility.
    """
    pass


def _load_profile_file(path: Path, label: str) -> dict:
    """Load and structurally validate a single character profile file.

    Performs only the structural checks required to produce a merge-able dict:
    file exists, parses as JSON, top-level value is an object (not array, not
    primitive), no forbidden envelope keys at the top level.

    Args:
        path: Path to the character profile JSON file.
        label: Human-readable label for error messages — typically "series-level"
            or "book-level" — so caller can identify which file failed.

    Returns:
        The parsed dict. Top-level keys are character canonical names; values
        are character profile objects (not validated at the field level here).

    Raises:
        FileNotFoundError: If the file does not exist.
        ProfileMergeError: If the file is not valid JSON, not a top-level object,
            or contains forbidden envelope keys.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Character profile file not found ({label}): {path}"
        )

    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ProfileMergeError(
            f"Character profile file is not valid JSON ({label}): {path} — "
            f"line {e.lineno} column {e.colno}: {e.msg}"
        ) from e

    if not isinstance(data, dict):
        raise ProfileMergeError(
            f"Character profile file top-level value must be a JSON object "
            f"({label}): {path} — got {type(data).__name__}"
        )

    forbidden_present = sorted(
        key for key in data.keys()
        if key in FORBIDDEN_TOP_LEVEL_KEYS
    )
    if forbidden_present:
        raise ProfileMergeError(
            f"Character profile file contains forbidden envelope keys at the "
            f"top level ({label}): {path} — found {forbidden_present}. Per "
            f"Schema §3.4, these belong in their canonical homes (intake, "
            f"book_config, series_config, series_bible), not in "
            f"character_profiles."
        )

    return data


def merge_character_profiles(series_path: Path, book_path: Path) -> dict:
    """Merge series-level and book-specific character profile files.

    Reads both files, validates structural conformance to Schema v1.1.0 §3.1–§3.4,
    confirms no character name appears in both files (§3.3), and returns the
    merged name-keyed dict containing all characters.

    The order of keys in the returned dict is series-level characters first,
    then book-specific characters, preserving the within-file order of each
    source. This is consistent with §3.1 / §3.2 key-order requirements but is
    not depended on by any V24 component — callers should not assume key order.

    Args:
        series_path: Path to series-level character_profiles.json.
            Per Schema §3.1: must contain at least one protagonist.
            Validated structurally; field-level validation is the auditor's job.
        book_path: Path to book-specific character_profiles.json.
            Per Schema §3.2: must contain at least one antagonist (when used as
            the book file for a generation run; not enforced here because this
            function is also used by tooling that operates on partial files).
            Validated structurally; field-level validation is the auditor's job.

    Returns:
        A name-keyed dict containing all characters from both files. Each value
        is the character profile object as it appeared in its source file —
        no field-level transformation, no defaulting, no normalization.

    Raises:
        FileNotFoundError: If either file does not exist.
        ProfileMergeError: If either file fails structural validation, or if a
            character name appears in both files.
    """
    series_data = _load_profile_file(series_path, "series-level")
    book_data = _load_profile_file(book_path, "book-level")

    series_names: Set[str] = set(series_data.keys())
    book_names: Set[str] = set(book_data.keys())
    collisions = sorted(series_names & book_names)
    if collisions:
        raise ProfileMergeError(
            f"Character name collision between series-level and book-specific "
            f"profile files: {collisions}. Per Schema §3.3, this is a Class A "
            f"error — recurring characters live in the series-level file only "
            f"and must not be duplicated in the book-specific file. "
            f"series_path={series_path} book_path={book_path}"
        )

    merged = {}
    merged.update(series_data)
    merged.update(book_data)
    return merged
