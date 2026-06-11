"""
fixer_preflight — Judgment layer between operation selection and execution.

Evaluates whether a candidate Tier 1 mechanical fix is safe in context.
Routes each finding to APPLY / ESCALATE_TIER_2 / ESCALATE_TIER_3.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from audit_checks import ManuscriptArtifact, BriefBundle


# ── Result ──────────────────────────────────────────────────────────────────

@dataclass
class PreFlightResult:
    decision: str   # "APPLY" | "ESCALATE_TIER_2" | "ESCALATE_TIER_3"
    reasoning: str
    checks_run: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)


# ── Bracketed marker regex (duplicated from manuscript_fixer to avoid
# circular import — fixer_preflight is imported by manuscript_fixer) ──────

_BRACKETED_MARKER_RE = re.compile(
    r"\[(?:NOTE|TODO|TBD|FIXME|XXX|PLACEHOLDER|INSERT|CHECK|TK|"
    r"ACTION|NON-ACTION|MIXED|POV|TYPE|CHAPTER|SCENE)"
    r"(?:[:\s][^\]]*)?\]",
    re.IGNORECASE,
)


# ── Individual check functions ──────────────────────────────────────────────
# Each returns (passed: bool, reason: str).

def _check_replacement_already_in_manuscript(
    replacement: str, scene_number: int, manuscript: ManuscriptArtifact,
) -> tuple[bool, str]:
    """Fail if replacement text appears as a whole word in any OTHER scene."""
    pat = re.compile(r"\b" + re.escape(replacement) + r"\b")
    for scene in manuscript.scenes:
        if scene.scene_number == scene_number:
            continue
        if pat.search(scene.text):
            return False, f"replacement '{replacement}' already appears in scene {scene.scene_number}"
    return True, "no collision"


def _check_replacement_is_known_character(
    replacement: str, briefs: BriefBundle,
) -> tuple[bool, str]:
    """Fail if replacement matches a known character name (case-insensitive)."""
    characters = briefs.character_profiles.get("characters", [])
    repl_lower = replacement.lower()
    for entry in characters:
        if isinstance(entry, dict):
            name = entry.get("name", "")
            if name.lower() == repl_lower:
                return False, f"replacement '{replacement}' is a known character '{name}'"
            for alias in entry.get("aliases", []):
                if alias.lower() == repl_lower:
                    return False, f"replacement '{replacement}' is alias of '{name}'"
    return True, "replacement is not a known character"


def _check_target_is_canonical_character(
    target_text: str, briefs: BriefBundle,
) -> tuple[bool, str]:
    """Fail if target text IS a canonical character name."""
    characters = briefs.character_profiles.get("characters", [])
    target_lower = target_text.lower().strip()
    for entry in characters:
        if isinstance(entry, dict):
            name = entry.get("name", "")
            if name.lower() == target_lower:
                return False, f"target '{target_text}' is canonical character '{name}'"
            for alias in entry.get("aliases", []):
                if alias.lower() == target_lower:
                    return False, f"target '{target_text}' is alias of '{name}'"
    return True, "target is not a canonical character"


def _check_target_in_series_bible(
    target_text: str, briefs: BriefBundle,
) -> tuple[bool, str]:
    """Fail if target text appears in the series bible (case-insensitive substring)."""
    if not briefs.series_bible:
        return True, "no series bible loaded"
    bible_str = json.dumps(briefs.series_bible).lower()
    if target_text.lower() in bible_str:
        return False, f"target '{target_text}' appears in series bible"
    return True, "target not in series bible"


def _check_target_appears_only_once_in_scene(
    target_text: str, scene_text: str,
) -> tuple[bool, str]:
    """Fail (escalate to T2) if target appears more than once in scene."""
    count = scene_text.count(target_text)
    if count == 0:
        return False, f"target '{target_text[:40]}' not found in scene"
    if count > 1:
        return False, f"target '{target_text[:40]}' appears {count} times in scene (ambiguous)"
    return True, "target appears exactly once"


def _check_target_is_well_formed_marker(target_text: str) -> tuple[bool, str]:
    """Pass if target matches a well-formed bracketed marker pattern."""
    if _BRACKETED_MARKER_RE.fullmatch(target_text.strip()):
        return True, "target is a well-formed bracketed marker"
    return False, "target is not a well-formed bracketed marker"


def _check_target_is_complete_sentence(
    target_text: str, scene_text: str,
) -> tuple[bool, str]:
    """Pass if target is bounded by sentence terminators."""
    pos = scene_text.find(target_text)
    if pos < 0:
        return False, "target not found in scene"
    # Check character before: must be sentence-end, newline, or start-of-text
    if pos > 0:
        before = scene_text[pos - 1]
        # Allow whitespace before if preceded by terminator
        check_pos = pos - 1
        while check_pos > 0 and scene_text[check_pos] in " \t":
            check_pos -= 1
        if check_pos >= 0 and scene_text[check_pos] not in ".!?\n":
            if check_pos != 0 or scene_text[0] not in ".!?\n":
                return False, "target does not start at a sentence boundary"
    # Check last character of target
    stripped = target_text.rstrip()
    if stripped and stripped[-1] not in ".!?":
        return False, "target does not end with sentence terminator"
    return True, "target is a complete sentence"


def _check_deletion_does_not_orphan_paragraph(
    target_text: str, scene_text: str,
) -> tuple[bool, str]:
    """Fail if removing target would leave an empty paragraph."""
    pos = scene_text.find(target_text)
    if pos < 0:
        return False, "target not found in scene"
    # Find paragraph boundaries (double-newline)
    para_start = scene_text.rfind("\n\n", 0, pos)
    para_start = para_start + 2 if para_start >= 0 else 0
    para_end = scene_text.find("\n\n", pos + len(target_text))
    if para_end < 0:
        para_end = len(scene_text)
    paragraph = scene_text[para_start:para_end]
    # Remove target from paragraph
    remaining = paragraph.replace(target_text, "", 1).strip()
    if not remaining:
        return False, "deletion would orphan (empty) the paragraph"
    return True, "paragraph retains content after deletion"


def _check_target_occupies_full_line(
    target_text: str, scene_text: str,
) -> tuple[bool, str]:
    """Pass if target matches a full line (trimmed) in the scene."""
    target_stripped = target_text.strip()
    for line in scene_text.split("\n"):
        if line.strip() == target_stripped:
            return True, "target occupies a full line"
    return False, "target does not occupy a full line"


# ── LLM check ──────────────────────────────────────────────────────────────

def _check_llm_surrounding_integrity(
    operation: str, target_text: str, params: dict,
    scene_text: str, scene_number: int, llm_callable,
) -> tuple[bool, str]:
    """Ask LLM whether the mechanical fix preserves sentence/story integrity."""
    # Build a window around the target
    pos = scene_text.find(target_text)
    if pos < 0:
        return False, "target not found in scene for LLM check"

    window_start = max(0, pos - 200)
    window_end = min(len(scene_text), pos + len(target_text) + 200)
    context = scene_text[window_start:window_end]

    if operation == "replace_span":
        replacement = params.get("replacement", "")
        action_desc = f'Replace "{target_text}" with "{replacement}"'
    elif operation in ("delete_span", "delete_sentence", "delete_line"):
        action_desc = f'Delete: "{target_text}"'
    else:
        action_desc = f'{operation}: "{target_text}"'

    prompt = (
        f"You are a manuscript integrity checker. A mechanical fixer wants to "
        f"apply this operation to scene {scene_number}:\n\n"
        f"Operation: {action_desc}\n\n"
        f"Surrounding context:\n```\n{context}\n```\n\n"
        f"Would this operation preserve grammatical correctness and narrative "
        f"coherence of the surrounding text? Answer on the first line with "
        f"exactly one word: SAFE, RISKY, or UNSAFE. On the second line, "
        f"give a brief reason."
    )

    try:
        response = llm_callable(
            provider="anthropic",
            model="claude-sonnet-4-5",
            system="You are a manuscript integrity checker. Be concise.",
            user=prompt,
            max_tokens=100,
            temperature=0.0,
        )
        text = response.text.strip()
    except Exception as exc:
        return False, f"LLM call failed: {exc}"

    # Parse response
    lines = text.split("\n", 1)
    verdict = lines[0].strip().upper()
    reason = lines[1].strip() if len(lines) > 1 else "no reason given"

    if verdict == "SAFE":
        return True, f"LLM: SAFE — {reason}"
    elif verdict == "RISKY":
        return False, f"LLM: RISKY — {reason}"
    elif verdict == "UNSAFE":
        return False, f"LLM: UNSAFE — {reason}"
    else:
        return False, f"LLM response unparseable: {text[:100]}"


# ── Main dispatcher ─────────────────────────────────────────────────────────

def preflight_tier_1(
    finding,
    operation: str,
    params: dict,
    target_text: str,
    scene_text: str,
    scene_number: int,
    manuscript: ManuscriptArtifact,
    briefs: BriefBundle,
    llm_callable=None,
) -> PreFlightResult:
    """Run pre-flight checks for a Tier 1 operation.

    Returns PreFlightResult with decision APPLY, ESCALATE_TIER_2, or
    ESCALATE_TIER_3.
    """
    checks_run: list[str] = []
    checks_failed: list[str] = []

    def _run(name: str, check_fn, *, escalate_to: str = "ESCALATE_TIER_3"):
        checks_run.append(name)
        passed, reason = check_fn()
        if not passed:
            checks_failed.append(name)
            return PreFlightResult(
                decision=escalate_to,
                reasoning=reason,
                checks_run=list(checks_run),
                checks_failed=list(checks_failed),
            )
        return None

    if operation == "replace_span":
        replacement = params.get("replacement", "")

        # 1. Replacement not already in manuscript (collision → T3)
        r = _run("replacement_already_in_manuscript",
                 lambda: _check_replacement_already_in_manuscript(replacement, scene_number, manuscript))
        if r: return r

        # 2. Replacement is not a known character (→ T3)
        r = _run("replacement_is_known_character",
                 lambda: _check_replacement_is_known_character(replacement, briefs))
        if r: return r

        # 3. Target is not a canonical character (→ T3)
        r = _run("target_is_canonical_character",
                 lambda: _check_target_is_canonical_character(target_text, briefs))
        if r: return r

        # 4. Target in series bible (→ T3)
        r = _run("target_in_series_bible",
                 lambda: _check_target_in_series_bible(target_text, briefs))
        if r: return r

        # 5. Target appears only once (ambiguous → T2)
        r = _run("target_appears_only_once",
                 lambda: _check_target_appears_only_once_in_scene(target_text, scene_text),
                 escalate_to="ESCALATE_TIER_2")
        if r: return r

        # LLM check
        if llm_callable is not None:
            r = _run("llm_surrounding_integrity",
                     lambda: _check_llm_surrounding_integrity(
                         operation, target_text, params, scene_text, scene_number, llm_callable))
            if r: return r

    elif operation == "delete_span":
        # 1. Is target a well-formed marker? If so, skip LLM.
        is_marker_result = _check_target_is_well_formed_marker(target_text)
        checks_run.append("target_is_well_formed_marker")
        skip_llm = is_marker_result[0]

        # 2. Target appears only once (→ T2)
        r = _run("target_appears_only_once",
                 lambda: _check_target_appears_only_once_in_scene(target_text, scene_text),
                 escalate_to="ESCALATE_TIER_2")
        if r: return r

        # 3. Deletion does not orphan paragraph (→ T3)
        r = _run("deletion_does_not_orphan_paragraph",
                 lambda: _check_deletion_does_not_orphan_paragraph(target_text, scene_text))
        if r: return r

        # LLM check (only for non-marker targets)
        if not skip_llm and llm_callable is not None:
            r = _run("llm_surrounding_integrity",
                     lambda: _check_llm_surrounding_integrity(
                         operation, target_text, params, scene_text, scene_number, llm_callable))
            if r: return r

    elif operation == "delete_sentence":
        # For delete_sentence, target_text is the pattern that locates the
        # sentence — the operation itself finds and deletes the full sentence
        # containing this target. So we check that the target exists and
        # that the containing paragraph won't be orphaned.

        # 1. Target appears only once (→ T2, ensures unambiguous sentence location)
        r = _run("target_appears_only_once",
                 lambda: _check_target_appears_only_once_in_scene(target_text, scene_text),
                 escalate_to="ESCALATE_TIER_2")
        if r: return r

        # 2. Deletion does not orphan paragraph (→ T3)
        # Use the full sentence bounds for this check
        from manuscript_fixer import _find_sentence_containing
        bounds = _find_sentence_containing(scene_text, target_text)
        if bounds:
            sentence = scene_text[bounds[0]:bounds[1]]
            r = _run("deletion_does_not_orphan_paragraph",
                     lambda: _check_deletion_does_not_orphan_paragraph(sentence, scene_text))
            if r: return r

        # LLM check
        if llm_callable is not None:
            r = _run("llm_surrounding_integrity",
                     lambda: _check_llm_surrounding_integrity(
                         operation, target_text, params, scene_text, scene_number, llm_callable))
            if r: return r

    elif operation == "delete_line":
        # 1. Target occupies full line (→ T3)
        r = _run("target_occupies_full_line",
                 lambda: _check_target_occupies_full_line(target_text, scene_text))
        if r: return r

        # 2. Deletion does not orphan paragraph (→ T3)
        r = _run("deletion_does_not_orphan_paragraph",
                 lambda: _check_deletion_does_not_orphan_paragraph(target_text, scene_text))
        if r: return r

        # No LLM check for delete_line

    return PreFlightResult(
        decision="APPLY",
        reasoning="all checks passed",
        checks_run=checks_run,
        checks_failed=[],
    )
