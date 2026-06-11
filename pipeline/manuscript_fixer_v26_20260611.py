"""
ManuscriptFixer — converts findings into manuscript revisions.

F2: Tier 1 surgical fixes + workspace setup + patch logging.
F3: Tier 2 scene regeneration with finding-derived constraints.
Tier 3 (escalation) deferred to F4.
Iteration loop deferred to F5.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from audit_checks import Finding, ManuscriptArtifact, SceneText, BriefBundle
from fixer_preflight import preflight_tier_1
from scene_writer import write_scene, SceneProse


# ── Tier classification ──────────────────────────────────────────────────

_TIER_MAP: dict[str, int] = {
    "MA-001-character-detail-consistency": 2,
    "MA-002-character-name-registry":      2,
    "MA-003-character-location-temporal":   2,
    "MA-004-object-state-continuity":       2,
    "MA-005-pipeline-note-leak":            1,
    "MA-006-reintroduction":                2,
    "MA-007-voice-register-adherence":      2,
    "MA-008-pillar-position-verification":  3,
    "MA-009-word-count-discipline":         3,
}

_MA002_BANNED_KEYWORDS = ("banned",)
_MA007_FORBIDDEN_KEYWORDS = (
    "anaphora", "relative time", "future-tense", "exposition dump", "AI-ism",
)


def classify_tier(finding: Finding) -> int:
    """Map a finding to its tier. Returns 1, 2, or 3."""
    suggested = getattr(finding, "suggested_tier", None)
    if suggested in (1, 2, 3):
        return suggested

    base_tier = _TIER_MAP.get(finding.check_id, 2)
    desc_lower = finding.description.lower()

    if finding.check_id == "MA-002-character-name-registry":
        if any(kw in desc_lower for kw in _MA002_BANNED_KEYWORDS):
            return 1

    if finding.check_id == "MA-007-voice-register-adherence":
        if any(kw in desc_lower for kw in _MA007_FORBIDDEN_KEYWORDS):
            return 1

    return base_tier


# ── Banned-name replacement table ────────────────────────────────────────

_BANNED_NAME_REPLACEMENTS: dict[str, str] = {
    "Sarah":       "Maria",
    "Chen":        "Castro",
    "Marcus":      "Anton",
    "Webb":        "Reyes",
    "Marcus Webb": "Anton Reyes",
}


# ── Result ───────────────────────────────────────────────────────────────

@dataclass
class FixerResult:
    book_dir: Path
    workspace_dir: Path
    iterations: int = 1
    tier_1_applied: int = 0
    tier_1_skipped: int = 0
    tier_2_applied: int = 0
    tier_2_skipped: int = 0
    regeneration_cost_usd: float = 0.0
    regeneration_time_sec: float = 0.0
    scenes_regenerated: list[int] = field(default_factory=list)
    tier_3_escalated: int = 0
    patch_files: list[Path] = field(default_factory=list)
    fixer_log_path: Path | None = None


# ── Helpers ──────────────────────────────────────────────────────────────

_BRACKETED_MARKER_RE = re.compile(
    r"\[(?:NOTE|TODO|TBD|FIXME|XXX|PLACEHOLDER|INSERT|CHECK|TK|"
    r"ACTION|NON-ACTION|MIXED|POV|TYPE|CHAPTER|SCENE)"
    r"(?:[:\s][^\]]*)?\]",
    re.IGNORECASE,
)

# Patterns reused from pipeline_note_leak.py for locating leaked text in scenes
_BOOK_NUMBER_WORDS = r"(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)"
_META_BOOK_REFS = [
    re.compile(r"\bBook\s+" + _BOOK_NUMBER_WORDS + r"['\u2019]s\s+\w+", re.IGNORECASE),
    re.compile(r"\bBook\s+\d+['\u2019]s\s+\w+", re.IGNORECASE),
    re.compile(r"\b(?:in|for|of)\s+Book\s+(?:" + _BOOK_NUMBER_WORDS + r"|\d+)\b", re.IGNORECASE),
    re.compile(r"\bthe\s+(?:next\s+book|sequel|prequel)\b", re.IGNORECASE),
    re.compile(r"\b(?:next|previous|the\s+previous|the\s+next)\s+chapter\b", re.IGNORECASE),
    re.compile(
        r"\b(?:plants?|sets?\s+up|seeds?|establishes?)\s+(?:in\s+)?Chapter\s+(?:"
        + _BOOK_NUMBER_WORDS + r"|\d+)\b", re.IGNORECASE),
    re.compile(r"\bto\s+be\s+continued\b", re.IGNORECASE),
]
_SYNOPSIS_PHRASES = [
    re.compile(r"\bas\s+described\s+in\s+the\s+synopsis\b", re.IGNORECASE),
    re.compile(r"\bper\s+the\s+outline\b", re.IGNORECASE),
    re.compile(r"\bas\s+the\s+synopsis\s+indicates\b", re.IGNORECASE),
    re.compile(r"\bas\s+established\s+in\s+scene\b", re.IGNORECASE),
    re.compile(r"\bseeds?\s+for\s+chapter\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bplant\s+for\s+(?:chapter|scene)\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bsetup\s+for\s+(?:chapter|scene)\s+\d+\b", re.IGNORECASE),
]
_LLM_ARTIFACTS_CLASS_A = [
    re.compile(r"\bAs\s+an\s+AI\b"),
    re.compile(r"\bI\s+cannot\s+generate\b", re.IGNORECASE),
    re.compile(r"\bHere\s+is\s+the\s+(?:next\s+)?scene\b", re.IGNORECASE),
    re.compile(r"\[Generated\s+content\]", re.IGNORECASE),
    re.compile(r"\bNote:\s+I\s+have\b", re.IGNORECASE),
]
_LLM_TICS_CLASS_B = re.compile(
    r'(?:^|\n\n|\.\s+)((?:Of\s+course|Certainly|Indeed),\s)', re.MULTILINE,
)

_BANNED_NAME_FROM_DESC_RE = re.compile(r"[Bb]anned name '([^']+)'")


def _extract_banned_name(finding: Finding) -> str | None:
    """Pull the banned name string from a MA-002 finding's description."""
    m = _BANNED_NAME_FROM_DESC_RE.search(finding.description)
    return m.group(1) if m else None


def _extract_evidence_excerpt(finding: Finding) -> str | None:
    """Pull the raw excerpt from the first evidence string.

    Evidence strings look like: 'Scene 63: ...excerpt...' or 'Scene 63: "excerpt"'
    """
    if not finding.evidence:
        return None
    ev = finding.evidence[0]
    # Strip the "Scene N: " prefix
    colon_pos = ev.find(": ")
    if colon_pos >= 0:
        return ev[colon_pos + 2:]
    return ev


def _find_sentence_containing(text: str, target: str) -> tuple[int, int] | None:
    """Find the sentence boundaries around the first occurrence of target.

    Returns (start, end) indices of the sentence, or None if target not found.
    A sentence starts after a sentence terminator + whitespace (or at text start)
    and ends at the next sentence terminator (. ! ?) including trailing whitespace.
    """
    pos = text.find(target)
    if pos < 0:
        return None

    # Walk backward to find sentence start
    start = pos
    while start > 0:
        if text[start - 1] in '.!?\n' and (start < 2 or text[start - 2] != '.'):
            break
        start -= 1
    # Skip whitespace at start
    while start < pos and text[start] in ' \t':
        start += 1

    # Walk forward from target end to find sentence terminator
    end = pos + len(target)
    while end < len(text):
        if text[end] in '.!?':
            end += 1  # include the terminator
            # Include trailing whitespace (but not double-newline)
            while end < len(text) and text[end] in ' \t':
                end += 1
            break
        end += 1
    else:
        # Reached end of text — sentence runs to end
        pass

    return (start, end)


def _tidy_whitespace(text: str) -> str:
    """Collapse runs of two+ spaces to one; leave newlines alone."""
    return re.sub(r"  +", " ", text)


# ── Tier 2 constants ────────────────────────────────────────────────────

_INTRUSION_BUDGET: dict[str, float] = {
    "ACTION": 0.0,
    "SUSPENSE": 5.0,
    "NON-ACTION": 15.0,
    "MIXED": 15.0,
}

_SYNOPSIS_SCENE_HEADER_RE = re.compile(
    r"^###\s+Scene\s+\d+\b", re.MULTILINE,
)


def load_synopsis_subscene(synopsis_path: Path, flat_scene_number: int) -> str | None:
    """Return the full sub-scene markdown block (header + bullet content) for
    the flat-sequential scene N, or None if not found."""
    if not synopsis_path.exists():
        return None
    text = synopsis_path.read_text(encoding="utf-8")
    headers = list(_SYNOPSIS_SCENE_HEADER_RE.finditer(text))
    if flat_scene_number < 1 or flat_scene_number > len(headers):
        return None
    start = headers[flat_scene_number - 1].start()
    end = headers[flat_scene_number].start() if flat_scene_number < len(headers) else len(text)
    block = text[start:end].rstrip()
    return block


def _group_tier_2_by_scene(findings: list[Finding]) -> dict[int, list[Finding]]:
    """Group Tier 2 findings by scene_number. Findings without a scene are skipped.

    Multi-scene findings (scene_numbers list) are assigned to every listed scene.
    """
    grouped: dict[int, list[Finding]] = {}
    for f in findings:
        scenes: list[int] = []
        if f.scene_numbers:
            scenes = list(f.scene_numbers)
        elif f.scene_number is not None:
            scenes = [f.scene_number]
        for sn in scenes:
            grouped.setdefault(sn, []).append(f)
    return grouped


@dataclass
class _RegenScene:
    """Lightweight scene object for passing to scene_writer.write_scene()."""
    chapter_number: int
    scene_number: int
    title: str
    scene_type: str
    pov: str
    body: str
    position_in_chapter: int = 1


# ── Main class ───────────────────────────────────────────────────────────

class ManuscriptFixer:
    def __init__(self, book_dir: Path, briefs: BriefBundle, llm_client=None,
                 skip_workspace_setup: bool = False, iteration_number: int = 1,
                 manuscript_src: Path | None = None):
        self.book_dir = Path(book_dir)
        self.briefs = briefs
        self.llm_client = llm_client
        self.skip_workspace_setup = skip_workspace_setup
        self.iteration_number = iteration_number
        self.workspace_dir = self.book_dir / "_fixer_workspace"
        # If caller (typically fixer_runner) supplies a manuscript_src, use it.
        # Otherwise fall back to legacy convention for backwards compatibility
        # with tests and any caller that hasn't been updated yet.
        if manuscript_src is not None:
            self.manuscript_src = Path(manuscript_src)
        else:
            self.manuscript_src = self.book_dir / "out" / "manuscript"
        self.workspace_manuscript = self.workspace_dir / "manuscript"
        self.workspace_patches = self.workspace_dir / "patches"
        self.workspace_audit_runs = self.workspace_dir / "audit_runs"
        self.fixer_log_path = self.workspace_dir / "fixer_log.md"
        self._tier_2_from_preflight: list[Finding] = []

    # ── Workspace ────────────────────────────────────────────────────────

    def _setup_workspace(self) -> None:
        """Create workspace, copy manuscript. Overwrites if exists."""
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir)
        self.workspace_manuscript.mkdir(parents=True)
        self.workspace_patches.mkdir(parents=True)
        self.workspace_audit_runs.mkdir(parents=True)
        # Assembler writes per-scene files to <manuscript_dir>/scene_prose/.
        # Legacy/test layouts have them flat under <manuscript_dir>/.
        scene_prose_dir = self.manuscript_src / "scene_prose"
        scan_dir = scene_prose_dir if scene_prose_dir.is_dir() else self.manuscript_src
        for scene_file in sorted(scan_dir.glob("sc_*.md")):
            shutil.copy2(scene_file, self.workspace_manuscript / scene_file.name)
        self._write_log_header()

    def _write_log_header(self) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fixer_log_path.write_text(
            f"# Fixer Run — {ts}\n"
            f"Book: {self.book_dir}\n"
            f"Workspace: _fixer_workspace/ (overwritten if previously existed)\n"
            f"\n",
            encoding="utf-8",
        )

    def _resolve_scene_file(self, scene_number: int) -> Path | None:
        """Return path to scene file in workspace, or None if not found."""
        candidate = self.workspace_manuscript / f"sc_{scene_number:03d}.md"
        return candidate if candidate.exists() else None

    def _build_manuscript_artifact(self) -> ManuscriptArtifact:
        """Build ManuscriptArtifact from workspace scene files."""
        scenes: list[SceneText] = []
        scene_dir = self.workspace_manuscript if self.workspace_manuscript.exists() else self.manuscript_src
        for sf in sorted(scene_dir.glob("sc_*.md")):
            m = re.match(r"sc_(\d+)\.md$", sf.name)
            if m:
                sn = int(m.group(1))
                text = sf.read_text(encoding="utf-8")
                scenes.append(SceneText(scene_number=sn, text=text, file_path=str(sf)))
        return ManuscriptArtifact(scenes=scenes, manuscript_dir=str(scene_dir))

    # ── Operation selection ──────────────────────────────────────────────

    def _choose_operation(
        self, finding: Finding, scene_text: str,
    ) -> tuple[str | None, dict, str | None]:
        """Determine which Tier 1 operation to apply.

        Returns (operation_name, params_dict, target_text) or (None, {}, None)
        if the operation cannot be determined unambiguously.
        """
        desc_lower = finding.description.lower()

        # ── MA-005: pipeline-note-leak ───────────────────────────────────
        if finding.check_id == "MA-005-pipeline-note-leak":
            # Sub-check A: bracketed editorial markers → delete_span
            if "bracketed_editorial_marker" in desc_lower:
                m = _BRACKETED_MARKER_RE.search(scene_text)
                if m:
                    return ("delete_span", {}, m.group(0))
                return (None, {}, None)

            # Sub-check D: stage directions → delete_span
            if "stage_direction" in desc_lower:
                # Stage directions are bracketed or tagged; find in scene
                excerpt = _extract_evidence_excerpt(finding)
                if excerpt:
                    # Try to find an exact bracketed/tagged span
                    stage_re = re.compile(
                        r"\[\s*(?:continued|end\s+of\s+scene|end\s+scene|scene\s+break|"
                        r"chapter\s+break)\s*\]|</?(?:scene|chapter)>",
                        re.IGNORECASE,
                    )
                    m = stage_re.search(scene_text)
                    if m:
                        return ("delete_span", {}, m.group(0))
                return (None, {}, None)

            # Sub-checks B, C, E: meta-narrative, synopsis scaffolding,
            # LLM artifacts → delete_sentence (find the actual pattern in scene)
            if "meta_narrative_reference" in desc_lower:
                for pat in _META_BOOK_REFS:
                    m = pat.search(scene_text)
                    if m:
                        return ("delete_sentence", {}, m.group(0))
                return (None, {}, None)

            if "synopsis_scaffolding" in desc_lower:
                for pat in _SYNOPSIS_PHRASES:
                    m = pat.search(scene_text)
                    if m:
                        return ("delete_sentence", {}, m.group(0))
                return (None, {}, None)

            if "llm_artifact" in desc_lower:
                for pat in _LLM_ARTIFACTS_CLASS_A:
                    m = pat.search(scene_text)
                    if m:
                        return ("delete_sentence", {}, m.group(0))
                return (None, {}, None)

            if "llm_tic_narration" in desc_lower:
                m = _LLM_TICS_CLASS_B.search(scene_text)
                if m:
                    target = m.group(1) if m.lastindex else m.group(0)
                    return ("delete_sentence", {}, target)
                return (None, {}, None)

            # Unknown MA-005 subtype — skip
            return (None, {}, None)

        # ── MA-002: banned-name ──────────────────────────────────────────
        if finding.check_id == "MA-002-character-name-registry":
            banned_name = _extract_banned_name(finding)
            if not banned_name:
                return (None, {}, None)

            # Try longest match first (e.g. "Marcus Webb" before "Marcus")
            for name in sorted(_BANNED_NAME_REPLACEMENTS, key=len, reverse=True):
                if name in banned_name and name in scene_text:
                    replacement = _BANNED_NAME_REPLACEMENTS[name]
                    return ("replace_span", {"replacement": replacement}, name)

            # Banned name not in replacement table
            return (None, {"skip_reason": "no replacement registered"}, banned_name)

        # ── MA-007: forbidden-pattern ────────────────────────────────────
        if finding.check_id == "MA-007-voice-register-adherence":
            excerpt = _extract_evidence_excerpt(finding)
            if not excerpt:
                return (None, {}, None)

            clean = excerpt.strip(".")
            if clean and clean in scene_text:
                # Forbidden patterns typically occupy a line or a short span
                # Check if the pattern occupies a full line
                for line in scene_text.split("\n"):
                    if clean in line and line.strip():
                        return ("delete_line", {}, clean)
                # Fallback to delete_span
                return ("delete_span", {}, clean)

            return (None, {}, None)

        # Unknown check_id at Tier 1 — should not happen but be safe
        return (None, {}, None)

    # ── Operation execution ──────────────────────────────────────────────

    def _execute_operation(
        self, scene_text: str, operation: str, params: dict, target: str,
    ) -> str:
        """Apply an operation. Returns new scene text (or unchanged if target not found)."""
        if operation == "delete_span":
            if target not in scene_text:
                return scene_text
            result = scene_text.replace(target, "", 1)
            return _tidy_whitespace(result)

        if operation == "replace_span":
            if target not in scene_text:
                return scene_text
            return scene_text.replace(target, params["replacement"], 1)

        if operation == "delete_sentence":
            bounds = _find_sentence_containing(scene_text, target)
            if bounds is None:
                return scene_text
            start, end = bounds
            result = scene_text[:start] + scene_text[end:]
            return _tidy_whitespace(result)

        if operation == "delete_line":
            if target not in scene_text:
                return scene_text
            lines = scene_text.split("\n")
            new_lines = [line for line in lines if target not in line]
            return "\n".join(new_lines)

        return scene_text

    # ── Tier 1 application ───────────────────────────────────────────────

    def _apply_tier_1(self, finding: Finding) -> dict:
        """Apply a single Tier 1 fix; return patch entry dict."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sn = finding.scene_number or (
            finding.scene_numbers[0] if finding.scene_numbers else None
        )

        if sn is None:
            return {
                "fixer_iteration": self.iteration_number, "timestamp": ts,
                "check_id": finding.check_id, "tier": 1,
                "success": False, "skip_reason": "no scene number on finding",
            }

        scene_path = self._resolve_scene_file(sn)
        if scene_path is None:
            return {
                "fixer_iteration": self.iteration_number, "timestamp": ts,
                "check_id": finding.check_id, "tier": 1, "scene_number": sn,
                "success": False, "skip_reason": "scene file not found",
            }

        scene_text = scene_path.read_text(encoding="utf-8")
        operation, params, target_text = self._choose_operation(finding, scene_text)

        if operation is None:
            skip_reason = params.get("skip_reason", "operation could not be determined")
            return {
                "fixer_iteration": self.iteration_number, "timestamp": ts,
                "check_id": finding.check_id, "tier": 1, "scene_number": sn,
                "success": False, "skip_reason": skip_reason,
            }

        # ── Pre-flight judgment ─────────────────────────────────────────
        manuscript = self._build_manuscript_artifact()
        pf = preflight_tier_1(
            finding=finding,
            operation=operation,
            params=params,
            target_text=target_text,
            scene_text=scene_text,
            scene_number=sn,
            manuscript=manuscript,
            briefs=self.briefs,
            llm_callable=self.llm_client,
        )

        if pf.decision == "ESCALATE_TIER_2":
            self._tier_2_from_preflight.append(finding)
            return {
                "fixer_iteration": self.iteration_number, "timestamp": ts,
                "check_id": finding.check_id, "tier": 1, "scene_number": sn,
                "operation": operation,
                "target_text": (target_text[:120] if target_text else None),
                "success": False,
                "preflight_decision": pf.decision,
                "preflight_reasoning": pf.reasoning,
                "preflight_checks_run": pf.checks_run,
                "preflight_checks_failed": pf.checks_failed,
                "escalated_to": "tier_2",
            }

        if pf.decision == "ESCALATE_TIER_3":
            return {
                "fixer_iteration": self.iteration_number, "timestamp": ts,
                "check_id": finding.check_id, "tier": 1, "scene_number": sn,
                "operation": operation,
                "target_text": (target_text[:120] if target_text else None),
                "success": False,
                "preflight_decision": pf.decision,
                "preflight_reasoning": pf.reasoning,
                "preflight_checks_run": pf.checks_run,
                "preflight_checks_failed": pf.checks_failed,
                "escalated_to": "tier_3",
            }

        # ── Pre-flight passed (APPLY) — execute operation ───────────────
        new_text = self._execute_operation(scene_text, operation, params, target_text)
        if new_text == scene_text:
            return {
                "fixer_iteration": self.iteration_number, "timestamp": ts,
                "check_id": finding.check_id, "tier": 1, "scene_number": sn,
                "operation": operation,
                "success": False, "skip_reason": "target text not found in scene",
            }

        scene_path.write_text(new_text, encoding="utf-8")
        return {
            "fixer_iteration": self.iteration_number, "timestamp": ts,
            "check_id": finding.check_id, "tier": 1, "scene_number": sn,
            "operation": operation,
            "target_text": (target_text[:120] if target_text else None),
            "success": True,
            "preflight_decision": "APPLY",
        }

    # ── Tier 2 — Scene regeneration ─────────────────────────────────────

    def _get_scene_text(self, scene_number: int) -> str:
        """Read scene text from workspace, return empty string if missing."""
        p = self._resolve_scene_file(scene_number)
        return p.read_text(encoding="utf-8") if p else ""

    def _build_constraint_block(
        self, scene_number: int, findings_for_scene: list[Finding],
    ) -> str:
        """Build the CORRECTIONS REQUIRED + NARRATIVE/VOICE CONTINUITY block."""
        # ── CORRECTIONS REQUIRED ────────────────────────────────────────
        correction_lines: list[str] = []
        for f in findings_for_scene:
            tag = f.check_id.split("-", 1)[1] if "-" in f.check_id else f.check_id
            evidence_str = ""
            if f.evidence:
                joined = " | ".join(e[:200] for e in f.evidence)
                evidence_str = f"\n  Evidence: {joined}"
            correction_lines.append(f"[{tag}]\n- {f.description}{evidence_str}")

        corrections_text = "CORRECTIONS REQUIRED:\n\n" + "\n\n".join(correction_lines)

        # ── NARRATIVE CONTINUITY ────────────────────────────────────────
        narrative_lines: list[str] = ["\n\nNARRATIVE CONTINUITY:"]
        if scene_number <= 1:
            narrative_lines.append("\n- BOOK OPENING — no prior scene.")
        else:
            prior_text = self._get_scene_text(scene_number - 1)
            if prior_text:
                ending = prior_text.strip()[-200:]
                narrative_lines.append(
                    f"\n- Prior scene (sc_{scene_number - 1:03d}) ended with: {ending}"
                )

        next_text = self._get_scene_text(scene_number + 1)
        if next_text:
            opening = next_text.strip()[:200]
            narrative_lines.append(
                f"\n- Following scene (sc_{scene_number + 1:03d}) begins with: {opening}"
            )
        else:
            narrative_lines.append("\n- BOOK ENDING — no subsequent scene.")

        # ── VOICE CONTINUITY ────────────────────────────────────────────
        voice_lines = [
            "\n\nVOICE CONTINUITY:",
            "\n- Match the prose voice of the original scene (provided as reference).",
            "\n- Do not introduce stylistic shifts.",
        ]

        return corrections_text + "".join(narrative_lines) + "".join(voice_lines)

    def _build_regen_scene(
        self, scene_number: int, synopsis_path: Path,
    ) -> _RegenScene | None:
        """Build a _RegenScene from the synopsis subscene text."""
        from audit_checks._lib.synopsis_scene_types import load_scene_type_map

        subscene = load_synopsis_subscene(synopsis_path, scene_number)
        if subscene is None:
            return None

        # Parse TYPE and POV from the header line
        type_map = load_scene_type_map(synopsis_path)
        scene_type = type_map.get(scene_number, "MIXED")

        pov = ""
        pov_m = re.search(r"\[POV:\s*([^\]]+)\]", subscene, re.IGNORECASE)
        if pov_m:
            pov = pov_m.group(1).strip()

        title = ""
        title_m = re.match(r"###\s+Scene\s+\d+\s*—\s*(.+?)(?:\s*\[)", subscene)
        if title_m:
            title = title_m.group(1).strip()

        # Chapter number: count which chapter this scene falls in
        # (for the user prompt's "Chapter N, Scene M" label)
        chapter_number = 1
        if synopsis_path.exists():
            full_text = synopsis_path.read_text(encoding="utf-8")
            chapter_re = re.compile(r"^##\s+Chapter\s+(\d+)", re.MULTILINE)
            scene_header_re = re.compile(r"^###\s+Scene\s+\d+", re.MULTILINE)
            current_ch = 1
            flat = 0
            for m in re.finditer(r"^(##\s+Chapter\s+\d+|###\s+Scene\s+\d+)", full_text, re.MULTILINE):
                line = m.group(0)
                if line.startswith("## Chapter"):
                    ch_m = chapter_re.match(line)
                    if ch_m:
                        current_ch = int(ch_m.group(1))
                elif line.startswith("### Scene"):
                    flat += 1
                    if flat == scene_number:
                        chapter_number = current_ch
                        break

        return _RegenScene(
            chapter_number=chapter_number,
            scene_number=scene_number,
            title=title,
            scene_type=scene_type,
            pov=pov,
            body=subscene,
        )

    def _regenerate_scene(
        self, scene_number: int, findings_for_scene: list[Finding],
        synopsis_path: Path, series_bible: dict, character_profiles: dict,
        craft_principles: list,
    ) -> dict:
        """Regenerate a single scene with Tier 2 constraints. Returns patch entry."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        base_entry = {
            "fixer_iteration": self.iteration_number, "timestamp": ts,
            "scene_number": scene_number, "tier": 2,
            "operation": "scene_regeneration",
            "findings_addressed": [
                {"check_id": f.check_id, "severity": f.severity}
                for f in findings_for_scene
            ],
        }

        # Build scene object from synopsis
        regen_scene = self._build_regen_scene(scene_number, synopsis_path)
        if regen_scene is None:
            return {**base_entry, "success": False, "failure_reason": "synopsis_lookup_failed"}

        # Read original scene text
        scene_path = self._resolve_scene_file(scene_number)
        if scene_path is None:
            return {**base_entry, "success": False, "failure_reason": "scene_file_not_found"}
        original_text = scene_path.read_text(encoding="utf-8")
        original_wc = len(original_text.split())

        # Build constraint block
        constraint_block = self._build_constraint_block(scene_number, findings_for_scene)

        # Include original scene as voice reference in the constraint block
        orig_ref = original_text[:2000] if len(original_text) > 2000 else original_text
        constraint_block += (
            f"\n\nORIGINAL SCENE (voice reference — match this register):\n{orig_ref}"
        )

        # Build prior/subsequent window context for adjacent param
        # Pass as None — all context is in the constraint block
        adjacent = {"prior": None, "next": None}

        # Call scene_writer with retry
        last_error = None
        result_prose: SceneProse | None = None
        t0 = time.time()

        for attempt in range(2):
            try:
                result_prose = write_scene(
                    scene=regen_scene,
                    adjacent=adjacent,
                    series_bible=series_bible,
                    character_profiles=character_profiles,
                    craft_principles=craft_principles,
                    corrections=constraint_block,
                    entity_ledger=self.briefs.entity_ledger if self.briefs else None,
                )
                last_error = None
                break
            except Exception as e:
                last_error = e

        elapsed = time.time() - t0

        if last_error is not None or result_prose is None:
            return {
                **base_entry,
                "success": False, "failure_reason": "llm_api_error",
                "original_word_count": original_wc,
                "regeneration_time_sec": round(elapsed, 1),
            }

        new_text = result_prose.prose
        new_wc = len(new_text.split())

        # Validate: too short
        if new_wc < 200:
            return {
                **base_entry, "success": False, "failure_reason": "regeneration_too_short",
                "original_word_count": original_wc, "new_word_count": new_wc,
                "regeneration_time_sec": round(elapsed, 1),
            }

        # Validate: runaway
        if new_wc > 2 * original_wc:
            return {
                **base_entry, "success": False, "failure_reason": "regeneration_runaway",
                "original_word_count": original_wc, "new_word_count": new_wc,
                "regeneration_time_sec": round(elapsed, 1),
            }

        # Compute cost from tokens
        cost = 0.0
        tokens = result_prose.tokens_used
        if tokens:
            inp = tokens.get("input_tokens", 0)
            out = tokens.get("output_tokens", 0)
            # sonnet pricing: $3/M input, $15/M output
            cost = (inp * 3.0 + out * 15.0) / 1_000_000

        # Write regenerated text to workspace
        scene_path.write_text(new_text, encoding="utf-8")

        return {
            **base_entry,
            "success": True,
            "original_word_count": original_wc,
            "new_word_count": new_wc,
            "constraint_block_excerpt": constraint_block[:300],
            "regeneration_cost_usd": round(cost, 4),
            "regeneration_time_sec": round(elapsed, 1),
        }

    def _apply_tier_2(
        self, tier_2_findings: list[Finding], result: FixerResult,
    ) -> list[dict]:
        """Apply all Tier 2 regenerations. Returns list of patch entries."""
        grouped = _group_tier_2_by_scene(tier_2_findings)
        if not grouped:
            return []

        # Load shared resources
        synopsis_path = self.book_dir / "work" / "synopsis.md"
        series_dir = self.book_dir.parent  # e.g., /anpd/v25/series/black_tide
        series_bible_path = series_dir / "series_bible.json"
        char_profiles_path = series_dir / "character_profiles.json"
        craft_principles_path = Path(
            os.path.dirname(os.path.abspath(__file__))
        ).parent / "principles" / "craft_principles.json"

        series_bible = {}
        if series_bible_path.exists():
            series_bible = json.loads(series_bible_path.read_text(encoding="utf-8"))
        character_profiles = {}
        if char_profiles_path.exists():
            character_profiles = json.loads(char_profiles_path.read_text(encoding="utf-8"))
        craft_principles = []
        if craft_principles_path.exists():
            pdata = json.loads(craft_principles_path.read_text(encoding="utf-8"))
            craft_principles = pdata.get("principles", pdata if isinstance(pdata, list) else [])

        entries: list[dict] = []
        for sn in sorted(grouped):
            entry = self._regenerate_scene(
                sn, grouped[sn], synopsis_path,
                series_bible, character_profiles, craft_principles,
            )
            entries.append(entry)
            if entry.get("success"):
                result.tier_2_applied += 1
                result.scenes_regenerated.append(sn)
                result.regeneration_cost_usd += entry.get("regeneration_cost_usd", 0.0)
                result.regeneration_time_sec += entry.get("regeneration_time_sec", 0.0)
            else:
                result.tier_2_skipped += 1

        return entries

    # ── Run ──────────────────────────────────────────────────────────────

    def run(self, findings: list[Finding]) -> FixerResult:
        """Apply Tier 1 surgical fixes, then Tier 2 scene regeneration.

        Tier 1 runs first so that regeneration sees Tier-1-cleaned text.
        Tier 3 findings are recorded but not actioned (escalation deferred to F4).
        """
        if not self.skip_workspace_setup:
            self._setup_workspace()
        result = FixerResult(book_dir=self.book_dir, workspace_dir=self.workspace_dir)
        patches_by_scene: dict[int, list[dict]] = {}
        log_entries: list[dict] = []

        tier_1_findings: list[Finding] = []
        tier_2_findings: list[Finding] = []
        tier_3_findings: list[Finding] = []

        for finding in findings:
            tier = classify_tier(finding)
            if tier == 1:
                tier_1_findings.append(finding)
            elif tier == 2:
                tier_2_findings.append(finding)
            elif tier == 3:
                tier_3_findings.append(finding)

        # ── Tier 1 ──────────────────────────────────────────────────────
        for finding in tier_1_findings:
            entry = self._apply_tier_1(finding)
            sn = entry.get("scene_number", 0) or 0
            if entry.get("success"):
                result.tier_1_applied += 1
            else:
                result.tier_1_skipped += 1
            patches_by_scene.setdefault(sn, []).append(entry)
            log_entries.append(entry)

        # ── Tier 2 (including pre-flight escalations from Tier 1) ──────
        tier_2_findings.extend(self._tier_2_from_preflight)
        tier_2_entries = self._apply_tier_2(tier_2_findings, result)
        for entry in tier_2_entries:
            sn = entry.get("scene_number", 0) or 0
            patches_by_scene.setdefault(sn, []).append(entry)
            log_entries.append(entry)

        # ── Tier 3 (deferred to F4) ─────────────────────────────────────
        for finding in tier_3_findings:
            sn = finding.scene_number or (
                finding.scene_numbers[0] if finding.scene_numbers else None
            )
            result.tier_3_escalated += 1
            entry = {
                "fixer_iteration": self.iteration_number,
                "check_id": finding.check_id, "tier": 3, "scene_number": sn,
                "success": False, "deferred_to": "F4",
                "description": finding.description[:200],
            }
            scene_key = sn if sn is not None else 0
            patches_by_scene.setdefault(scene_key, []).append(entry)
            log_entries.append(entry)

        # Write patch files
        for sn, entries in sorted(patches_by_scene.items()):
            patch_path = self.workspace_patches / f"sc_{sn:03d}.patch.json"
            patch_path.write_text(
                json.dumps({"scene_number": sn, "patches": entries}, indent=2),
                encoding="utf-8",
            )
            result.patch_files.append(patch_path)

        self._append_log_summary(result, log_entries, len(findings))
        result.fixer_log_path = self.fixer_log_path
        return result

    # ── Log summary ──────────────────────────────────────────────────────

    def _append_log_summary(
        self, result: FixerResult, log_entries: list[dict], total_findings: int,
    ) -> None:
        """Append the human-readable summary to fixer_log.md."""
        lines: list[str] = []
        lines.append(f"Findings input: {total_findings}\n")

        # Tier 1 applied
        applied = [e for e in log_entries if e.get("tier") == 1 and e.get("success")]
        lines.append(f"\n## Tier 1 — Surgical fixes applied: {len(applied)}")
        for e in applied:
            sn = e.get("scene_number", "?")
            op = e.get("operation", "?")
            target = e.get("target_text", "")
            target_display = f' "{target}"' if target else ""
            lines.append(f"- sc_{sn:03d}: {op}{target_display} ({e['check_id']})")

        # Tier 1 skipped
        skipped = [e for e in log_entries if e.get("tier") == 1 and not e.get("success")]
        lines.append(f"\n## Tier 1 — Skipped: {len(skipped)}")
        for e in skipped:
            sn = e.get("scene_number", "?")
            reason = e.get("skip_reason", "unknown")
            lines.append(f"- sc_{sn:03d}: {e['check_id']} — {reason}" if isinstance(sn, int)
                         else f"- (no scene): {e['check_id']} — {reason}")

        # Tier 2 applied
        t2_applied = [e for e in log_entries if e.get("tier") == 2 and e.get("success")]
        checks_fmt = lambda e: ", ".join(
            fa["check_id"] for fa in e.get("findings_addressed", [])
        )
        lines.append(f"\n## Tier 2 — Scene regeneration applied: {len(t2_applied)}")
        for e in t2_applied:
            sn = e.get("scene_number", "?")
            owc = e.get("original_word_count", "?")
            nwc = e.get("new_word_count", "?")
            cost = e.get("regeneration_cost_usd", 0)
            sec = e.get("regeneration_time_sec", 0)
            lines.append(
                f"- sc_{sn:03d}: regenerated for [{checks_fmt(e)}] "
                f"({owc}\u2192{nwc} words, ${cost:.3f}, {sec:.1f}s)"
            )

        # Tier 2 skipped
        t2_skipped = [e for e in log_entries if e.get("tier") == 2 and not e.get("success")]
        lines.append(f"\n## Tier 2 — Scene regeneration skipped: {len(t2_skipped)}")
        for e in t2_skipped:
            sn = e.get("scene_number", "?")
            reason = e.get("failure_reason", "unknown")
            lines.append(f"- sc_{sn:03d}: SKIP {reason} (will retry next iteration)")

        # Tier 2 totals
        lines.append(
            f"\n## Tier 2 totals: {result.tier_2_applied} applied, "
            f"{result.tier_2_skipped} skipped, "
            f"${result.regeneration_cost_usd:.2f} cost, "
            f"{result.regeneration_time_sec / 60:.1f} min wall time"
        )

        # Tier 3 escalated
        escalated = [e for e in log_entries if e.get("tier") == 3]
        lines.append(f"\n## Tier 3 — Escalated (operator review not yet implemented): {len(escalated)}")
        for e in escalated:
            sn = e.get("scene_number")
            desc = e.get("description", e.get("check_id", ""))
            if sn is not None:
                lines.append(f"- sc_{sn:03d}: {desc}")
            else:
                lines.append(f"- (book-level): {desc}")

        lines.append("")

        with open(self.fixer_log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
