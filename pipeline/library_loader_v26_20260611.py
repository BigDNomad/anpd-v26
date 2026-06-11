# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
library_loader.py — Library access module for ANPD V24 pipeline.

Reads intake.library_constraints, lazy-loads sub-library files (twist=md,
action_scene=json, voice=json), serves filtered entries to synopsis_generator
(and future character_generator) with deterministic round-robin sampling
per book identity.

Non-pipeline component — invoked by other components, not by master_controller.

Component version: 1.0.0
Copyright 2026 Endeavor Publishing LLC
"""

from __future__ import annotations

import glob as _glob
import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ─── Exceptions ──────────────────────────────────────────────────────────────

class LibraryConstraintError(Exception):
    """Fatal: sub-library name in constraints doesn't resolve to existing file."""
    pass


class LibraryFileError(Exception):
    """Fatal: sub-library file is missing, malformed, or has missing required fields."""
    pass


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class TwistLibraryEntry:
    entry_id: str
    sub_library: str
    structural_position: str  # "EOA1" | "Midpoint" | "EOA2"
    source: dict  # {title, author_or_director, year, medium}
    pattern_summary: str
    raw_markdown: str


@dataclass
class ActionSceneLibraryEntry:
    entry_id: str
    sub_library: str
    scene_type: str
    engagement_tags: List[str]
    structural_positions_supported: List[str]
    genre_categories: List[str]
    source: dict
    pattern_summary: str
    character_input_requirements: List[str]
    setup_requirements: List[str]
    delivers: List[str]
    fidelity_rules: List[str]
    forbidden_patterns: List[str]
    raw_markdown: str


@dataclass
class VoiceLibraryEntry:
    entry_id: str
    trademark_function: str
    character_name: str
    source: dict
    pattern_summary: str
    voice_register: str
    worldview: str
    reaction_patterns: List[str]
    blindspots: List[str]
    what_they_refuse: List[str]
    what_they_fail_at: List[str]
    friction_pattern: str
    adaptation_notes: str
    do_not_copy: List[str]
    raw_markdown: str


# ─── Voice trademark function enum ──────────────────────────────────────────

VOICE_TRADEMARK_FUNCTIONS = [
    "amplified_ensemble_warmth",
    "protagonist_mirror_companion",
    "proportional_incompatibility_anchor",
    "moral_complexity_through_conviction",
]


# ─── Twist position mapping ─────────────────────────────────────────────────

_TWIST_POSITION_MAP = {
    "End of Act 1 Twist": "EOA1",
    "End of Act 1": "EOA1",
    "Midpoint Twist": "Midpoint",
    "Midpoint": "Midpoint",
    "End of Act 2 Twist": "EOA2",
    "End of Act 2": "EOA2",
}


# ─── LibraryLoader ───────────────────────────────────────────────────────────

class LibraryLoader:
    """Library access module. Reads intake.library_constraints, lazy-loads
    sub-library files, serves filtered entries with deterministic sampling."""

    def __init__(
        self,
        intake_path: Path,
        libraries_root: Path = Path("/anpd/v25/libraries"),
    ):
        self._libraries_root = Path(libraries_root)
        self._intake_path = Path(intake_path)

        # Load intake to extract constraints.
        try:
            with open(self._intake_path, "r", encoding="utf-8") as f:
                intake = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise LibraryFileError(f"Failed to load intake at {intake_path}: {e}") from e

        # Extract book identity for deterministic seeding.
        self._series_name = intake.get("series", intake.get("series_name", "unknown"))
        self._book_number = intake.get("book_number", 0)

        # Parse constraints with defaults per §5.1.
        constraints = intake.get("library_constraints", {})
        self.twist_constraints: List[str] = constraints.get("twist_libraries", ["all"])
        self.action_scene_constraints: List[str] = constraints.get("action_scene_sub_libraries", [])
        self.voice_constraints: List[str] = constraints.get("voice_library_trademark_functions", ["all"])

        # Resolve "all" expansion.
        if self.twist_constraints == ["all"]:
            self.twist_constraints = self._discover_sub_libraries("twist")
        if self.action_scene_constraints == ["all"]:
            self.action_scene_constraints = self._discover_sub_libraries("action_scene")
        if self.voice_constraints == ["all"]:
            self.voice_constraints = list(VOICE_TRADEMARK_FUNCTIONS)

        # Validate constraint names.
        self._validate_twist_constraints()
        self._validate_action_scene_constraints()
        self._validate_voice_constraints()

        # Lazy-load caches (populated on first query).
        self._twist_cache: dict[str, List[TwistLibraryEntry]] = {}
        self._action_scene_cache: dict[str, List[ActionSceneLibraryEntry]] = {}
        self._voice_cache: dict[str, List[VoiceLibraryEntry]] = {}

        # Query log for constraints_summary.
        self._queries: List[dict] = []

    # ─── Public API ──────────────────────────────────────────────────────

    def twist_entries_for_position(
        self,
        structural_position: str,
        max_entries: int = 5,
    ) -> List[TwistLibraryEntry]:
        """Return twist entries filtered to structural_position, sampled across sub-libraries."""
        all_entries = []
        for sub_lib in self.twist_constraints:
            entries = self._load_twist_sub_library(sub_lib)
            filtered = [e for e in entries if e.structural_position == structural_position]
            all_entries.extend(filtered)

        sampled = self._sample_round_robin(
            all_entries, max_entries, structural_position,
            key_fn=lambda e: e.sub_library,
        )

        self._queries.append({
            "library_type": "twist",
            "structural_position": structural_position,
            "entries_returned": len(sampled),
            "from_sub_libraries": list({e.sub_library for e in sampled}),
        })
        return sampled

    def action_scene_entries_for_position(
        self,
        structural_position: str,
        scene_type: Optional[str] = None,
        max_entries: int = 5,
    ) -> List[ActionSceneLibraryEntry]:
        """Return action scene entries filtered to structural_position and optional scene_type."""
        all_entries = []
        for sub_lib in self.action_scene_constraints:
            entries = self._load_action_scene_sub_library(sub_lib)
            filtered = [
                e for e in entries
                if structural_position in e.structural_positions_supported
            ]
            if scene_type is not None:
                filtered = [e for e in filtered if e.scene_type == scene_type]
            all_entries.extend(filtered)

        sampled = self._sample_round_robin(
            all_entries, max_entries, structural_position,
            key_fn=lambda e: e.sub_library,
        )

        query_record = {
            "library_type": "action_scene",
            "structural_position": structural_position,
            "entries_returned": len(sampled),
            "from_sub_libraries": list({e.sub_library for e in sampled}),
        }
        if scene_type is not None:
            query_record["scene_type"] = scene_type
        self._queries.append(query_record)
        return sampled

    def voice_entries_for_function(
        self,
        trademark_function: str,
        max_entries: int = 4,
    ) -> List[VoiceLibraryEntry]:
        """Return voice library entries for the given trademark function."""
        if trademark_function not in VOICE_TRADEMARK_FUNCTIONS:
            raise LibraryConstraintError(
                f"Invalid trademark function: {trademark_function!r}. "
                f"Valid values: {VOICE_TRADEMARK_FUNCTIONS}"
            )

        if self.voice_constraints != ["all"] and trademark_function not in self.voice_constraints:
            self._queries.append({
                "library_type": "voice",
                "trademark_function": trademark_function,
                "entries_returned": 0,
                "from_sub_libraries": [],
            })
            return []

        entries = self._load_voice_sub_library(trademark_function)

        sampled = entries[:max_entries] if len(entries) > max_entries else entries

        self._queries.append({
            "library_type": "voice",
            "trademark_function": trademark_function,
            "entries_returned": len(sampled),
            "from_sub_libraries": [trademark_function] if sampled else [],
        })
        return sampled

    def constraints_summary(self) -> dict:
        """Return summary of active constraints and queries made."""
        return {
            "library_constraints_active": {
                "twist_libraries": self.twist_constraints,
                "action_scene_sub_libraries": self.action_scene_constraints,
                "voice_library_trademark_functions": self.voice_constraints,
            },
            "library_queries_made": list(self._queries),
        }

    # ─── Discovery + Validation ──────────────────────────────────────────

    def _discover_sub_libraries(self, library_type: str) -> List[str]:
        """Discover canonical sub-library names in library directory.

        Canonical names are identified by symlinks (production convention)
        or by files whose stems don't contain timestamp suffixes (_YYYYMMDD).
        """
        lib_dir = self._libraries_root / library_type
        if not lib_dir.is_dir():
            return []

        if library_type == "action_scene":
            pattern = str(lib_dir / "*.json")
        else:
            pattern = str(lib_dir / "*.md")

        names = set()
        for path in _glob.glob(pattern):
            p = Path(path)
            stem = p.stem
            # Canonical: is a symlink, or doesn't have a timestamp suffix.
            if os.path.islink(path):
                names.add(stem)
            elif not re.search(r'_\d{8}', stem):
                names.add(stem)

        return sorted(names)

    def _validate_twist_constraints(self) -> None:
        for name in self.twist_constraints:
            path = self._libraries_root / "twist" / f"{name}.md"
            if not path.exists():
                available = self._discover_sub_libraries("twist")
                raise LibraryConstraintError(
                    f"Twist sub-library '{name}' not found at {path}. "
                    f"Available: {available}"
                )

    def _validate_action_scene_constraints(self) -> None:
        for name in self.action_scene_constraints:
            path = self._libraries_root / "action_scene" / f"{name}.json"
            if not path.exists():
                available = self._discover_sub_libraries("action_scene")
                raise LibraryConstraintError(
                    f"Action scene sub-library '{name}' not found at {path}. "
                    f"Available: {available}"
                )

    def _validate_voice_constraints(self) -> None:
        if self.voice_constraints == ["all"]:
            return
        for name in self.voice_constraints:
            if name not in VOICE_TRADEMARK_FUNCTIONS:
                raise LibraryConstraintError(
                    f"Invalid voice trademark function: '{name}'. "
                    f"Valid values: {VOICE_TRADEMARK_FUNCTIONS}"
                )

    # ─── Lazy Loading ────────────────────────────────────────────────────

    def _load_twist_sub_library(self, name: str) -> List[TwistLibraryEntry]:
        if name in self._twist_cache:
            return self._twist_cache[name]

        path = self._libraries_root / "twist" / f"{name}.md"
        if not path.exists():
            raise LibraryFileError(f"Twist sub-library file not found: {path}")

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise LibraryFileError(f"Failed to read twist sub-library {path}: {e}") from e

        entries = self._parse_twist_markdown(text, name)
        self._twist_cache[name] = entries
        return entries

    def _load_action_scene_sub_library(self, name: str) -> List[ActionSceneLibraryEntry]:
        if name in self._action_scene_cache:
            return self._action_scene_cache[name]

        path = self._libraries_root / "action_scene" / f"{name}.json"
        if not path.exists():
            raise LibraryFileError(f"Action scene sub-library file not found: {path}")

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise LibraryFileError(f"Failed to parse action scene sub-library {path}: {e}") from e

        raw_entries = data.get("entries", [])
        entries = []
        for raw in raw_entries:
            entry_id = raw.get("entry_id", "")
            if not entry_id:
                raise LibraryFileError(
                    f"Action scene entry missing entry_id in {path}"
                )
            entries.append(ActionSceneLibraryEntry(
                entry_id=entry_id,
                sub_library=raw.get("sub_library", name),
                scene_type=raw.get("scene_type", name),
                engagement_tags=raw.get("engagement_tags", []),
                structural_positions_supported=raw.get("structural_positions_supported", []),
                genre_categories=raw.get("genre_categories", []),
                source=raw.get("source", {}),
                pattern_summary=raw.get("pattern_summary", ""),
                character_input_requirements=raw.get("character_input_requirements", []),
                setup_requirements=raw.get("setup_requirements", []),
                delivers=raw.get("delivers", []),
                fidelity_rules=raw.get("fidelity_rules", []),
                forbidden_patterns=raw.get("forbidden_patterns", []),
                raw_markdown=self._action_scene_entry_to_markdown(raw),
            ))

        self._action_scene_cache[name] = entries
        return entries

    def _load_voice_sub_library(self, trademark_function: str) -> List[VoiceLibraryEntry]:
        if trademark_function in self._voice_cache:
            return self._voice_cache[trademark_function]

        path = self._libraries_root / "voice" / f"{trademark_function}.json"
        if not path.exists():
            # Voice library content may not exist yet — return empty per §9.3.
            self._voice_cache[trademark_function] = []
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise LibraryFileError(f"Failed to parse voice sub-library {path}: {e}") from e

        raw_entries = data.get("entries", [])
        entries = []
        for raw in raw_entries:
            entry_id = raw.get("entry_id", "")
            if not entry_id:
                raise LibraryFileError(
                    f"Voice entry missing entry_id in {path}"
                )
            entries.append(VoiceLibraryEntry(
                entry_id=entry_id,
                trademark_function=raw.get("trademark_function", trademark_function),
                character_name=raw.get("character_name", ""),
                source=raw.get("source", {}),
                pattern_summary=raw.get("pattern_summary", ""),
                voice_register=raw.get("voice_register", ""),
                worldview=raw.get("worldview", ""),
                reaction_patterns=raw.get("reaction_patterns", []),
                blindspots=raw.get("blindspots", []),
                what_they_refuse=raw.get("what_they_refuse", []),
                what_they_fail_at=raw.get("what_they_fail_at", []),
                friction_pattern=raw.get("friction_pattern", ""),
                adaptation_notes=raw.get("adaptation_notes", ""),
                do_not_copy=raw.get("do_not_copy", []),
                raw_markdown=self._voice_entry_to_markdown(raw),
            ))

        self._voice_cache[trademark_function] = entries
        return entries

    # ─── Twist Markdown Parser ───────────────────────────────────────────

    def _parse_twist_markdown(self, text: str, sub_library: str) -> List[TwistLibraryEntry]:
        """Parse twist library markdown into TwistLibraryEntry objects.

        Format per CCG 3 production convention:
        ---
        **Source:** Title — Position Type
        **Mechanism:** ...
        **Subtext:** ...
        **Applicability:** ...
        ---
        """
        entries = []

        # Split on horizontal rules.
        blocks = re.split(r'\n---\n', text)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Extract source line.
            source_match = re.search(
                r'\*\*Source:\*\*\s*(.+?)(?:\s*[—\-]\s*(.+?))?$',
                block, re.MULTILINE
            )
            if not source_match:
                continue

            source_text = source_match.group(1).strip()
            position_text = (source_match.group(2) or "").strip()

            # Map position text to canonical position.
            structural_position = None
            for key, value in _TWIST_POSITION_MAP.items():
                if key.lower() in position_text.lower():
                    structural_position = value
                    break

            if structural_position is None:
                continue  # Skip non-twist entries (e.g., headers, purpose sections)

            # Extract mechanism.
            mech_match = re.search(
                r'\*\*Mechanism:\*\*\s*(.+?)(?=\n\*\*|\Z)',
                block, re.DOTALL
            )
            mechanism = mech_match.group(1).strip() if mech_match else ""

            # Extract subtext.
            sub_match = re.search(
                r'\*\*Subtext:\*\*\s*(.+?)(?=\n\*\*|\Z)',
                block, re.DOTALL
            )
            subtext = sub_match.group(1).strip() if sub_match else ""

            # Build entry_id from source title.
            entry_id = re.sub(r'[^a-z0-9]+', '-', source_text.lower()).strip('-')

            # Parse source into structured dict.
            source_dict = {
                "title": source_text,
                "author_or_director": "",
                "year": 0,
                "medium": "other",
            }

            entries.append(TwistLibraryEntry(
                entry_id=entry_id,
                sub_library=sub_library,
                structural_position=structural_position,
                source=source_dict,
                pattern_summary=mechanism,
                raw_markdown=block,
            ))

        return entries

    # ─── Rendering helpers ───────────────────────────────────────────────

    def _action_scene_entry_to_markdown(self, raw: dict) -> str:
        """Render action scene JSON entry to markdown for prompt injection."""
        lines = []
        source = raw.get("source", {})
        lines.append(f"**Source:** {source.get('title', '')} ({source.get('year', '')}, {source.get('medium', '')})")
        lines.append(f"**Author/Director:** {source.get('author_or_director', '')}")
        lines.append(f"**Scene Type:** {raw.get('scene_type', '')}")
        lines.append(f"**Positions:** {', '.join(raw.get('structural_positions_supported', []))}")
        lines.append(f"**Pattern:** {raw.get('pattern_summary', '')}")
        if raw.get("character_input_requirements"):
            lines.append(f"**Character Requirements:** {'; '.join(raw['character_input_requirements'])}")
        if raw.get("setup_requirements"):
            lines.append(f"**Setup:** {'; '.join(raw['setup_requirements'])}")
        if raw.get("delivers"):
            lines.append(f"**Delivers:** {'; '.join(raw['delivers'])}")
        if raw.get("fidelity_rules"):
            lines.append(f"**Fidelity:** {'; '.join(raw['fidelity_rules'])}")
        if raw.get("forbidden_patterns"):
            lines.append(f"**Forbidden:** {'; '.join(raw['forbidden_patterns'])}")
        return "\n".join(lines)

    def _voice_entry_to_markdown(self, raw: dict) -> str:
        """Render voice JSON entry to markdown for prompt injection."""
        lines = []
        source = raw.get("source", {})
        lines.append(f"**Character:** {raw.get('character_name', '')}")
        lines.append(f"**Source:** {source.get('title', '')} ({source.get('year', '')}, {source.get('medium', '')})")
        lines.append(f"**Trademark Function:** {raw.get('trademark_function', '')}")
        lines.append(f"**Pattern:** {raw.get('pattern_summary', '')}")
        lines.append(f"**Voice Register:** {raw.get('voice_register', '')}")
        lines.append(f"**Worldview:** {raw.get('worldview', '')}")
        if raw.get("reaction_patterns"):
            lines.append(f"**Reactions:** {'; '.join(raw['reaction_patterns'])}")
        if raw.get("friction_pattern"):
            lines.append(f"**Friction:** {raw['friction_pattern']}")
        if raw.get("adaptation_notes"):
            lines.append(f"**Adaptation:** {raw['adaptation_notes']}")
        if raw.get("do_not_copy"):
            lines.append(f"**Do Not Copy:** {'; '.join(raw['do_not_copy'])}")
        return "\n".join(lines)

    # ─── Sampling ────────────────────────────────────────────────────────

    def _sample_round_robin(
        self,
        entries: list,
        max_entries: int,
        structural_position: str,
        key_fn,
    ) -> list:
        """Sample up to max_entries with round-robin across sub-libraries.

        Deterministic seed per §6.4: series_name + book_number + structural_position.
        """
        if len(entries) <= max_entries:
            return entries

        # Group by sub-library.
        groups: dict[str, list] = {}
        for entry in entries:
            key = key_fn(entry)
            groups.setdefault(key, []).append(entry)

        # Deterministic shuffle within each group.
        seed = f"{self._series_name}_{self._book_number}_{structural_position}"
        rng = random.Random(seed)
        for key in groups:
            rng.shuffle(groups[key])

        # Round-robin selection.
        result = []
        group_keys = sorted(groups.keys())
        indices = {k: 0 for k in group_keys}

        while len(result) < max_entries:
            added_this_round = False
            for key in group_keys:
                if len(result) >= max_entries:
                    break
                idx = indices[key]
                if idx < len(groups[key]):
                    result.append(groups[key][idx])
                    indices[key] = idx + 1
                    added_this_round = True
            if not added_this_round:
                break

        return result
