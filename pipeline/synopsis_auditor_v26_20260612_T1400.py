#!/usr/bin/env python3
"""
synopsis_auditor.py — Pre-Generation Synopsis Gate
ANPD V24 | STOP_REPORT path aligned to V24 Data Standards (out/reports/)

Runs as Phase 0b after preflight, before psychology_pipeline.
If any rubric item FAILs, the pipeline stops.

Two sequential API calls (Haiku):
  Call 1: Structural and mechanical checks
  Call 2: Quality and engagement checks (receives Call 1 results)

Input:
  - Synopsis .md file
  - Intake .json file
  - Series dir (to load character profiles)

Output:
  - {book_dir}/synopsis_audit_report.json — structured findings
  - {book_dir}/synopsis_audit_report.md  — human-readable report
  - Exit code 0 = PASS, exit code 1 = FAIL

Usage:
    python3 synopsis_auditor.py \\
      --synopsis <synopsis_path> \\
      --intake <intake_path> \\
      --series-dir <series_dir>

Copyright (c) 2026 Endeavor Publishing LLC
"""

import os
from pathlib import Path
import re
import sys
import json
import glob
import time
import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from config_resolver import resolve_config
from findings import create_finding, serialize_findings


# ── Scene dataclass & deterministic parser ───────────────────────────────────

@dataclass(frozen=True)
class Scene:
    number: int
    title: str
    body: str
    scene_type: str = "UNKNOWN"   # ACTION / NON-ACTION / SUSPENSE / MIXED / UNKNOWN
    pov: str = ""                  # POV character name from [POV: ...] bracket


# V25 format: ### Scene 1 — Title [TYPE: ACTION] [POV: Hank Reyes]
_SCENE_HEADER_V25_RE = re.compile(
    r"^###\s+Scene\s+(\d+)\s*[\u2014\u2013\-]\s*(.+?)(?:\s+\[TYPE:.*?\])?(?:\s+\[POV:.*?\])?\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# V24 format (legacy fallback): ## SCENE 1: Title
_SCENE_HEADER_V24_RE = re.compile(r'^## SCENE (\d+):\s*(.*?)$', re.MULTILINE)

# Metadata extractors for V25 headers
_TYPE_RE = re.compile(r"\[TYPE:\s*([A-Z\-]+)\s*\]", re.IGNORECASE)
_POV_RE = re.compile(r"\[POV:\s*([^\]]+)\s*\]", re.IGNORECASE)


def parse_synopsis(text: str) -> List[Scene]:
    """Parse synopsis into Scene records.

    Supports V25 format (### Scene N — Title [TYPE: X] [POV: Y]) and falls
    back to V24 format (## SCENE N: Title) if V25 produces zero matches.
    V24 fallback exists for legacy synopses and will be removed when V24 retires.

    Duplicate scene numbers are preserved — integrity checks detect them.
    Returns [] on empty input or no matching headers.
    """
    if not text or not text.strip():
        return []

    # Try V25 format first
    headers = list(_SCENE_HEADER_V25_RE.finditer(text))
    is_v25 = bool(headers)

    # Fallback to V24 if no V25 matches
    if not headers:
        headers = list(_SCENE_HEADER_V24_RE.finditer(text))

    if not headers:
        return []

    scenes = []
    for i, match in enumerate(headers):
        number = int(match.group(1))
        title = match.group(2).strip()
        # Clean title of any remaining bracket metadata
        title = re.sub(r"\s*\[TYPE:.*?\]", "", title)
        title = re.sub(r"\s*\[POV:.*?\]", "", title)
        title = title.strip()

        body_start = match.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[body_start:body_end].strip()

        # Extract TYPE and POV from the full matched header line
        scene_type = "UNKNOWN"
        pov = ""
        if is_v25:
            header_line = match.group(0)
            type_m = _TYPE_RE.search(header_line)
            if type_m:
                scene_type = type_m.group(1).upper()
            pov_m = _POV_RE.search(header_line)
            if pov_m:
                pov = pov_m.group(1).strip()

        scenes.append(Scene(number=number, title=title, body=body,
                            scene_type=scene_type, pov=pov))

    return scenes


def check_synopsis_integrity(scenes: List[Scene], intake: dict, synopsis_path: str) -> List[dict]:
    """Run deterministic integrity checks on parsed scenes.

    Returns a list of Class A findings for:
      - scene count mismatch vs intake.target_scene_count
      - duplicate scene numbers
      - non-contiguous scene numbers (not 1..N)

    Findings bypass the rubric/Haiku path — constructed directly via
    create_finding().
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    findings = []

    # ── target_scene_count validation ──
    if 'target_scene_count' not in intake:
        findings.append(create_finding(
            finding_id="synopsis_target_missing_0001",
            auditor="synopsis_auditor",
            gate="synopsis",
            pass_name="target_scene_count_present",
            class_="A",
            tier="3",
            category="intake_integrity",
            description="intake.json is missing the target_scene_count field.",
            location={"type": "whole_artifact", "synopsis_path": synopsis_path},
            evidence=None,
            confidence="HIGH",
            fix_action="route_to_rewrite",
            suggested_fix="Set target_scene_count to an integer in intake.json matching the intended number of scenes.",
            timestamp=timestamp,
        ))
    else:
        target = intake['target_scene_count']
        if isinstance(target, bool) or not isinstance(target, int):
            findings.append(create_finding(
                finding_id="synopsis_target_invalid_type_0001",
                auditor="synopsis_auditor",
                gate="synopsis",
                pass_name="target_scene_count_valid",
                class_="A",
                tier="3",
                category="intake_integrity",
                description=(
                    f"intake.target_scene_count has invalid type: "
                    f"{target!r} (type {type(target).__name__}). Expected int."
                ),
                location={"type": "whole_artifact", "synopsis_path": synopsis_path},
                evidence=None,
                confidence="HIGH",
                fix_action="route_to_rewrite",
                suggested_fix="Set target_scene_count to an integer in intake.json (e.g. 25).",
                timestamp=timestamp,
            ))
        elif len(scenes) != target:
            # ── scene count mismatch ──
            findings.append(create_finding(
                finding_id="synopsis_scene_count_0001",
                auditor="synopsis_auditor",
                gate="synopsis",
                pass_name="scene_count",
                class_="A",
                tier="3",
                category="structure",
                description=(
                    f"Scene count mismatch: synopsis has {len(scenes)} scenes "
                    f"but intake.target_scene_count is {target}."
                ),
                location={"type": "whole_artifact", "synopsis_path": synopsis_path},
                evidence=None,
                confidence="HIGH",
                fix_action="route_to_rewrite",
                suggested_fix=(
                    f"Adjust the synopsis to contain exactly {target} scenes "
                    f"(currently {len(scenes)}), or update intake.target_scene_count "
                    f"if the synopsis count is correct."
                ),
                timestamp=timestamp,
            ))

    # ── scene duplicates ──
    counts = Counter(s.number for s in scenes)
    duplicates = sorted(n for n, c in counts.items() if c > 1)
    if duplicates:
        dup_str = ', '.join(str(n) for n in duplicates)
        findings.append(create_finding(
            finding_id="synopsis_scene_duplicates_0001",
            auditor="synopsis_auditor",
            gate="synopsis",
            pass_name="scene_duplicates",
            class_="A",
            tier="2",
            category="structure",
            description=(
                f"Duplicate scene numbers found: {dup_str}."
            ),
            location={"type": "whole_artifact", "synopsis_path": synopsis_path},
            evidence=None,
            confidence="HIGH",
            fix_action="verify_then_fix",
            suggested_fix=(
                f"Renumber scenes so each scene number appears exactly once. "
                f"Duplicated numbers: {dup_str}."
            ),
            timestamp=timestamp,
        ))

    # ── scene contiguity ──
    if scenes:
        actual_numbers = sorted(set(s.number for s in scenes))
        expected = list(range(1, len(actual_numbers) + 1))
        if actual_numbers != expected:
            actual_str = ', '.join(str(n) for n in actual_numbers)
            findings.append(create_finding(
                finding_id="synopsis_scene_contiguity_0001",
                auditor="synopsis_auditor",
                gate="synopsis",
                pass_name="scene_contiguity",
                class_="A",
                tier="1",
                category="structure",
                description=(
                    f"Scene numbers are not contiguous 1..N. "
                    f"Found: [{actual_str}], expected: [{', '.join(str(n) for n in expected)}]."
                ),
                location={"type": "whole_artifact", "synopsis_path": synopsis_path},
                evidence=None,
                confidence="HIGH",
                fix_action="auto_fix",
                suggested_fix=(
                    f"Renumber scenes to form a contiguous sequence starting at 1. "
                    f"Current numbers: [{actual_str}]."
                ),
                timestamp=timestamp,
            ))

    return findings


# ── Banned-phrases file validation ───────────────────────────────────────────

_RECOGNIZED_SCHEMA_VERSIONS = {"1.0.0"}
_PERMANENT_BANNED_NAMES = ["Sarah", "Chen", "Marcus", "Webb"]


def validate_banned_phrases_file(banned_path: str) -> Tuple[Optional[dict], List[dict]]:
    """Validate banned_phrases.json per Data Standards §4.7.

    Returns (parsed_data_or_None, findings_list). If validation passes,
    returns (parsed_dict, []). If validation fails at a structural level
    (missing file, bad JSON, wrong top-level type), returns (None, findings).
    Field-level failures still return (None, findings) to prevent the
    matching passes from running on malformed data.

    Reusable from preflight.py.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    findings = []
    location = {"type": "whole_artifact", "path": banned_path}

    def _finding(finding_id, pass_name, description, suggested_fix, evidence=None):
        return create_finding(
            finding_id=finding_id,
            auditor="synopsis_auditor",
            gate="synopsis",
            pass_name=pass_name,
            class_="A",
            tier="3",
            category="intake_integrity",
            description=description,
            location=location,
            evidence=evidence,
            confidence="HIGH",
            fix_action="route_to_rewrite",
            suggested_fix=suggested_fix,
            timestamp=timestamp,
        )

    # ── file exists? ──
    if not os.path.exists(banned_path):
        findings.append(_finding(
            "synopsis_banned_file_missing_0001",
            "banned_phrases_file_present",
            f"Banned phrases file not found: {banned_path}",
            f"Create {banned_path} with schema_version, names, and phrases keys per Data Standards §4.7.",
        ))
        return (None, findings)

    # ── parseable JSON? ──
    try:
        with open(banned_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        findings.append(_finding(
            "synopsis_banned_bad_json_0001",
            "banned_phrases_file_valid_json",
            f"Banned phrases file is not valid JSON: {e}",
            "Fix the JSON syntax error in the banned phrases file.",
        ))
        return (None, findings)

    # ── top-level dict? ──
    if not isinstance(data, dict):
        findings.append(_finding(
            "synopsis_banned_not_dict_0001",
            "banned_phrases_top_level_type",
            f"Banned phrases file top-level value is {type(data).__name__}, expected dict.",
            "Rewrite the banned phrases file so the top-level value is a JSON object, not an array or scalar.",
        ))
        return (None, findings)

    # ── required keys ──
    has_field_errors = False
    for key in ("schema_version", "names", "phrases"):
        if key not in data:
            has_field_errors = True
            findings.append(_finding(
                f"synopsis_banned_missing_key_{key}_0001",
                "banned_phrases_required_key",
                f"Banned phrases file is missing required key: {key!r}.",
                f"Add the {key!r} key to {banned_path} per Data Standards §4.7.",
                evidence={"missing_key": key},
            ))

    # ── schema_version recognized? ──
    sv = data.get("schema_version")
    if sv is not None and sv not in _RECOGNIZED_SCHEMA_VERSIONS:
        has_field_errors = True
        findings.append(_finding(
            "synopsis_banned_unknown_version_0001",
            "banned_phrases_schema_version",
            f"Unrecognized schema_version: {sv!r}. Recognized: {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}.",
            f"Set schema_version to one of {sorted(_RECOGNIZED_SCHEMA_VERSIONS)}.",
            evidence={"schema_version": sv},
        ))

    # ── names and phrases are lists? ──
    for key in ("names", "phrases"):
        val = data.get(key)
        if val is not None and not isinstance(val, list):
            has_field_errors = True
            findings.append(_finding(
                f"synopsis_banned_{key}_not_array_0001",
                "banned_phrases_array_type",
                f"Banned phrases file {key!r} is {type(val).__name__}, expected array.",
                f"Set {key!r} to a JSON array of strings in {banned_path}.",
            ))

    # ── items in names/phrases are strings? ──
    for key in ("names", "phrases"):
        val = data.get(key)
        if isinstance(val, list):
            bad_count = sum(1 for item in val if not isinstance(item, str))
            if bad_count > 0:
                has_field_errors = True
                findings.append(_finding(
                    f"synopsis_banned_{key}_bad_items_0001",
                    "banned_phrases_item_type",
                    f"Banned phrases file {key!r} contains {bad_count} non-string item(s).",
                    f"Ensure all items in {key!r} are strings.",
                    evidence={"list": key, "non_string_count": bad_count},
                ))

    # ── permanent names present? ──
    names_val = data.get("names")
    if isinstance(names_val, list):
        names_set = set(names_val)
        missing = [n for n in _PERMANENT_BANNED_NAMES if n not in names_set]
        if missing:
            has_field_errors = True
            findings.append(_finding(
                "synopsis_banned_permanent_missing_0001",
                "banned_phrases_permanent_names",
                f"Banned phrases file is missing ANPD-permanent name(s): {', '.join(missing)}.",
                f"Add the missing name(s) to the names array: {', '.join(missing)}.",
            ))

    # ── empty names? ──
    if isinstance(names_val, list) and len(names_val) == 0:
        has_field_errors = True
        findings.append(_finding(
            "synopsis_banned_names_empty_0001",
            "banned_phrases_names_empty",
            "Banned phrases file has empty names array.",
            "Add at least the four ANPD-permanent names (Sarah, Chen, Marcus, Webb) to the names array.",
        ))

    if has_field_errors:
        return (None, findings)
    return (data, findings)


# ── Banned-content detection passes ──────────────────────────────────────────

def _match_snippet(text: str, match: re.Match, context_chars: int = 25) -> str:
    """Extract a short context snippet centered on a regex match."""
    start = max(0, match.start() - context_chars)
    end = min(len(text), match.end() + context_chars)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def check_banned_names(scenes: List[Scene], banned_data: dict, synopsis_path: str) -> List[dict]:
    """Detect banned names in scene bodies. Detection only — no substitution.

    One finding per (banned_name, scene_number) pair. Multiple occurrences
    within one scene produce one finding with the count in evidence.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    findings = []
    names = banned_data.get("names", [])

    compiled = [(name, re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)) for name in names]

    for scene in scenes:
        for name, pattern in compiled:
            matches = list(pattern.finditer(scene.body))
            if matches:
                snippet = _match_snippet(scene.body, matches[0])
                findings.append(create_finding(
                    finding_id=f"synopsis_banned_name_{name.lower()}_{scene.number:04d}",
                    auditor="synopsis_auditor",
                    gate="synopsis",
                    pass_name="banned_name_in_synopsis",
                    class_="A",
                    tier="3",
                    category="banned_content",
                    description=(
                        f"Banned name {name!r} found in scene {scene.number} "
                        f"({len(matches)} occurrence(s))."
                    ),
                    location={
                        "type": "scene",
                        "scene_number": scene.number,
                        "synopsis_path": synopsis_path,
                    },
                    evidence={
                        "match_count": len(matches),
                        "snippet": snippet,
                    },
                    confidence="HIGH",
                    fix_action="route_to_rewrite",
                    suggested_fix=(
                        f"Rename the character currently called {name!r} in scene "
                        f"{scene.number} to a name not on the banned list."
                    ),
                    timestamp=timestamp,
                ))

    return findings


def check_banned_phrases(scenes: List[Scene], banned_data: dict, synopsis_path: str) -> List[dict]:
    """Detect banned phrases in scene bodies. Detection only — no substitution.

    Same structure and convention as check_banned_names.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    findings = []
    phrases = banned_data.get("phrases", [])

    compiled = [(phrase, re.compile(r'\b' + re.escape(phrase) + r'\b', re.IGNORECASE)) for phrase in phrases]

    for scene in scenes:
        for phrase, pattern in compiled:
            matches = list(pattern.finditer(scene.body))
            if matches:
                snippet = _match_snippet(scene.body, matches[0])
                findings.append(create_finding(
                    finding_id=f"synopsis_banned_phrase_{scene.number:04d}",
                    auditor="synopsis_auditor",
                    gate="synopsis",
                    pass_name="banned_phrase_in_synopsis",
                    class_="A",
                    tier="3",
                    category="banned_content",
                    description=(
                        f"Banned phrase {phrase!r} found in scene {scene.number} "
                        f"({len(matches)} occurrence(s))."
                    ),
                    location={
                        "type": "scene",
                        "scene_number": scene.number,
                        "synopsis_path": synopsis_path,
                    },
                    evidence={
                        "match_count": len(matches),
                        "snippet": snippet,
                    },
                    confidence="HIGH",
                    fix_action="route_to_rewrite",
                    suggested_fix=(
                        f"Rephrase scene {scene.number} to remove the banned "
                        f"phrase {phrase!r}."
                    ),
                    timestamp=timestamp,
                ))

    return findings


# ── Chapter count check ────────────────────────────────────────────────────────

def check_chapter_count(synopsis_text, effective_config, synopsis_path):
    """Deterministic chapter-count check against effective config.

    Counts ### Chapter N markers in the synopsis and compares to
    effective_config['target_chapter_count']. Emits a finding on mismatch.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    findings = []

    expected = effective_config['target_chapter_count']
    if expected is None:
        return findings

    chapter_markers = re.findall(r'^### Chapter \d+', synopsis_text, re.MULTILINE)
    actual = len(chapter_markers)

    if actual != expected:
        findings.append(create_finding(
            finding_id="synopsis_chapter_count_0001",
            auditor="synopsis_auditor",
            gate="synopsis",
            pass_name="chapter_count",
            class_="A",
            tier="3",
            category="structure",
            description=(
                f"Synopsis contains {actual} chapter markers; "
                f"expected {expected} per series_config genre template."
            ),
            location={"type": "whole_artifact", "synopsis_path": synopsis_path},
            evidence={"actual_count": actual, "expected_count": expected},
            confidence="HIGH",
            fix_action="route_to_rewrite",
            suggested_fix=(
                f"Regenerate the synopsis with exactly {expected} chapter markers, "
                f"or update series_config.structural_overrides if the book "
                f"intentionally deviates from genre defaults."
            ),
            timestamp=timestamp,
        ))

    return findings


# ── API client ────────────────────────────────────────────────────────────────

def call_haiku(prompt, system_prompt, model="claude-haiku-4-5", max_tokens=8000):
    """Call the Anthropic API for synopsis audit passes via llm_client."""
    from llm_client import call_llm
    response = call_llm(
        provider="anthropic",
        model=model,
        system=system_prompt,
        user=prompt,
        max_tokens=max_tokens,
    )
    return response.text


# ── File loaders ──────────────────────────────────────────────────────────────

def find_latest(directory, pattern):
    matches = glob.glob(os.path.join(directory, pattern))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def load_text(path):
    with open(path) as f:
        return f.read()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def clean_json_response(raw):
    """Strip markdown fences and parse JSON from model response."""
    clean = raw.strip()
    if clean.startswith('```'):
        clean = '\n'.join(clean.split('\n')[1:])
    if clean.endswith('```'):
        clean = '\n'.join(clean.split('\n')[:-1])
    clean = clean.strip()
    return json.loads(clean)


# ── Rubric definitions ───────────────────────────────────────────────────────
# Rubric text is built by functions that interpolate effective_config values.
# Per White Paper §2.1 (single-canonical-source): thresholds come from the
# genre template via series_config, not from hardcoded constants.

def _build_call_1_rubric(effective_config):
    """Build Call 1 rubric text with effective_config values interpolated."""
    t1 = effective_config['twist_1_position']
    t2 = effective_config['twist_2_position']
    t3 = effective_config['twist_3_position']
    action_min = int(effective_config['action_scene_percentage_min'] * 100)

    return f"""[Q1] Does the synopsis cover all elements in the intake form?
- Series engine, protagonist core, antagonist, victim/client, setting, emotional core
- All inviolable rules from intake are visible in the synopsis
- FAIL if any intake element is absent from the synopsis

[Q2] Does the synopsis follow correct story structure?
- Twist 1 must land at end of Act 1 (approximately {t1 - 5}-{t1 + 5}% through synopsis)
- Twist 2 must land at midpoint (approximately {t2 - 5}-{t2 + 5}% through synopsis)
- Twist 3 must land at end of Act 2 (approximately {t3 - 5}-{t3 + 5}% through synopsis)
- Hero's lowest point must be in Act 3 (after Twist 3)
- Final battle must be identifiable and positioned after lowest point
- FAIL if any twist is missing, misplaced by more than 10% of total synopsis length,
  or delivered as exposition rather than operational discovery

[Q5] Do the characters in the synopsis follow their character profiles?
- Protagonist behavior matches inviolable rules from character profile
- Antagonist motivation is internally consistent with their profile
- Supporting characters operate within their established limitations
- FAIL if any character violates their profile rules

[Q9] What percentage of scenes are action scenes?
- Count total scenes
- Count action scenes: a scene counts as an action scene if and only if its header
  TYPE tag is [TYPE: ACTION] or [TYPE: SUSPENSE]. Count these tags directly from the
  scene headers; do not re-judge scene content.
- Calculate percentage
- FAIL if action scenes are below {action_min}% of total scene count
- Report exact count and percentage regardless of verdict

[Q12] Is the final battle the largest and most complex scene in the synopsis?
- The final battle should have more events, more characters active, and higher
  simultaneous physical and emotional stakes than any other scene
- FAIL if any earlier scene appears larger or more complex than the final battle

[Q19] Post-climax scene classification: DENOUEMENT scene count bands.
- Identify all scenes after the antagonist is neutralized and the central conflict is resolved.
- Classify EACH post-climax scene as one of:
  AFTERMATH — active stakes remain (urgency, movement, extraction completion, medical jeopardy, pursuit, adrenaline)
  DENOUEMENT — stakes spent (reflection, farewells, prognosis delivered as closure, coda, character-arc compression)
- Calibration anchor: in the published CSAR ending, Scene 97 (aid station, medics swarm, urgency) = AFTERMATH.
- For each post-climax scene, output a one-line justification for your classification.
- AFTERMATH scenes are unlimited and unpenalized.
- Severity bands for DENOUEMENT count:
    0–1 DENOUEMENT → FAIL (deficit: resolution feels abrupt or missing)
    2   DENOUEMENT → PASS (ideal: clean closure without overstay)
    3   DENOUEMENT → WEAK (advisory: borderline — acceptable if each scene is load-bearing)
    4+  DENOUEMENT → FAIL (excess: resolution drags, energy dissipates)
- Report the per-scene classification table regardless of verdict.

Return Q19 data in this format within the items array:
  {{"id": "Q19", "verdict": "PASS or FAIL", "note": "...",
    "post_climax_scenes": [
      {{"scene": 97, "classification": "AFTERMATH", "justification": "..."}},
      {{"scene": 98, "classification": "DENOUEMENT", "justification": "..."}}
    ]}}"""


def _build_call_2_rubric(effective_config):
    """Build Call 2 rubric text with effective_config values interpolated."""
    word_min = effective_config['target_synopsis_word_min']
    word_max = effective_config['target_synopsis_word_max']

    return f"""[Q8] Is the synopsis within the target word count range of {word_min:,}–{word_max:,} words?
- Use the pre-computed word count provided in the instructions
- FAIL if below {word_min:,} words or above {word_max:,} words
- Report exact word count regardless of verdict

[Q10] Does the synopsis accelerate and gain momentum from beginning to end?
- Act 1 should establish situation and move toward inciting incident
- Act 2 should escalate through complications toward the lowest point
- Act 3 should move faster than Act 2
- WEAK if any act feels slower than the preceding act
- FAIL if the synopsis decelerates in Act 3

[Q13] Is the final battle emotionally satisfying?
- The final battle must resolve both the external conflict AND the protagonist's
  internal wound simultaneously
- The resolution must feel earned by everything that preceded it
- WEAK if emotional resolution feels separate from physical resolution
- FAIL if the final battle resolves only externally with no emotional component

[Q16] Does the synopsis foreshadow future events prematurely?
- No scene should imply outcomes the protagonist does not yet know
- No scene should contain information that makes a later twist feel redundant
- FAIL if any scene reveals information that should arrive as a twist

[Q17] Does the synopsis reveal too much of the story too early?
- Act 1 should establish stakes without revealing their full scale
- The full threat should only become visible at its designated reveal point
- FAIL if the central antagonist's full plan is visible before Act 2

[Q18] Is there a strong organic mystery element?
- Is there a central unknown the reader wants resolved?
- Is that unknown discovered through the protagonist's active investigation
  rather than revealed through declaration or coincidence?
- Is the mystery inseparable from the plot — would removing it collapse the story?
- FAIL if the mystery is a contrivance (information withheld artificially)
- FAIL if there is no identifiable mystery element"""


# Maps rubric item IDs to finding metadata.
# Each entry: pass_name, category, location_type, suggested_fix template.
# The suggested_fix here is the Tier 3 routing default — most synopsis-gate
# rubric items in this auditor are interpretive (require synopsis revision
# or operator review) rather than mechanically auto-fixable. When Tier 1/2
# auto-fix handlers are added in later tasks, this table is updated.
RUBRIC_METADATA = {
    "Q1":  {"pass_name": "intake_coverage",        "category": "completeness",       "location_type": "whole_artifact", "suggested_fix": "Revise synopsis to include the missing intake elements identified in the description. Route to operator if intake itself is incomplete."},
    "Q2":  {"pass_name": "twist_placement",        "category": "structure",          "location_type": "scene",          "suggested_fix": "Reposition the cited twist scene to its required act position, or rewrite the scene so the twist functions as operational discovery rather than exposition."},
    "Q5":  {"pass_name": "character_profile_fidelity","category": "character",        "location_type": "scene",          "suggested_fix": "Revise the cited scene so the character behavior matches the inviolable rules in the character profile, or update the profile if the synopsis behavior is correct."},
    "Q8":  {"pass_name": "synopsis_word_count",    "category": "completeness",       "location_type": "whole_artifact", "suggested_fix": "Adjust synopsis word count to fall within the target range of {target_synopsis_word_min:,}–{target_synopsis_word_max:,} words. Word count outside this range indicates either insufficient detail (below) or padding (above) for downstream generation."},
    "Q9":  {"pass_name": "action_scene_percentage","category": "pacing",             "location_type": "whole_artifact", "suggested_fix": "Increase action-scene proportion to at least {action_pct_min}% by converting non-action scenes to action or by adding action scenes. See action-scene definition in rubric."},
    "Q10": {"pass_name": "story_acceleration",     "category": "pacing",             "location_type": "whole_artifact", "suggested_fix": "Revise the decelerating act so it moves faster than the act preceding it. Compress non-essential beats or escalate stakes."},
    "Q12": {"pass_name": "final_battle_complexity","category": "structure",          "location_type": "scene",          "suggested_fix": "Revise so the final battle has more events, more characters active, and higher simultaneous stakes than any earlier scene, or reduce the cited earlier scene's scope."},
    "Q13": {"pass_name": "emotional_payoff",       "category": "emotional_architecture","location_type": "scene",        "suggested_fix": "Revise the final battle to resolve the protagonist's internal wound simultaneously with the external conflict. Emotional resolution must be inseparable from physical resolution."},
    "Q16": {"pass_name": "premature_foreshadowing","category": "information_control","location_type": "scene",          "suggested_fix": "Revise the cited scene to remove information the protagonist does not yet know, or restructure so the foreshadowed event is no longer a twist."},
    "Q17": {"pass_name": "premature_reveal",       "category": "information_control","location_type": "scene",          "suggested_fix": "Revise Act 1 to establish stakes without revealing the antagonist's full plan. Move the full reveal to its designated point in Act 2 or later."},
    "Q18": {"pass_name": "mystery_integrity",      "category": "structure",          "location_type": "whole_artifact", "suggested_fix": "Add or strengthen the central mystery so it is discovered through active investigation and is inseparable from the plot. If the mystery is contrived, revise to remove the artificial information withhold."},
    "Q19": {"pass_name": "denouement_scene_count", "category": "structure",          "location_type": "whole_artifact", "suggested_fix": "Adjust to exactly 2 DENOUEMENT scenes post-climax. AFTERMATH scenes (active stakes) are unlimited. Only DENOUEMENT (stakes spent: reflection, closure, coda) is counted."},
}


# Verdicts the auditor accepts from Haiku. Any verdict outside this set
# raises ValueError in items_to_findings — White Paper §2.1, no silent fail.
VALID_VERDICTS = {"PASS", "WEAK", "FAIL", "N/A"}
SKIP_VERDICTS = {"PASS", "N/A"}      # known-good, no finding needed
FINDING_VERDICTS = {"WEAK", "FAIL"}  # produce findings


SYSTEM_PROMPT = """You are a synopsis auditor for a commercial fiction production system. You evaluate synopses before generation begins. Be specific and honest. Cite scene numbers for any finding. Return only valid JSON."""


# ── Prompt builders ──────────────────────────────────────────────────────────

def build_call_1_prompt(synopsis, intake_json, character_profiles, effective_config):
    """Build prompt for Call 1: structural and mechanical checks."""
    intake_str = json.dumps(intake_json, indent=2)
    profiles_str = json.dumps(character_profiles, indent=2) if isinstance(character_profiles, dict) else str(character_profiles)
    profiles_str = profiles_str[:20000]
    rubric = _build_call_1_rubric(effective_config)

    return f"""You are running Synopsis Audit — Call 1 of 2: Structural and Mechanical Checks.

=== INTAKE FORM ===
{intake_str}

=== CHARACTER PROFILES ===
{profiles_str}

=== RUBRIC ITEMS ===
Score each item: PASS / WEAK / FAIL

{rubric}

=== SYNOPSIS ===
{synopsis}

=== INSTRUCTIONS ===
Score ONLY the rubric items listed above. For any WEAK or FAIL, cite the specific scene number and describe the issue.

For Q9, count every scene in the synopsis and classify each as action or non-action. Report exact counts.
For Q19, identify all post-climax scenes and classify each as AFTERMATH or DENOUEMENT per the rubric. Include the post_climax_scenes array in your Q19 item.

Return your results as this exact JSON structure:

{{
  "call": 1,
  "focus": "Structural and Mechanical Checks",
  "total_scenes": 0,
  "action_scenes": 0,
  "action_scene_percentage": 0.0,
  "resolution_scenes": 0,
  "items": [
    {{
      "id": "Q1",
      "verdict": "PASS or WEAK or FAIL",
      "note": "specific finding with scene citation, or empty string if PASS"
    }}
  ]
}}

Output ONLY the JSON. No preamble, no markdown fences."""


def build_call_2_prompt(synopsis, intake_json, character_profiles, call_1_results, word_count, effective_config):
    """Build prompt for Call 2: quality and engagement checks."""
    intake_str = json.dumps(intake_json, indent=2)
    profiles_str = json.dumps(character_profiles, indent=2) if isinstance(character_profiles, dict) else str(character_profiles)
    profiles_str = profiles_str[:20000]
    call_1_str = json.dumps(call_1_results, indent=2)
    rubric = _build_call_2_rubric(effective_config)

    return f"""You are running Synopsis Audit — Call 2 of 2: Quality and Engagement Checks.

ACTUAL WORD COUNT (pre-computed): {word_count:,}

=== INTAKE FORM ===
{intake_str}

=== CHARACTER PROFILES ===
{profiles_str}

=== CALL 1 RESULTS (for context) ===
{call_1_str}

=== RUBRIC ITEMS ===
Score each item: PASS / WEAK / FAIL

{rubric}

=== SYNOPSIS ===
{synopsis}

=== INSTRUCTIONS ===
Score ONLY the rubric items listed above. For any WEAK or FAIL, cite the specific scene number and describe the issue.

For Q8, use the pre-computed word count provided above. Do not re-count.

Return your results as this exact JSON structure:

{{
  "call": 2,
  "focus": "Quality and Engagement Checks",
  "synopsis_word_count": {word_count},
  "items": [
    {{
      "id": "Q8",
      "verdict": "PASS or WEAK or FAIL",
      "note": "specific finding with scene citation, or empty string if PASS"
    }}
  ]
}}

Output ONLY the JSON. No preamble, no markdown fences."""


# ── Consolidator ──────────────────────────────────────────────────────────────

def _count_denouement_scenes(q19_item: dict) -> tuple[int, str]:
    """Count DENOUEMENT scenes from LLM's per-scene classification in a Q19 item.

    Returns (denouement_count, classification_table_string).
    Raises ValueError if no structured post_climax_scenes data — caller must
    re-prompt or verdict ERROR; never fabricate a count from note text.
    """
    scenes = q19_item.get('post_climax_scenes', [])
    if not scenes:
        raise ValueError(
            "Q19 response missing structured post_climax_scenes array. "
            "Cannot determine DENOUEMENT count without per-scene classification data."
        )

    denouement_count = sum(
        1 for s in scenes
        if s.get('classification', '').upper() == 'DENOUEMENT'
    )
    aftermath_count = sum(
        1 for s in scenes
        if s.get('classification', '').upper() == 'AFTERMATH'
    )

    lines = [f"Per-scene table ({len(scenes)} post-climax scenes, "
             f"{aftermath_count} AFTERMATH, {denouement_count} DENOUEMENT):"]
    for s in scenes:
        lines.append(
            f"  Scene {s.get('scene', '?')}: {s.get('classification', '?')} "
            f"— {s.get('justification', 'no justification')}"
        )
    return (denouement_count, " ".join(lines))


def consolidate(call_1_data, call_2_data, title, word_count, effective_config):
    """Merge Call 1 and Call 2 results into final output."""
    all_items = []
    for item in call_1_data.get('items', []):
        all_items.append(item)
    for item in call_2_data.get('items', []):
        all_items.append(item)

    # Q8 deterministic override: recalibrated word-count severity bands.
    # Floor set from published-book evidence (CSAR synopsis 14,487w → 92.7K ms).
    # <13,000 → FAIL; 13,000–17,999 → WEAK (advisory); 18,000–28,000 → PASS; >28,000 → FAIL.
    word_min = effective_config['target_synopsis_word_min']
    word_max = effective_config['target_synopsis_word_max']
    Q8_HARD_FLOOR = 13000
    for item in all_items:
        if item.get('id') == 'Q8':
            if word_count < Q8_HARD_FLOOR:
                item['verdict'] = 'FAIL'
                item['note'] = (
                    f"Synopsis word count is {word_count:,} words. "
                    f"Hard floor is {Q8_HARD_FLOOR:,} words. "
                    f"FAIL: {Q8_HARD_FLOOR - word_count:,} words below minimum. "
                    f"(Calibration: published-book floor from CSAR synopsis 14,487w → 92.7K ms.)"
                )
            elif word_count < word_min:
                item['verdict'] = 'WEAK'
                item['note'] = (
                    f"Synopsis word count is {word_count:,} words. "
                    f"Target range is {word_min:,}\u2013{word_max:,} words; "
                    f"hard floor is {Q8_HARD_FLOOR:,}. "
                    f"WEAK (advisory): {word_min - word_count:,} words below target minimum, "
                    f"but above published-book floor. "
                    f"(Calibration: CSAR synopsis 14,487w → 92.7K ms.)"
                )
            elif word_count <= word_max:
                item['verdict'] = 'PASS'
            # >word_max keeps existing LLM verdict (FAIL)
            break

    # Q19 deterministic override: count DENOUEMENT scenes from LLM classifications.
    # AFTERMATH scenes are unlimited.  Severity bands:
    #   0–1 → FAIL (deficit)   2 → PASS   3 → WEAK (advisory)   4+ → FAIL (excess)
    # Raises ValueError if structured data missing — caller handles re-prompt.
    for item in all_items:
        if item.get('id') == 'Q19':
            denouement_count, classification_table = _count_denouement_scenes(item)
            if denouement_count == 2:
                item['verdict'] = 'PASS'
                item['note'] = (
                    f"Post-climax classification: {denouement_count} DENOUEMENT scenes "
                    f"(2 ideal). {classification_table}"
                )
            elif denouement_count == 3:
                item['verdict'] = 'WEAK'
                item['note'] = (
                    f"Post-climax classification: {denouement_count} DENOUEMENT scenes "
                    f"(2 ideal, 3 acceptable if each is load-bearing). WEAK: advisory. "
                    f"{classification_table}"
                )
            elif denouement_count <= 1:
                item['verdict'] = 'FAIL'
                item['note'] = (
                    f"Post-climax classification: {denouement_count} DENOUEMENT scenes "
                    f"(2 ideal, minimum 2 required). FAIL: deficit. "
                    f"{classification_table}"
                )
            else:  # 4+
                item['verdict'] = 'FAIL'
                item['note'] = (
                    f"Post-climax classification: {denouement_count} DENOUEMENT scenes "
                    f"(2 ideal, maximum 3 allowed). FAIL: excess. "
                    f"{classification_table}"
                )
            break

    fails = [i for i in all_items if i.get('verdict') == 'FAIL']
    weaks = [i for i in all_items if i.get('verdict') == 'WEAK']
    passes = [i for i in all_items if i.get('verdict') in ('PASS', 'N/A')]

    total_scenes = call_1_data.get('total_scenes', 0)
    action_scenes = call_1_data.get('action_scenes', 0)
    action_pct = call_1_data.get('action_scene_percentage', 0.0)
    resolution_scenes = call_1_data.get('resolution_scenes', 0)

    verdict = "FAIL" if fails else "PASS"

    # Build JSON output
    output_json = {
        "title": title,
        "synopsis_word_count": word_count,
        "total_scenes": total_scenes,
        "action_scenes": action_scenes,
        "action_scene_percentage": action_pct,
        "resolution_scenes": resolution_scenes,
        "verdict": verdict,
        "items": all_items,
        "fails": [i['id'] for i in fails],
        "weaks": [i['id'] for i in weaks],
    }

    # Build markdown report
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = []
    lines.append(f"# Synopsis Audit — {title} — {now}")
    lines.append("")
    word_min = effective_config['target_synopsis_word_min']
    word_max = effective_config['target_synopsis_word_max']
    action_min = int(effective_config['action_scene_percentage_min'] * 100)
    lines.append(f"Word count: {word_count:,} (target range: {word_min:,}–{word_max:,})")
    lines.append(f"Total scenes: {total_scenes}")
    lines.append(f"Action scenes: {action_scenes} ({action_pct:.0f}%) — minimum {action_min}%")
    lines.append(f"Resolution scenes: {resolution_scenes} — required: exactly 2")
    lines.append("")

    if fails:
        lines.append("## FAIL (must fix before production run)")
        for i in fails:
            lines.append(f"- [{i['id']}] {i.get('note', '')}")
        lines.append("")

    if weaks:
        lines.append("## WEAK (review before production run)")
        for i in weaks:
            lines.append(f"- [{i['id']}] {i.get('note', '')}")
        lines.append("")

    lines.append("## PASS")
    pass_ids = sorted([i['id'] for i in passes])
    lines.append(', '.join(pass_ids) if pass_ids else 'None')
    lines.append("")

    lines.append("## Overall Verdict")
    lines.append(f"**{verdict}**")
    lines.append("")
    if verdict == "FAIL":
        lines.append("FAIL = pipeline blocked. Fix synopsis and re-run before proceeding.")
    else:
        lines.append("PASS = synopsis approved for production run.")
        if weaks:
            lines.append(f"  {len(weaks)} WEAK items — review before proceeding.")
    lines.append("")
    lines.append("---")
    lines.append(f"*ANPD V23 Synopsis Auditor — synopsis_auditor.py 20260331_1100*")
    lines.append("*Copyright (c) 2026 Endeavor Publishing LLC*")

    return output_json, '\n'.join(lines), verdict


# ── Finding converter ────────────────────────────────────────────────────────

def _interpolate_suggested_fix(template, effective_config):
    """Interpolate RUBRIC_METADATA suggested_fix templates with effective_config values.

    Builds a substitution dict from config_resolver fields, then applies
    str.format_map. Unknown placeholders are left as-is (no KeyError).
    """
    action_pct_min = int(effective_config['action_scene_percentage_min'] * 100)
    subs = {
        'target_synopsis_word_min': effective_config['target_synopsis_word_min'],
        'target_synopsis_word_max': effective_config['target_synopsis_word_max'],
        'action_pct_min': action_pct_min,
    }
    try:
        return template.format_map(subs)
    except (KeyError, ValueError):
        return template


def items_to_findings(call_1_data, call_2_data, synopsis_path, effective_config):
    """Convert PASS/WEAK/FAIL rubric items into V24 findings.

    Only WEAK and FAIL items become findings. PASS and N/A items do not
    generate findings (no problem to record). Each finding is constructed
    via create_finding so the schema is enforced at construction time.

    Returns a list of finding dicts.

    Raises ValueError if a rubric item ID is encountered that has no entry
    in RUBRIC_METADATA — fail loudly per White Paper §2.1, no silent
    degradation.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    findings = []
    counter = 0

    all_items = []
    for item in call_1_data.get('items', []):
        all_items.append(item)
    for item in call_2_data.get('items', []):
        all_items.append(item)

    for item in all_items:
        verdict = item.get('verdict')

        if verdict not in VALID_VERDICTS:
            rubric_id = item.get('id', '<missing id>')
            raise ValueError(
                f"Rubric item {rubric_id!r} returned unrecognized verdict "
                f"{verdict!r}. Valid verdicts: {sorted(VALID_VERDICTS)}. "
                "Either Haiku returned malformed output or a new verdict was "
                "added without updating VALID_VERDICTS."
            )

        if verdict in SKIP_VERDICTS:
            continue

        # verdict is WEAK or FAIL — produce a finding
        rubric_id = item.get('id')
        if rubric_id not in RUBRIC_METADATA:
            raise ValueError(
                f"Rubric item {rubric_id!r} has no entry in RUBRIC_METADATA. "
                "Add the metadata mapping before this auditor can run."
            )

        meta = RUBRIC_METADATA[rubric_id]
        counter += 1

        note = item.get('note', '')
        if not note or not note.strip():
            description = f"{rubric_id} {verdict} (no detail provided)"
        else:
            description = note

        suggested_fix = _interpolate_suggested_fix(meta['suggested_fix'], effective_config)

        finding = create_finding(
            finding_id=f"synopsis_{meta['pass_name']}_{counter:04d}",
            auditor="synopsis_auditor",
            gate="synopsis",
            pass_name=meta['pass_name'],
            class_="A" if verdict == "FAIL" else "B",
            tier="3",
            category=meta['category'],
            description=description,
            location={
                "type": meta['location_type'],
                "rubric_id": rubric_id,
                "synopsis_path": synopsis_path,
            },
            evidence=None,
            confidence="HIGH" if verdict == "FAIL" else "MEDIUM",
            fix_action="route_to_rewrite",
            suggested_fix=suggested_fix,
            timestamp=timestamp,
        )
        findings.append(finding)

    return findings


# ── Main ──────────────────────────────────────────────────────────────────────

# ── Multi-pass helpers ────────────────────────────────────────────────────────

VERDICT_SEVERITY = {'PASS': 0, 'N/A': 0, 'WEAK': 1, 'FAIL': 2, 'ERROR': 3}
NUM_PASSES = 3


def _run_single_audit_pass(
    synopsis, intake, character_profiles, effective_config, parsed_scenes, pass_number
):
    """Run Call 1 + Call 2 once. Returns (call_1_data, call_2_data).

    Raises RuntimeError on unrecoverable API/JSON failures.
    """
    audit_model = effective_config['model_synopsis_audit']
    call_1_prompt = build_call_1_prompt(synopsis, intake, character_profiles, effective_config)
    start = time.time()

    # Call 1
    try:
        raw_1 = call_haiku(call_1_prompt, SYSTEM_PROMPT, model=audit_model, max_tokens=8000)
        call_1_data = clean_json_response(raw_1)
        call_1_data['_elapsed_seconds'] = round(time.time() - start, 1)
    except json.JSONDecodeError:
        retry_prompt = call_1_prompt + "\n\nReturn only valid JSON. No preamble, no code fences, no commentary."
        raw_1 = call_haiku(retry_prompt, SYSTEM_PROMPT, model=audit_model, max_tokens=8000)
        call_1_data = clean_json_response(raw_1)
        call_1_data['_elapsed_seconds'] = round(time.time() - start, 1)

    # Q19 structured-data enforcement: re-prompt up to 2 times if missing
    q19_item = None
    for item in call_1_data.get('items', []):
        if item.get('id') == 'Q19':
            q19_item = item
            break

    if q19_item and not q19_item.get('post_climax_scenes'):
        for retry in range(2):
            print(f"    Pass {pass_number}: Q19 missing structured post_climax_scenes, re-prompting ({retry + 1}/2)...")
            reprompt = call_1_prompt + (
                "\n\nCRITICAL: Your Q19 response MUST include a 'post_climax_scenes' array. "
                "Each element must have 'scene' (integer), 'classification' ('AFTERMATH' or 'DENOUEMENT'), "
                "and 'justification' (string). Without this array the audit cannot proceed."
            )
            try:
                raw_retry = call_haiku(reprompt, SYSTEM_PROMPT, model=audit_model, max_tokens=8000)
                retry_data = clean_json_response(raw_retry)
                for ri in retry_data.get('items', []):
                    if ri.get('id') == 'Q19' and ri.get('post_climax_scenes'):
                        q19_item.update(ri)
                        print(f"    Pass {pass_number}: Q19 structured data obtained on retry {retry + 1}")
                        break
                if q19_item.get('post_climax_scenes'):
                    break
            except (json.JSONDecodeError, Exception) as e:
                print(f"    Pass {pass_number}: Q19 re-prompt {retry + 1} failed: {e}")

        if not q19_item.get('post_climax_scenes'):
            q19_item['verdict'] = 'ERROR'
            q19_item['note'] = (
                "Q19 ERROR: LLM failed to produce structured post_climax_scenes array "
                "after 2 re-prompts. Cannot determine DENOUEMENT count."
            )

    # Q9 deterministic override
    _action_n = sum(1 for s in parsed_scenes if s.scene_type in ("ACTION", "SUSPENSE"))
    _total_n = len(parsed_scenes)
    _action_pct = round(100.0 * _action_n / _total_n, 1) if _total_n else 0.0
    _action_min_pct = effective_config['action_scene_percentage_min'] * 100
    call_1_data['action_scenes'] = _action_n
    call_1_data['action_scene_percentage'] = _action_pct
    call_1_data['total_scenes'] = _total_n
    for _it in call_1_data.get('items', []):
        if _it.get('id') == 'Q9':
            _it['verdict'] = 'PASS' if _action_pct >= _action_min_pct else 'FAIL'
            _it['note'] = (f"Action scenes (ACTION+SUSPENSE tags): {_action_n} of {_total_n} = {_action_pct}%. "
                           f"Minimum {int(_action_min_pct)}%. Counted deterministically from [TYPE:] tags.")

    # Call 2
    word_count = len(synopsis.split())
    call_2_prompt = build_call_2_prompt(synopsis, intake, character_profiles, call_1_data, word_count, effective_config)
    start = time.time()
    try:
        raw_2 = call_haiku(call_2_prompt, SYSTEM_PROMPT, model=audit_model, max_tokens=8000)
        call_2_data = clean_json_response(raw_2)
        call_2_data['_elapsed_seconds'] = round(time.time() - start, 1)
    except json.JSONDecodeError:
        retry_prompt = call_2_prompt + "\n\nReturn only valid JSON. No preamble, no code fences, no commentary."
        raw_2 = call_haiku(retry_prompt, SYSTEM_PROMPT, model=audit_model, max_tokens=8000)
        call_2_data = clean_json_response(raw_2)
        call_2_data['_elapsed_seconds'] = round(time.time() - start, 1)

    return call_1_data, call_2_data


def _majority_verdict(verdicts: list[str]) -> tuple[str, bool]:
    """Compute majority verdict from a list (typically 3 values).

    Returns (final_verdict, is_stable).
    is_stable=True if 2+ agree; False if 3-way split (takes worst).
    """
    counts = Counter(verdicts)
    most_common = counts.most_common()
    if most_common[0][1] >= 2:
        return most_common[0][0], True
    # 3-way split: take worst
    worst = max(verdicts, key=lambda v: VERDICT_SEVERITY.get(v, 99))
    return worst, False


def _majority_q19_scenes(all_pass_q19_items: list[dict]) -> list[dict]:
    """Compute per-scene majority AFTERMATH/DENOUEMENT classification across passes.

    Returns the merged post_climax_scenes list with majority classifications.
    """
    scene_votes = {}  # scene_number -> list of (classification, justification)
    for item in all_pass_q19_items:
        for s in item.get('post_climax_scenes', []):
            sn = s.get('scene')
            if sn is not None:
                scene_votes.setdefault(sn, []).append(
                    (s.get('classification', '').upper(), s.get('justification', ''))
                )

    merged = []
    for scene_num in sorted(scene_votes.keys()):
        votes = scene_votes[scene_num]
        classifications = [v[0] for v in votes]
        counts = Counter(classifications)
        majority_cls, _ = counts.most_common(1)[0]
        # Use justification from the first vote with the majority classification
        justification = next(
            (v[1] for v in votes if v[0] == majority_cls),
            f"Majority {majority_cls} ({'/'.join(classifications)})"
        )
        merged.append({
            "scene": scene_num,
            "classification": majority_cls,
            "justification": f"{justification} [votes: {'/'.join(classifications)}]",
        })
    return merged


def _merge_multipass_results(
    all_passes: list[tuple[dict, dict]],
    title: str,
    word_count: int,
    effective_config: dict,
) -> tuple[dict, dict]:
    """Merge N pass results into a single (call_1_data, call_2_data) with majority verdicts.

    For each Q-check, takes the majority verdict across passes.
    For Q19, merges per-scene classifications via majority vote.
    """
    # Collect per-Q verdicts across passes
    q_verdicts = {}  # q_id -> [verdict_pass1, verdict_pass2, ...]
    q_notes = {}     # q_id -> [note_pass1, ...]
    q19_items = []

    for pass_idx, (c1, c2) in enumerate(all_passes):
        all_items = c1.get('items', []) + c2.get('items', [])
        for item in all_items:
            qid = item.get('id')
            if qid:
                q_verdicts.setdefault(qid, []).append(item.get('verdict', 'ERROR'))
                q_notes.setdefault(qid, []).append(item.get('note', ''))
                if qid == 'Q19':
                    q19_items.append(item)

    # Build merged call_1 and call_2 from last pass as template
    final_c1 = all_passes[-1][0].copy()
    final_c2 = all_passes[-1][1].copy()

    # Apply majority verdicts to items
    for items_list in [final_c1.get('items', []), final_c2.get('items', [])]:
        for item in items_list:
            qid = item.get('id')
            if qid and qid in q_verdicts:
                verdicts = q_verdicts[qid]
                majority, stable = _majority_verdict(verdicts)
                item['verdict'] = majority
                if not stable:
                    item['note'] = (
                        item.get('note', '') +
                        f" [unstable: {'/'.join(verdicts)}]"
                    )
                elif len(set(verdicts)) > 1:
                    item['note'] = (
                        item.get('note', '') +
                        f" [majority {majority}: {'/'.join(verdicts)}]"
                    )

    # Q19: merge per-scene classifications via majority vote
    valid_q19_items = [i for i in q19_items if i.get('post_climax_scenes')]
    if valid_q19_items:
        merged_scenes = _majority_q19_scenes(valid_q19_items)
        for items_list in [final_c1.get('items', []), final_c2.get('items', [])]:
            for item in items_list:
                if item.get('id') == 'Q19':
                    item['post_climax_scenes'] = merged_scenes

    return final_c1, final_c2


def _write_stop_report(book_dir, error_msg, pipeline_state):
    """Write a STOP_REPORT.json and return the path."""
    reports_dir = os.path.join(book_dir, 'out', 'reports')
    os.makedirs(reports_dir, exist_ok=True)
    stop_report = {
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "component": "synopsis_auditor.py",
        "phase": 0,
        "scene_number": None,
        "error_type": "Class A",
        "error_message": error_msg,
        "file_path": os.path.abspath(__file__),
        "suggested_fix": "Re-run, or inspect model output. If this repeats, the prompt needs revision.",
        "pipeline_state": pipeline_state,
    }
    stop_path = os.path.join(reports_dir, 'STOP_REPORT.json')
    with open(stop_path, 'w') as f:
        json.dump(stop_report, f, indent=2)
    return stop_path


def main():
    parser = argparse.ArgumentParser(description='ANPD V26 Synopsis Auditor — Gate 1 (3-pass majority)')
    parser.add_argument('--synopsis', required=True, help='Path to synopsis .md file')
    parser.add_argument('--intake', required=True, help='Path to intake .json file')
    parser.add_argument('--series-dir', required=True, help='Series directory')
    parser.add_argument('--series-config', required=True, help='Path to series_config.json')
    args = parser.parse_args()

    # Load effective config
    try:
        effective_config = resolve_config(args.series_config)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        print(f"  FATAL: Failed to load effective config: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"  ANPD V26 — SYNOPSIS AUDIT ({NUM_PASSES}-PASS MAJORITY)")
    print(f"{'='*70}")

    # Validate inputs
    if not os.path.exists(args.synopsis):
        print(f"  FATAL: Synopsis not found: {args.synopsis}")
        sys.exit(1)
    if not os.path.exists(args.intake):
        print(f"  FATAL: Intake not found: {args.intake}")
        sys.exit(1)

    # Load files
    synopsis = load_text(args.synopsis)
    intake = load_json(args.intake)
    title = intake.get('title', intake.get('book_title', 'Unknown'))
    word_count = len(synopsis.split())

    profiles_path = find_latest(args.series_dir, '*character_profiles*.json')
    character_profiles = load_json(profiles_path) if profiles_path else {}
    book_dir = os.path.dirname(args.synopsis)

    print(f"  Synopsis:  {os.path.basename(args.synopsis)}")
    print(f"  Intake:    {os.path.basename(args.intake)}")
    print(f"  Series:    {args.series_dir}")
    if profiles_path:
        print(f"  Profiles:  {os.path.basename(profiles_path)}")
    print(f"  Title:     {title}")
    print(f"  Words:     {word_count:,}")

    # ── Deterministic checks (run once, not per-pass) ──
    parsed_scenes = parse_synopsis(synopsis)
    deterministic_findings = check_synopsis_integrity(parsed_scenes, intake, args.synopsis)
    print(f"  Deterministic checks: {len(deterministic_findings) or 'all clear'}"
          f" ({len(parsed_scenes)} scenes parsed)")

    banned_path = os.path.join(args.series_dir, "banned_phrases.json")
    banned_data, banned_validation_findings = validate_banned_phrases_file(banned_path)
    deterministic_findings.extend(banned_validation_findings)
    if banned_data is not None and parsed_scenes:
        deterministic_findings.extend(check_banned_names(parsed_scenes, banned_data, args.synopsis))
        deterministic_findings.extend(check_banned_phrases(parsed_scenes, banned_data, args.synopsis))

    chapter_findings = check_chapter_count(synopsis, effective_config, args.synopsis)
    deterministic_findings.extend(chapter_findings)

    # ── Multi-pass LLM audit ──
    all_passes = []
    for pass_num in range(1, NUM_PASSES + 1):
        print(f"\n  ── Pass {pass_num}/{NUM_PASSES} ──")
        try:
            c1, c2 = _run_single_audit_pass(
                synopsis, intake, character_profiles, effective_config,
                parsed_scenes, pass_num,
            )
            # Log per-pass verdicts
            all_items = c1.get('items', []) + c2.get('items', [])
            verdicts_str = ", ".join(
                f"{i['id']}={i.get('verdict','?')}" for i in all_items if i.get('id')
            )
            print(f"    Pass {pass_num} verdicts: {verdicts_str}")
            all_passes.append((c1, c2))
        except Exception as e:
            print(f"  FATAL: Pass {pass_num} failed: {e}", file=sys.stderr)
            stop_path = _write_stop_report(book_dir, str(e), f"Pass {pass_num} failed")
            print(f"  STOP_REPORT written: {stop_path}", file=sys.stderr)
            sys.exit(1)

    # ── Merge multi-pass results ──
    print(f"\n  Merging {NUM_PASSES}-pass results (majority vote)...")
    call_1_data, call_2_data = _merge_multipass_results(
        all_passes, title, word_count, effective_config
    )

    # ── Consolidate (applies Q8/Q19 deterministic overrides) ──
    print(f"  Consolidating...")
    try:
        output_json, report_md, verdict = consolidate(
            call_1_data, call_2_data, title, word_count, effective_config
        )
    except ValueError as e:
        # Q19 structured data missing after multi-pass — ERROR verdict
        error_msg = f"Q19 structured data absent across all {NUM_PASSES} passes: {e}"
        print(f"  ERROR: {error_msg}", file=sys.stderr)
        stop_path = _write_stop_report(book_dir, error_msg, "consolidate failed — Q19 missing structured data")
        print(f"  STOP_REPORT written: {stop_path}", file=sys.stderr)
        sys.exit(1)

    # V24 finding schema output
    try:
        findings_list = deterministic_findings + items_to_findings(
            call_1_data, call_2_data, args.synopsis, effective_config
        )
    except ValueError as e:
        error_msg = f"Finding conversion failed in synopsis_auditor: {e}"
        print(f"  FATAL: {error_msg}", file=sys.stderr)
        stop_path = _write_stop_report(book_dir, error_msg, "finding conversion failed")
        print(f"  STOP_REPORT written: {stop_path}", file=sys.stderr)
        sys.exit(1)

    findings_envelope = serialize_findings(findings_list)
    findings_path = os.path.join(book_dir, 'synopsis_findings.json')
    with open(findings_path, 'w') as f:
        json.dump(findings_envelope, f, indent=2)

    # Save legacy outputs
    json_path = os.path.join(book_dir, 'synopsis_audit_report.json')
    md_path = os.path.join(book_dir, 'synopsis_audit_report.md')
    with open(json_path, 'w') as f:
        json.dump(output_json, f, indent=2)
    with open(md_path, 'w') as f:
        f.write(report_md)

    print(f"\n  JSON:     {json_path}")
    print(f"  Report:   {md_path}")
    print(f"  Findings: {findings_path} ({len(findings_list)} findings)")
    print(f"  Verdict:  {verdict}")

    if verdict == "FAIL":
        fail_ids = output_json.get('fails', [])
        print(f"\n  FAILED items: {', '.join(fail_ids)}")
        print(f"  Fix synopsis and re-run before proceeding to production.")

    print(f"\n{'='*70}")
    print(f"  SYNOPSIS AUDIT COMPLETE — Verdict: {verdict}")
    print(f"{'='*70}\n")

    sys.exit(1 if verdict == "FAIL" else 0)


def audit_synopsis(
    synopsis_path: str,
    intake_path: str,
    series_dir: str,
    series_config_path: str = "",
) -> dict:
    """Callable entry point for synopsis audit. Runs the auditor as subprocess.

    Returns dict with keys: verdict, fails, weaks, total_scenes, error.
    Does NOT sys.exit — returns the result for programmatic use.
    """
    import subprocess

    if not series_config_path:
        series_config_path = os.path.join(series_dir, "series_config.json")

    cmd = [
        sys.executable, "-m", "pipeline.synopsis_auditor",
        "--synopsis", str(synopsis_path),
        "--intake", str(intake_path),
        "--series-dir", str(series_dir),
        "--series-config", str(series_config_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                            cwd="/anpd/v26")

    # Try to read the JSON report regardless of exit code
    book_dir = str(Path(synopsis_path).resolve().parent)
    report_path = os.path.join(book_dir, "synopsis_audit_report.json")

    if os.path.exists(report_path):
        try:
            with open(report_path) as f:
                report = json.load(f)
            return {
                "verdict": report.get("verdict", "UNKNOWN"),
                "fails": report.get("fails", []),
                "weaks": report.get("weaks", []),
                "total_scenes": report.get("total_scenes", 0),
                "error": None if result.returncode == 0 else result.stderr[-500:] if result.stderr else None,
            }
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "verdict": "ERROR",
        "fails": [],
        "weaks": [],
        "total_scenes": 0,
        "error": result.stderr[-500:] if result.stderr else f"Exit code {result.returncode}",
    }


if __name__ == '__main__':
    main()
