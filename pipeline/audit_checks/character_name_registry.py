"""
MA-002: Character Name Registry Check

Detects characters in the manuscript who are not in the canonical roster.
Canonical roster is the union of:
  - Named characters in series_bible.json (recurring_characters)
  - Named characters in character_profiles.json
  - Named characters explicitly introduced in synopsis (sc_NNN.md files)
  - Banned names from banned_phrases.json (flagged as Class A violations)

Findings:
  CLASS_A: banned name appears (Sarah, Chen, Marcus Webb)
  CLASS_A: character speaks dialogue or takes action but is not in roster
  CLASS_B: character mentioned by name but never appears directly

Severity: CLASS_A (blocks publication).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

from pathlib import Path

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Constants ────────────────────────────────────────────────────────────────

SONNET_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 5  # scenes per extraction call
MAX_RETRIES = 2

NAME_EXTRACTION_SYSTEM = """You are a character name extractor for a novel manuscript. Your job is to identify every proper noun that acts as a CHARACTER NAME in the text.

Extract ONLY character names — people who are named in the narrative. Include:
- Characters who speak dialogue
- Characters who take physical actions
- Characters who are mentioned by name (even in passing or in someone's thoughts)
- Characters referenced by title + surname (e.g., "Capitán Vera")

Do NOT extract:
- Place names (Caracas, Madrid, Langley, Petare, Maracaibo)
- Organization names (CIA, NSA, SEBIN, FBI)
- Ranks/titles without a name ("the Capitán", "the Chief")
- Descriptive references ("the aide", "the operator", "his wife")
- Brand names (ThinkPad, MacBook)
- Country names

For each character name found, output a JSON object on its own line:
{"name": "Full Name", "scene_number": N, "appears_directly": true/false, "evidence": "exact quote <=120 chars"}

appears_directly = true if the character speaks dialogue, takes physical action, or is physically present in the scene.
appears_directly = false if the character is only mentioned, referenced, or thought about.

Output one JSON per line. Nothing else."""

NAME_EXTRACTION_PROMPT = """Extract all character names from these manuscript scenes.

SCENES:
{scenes_block}

Output one JSON object per line."""


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class CharacterAppearance:
    """A character name appearance in the manuscript."""
    name: str
    scene_number: int
    appears_directly: bool
    evidence: str


# ── LLM helper ───────────────────────────────────────────────────────────────

def _call_llm(system: str, user: str, model: str = SONNET_MODEL) -> str:
    """Call LLM via the pipeline's llm_client."""
    pipeline_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from llm_client import call_llm
    response = call_llm(
        provider="anthropic",
        model=model,
        system=system,
        user=user,
        max_tokens=4096,
        temperature=0.0,
    )
    return response.text


# ── Canonical roster building ────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Normalize a character name for comparison."""
    # Strip titles/ranks
    name = re.sub(r'^(Capitán|Captain|Major|Colonel|General|Teniente|Comandante|Dr\.?|Mr\.?|Mrs\.?|Ms\.?)\s+',
                  '', name, flags=re.IGNORECASE)
    return name.strip().lower()


def _extract_names_from_text(text: str) -> set[str]:
    """Extract capitalized proper nouns from text that look like names."""
    # Match sequences of capitalized words that look like names
    pattern = re.compile(r'\b([A-Z][a-záéíóúñ]+(?:\s+[A-Z][a-záéíóúñ]+)*)\b')
    matches = set()
    for m in pattern.finditer(text):
        candidate = m.group(1)
        # Filter out common non-name words that happen to be capitalized
        if candidate.lower() not in {
            "the", "scene", "type", "pov", "action", "non", "mixed",
            "suspense", "chapter", "book", "series",
        }:
            matches.add(candidate)
    return matches


def build_canonical_roster(briefs: BriefBundle, synopsis_dir: str | None = None) -> tuple[set[str], set[str]]:
    """Build the canonical character roster from all brief sources.

    Returns:
        (roster_names, banned_names) — both sets of normalized names.
        roster_names: all legitimate character names
        banned_names: names that should never appear
    """
    roster: set[str] = set()
    banned: set[str] = set()

    # Source 1: series_bible recurring_characters
    bible = briefs.series_bible
    for char in bible.get("recurring_characters", []):
        name = char.get("name", "")
        if name:
            roster.add(_normalize_name(name))
            # Also add first name and last name separately
            parts = name.strip().split()
            for part in parts:
                if len(part) > 1:
                    roster.add(part.lower())

    # Banned names from series_bible
    for name in bible.get("banned_names", []):
        if name:
            banned.add(_normalize_name(name))
            parts = name.strip().split()
            for part in parts:
                if len(part) > 1:
                    banned.add(part.lower())

    # Source 2: character_profiles (series-level + book-level, both formats)
    # Supports legacy {"characters": [...]} array format AND schema 1.1.0
    # name-keyed format where top-level keys are character names.
    def _ingest_profiles(profiles: dict) -> None:
        """Add names from a profiles dict (either format) to the roster."""
        if "characters" in profiles and isinstance(profiles["characters"], list):
            # Legacy array format: {"characters": [{name: ...}, ...]}
            chars = profiles["characters"]
        else:
            # Schema 1.1.0 name-keyed: {"Name": {name: "Name", ...}, ...}
            chars = list(profiles.values())
            # Filter out non-dict entries (shouldn't exist but defensive)
            chars = [c for c in chars if isinstance(c, dict)]

        for char in chars:
            name = char.get("name", "")
            if name:
                roster.add(_normalize_name(name))
                parts = name.strip().split()
                for part in parts:
                    if len(part) > 1:
                        roster.add(part.lower())
            # Also extract names from relationships
            for rel_name in char.get("relationships", {}).keys():
                if rel_name:
                    roster.add(_normalize_name(rel_name))
                    parts = rel_name.strip().split()
                    for part in parts:
                        if len(part) > 1:
                            roster.add(part.lower())

    _ingest_profiles(briefs.character_profiles)
    _ingest_profiles(briefs.book_character_profiles)

    # Source 3: banned_phrases.json names (via book_config, which may contain it)
    book_config = briefs.book_config
    for name in book_config.get("names", []):
        if name:
            banned.add(_normalize_name(name))
            parts = name.strip().split()
            for part in parts:
                if len(part) > 1:
                    banned.add(part.lower())

    # Source 4: synopsis — extract character names from canonical synopsis file
    if briefs.synopsis_path and Path(briefs.synopsis_path).exists():
        try:
            text = Path(briefs.synopsis_path).read_text(encoding="utf-8")
            # Extract POV/FOCUS characters
            for m in re.finditer(r'\[(?:POV|FOCUS):\s*([^\]]+)\]', text):
                name = m.group(1).strip()
                roster.add(_normalize_name(name))
                parts = name.split()
                for part in parts:
                    if len(part) > 1:
                        roster.add(part.lower())
            # Extract other capitalized names from synopsis text
            for name in _extract_names_from_text(text):
                norm = _normalize_name(name)
                if len(norm) > 2:
                    roster.add(norm)
        except Exception:
            pass

    return roster, banned


# ── Name extraction from manuscript ──────────────────────────────────────────

def extract_character_names(manuscript: ManuscriptArtifact) -> list[CharacterAppearance]:
    """Extract all character name appearances from the manuscript via LLM."""
    all_appearances: list[CharacterAppearance] = []
    scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)

    for i in range(0, len(scenes), BATCH_SIZE):
        batch = scenes[i:i + BATCH_SIZE]
        scenes_block = "\n\n".join(
            f"--- SCENE {s.scene_number} ---\n{s.text[:3000]}"
            for s in batch
        )
        prompt = NAME_EXTRACTION_PROMPT.format(scenes_block=scenes_block)

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = _call_llm(NAME_EXTRACTION_SYSTEM, prompt)
                for line in response.strip().splitlines():
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                        all_appearances.append(CharacterAppearance(
                            name=obj.get("name", ""),
                            scene_number=obj.get("scene_number", 0),
                            appears_directly=obj.get("appears_directly", False),
                            evidence=obj.get("evidence", ""),
                        ))
                    except json.JSONDecodeError:
                        continue
                break
            except Exception as e:
                if attempt < MAX_RETRIES:
                    time.sleep(5 * (attempt + 1))
                    continue
                print(f"    WARN: name extraction failed for scenes "
                      f"{batch[0].scene_number}-{batch[-1].scene_number}: {e}",
                      file=sys.stderr)

    return all_appearances


# ── Comparison and findings ──────────────────────────────────────────────────

def _group_appearances_by_name(appearances: list[CharacterAppearance]) -> dict[str, list[CharacterAppearance]]:
    """Group appearances by normalized character name."""
    groups: dict[str, list[CharacterAppearance]] = {}
    for a in appearances:
        key = _normalize_name(a.name)
        groups.setdefault(key, []).append(a)
    return groups


def _build_roster_parts_index(roster: set[str]) -> dict[str, str]:
    """Build a reverse index: individual word → full roster entry.

    This allows matching a single-word extracted name ("Silas") against
    multi-word roster entries ("silas vance") even when the individual
    part was not added to the roster set separately.
    """
    index: dict[str, str] = {}
    for entry in roster:
        parts = entry.split()
        if len(parts) >= 2:
            for part in parts:
                if len(part) > 2:
                    index.setdefault(part, entry)
    return index


def check_names_against_roster(
    appearances: list[CharacterAppearance],
    roster: set[str],
    banned: set[str],
) -> list[Finding]:
    """Compare extracted names against canonical roster and produce findings."""
    findings: list[Finding] = []
    grouped = _group_appearances_by_name(appearances)

    # Reverse index: single word → full roster name.  Ensures first-name-only
    # ("Silas") and surname-only ("Kowalski") references to rostered full names
    # ("silas vance", "meat kowalski") are recognised before flagging.
    roster_parts = _build_roster_parts_index(roster)

    for norm_name, apps in sorted(grouped.items()):
        if not norm_name or len(norm_name) < 2:
            continue

        # Check if name (or any part) matches roster — forward match
        display_name = apps[0].name
        name_parts = set(norm_name.split())
        in_roster = (norm_name in roster or
                     any(part in roster for part in name_parts))

        # Reverse match: extracted single-word name is a component of a
        # multi-word roster entry (e.g. "kowalski" → "meat kowalski").
        if not in_roster and len(name_parts) == 1:
            in_roster = norm_name in roster_parts

        # Check if name matches banned list
        in_banned = (norm_name in banned or
                     any(part in banned for part in name_parts))

        if in_banned:
            scenes = sorted(set(a.scene_number for a in apps))
            evidence = [f"Scene {a.scene_number}: \"{a.evidence}\"" for a in apps[:3]]
            findings.append(Finding(
                check_id="MA-002-character-name-registry",
                severity="CLASS_A",
                scene_number=None,
                scene_numbers=scenes,
                description=(
                    f"Banned name '{display_name}' appears in manuscript"
                ),
                evidence=evidence,
                suggested_fix=f"Remove or replace banned name '{display_name}'",
            ))
        elif not in_roster:
            scenes = sorted(set(a.scene_number for a in apps))
            has_direct = any(a.appears_directly for a in apps)
            evidence = [f"Scene {a.scene_number}: \"{a.evidence}\"" for a in apps[:3]]

            if has_direct:
                # CLASS_A: character speaks or acts but isn't in roster
                findings.append(Finding(
                    check_id="MA-002-character-name-registry",
                    severity="CLASS_A",
                    scene_number=None,
                    scene_numbers=scenes,
                    description=(
                        f"Invented character '{display_name}' speaks dialogue or takes action "
                        f"but is not in canonical roster"
                    ),
                    evidence=evidence,
                    suggested_fix=(
                        f"Add '{display_name}' to character roster or remove from manuscript"
                    ),
                ))
            else:
                # CLASS_B: mentioned but doesn't appear directly
                findings.append(Finding(
                    check_id="MA-002-character-name-registry",
                    severity="CLASS_B",
                    scene_number=None,
                    scene_numbers=scenes,
                    description=(
                        f"Character '{display_name}' mentioned by name but not in canonical roster "
                        f"(background reference — review)"
                    ),
                    evidence=evidence,
                    suggested_fix=(
                        f"Verify '{display_name}' is intentional; if recurring, add to roster"
                    ),
                ))

    return findings


# ── Check module class ────────────────────────────────────────────────────────

class CharacterNameRegistry:
    check_id = "MA-002-character-name-registry"
    severity = "CLASS_A"
    description = (
        "Character name registry: detects characters not in canonical roster, "
        "banned names, and invented characters"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        # Resolve synopsis directory
        synopsis_dir = None
        # Check briefs.scene_map for explicit path hint
        if briefs.scene_map and isinstance(briefs.scene_map, dict):
            hint = briefs.scene_map.get("synopsis_dir", "")
            if hint and os.path.isdir(hint):
                synopsis_dir = hint
        # Try common paths relative to manuscript
        if not synopsis_dir:
            ms_dir = manuscript.manuscript_dir
            candidate_paths = [
                os.path.join(os.path.dirname(ms_dir), "work", "synopsis_chapters"),
                os.path.join(os.path.dirname(os.path.dirname(ms_dir)), "work", "synopsis_chapters"),
            ]
            for candidate in candidate_paths:
                if os.path.isdir(candidate):
                    synopsis_dir = candidate
                    break

        # Phase 1: Build canonical roster
        print("    Phase 1: building canonical roster", file=sys.stderr)
        roster, banned = build_canonical_roster(briefs, synopsis_dir)
        print(f"    -> {len(roster)} roster names, {len(banned)} banned names", file=sys.stderr)

        # Phase 2: Extract character names from manuscript
        print("    Phase 2: extracting character names (LLM)", file=sys.stderr)
        appearances = extract_character_names(manuscript)
        print(f"    -> {len(appearances)} character appearances extracted", file=sys.stderr)

        # Phase 3: Compare against roster
        print("    Phase 3: comparing against roster", file=sys.stderr)
        findings = check_names_against_roster(appearances, roster, banned)
        print(f"    -> {len(findings)} findings", file=sys.stderr)

        return findings
