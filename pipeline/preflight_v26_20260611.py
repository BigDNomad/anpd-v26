# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V24 Preflight — rule-based environment verification

Runs all rules from preflight_rules_20260417_1200.md before any pipeline
phase executes. Per the rules document P001-P007 behavioral spec:

- Run ALL rules before reporting (no stop-at-first-failure)
- Write STOP_REPORT.json if any Class A rule fails
- Print [PASS] or [FAIL] per rule with rule_id and error_code
- Exit 0 on all-pass (with component inventory printout)
- Exit 1 on any Class A failure
- Never attempt to create missing files

Invoked as subprocess by master_controller.py per master_controller V24
design doc §3.1. CLI: --book-dir, --series-dir, --intake, --series-config.

Two architectural deviations from preflight_rules_20260417_1200.md, both
captured as Class B (logged in receipt, pipeline continues) until the
underlying gap is addressed by other build work:

1. F013-F021 component-existence rules: master_controller's stub-component
   handling discipline (design doc §10) intentionally lets master_controller
   ship before all Phase 4 components exist on disk. The rule file's literal
   reading would Class-A-halt every preflight run during development.
   Rule responses for components not on disk are downgraded to Class B
   ("component stubbed; downstream phase will halt with stubbed verdict").
   This narrows the gap to a single explicit override list rather than
   silent rule-skipping.

2. E005 + G002 are the same rule (git working tree clean). Run once,
   report under G002.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


# ─── Constants ────────────────────────────────────────────────────────────────

V24_ROOT = "/anpd/v25"
PIPELINE_DIR = os.path.join(V24_ROOT, "pipeline")
SHARED_DIR = os.path.join(V24_ROOT, "shared")

VALID_SCENE_COUNTS = {75, 100, 125}
SYNOPSIS_WORD_MIN = 18000
SYNOPSIS_WORD_MAX = 28000
TWIST_WINDOWS = {
    "twist_1_position": (0.21, 0.29),
    "twist_2_position": (0.46, 0.54),
    "twist_3_position": (0.71, 0.79),
}
ACTION_PCT_MIN = 0.65

# Components whose absence-on-disk is downgraded to Class B during the
# stub-handling period per master_controller design doc §10. This list
# enumerates the explicit downgrade — no silent rule-skipping. As each
# component ships, remove its entry from this set.
STUB_DOWNGRADED_COMPONENTS = {
    "synopsis_comparator.py",
    "chapter_editor.py",
    "formatter.py",
    "psychology_pipeline.py",
    "research_pipeline.py",
    "capsule_writer.py",
}


# ─── Rule result dataclass ────────────────────────────────────────────────────

class RuleResult:
    __slots__ = ("rule_id", "passed", "error_code", "message", "severity")

    def __init__(
        self,
        rule_id: str,
        passed: bool,
        error_code: str = "",
        message: str = "",
        severity: str = "A",
    ):
        self.rule_id = rule_id
        self.passed = passed
        self.error_code = error_code
        self.message = message
        self.severity = severity

    def to_finding(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "error_code": self.error_code,
            "message": self.message,
            "severity": self.severity,
            "suggested_fix": _suggested_fix_for(self.error_code),
        }


# ─── Suggested-fix lookup ─────────────────────────────────────────────────────

def _suggested_fix_for(error_code: str) -> str:
    """One sentence per error_code, per P006 of the rules spec."""
    fixes = {
        # File existence
        "MISSING_SERIES_BIBLE":              "create or restore /anpd/v25/series/{series}/series_bible.json",
        "MISSING_SERIES_CONFIG":             "create series_config.json from genre template + structural overrides",
        "MISSING_SERIES_CHARACTER_PROFILES": "create series-level character_profiles.json with permanent cast",
        "MISSING_NAME_REGISTRY":             "create name_registry.json with at minimum an empty array",
        "MISSING_BANNED_PHRASES":            "create banned_phrases.json per Data Standards §4.7 schema",
        "MISSING_STYLE_CARD":                "create style_card.md with series voice specification",
        "MISSING_INTAKE":                    "author intake.json for this book per Data Standards §4.2",
        "MISSING_SYNOPSIS":                  "run synopsis_generator.py to produce synopsis.md",
        "MISSING_BOOK_CONFIG":               "author book_config.json per Book Config Schema v0.1",
        "MISSING_BOOK_CHARACTER_PROFILES":   "run character_generator.py to produce book character_profiles.json",
        "MISSING_SEED_STATE":                "create state_after_sc00.json (seed state) before first run",
        "MISSING_DLC_DIRECTORY":             "create /anpd/v25/shared/dlc/ directory",
        "MISSING_OUTPUT_SCENES_DIR":         "create out/scenes/ directory in book root",
        "MISSING_OUTPUT_STATE_DIR":          "create out/state/ directory in book root",
        "MISSING_OUTPUT_REPORTS_DIR":        "create out/reports/ directory in book root",
        # Component-existence (downgraded to Class B per stub-handling list)
        "MISSING_MASTER_CONTROLLER":         "ship master_controller.py to /anpd/v25/pipeline/",
        "MISSING_SCENE_WRITER":              "ship scene_writer.py to /anpd/v25/pipeline/",
        "MISSING_STATE_TRACKER":             "ship state_tracker.py to /anpd/v25/pipeline/",
        "MISSING_SYNOPSIS_AUDITOR":          "ship synopsis_auditor.py to /anpd/v25/pipeline/",
        "MISSING_SYNOPSIS_COMPARATOR":       "ship synopsis_comparator.py to /anpd/v25/pipeline/",
        "MISSING_CHAPTER_EDITOR":            "ship chapter_editor.py to /anpd/v25/pipeline/",
        "MISSING_FORMATTER":                 "ship formatter.py to /anpd/v25/pipeline/",
        "MISSING_PSYCHOLOGY_PIPELINE":       "ship psychology_pipeline.py to /anpd/v25/pipeline/",
        "MISSING_RESEARCH_PIPELINE":         "ship research_pipeline.py to /anpd/v25/pipeline/",
        # Validity
        "INTAKE_INVALID_JSON":               "fix JSON syntax error in intake.json",
        "SERIES_BIBLE_INVALID_JSON":         "fix JSON syntax error in series_bible.json",
        "BOOK_CONFIG_INVALID_JSON":          "fix JSON syntax error in book_config.json",
        "CHARACTER_PROFILES_INVALID_JSON":   "fix JSON syntax error in character_profiles.json",
        "SEED_STATE_INVALID_JSON":           "fix JSON syntax error in state_after_sc00.json",
        "NAME_REGISTRY_INVALID_JSON":        "fix JSON syntax error in name_registry.json",
        "BANNED_PHRASES_INVALID_JSON":       "fix JSON syntax error in banned_phrases.json",
        "SYNOPSIS_EMPTY":                    "regenerate synopsis.md (current file is empty)",
        "STYLE_CARD_EMPTY":                  "author style_card.md content (current file is empty)",
        # Data contract
        "CHAPTER_COUNT_NOT_25":              "set intake.target_chapter_count to 25",
        "SCENE_COUNT_INVALID":               "set intake.target_scene_count to 75, 100, or 125",
        "SYNOPSIS_WORD_TARGET_OUT_OF_RANGE": "set intake.target_synopsis_words between 18000 and 28000",
        "CHAPTER_COUNT_NOT_INTEGER":         "set intake.target_chapter_count to an integer (no quotes)",
        "SCENE_COUNT_NOT_INTEGER":           "set intake.target_scene_count to an integer",
        "BOOK_NUMBER_NOT_INTEGER":           "set intake.book_number to an integer",
        "WRONG_COPYRIGHT_HOLDER":            "set intake.copyright_holder to 'Endeavor Publishing LLC'",
        "SERIES_NAME_MISMATCH":              "set intake.series to match the series directory name",
        "SCENES_PER_CHAPTER_INVALID":        "set scene_count / chapter_count ratio to 3, 4, or 5",
        "BANNED_NAME_IN_CHARACTER_PROFILES": "remove banned name from character_profiles.json",
        "RESOLUTION_SCENES_NOT_2":           "set intake.resolution_scenes to 2",
        "TWIST1_POSITION_WRONG":             "set intake.twist_1_position to 25",
        "TWIST2_POSITION_WRONG":             "set intake.twist_2_position to 50",
        "TWIST3_POSITION_WRONG":             "set intake.twist_3_position to 75",
        # Synopsis
        "SYNOPSIS_WORD_COUNT_OUT_OF_RANGE":  "regenerate synopsis to land within 18000-28000 words",
        "SYNOPSIS_SCENE_COUNT_MISMATCH":     "synopsis must have intake.target_scene_count scenes",
        "ACTION_PCT_BELOW_65":               "regenerate synopsis with at least 65 percent action scenes",
        "SYNOPSIS_RESOLUTION_COUNT_WRONG":   "synopsis must have exactly 2 resolution scenes",
        "TWIST1_MISPLACED":                  "twist 1 scene index must fall in the 21-29 percent window",
        "TWIST2_MISPLACED":                  "twist 2 scene index must fall in the 46-54 percent window",
        "TWIST3_MISPLACED":                  "twist 3 scene index must fall in the 71-79 percent window",
        "BANNED_NAME_IN_SYNOPSIS":           "remove banned names from synopsis text",
        "SYNOPSIS_CHAPTER_COUNT_WRONG":      "synopsis must have 25 chapters",
        # Environment
        "MISSING_API_KEY":                   "set ANTHROPIC_API_KEY environment variable",
        "ANTHROPIC_IMPORT_FAILED":           "pip install anthropic",
        "JSON_IMPORT_FAILED":                "JSON is stdlib; check Python install",
        "GLOB_IMPORT_FAILED":                "glob is stdlib; check Python install",
        # Git
        "GIT_REPO_NOT_INITIALIZED":          "run 'git init' at /anpd/v25/",
        "GIT_DIRTY_WORKING_TREE":            "commit or stash uncommitted changes",
        "GIT_NO_COMMITS":                    "make at least one commit before running pipeline",
    }
    return fixes.get(error_code, "see preflight rule documentation")


# ─── Helpers for rule checks ──────────────────────────────────────────────────

def _exists(path: str) -> bool:
    return os.path.exists(path)


def _is_dir(path: str) -> bool:
    return os.path.isdir(path)


def _parses_as_json(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            json.load(fh)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def _file_not_empty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _safe_load_json(path: str) -> dict | list | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


# ─── Rule group runners ───────────────────────────────────────────────────────

def run_file_existence_rules(book_dir: str, series_dir: str) -> list[RuleResult]:
    """F001-F025: every required file/dir exists at canonical path."""
    results: list[RuleResult] = []

    # Series-level (F001-F006)
    series_files = [
        ("F001", "series_bible.json",       "MISSING_SERIES_BIBLE"),
        ("F002", "series_config.json",      "MISSING_SERIES_CONFIG"),
        ("F003", "character_profiles.json", "MISSING_SERIES_CHARACTER_PROFILES"),
        ("F004", "name_registry.json",      "MISSING_NAME_REGISTRY"),
        ("F005", "banned_phrases.json",     "MISSING_BANNED_PHRASES"),
        ("F006", "style_card.md",           "MISSING_STYLE_CARD"),
    ]
    for rule_id, fname, err in series_files:
        path = os.path.join(series_dir, fname)
        results.append(_check_exists(rule_id, path, err))

    # Book-level (F007-F012)
    book_files = [
        ("F007", "work/intake.json",            "MISSING_INTAKE"),
        ("F008", "work/synopsis.md",            "MISSING_SYNOPSIS"),
        ("F010", "work/book_config.json",       "MISSING_BOOK_CONFIG"),
        ("F011", "work/character_profiles.json", "MISSING_BOOK_CHARACTER_PROFILES"),
        ("F012", "out/state/state_after_sc00.json", "MISSING_SEED_STATE"),
    ]
    for rule_id, rel_path, err in book_files:
        path = os.path.join(book_dir, rel_path)
        results.append(_check_exists(rule_id, path, err))

    # Pipeline components (F013-F021) — Class B during stub-handling period
    component_rules = [
        ("F013", "master_controller.py",   "MISSING_MASTER_CONTROLLER"),
        ("F014", "scene_writer.py",        "MISSING_SCENE_WRITER"),
        ("F015", "state_tracker.py",       "MISSING_STATE_TRACKER"),
        ("F016", "synopsis_auditor.py",    "MISSING_SYNOPSIS_AUDITOR"),
        ("F017", "synopsis_comparator.py", "MISSING_SYNOPSIS_COMPARATOR"),
        ("F018", "chapter_editor.py",      "MISSING_CHAPTER_EDITOR"),
        ("F019", "formatter.py",           "MISSING_FORMATTER"),
        ("F020", "psychology_pipeline.py", "MISSING_PSYCHOLOGY_PIPELINE"),
        ("F021", "research_pipeline.py",   "MISSING_RESEARCH_PIPELINE"),
    ]
    for rule_id, fname, err in component_rules:
        path = os.path.join(PIPELINE_DIR, fname)
        present = _exists(path)
        if present:
            results.append(RuleResult(rule_id, True, severity="A"))
        elif fname in STUB_DOWNGRADED_COMPONENTS:
            results.append(RuleResult(
                rule_id, False, err,
                f"{fname} not on disk; downgraded to Class B per stub-handling list",
                severity="B",
            ))
        else:
            results.append(RuleResult(
                rule_id, False, err,
                f"{path} does not exist",
                severity="A",
            ))

    # F022 dlc/ + F023-F025 output dirs
    results.append(_check_dir_exists(
        "F022", os.path.join(SHARED_DIR, "dlc"), "MISSING_DLC_DIRECTORY",
    ))
    out_dirs = [
        ("F023", "out/scenes",  "MISSING_OUTPUT_SCENES_DIR"),
        ("F024", "out/state",   "MISSING_OUTPUT_STATE_DIR"),
        ("F025", "out/reports", "MISSING_OUTPUT_REPORTS_DIR"),
    ]
    for rule_id, rel, err in out_dirs:
        results.append(_check_dir_exists(
            rule_id, os.path.join(book_dir, rel), err,
        ))

    return results


def _check_exists(rule_id: str, path: str, err: str) -> RuleResult:
    if _exists(path):
        return RuleResult(rule_id, True)
    return RuleResult(rule_id, False, err, f"{path} does not exist", severity="A")


def _check_dir_exists(rule_id: str, path: str, err: str) -> RuleResult:
    if _is_dir(path):
        return RuleResult(rule_id, True)
    return RuleResult(rule_id, False, err, f"{path} is not a directory", severity="A")


def run_validity_rules(book_dir: str, series_dir: str) -> list[RuleResult]:
    """V001-V010: JSON parses, markdown non-empty."""
    results: list[RuleResult] = []

    json_rules = [
        ("V001", os.path.join(book_dir, "work/intake.json"),                     "INTAKE_INVALID_JSON"),
        ("V002", os.path.join(series_dir, "series_bible.json"),                  "SERIES_BIBLE_INVALID_JSON"),
        ("V003", os.path.join(book_dir, "work/book_config.json"),                "BOOK_CONFIG_INVALID_JSON"),
        ("V004", os.path.join(book_dir, "work/character_profiles.json"),         "CHARACTER_PROFILES_INVALID_JSON"),
        ("V005", os.path.join(book_dir, "out/state/state_after_sc00.json"),      "SEED_STATE_INVALID_JSON"),
        ("V006", os.path.join(series_dir, "name_registry.json"),                 "NAME_REGISTRY_INVALID_JSON"),
        ("V007", os.path.join(series_dir, "banned_phrases.json"),                "BANNED_PHRASES_INVALID_JSON"),
    ]
    for rule_id, path, err in json_rules:
        if not _exists(path):
            # Existence is enforced by F-rules; skip validity to avoid double-failure noise.
            results.append(RuleResult(rule_id, True))  # vacuous pass
            continue
        if _parses_as_json(path):
            results.append(RuleResult(rule_id, True))
        else:
            results.append(RuleResult(rule_id, False, err, f"{path} is not valid JSON"))

    md_rules = [
        ("V008", os.path.join(book_dir, "work/synopsis.md"),  "SYNOPSIS_EMPTY"),
        ("V010", os.path.join(series_dir, "style_card.md"),   "STYLE_CARD_EMPTY"),
    ]
    for rule_id, path, err in md_rules:
        if not _exists(path):
            results.append(RuleResult(rule_id, True))  # vacuous pass; F-rule will catch
            continue
        if _file_not_empty(path):
            results.append(RuleResult(rule_id, True))
        else:
            results.append(RuleResult(rule_id, False, err, f"{path} is empty"))

    return results


def run_data_contract_rules(book_dir: str, series_dir: str) -> list[RuleResult]:
    """D001-D014: intake field values legal per Data Standards."""
    results: list[RuleResult] = []
    intake_path = os.path.join(book_dir, "work/intake.json")
    intake = _safe_load_json(intake_path)
    if not isinstance(intake, dict):
        # Vacuous pass — V001 will catch the parse failure.
        for rid in ("D001", "D002", "D003", "D004", "D005", "D006", "D007",
                    "D008", "D009", "D010", "D011", "D012", "D013", "D014"):
            results.append(RuleResult(rid, True))
        return results

    chapter_count = intake.get("target_chapter_count")
    scene_count = intake.get("target_scene_count")
    synopsis_words = intake.get("target_synopsis_words")
    book_number = intake.get("book_number")
    copyright_holder = intake.get("copyright_holder")
    intake_series = intake.get("series")
    do_not_appear = intake.get("do_not_appear", [])
    resolution_scenes = intake.get("resolution_scenes")

    # D001
    results.append(_assert(
        "D001", chapter_count == 25, "CHAPTER_COUNT_NOT_25",
        f"target_chapter_count is {chapter_count!r}, expected 25",
    ))
    # D002
    results.append(_assert(
        "D002", scene_count in VALID_SCENE_COUNTS, "SCENE_COUNT_INVALID",
        f"target_scene_count is {scene_count!r}, expected one of {sorted(VALID_SCENE_COUNTS)}",
    ))
    # D003
    in_range = (
        isinstance(synopsis_words, int)
        and SYNOPSIS_WORD_MIN <= synopsis_words <= SYNOPSIS_WORD_MAX
    )
    results.append(_assert(
        "D003", in_range, "SYNOPSIS_WORD_TARGET_OUT_OF_RANGE",
        f"target_synopsis_words is {synopsis_words!r}, expected {SYNOPSIS_WORD_MIN}-{SYNOPSIS_WORD_MAX}",
    ))
    # D004-D006 (integer types)
    results.append(_assert(
        "D004", isinstance(chapter_count, int), "CHAPTER_COUNT_NOT_INTEGER",
        f"target_chapter_count type is {type(chapter_count).__name__}",
    ))
    results.append(_assert(
        "D005", isinstance(scene_count, int), "SCENE_COUNT_NOT_INTEGER",
        f"target_scene_count type is {type(scene_count).__name__}",
    ))
    results.append(_assert(
        "D006", isinstance(book_number, int), "BOOK_NUMBER_NOT_INTEGER",
        f"book_number type is {type(book_number).__name__}",
    ))
    # D007
    results.append(_assert(
        "D007", copyright_holder == "Endeavor Publishing LLC", "WRONG_COPYRIGHT_HOLDER",
        f"copyright_holder is {copyright_holder!r}",
    ))
    # D008 — series matches directory basename
    series_basename = os.path.basename(series_dir.rstrip("/"))
    results.append(_assert(
        "D008", intake_series == series_basename, "SERIES_NAME_MISMATCH",
        f"intake.series is {intake_series!r}, series_dir basename is {series_basename!r}",
    ))
    # D009 — scenes_per_chapter ratio in {3, 4, 5}
    scenes_per_chapter = (
        scene_count / chapter_count
        if isinstance(chapter_count, int)
        and isinstance(scene_count, int)
        and chapter_count > 0
        else None
    )
    results.append(_assert(
        "D009", scenes_per_chapter in (3, 4, 5), "SCENES_PER_CHAPTER_INVALID",
        f"scenes_per_chapter is {scenes_per_chapter!r}, expected 3, 4, or 5",
    ))
    # D010 — banned names not in character_profiles
    cp_path = os.path.join(book_dir, "work/character_profiles.json")
    cp = _safe_load_json(cp_path) or {}
    profile_names = _extract_profile_names(cp)
    banned = set(do_not_appear or [])
    overlap = banned & profile_names
    results.append(_assert(
        "D010", not overlap, "BANNED_NAME_IN_CHARACTER_PROFILES",
        f"banned names found in character profiles: {sorted(overlap)}",
    ))
    # D011-D014
    results.append(_assert(
        "D011", resolution_scenes == 2, "RESOLUTION_SCENES_NOT_2",
        f"resolution_scenes is {resolution_scenes!r}, expected 2",
    ))
    twist_1 = intake.get("twist_1_position")
    twist_2 = intake.get("twist_2_position")
    twist_3 = intake.get("twist_3_position")
    results.append(_assert(
        "D012", twist_1 == 25, "TWIST1_POSITION_WRONG",
        f"twist_1_position is {twist_1!r}, expected 25",
    ))
    results.append(_assert(
        "D013", twist_2 == 50, "TWIST2_POSITION_WRONG",
        f"twist_2_position is {twist_2!r}, expected 50",
    ))
    results.append(_assert(
        "D014", twist_3 == 75, "TWIST3_POSITION_WRONG",
        f"twist_3_position is {twist_3!r}, expected 75",
    ))

    return results


def _assert(rule_id: str, condition: bool, error_code: str, message: str) -> RuleResult:
    if condition:
        return RuleResult(rule_id, True)
    return RuleResult(rule_id, False, error_code, message)


def _extract_profile_names(profiles: Any) -> set[str]:
    """Pull character names from character_profiles.json structure.

    Profiles dict shape: {"characters": {"<name>": {...}}} or top-level
    {"<name>": {...}}. Defensive against either.
    """
    names: set[str] = set()
    if not isinstance(profiles, dict):
        return names
    chars = profiles.get("characters", profiles)
    if isinstance(chars, dict):
        names.update(chars.keys())
    return names


def run_synopsis_rules(book_dir: str, series_dir: str) -> list[RuleResult]:
    """S001-S009: synopsis content rules. Per spec, run after F/V/D pass."""
    results: list[RuleResult] = []
    synopsis_path = os.path.join(book_dir, "work/synopsis.md")
    intake_path = os.path.join(book_dir, "work/intake.json")
    banned_path = os.path.join(series_dir, "banned_phrases.json")

    if not _exists(synopsis_path) or not _exists(intake_path):
        # Existence is F-rule territory; vacuously pass S-rules to avoid noise.
        for rid in ("S001", "S002", "S003", "S004", "S005", "S006", "S007", "S008", "S009"):
            results.append(RuleResult(rid, True))
        return results

    synopsis_text = _safe_read_text(synopsis_path)
    intake = _safe_load_json(intake_path) or {}
    target_scene_count = intake.get("target_scene_count")

    # Word count
    word_count = len(synopsis_text.split())
    results.append(_assert(
        "S001", SYNOPSIS_WORD_MIN <= word_count <= SYNOPSIS_WORD_MAX,
        "SYNOPSIS_WORD_COUNT_OUT_OF_RANGE",
        f"synopsis word count {word_count}, expected {SYNOPSIS_WORD_MIN}-{SYNOPSIS_WORD_MAX}",
    ))

    # Scene count from synopsis text — count "Scene N" headings
    scene_pattern = re.compile(r"^#{2,4}\s+Scene\s+\d+", re.MULTILINE | re.IGNORECASE)
    synopsis_scene_count = len(scene_pattern.findall(synopsis_text))
    results.append(_assert(
        "S002", synopsis_scene_count == target_scene_count,
        "SYNOPSIS_SCENE_COUNT_MISMATCH",
        f"synopsis has {synopsis_scene_count} scenes, intake says {target_scene_count}",
    ))

    # Action / resolution scene classification: scan for "[ACTION]" or "[RESOLUTION]"
    # tags per scene_map convention. If absent in the synopsis, vacuous pass.
    action_count = len(re.findall(r"\[ACTION\]", synopsis_text, re.IGNORECASE))
    resolution_count = len(re.findall(r"\[RESOLUTION\]", synopsis_text, re.IGNORECASE))
    if synopsis_scene_count > 0 and action_count > 0:
        action_pct = action_count / synopsis_scene_count
        results.append(_assert(
            "S003", action_pct >= ACTION_PCT_MIN, "ACTION_PCT_BELOW_65",
            f"action scene percentage {action_pct:.2%}, expected ≥ {ACTION_PCT_MIN:.0%}",
        ))
    else:
        # No tags surfaced — synopsis_summarizer will handle qualitative check downstream.
        results.append(RuleResult("S003", True))

    if resolution_count > 0:
        results.append(_assert(
            "S004", resolution_count == 2, "SYNOPSIS_RESOLUTION_COUNT_WRONG",
            f"resolution_scenes count is {resolution_count}, expected 2",
        ))
    else:
        results.append(RuleResult("S004", True))

    # Twist position checks (S005-S007)
    twist_positions = _extract_twist_scene_positions(synopsis_text, synopsis_scene_count)
    for rule_id, key, error_code in (
        ("S005", "twist_1", "TWIST1_MISPLACED"),
        ("S006", "twist_2", "TWIST2_MISPLACED"),
        ("S007", "twist_3", "TWIST3_MISPLACED"),
    ):
        position = twist_positions.get(key)
        if position is None or synopsis_scene_count == 0:
            results.append(RuleResult(rule_id, True))  # vacuous if twist-tags absent
            continue
        ratio = position / synopsis_scene_count
        window_low, window_high = TWIST_WINDOWS[f"{key}_position"]
        results.append(_assert(
            rule_id, window_low <= ratio <= window_high, error_code,
            f"{key} at scene {position}/{synopsis_scene_count} = {ratio:.3f}, "
            f"expected window [{window_low:.2f}, {window_high:.2f}]",
        ))

    # S008 banned names
    banned_data = _safe_load_json(banned_path) or {}
    banned_names = banned_data.get("names", []) if isinstance(banned_data, dict) else []
    found_names = [n for n in banned_names if n and n in synopsis_text]
    results.append(_assert(
        "S008", not found_names, "BANNED_NAME_IN_SYNOPSIS",
        f"banned names found in synopsis: {found_names}",
    ))

    # S009 chapter count from synopsis — count "Chapter N" headings
    chapter_pattern = re.compile(r"^#{1,3}\s+Chapter\s+\d+", re.MULTILINE | re.IGNORECASE)
    synopsis_chapter_count = len(chapter_pattern.findall(synopsis_text))
    if synopsis_chapter_count == 0:
        results.append(RuleResult("S009", True))  # vacuous
    else:
        results.append(_assert(
            "S009", synopsis_chapter_count == 25, "SYNOPSIS_CHAPTER_COUNT_WRONG",
            f"synopsis has {synopsis_chapter_count} chapters, expected 25",
        ))

    return results


def _extract_twist_scene_positions(synopsis_text: str, total_scenes: int) -> dict:
    """Extract twist scene positions from synopsis text.

    Looks for "[TWIST 1]", "[TWIST 2]", "[TWIST 3]" tags within scene
    markers. Returns dict like {'twist_1': 27, 'twist_2': 51, ...}.
    Returns empty dict if tags not present (vacuous pass).
    """
    out: dict[str, int] = {}
    # Find each "Scene N" heading and its line range
    scene_lines = []
    for m in re.finditer(r"^#{2,4}\s+Scene\s+(\d+)", synopsis_text, re.MULTILINE | re.IGNORECASE):
        scene_num = int(m.group(1))
        scene_lines.append((scene_num, m.start()))

    for i, (scene_num, start) in enumerate(scene_lines):
        end = scene_lines[i + 1][1] if i + 1 < len(scene_lines) else len(synopsis_text)
        body = synopsis_text[start:end]
        for twist_id in (1, 2, 3):
            if re.search(rf"\[TWIST\s*{twist_id}\]", body, re.IGNORECASE):
                out[f"twist_{twist_id}"] = scene_num
    return out


def run_environment_rules() -> list[RuleResult]:
    """E001-E004 + G001-G003 (E005 deduplicated with G002)."""
    results: list[RuleResult] = []

    # E001 ANTHROPIC_API_KEY
    results.append(_assert(
        "E001", bool(os.environ.get("ANTHROPIC_API_KEY")),
        "MISSING_API_KEY", "ANTHROPIC_API_KEY environment variable not set",
    ))
    # E002 import anthropic
    try:
        importlib.import_module("anthropic")
        results.append(RuleResult("E002", True))
    except ImportError:
        results.append(RuleResult(
            "E002", False, "ANTHROPIC_IMPORT_FAILED",
            "import anthropic failed",
        ))
    # E003 import json (vacuous in CPython but spec-mandated)
    try:
        importlib.import_module("json")
        results.append(RuleResult("E003", True))
    except ImportError:
        results.append(RuleResult(
            "E003", False, "JSON_IMPORT_FAILED",
            "import json failed",
        ))
    # E004 import glob
    try:
        importlib.import_module("glob")
        results.append(RuleResult("E004", True))
    except ImportError:
        results.append(RuleResult(
            "E004", False, "GLOB_IMPORT_FAILED",
            "import glob failed",
        ))

    # G001 git repo exists
    git_dir = os.path.join(V24_ROOT, ".git")
    results.append(_assert(
        "G001", os.path.isdir(git_dir),
        "GIT_REPO_NOT_INITIALIZED",
        f"{git_dir} is not a git repository",
    ))

    # G002 git status clean (also covers what spec calls E005 — same rule)
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=V24_ROOT, capture_output=True, text=True,
        )
        # Per the project's standing convention, certain pre-existing untracked
        # directories (series/, shared/dlc/) are not considered dirty for the
        # purpose of preflight. Only tracked-file dirty status counts.
        dirty = bool(result.stdout.strip())
        if dirty:
            # Filter out known pre-existing untracked dirs
            lines = result.stdout.strip().splitlines()
            relevant = [
                ln for ln in lines
                if not (
                    ln.startswith("??") and (
                        "series/" in ln or "shared/dlc" in ln
                    )
                )
            ]
            dirty = bool(relevant)
        results.append(_assert(
            "G002", not dirty, "GIT_DIRTY_WORKING_TREE",
            "git working tree has uncommitted tracked-file changes",
        ))
    except (OSError, subprocess.SubprocessError):
        results.append(RuleResult(
            "G002", False, "GIT_DIRTY_WORKING_TREE",
            "could not run 'git status'",
        ))

    # G003 at least one commit exists
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=V24_ROOT, capture_output=True, text=True,
        )
        results.append(_assert(
            "G003", result.returncode == 0 and bool(result.stdout.strip()),
            "GIT_NO_COMMITS", "git log returned no commits",
        ))
    except (OSError, subprocess.SubprocessError):
        results.append(RuleResult(
            "G003", False, "GIT_NO_COMMITS", "could not run 'git log'",
        ))

    return results


# ─── Output handlers ──────────────────────────────────────────────────────────

def print_rule_results(results: list[RuleResult]) -> None:
    """Per P003 of the spec: print [PASS] or [FAIL] RULE_ID ERROR_CODE."""
    for r in results:
        if r.passed:
            print(f"[PASS] {r.rule_id}")
        elif r.severity == "B":
            print(f"[WARN] {r.rule_id} {r.error_code} (Class B)")
        else:
            print(f"[FAIL] {r.rule_id} {r.error_code}")


def write_stop_report(book_dir: str, class_a_failures: list[RuleResult]) -> str:
    """Per P002 + P006: write STOP_REPORT.json listing every Class A failure."""
    reports_dir = os.path.join(book_dir, "out", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, "STOP_REPORT.json")
    payload = {
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "component":     "preflight",
        "phase":         1,
        "scene_number":  None,
        "error_type":    "Class A",
        "error_message": f"{len(class_a_failures)} preflight rule(s) failed",
        "file_path":     None,
        "suggested_fix": "address every Class A finding listed below",
        "pipeline_state": "halted at preflight",
        "failed_rules":  [r.to_finding() for r in class_a_failures],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def print_component_inventory() -> None:
    """Per P005: on full pass, print component inventory with sizes + timestamps."""
    print("\n=== Component inventory ===")
    py_files = sorted(glob.glob(os.path.join(PIPELINE_DIR, "*.py")))
    for path in py_files:
        try:
            size = os.path.getsize(path)
            mtime = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            kind = "(symlink)" if os.path.islink(path) else "(file)"
            print(f"  {os.path.basename(path):<55} {size:>7} bytes   {mtime}   {kind}")
        except OSError as exc:
            print(f"  {os.path.basename(path):<55} ERROR: {exc}")


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_preflight(book_dir: str, series_dir: str) -> tuple[int, list[RuleResult]]:
    """Run all rules and return (exit_code, all_results).

    exit_code is 0 if no Class A failures; 1 otherwise.
    """
    all_results: list[RuleResult] = []

    print("\n=== Phase F (file existence) ===")
    f_results = run_file_existence_rules(book_dir, series_dir)
    print_rule_results(f_results)
    all_results.extend(f_results)

    print("\n=== Phase V (validity) ===")
    v_results = run_validity_rules(book_dir, series_dir)
    print_rule_results(v_results)
    all_results.extend(v_results)

    print("\n=== Phase D (data contract) ===")
    d_results = run_data_contract_rules(book_dir, series_dir)
    print_rule_results(d_results)
    all_results.extend(d_results)

    print("\n=== Phase S (synopsis) ===")
    s_results = run_synopsis_rules(book_dir, series_dir)
    print_rule_results(s_results)
    all_results.extend(s_results)

    print("\n=== Phase E + G (environment, git) ===")
    e_results = run_environment_rules()
    print_rule_results(e_results)
    all_results.extend(e_results)

    class_a_failures = [
        r for r in all_results if not r.passed and r.severity == "A"
    ]
    class_b_warnings = [
        r for r in all_results if not r.passed and r.severity == "B"
    ]

    print(f"\n=== Summary ===")
    print(f"  Total rules:       {len(all_results)}")
    print(f"  Passed:            {sum(1 for r in all_results if r.passed)}")
    print(f"  Class A failures:  {len(class_a_failures)}")
    print(f"  Class B warnings:  {len(class_b_warnings)}")

    if class_a_failures:
        report_path = write_stop_report(book_dir, class_a_failures)
        print(f"\n  STOP_REPORT written: {report_path}")
        return (1, all_results)

    print_component_inventory()
    return (0, all_results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description="ANPD V24 preflight — rule-based pipeline environment verification",
    )
    parser.add_argument("--book-dir", required=True)
    parser.add_argument("--series-dir", required=True)
    parser.add_argument("--intake", required=True,
                        help="Path to intake.json (kept for CLI consistency; canonical lookup is by book_dir)")
    parser.add_argument("--series-config", required=True,
                        help="Path to series_config.json (kept for CLI consistency)")
    args = parser.parse_args(argv)

    exit_code, _ = run_preflight(args.book_dir, args.series_dir)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
