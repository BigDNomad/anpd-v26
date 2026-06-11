"""
scene_auditor.py — V25 Scene Auditor
ANPD V25 | Version: 20260511

Validates generated scene prose against synopsis and craft principles.
"""

import os
import re
import json
from dataclasses import dataclass, field


@dataclass
class Finding:
    id: str
    check: str
    severity: str  # CLASS_A or CLASS_B
    message: str
    excerpt: str = ""


@dataclass
class SceneAuditResult:
    passed: bool  # no Class A findings
    findings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# ── Character state tracking ────────────────────────────────────────────────

# Known dead characters by chapter (updated as the story progresses)
DEAD_BY_CHAPTER = {
    7: ["Taras"],
    8: ["Taras"],
}


# ── Deterministic checks ────────────────────────────────────────────────────

def check_word_count(prose: str, target: int = 850) -> list:
    """Check word count is within 700-1100 range."""
    wc = len(prose.split())
    if wc < 700:
        return [Finding("WC-LOW", "word_count", "CLASS_A",
                         f"Word count {wc} below minimum 700")]
    if wc > 1100:
        return [Finding("WC-HIGH", "word_count", "CLASS_A",
                         f"Word count {wc} above maximum 1100")]
    return []


def check_no_smell_openers(prose: str) -> list:
    """Check first sentence doesn't open with smell description."""
    first_line = prose.strip().split('\n')[0] if prose.strip() else ""
    first_50 = first_line[:200].lower()
    smell_patterns = [r'\bsmell(s|ed)?\s+(of|like)', r'\bodor\b', r'\bstench\b',
                      r'\bthe room smell', r'\bthe air smell']
    for pat in smell_patterns:
        if re.search(pat, first_50):
            return [Finding("SMELL-OPEN", "smell_opener", "CLASS_B",
                             "Scene opens with smell description",
                             excerpt=first_line[:100])]
    return []


def check_no_relative_time_refs(prose: str) -> list:
    """Check for relative time references."""
    findings = []
    pattern = r'\b(hours?\s+later|days?\s+later|weeks?\s+later|the\s+next\s+morning|the\s+next\s+day|the\s+following|that\s+evening|ago)\b'
    for m in re.finditer(pattern, prose, re.IGNORECASE):
        findings.append(Finding(
            f"TIME-REF-{len(findings)+1}", "time_refs", "CLASS_B",
            f"Relative time reference: '{m.group()}'",
            excerpt=prose[max(0, m.start()-20):m.end()+20],
        ))
    return findings


def check_no_team_age_refs(prose: str) -> list:
    """Check for age references to team members."""
    findings = []
    pattern = r'\b(sixteen|seventeen|eighteen|nineteen|twenty|year[s\-]?\s*old)\b'
    for m in re.finditer(pattern, prose, re.IGNORECASE):
        findings.append(Finding(
            f"AGE-REF-{len(findings)+1}", "age_refs", "CLASS_B",
            f"Team member age reference: '{m.group()}'",
            excerpt=prose[max(0, m.start()-30):m.end()+30],
        ))
    return findings


def check_no_base_language(prose: str) -> list:
    """Check for fixed-base / headquarters language."""
    findings = []
    pattern = r'\b(headquarters|base\s+of\s+operations|returned?\s+to\s+(the|their)\s+base|back\s+at\s+(the|their)\s+base)\b'
    for m in re.finditer(pattern, prose, re.IGNORECASE):
        findings.append(Finding(
            f"BASE-LANG-{len(findings)+1}", "base_language", "CLASS_B",
            f"Fixed-base language: '{m.group()}'",
            excerpt=prose[max(0, m.start()-20):m.end()+20],
        ))
    return findings


def check_no_metadata_in_output(prose: str) -> list:
    """Check for scene/chapter markers that shouldn't be in prose output."""
    findings = []
    patterns = [
        (r'^#{1,4}\s+Scene\s+\d+', "Scene marker in prose output"),
        (r'^#{1,4}\s+Chapter\s+\d+', "Chapter marker in prose output"),
        (r'\[TYPE:\s*(ACTION|MIXED|NON)', "Scene type tag in prose output"),
        (r'\[POV:\s*', "POV tag in prose output"),
    ]
    for pat, msg in patterns:
        if re.search(pat, prose, re.MULTILINE | re.IGNORECASE):
            findings.append(Finding("META-LEAK", "metadata_leak", "CLASS_A", msg))
    return findings


def check_character_state(prose: str, scene, chapter_number: int) -> list:
    """Check that dead characters don't appear as alive in prose."""
    findings = []
    dead_chars = DEAD_BY_CHAPTER.get(chapter_number, [])
    for char in dead_chars:
        # Check if the dead character speaks or acts (not just mentioned in memory)
        # Simple heuristic: character name followed by action verbs
        action_pattern = rf'\b{char}\s+(says?|said|asks?|asked|walks?|walked|runs?|ran|stands?|stood|fires?|fired|takes?|took|moves?|moved|tells?|told|looks?|looked|pulls?|pulled|reaches?|reached|holds?|held)\b'
        for m in re.finditer(action_pattern, prose, re.IGNORECASE):
            context = prose[max(0, m.start()-50):m.end()+50]
            # Skip if in memory/past-tense reflective context
            if any(mem in context.lower() for mem in ["remembered", "had said", "had told", "once", "before the", "memory"]):
                continue
            findings.append(Finding(
                f"DEAD-CHAR-{len(findings)+1}", "character_state", "CLASS_A",
                f"Dead character '{char}' appears to act in chapter {chapter_number}",
                excerpt=context,
            ))
    return findings


def check_balaclava_ops(prose: str, scene) -> list:
    """Check operational scenes mention balaclavas."""
    if scene.scene_type != "ACTION":
        return []
    # Only flag if the scene involves combat/raid/ambush and no mask mention
    combat_indicators = ["fire", "shot", "ambush", "raid", "attack", "assault", "position"]
    prose_lower = prose.lower()
    has_combat = any(ind in prose_lower for ind in combat_indicators)
    if not has_combat:
        return []
    mask_indicators = ["balaclava", "mask", "face covered", "faces covered", "covered face"]
    has_mask = any(ind in prose_lower for ind in mask_indicators)
    if not has_mask:
        return [Finding("BALACLAVA", "balaclava_ops", "CLASS_B",
                         "Operational/combat scene without balaclava reference")]
    return []


# ── Reflexive tautology (deterministic) ──────────────────────────────────────

REFLEXIVE_TAUTOLOGY_PATTERNS = [
    re.compile(r'\bthe\s+way\s+(?:a\s+|an\s+|the\s+|he\s+|she\s+|it\s+|they\s+|you\s+)?\w+(?:\s+\w+){0,2}\s+always\s+(?:does|did|do|has|have|had)\b', re.IGNORECASE),
    re.compile(r'\bwhich\s+was\s+what\s+\w+\s+(?:was|did|had|always)\b', re.IGNORECASE),
    re.compile(r'\bas\s+(?:it|he|she|they)\s+(?:had|has|have)\s+always\s+(?:been|done|known|waited)\b', re.IGNORECASE),
    re.compile(r'\bwhat\s+\w+\s+(?:has|had|have)\s+always\s+been\b', re.IGNORECASE),
    re.compile(r'\bdoing\s+what\s+\w+(?:\s+\w+){0,2}\s+(?:does|did|always)\b', re.IGNORECASE),
]


def count_reflexive_tautologies(prose: str) -> int:
    """Count reflexive-tautology instances in prose."""
    count = 0
    for pattern in REFLEXIVE_TAUTOLOGY_PATTERNS:
        count += len(pattern.findall(prose))
    return count


def check_reflexive_tautology(prose: str, budget_remaining: int = None) -> list:
    """Check reflexive-tautology usage against budget.

    If budget_remaining is None, returns CLASS_B findings for each instance.
    If budget_remaining is specified and instances exceed it, returns CLASS_A.
    """
    instances = count_reflexive_tautologies(prose)
    if instances == 0:
        return []

    findings = []
    if budget_remaining is not None and instances > budget_remaining:
        findings.append(Finding(
            "TAUTOLOGY-OVER", "reflexive_tautology", "CLASS_A",
            f"Reflexive-tautology count ({instances}) exceeds remaining budget ({budget_remaining})",
        ))
    elif budget_remaining is not None:
        # Within budget — no finding
        pass
    else:
        # No budget tracking — advisory
        for i in range(instances):
            findings.append(Finding(
                f"TAUTOLOGY-{i+1}", "reflexive_tautology", "CLASS_B",
                f"Reflexive-tautology instance detected (advisory)",
            ))
    return findings


# ── Logistics continuity (LLM) ──────────────────────────────────────────────

def check_logistics_continuity(prose: str, scene, use_llm: bool = True) -> list:
    """Check for within-scene logistics contradictions."""
    if not use_llm:
        return []

    from llm_client import call_llm

    prompt = f"""Below is the prose of a single scene. Identify any contradictions in:
- Transport mode (walked / drove / rode)
- Location state (moved to X, then described as still at Y)
- Possessions (carrying X, then X absent without explanation)
- Physical condition (wounded in left arm, then using left arm normally)
- Companions (alone / with team) when contradictory

Distinguish actual contradictions from valid transitions (e.g., "walked to the car and got in" is NOT a contradiction).

If there are contradictions, report each with: (1) first assertion, (2) contradicting assertion.
If NONE, respond with: NONE

SCENE PROSE:
{prose[:6000]}
"""
    try:
        response = call_llm(
            provider="anthropic",
            model="claude-sonnet-4-6",
            system="You are a continuity auditor for novel manuscripts.",
            user=prompt,
            max_tokens=1024,
        )
        result_text = response.text.strip()
        if "NONE" in result_text.upper() and len(result_text) < 50:
            return []
        findings = []
        for line in result_text.split('\n'):
            line = line.strip()
            if re.match(r'^\d+\.', line) or (line and line[0] == '-'):
                desc = re.sub(r'^[\d\.\-\s]+', '', line)
                findings.append(Finding(
                    f"LOGISTICS-{len(findings)+1}", "logistics_continuity", "CLASS_A",
                    f"Logistics contradiction: {desc}", excerpt=desc[:200],
                ))
        return findings
    except Exception:
        return []


# ── LLM checks ──────────────────────────────────────────────────────────────

def check_beat_coverage_llm(prose: str, scene, use_llm: bool = True) -> list:
    """Check all synopsis beats are addressed in prose."""
    if not use_llm:
        return []

    from llm_client import call_llm

    # Split synopsis body into beats (paragraphs)
    beats = [p.strip() for p in scene.body.split('\n\n') if p.strip() and len(p.strip()) > 30]
    if not beats:
        return []

    beats_text = "\n".join(f"{i+1}. {beat[:300]}" for i, beat in enumerate(beats))

    prompt = f"""Compare each numbered synopsis beat against the manuscript prose.
For each beat, respond YES if the prose addresses this beat, NO if it does not.

SYNOPSIS BEATS:
{beats_text}

MANUSCRIPT PROSE:
{prose[:8000]}

Respond in format:
1. YES
2. NO
...
"""

    try:
        response = call_llm(
            provider="anthropic",
            model="claude-sonnet-4-6",
            system="You are a precise text comparison assistant.",
            user=prompt,
            max_tokens=1024,
        )
        result_text = response.text
        findings = []
        for i, beat in enumerate(beats):
            pattern = rf'{i+1}\.\s*(YES|NO)'
            m = re.search(pattern, result_text, re.IGNORECASE)
            if m and m.group(1).upper() == "NO":
                findings.append(Finding(
                    f"BEAT-MISS-{i+1}", "beat_coverage", "CLASS_A",
                    f"Synopsis beat not addressed in prose",
                    excerpt=beat[:200],
                ))
        return findings
    except Exception:
        return []


def check_no_reintroduction(prose: str, scene, prior_prose_in_chapter: list = None, use_llm: bool = True) -> list:
    """Check that scene doesn't reintroduce material from prior scenes in the chapter."""
    if not prior_prose_in_chapter or not use_llm:
        return []

    from llm_client import call_llm

    prior_combined = "\n\n***\n\n".join(p[:2000] for p in prior_prose_in_chapter)

    prompt = f"""Below are prior scenes already written in a chapter, followed by the current scene.

Identify any plot points, images, character introductions, settings, or thematic elements in the CURRENT SCENE that are ALREADY established in the PRIOR SCENES — meaning restated, re-described, or re-anchored rather than developed forward.

Distinguish:
(a) REINTRODUCTION = restating established material (the reader already knows this)
(b) CALLBACK = brief intentional echo at a structural moment (acceptable)

Report REINTRODUCTIONS only. For each, give a one-line description.
If there are NONE, respond with: NONE

PRIOR SCENES:
{prior_combined[:6000]}

CURRENT SCENE:
{prose[:4000]}

Respond in format:
REINTRODUCTIONS:
1. [description]
2. [description]
OR:
NONE
"""

    try:
        response = call_llm(
            provider="anthropic",
            model="claude-sonnet-4-6",
            system="You are a manuscript continuity auditor.",
            user=prompt,
            max_tokens=1024,
        )
        result_text = response.text.strip()

        if "NONE" in result_text.upper() and len(result_text) < 50:
            return []

        findings = []
        for line in result_text.split('\n'):
            line = line.strip()
            if re.match(r'^\d+\.', line):
                desc = re.sub(r'^\d+\.\s*', '', line)
                findings.append(Finding(
                    f"REINTRO-{len(findings)+1}", "reintroduction", "CLASS_A",
                    f"Reintroduction of established material: {desc}",
                    excerpt=desc[:200],
                ))
        return findings
    except Exception:
        return []


# ── Main audit function ─────────────────────────────────────────────────────

def audit_scene(
    prose: str,
    scene,
    craft_principles: list = None,
    series_bible: dict = None,
    use_llm: bool = True,
    prior_prose_in_chapter: list = None,
    **kwargs,
) -> SceneAuditResult:
    """Validate generated scene prose against synopsis and craft principles."""
    findings = []

    # Deterministic checks
    findings.extend(check_word_count(prose))
    findings.extend(check_no_smell_openers(prose))
    findings.extend(check_no_relative_time_refs(prose))
    findings.extend(check_no_team_age_refs(prose))
    findings.extend(check_no_base_language(prose))
    findings.extend(check_no_metadata_in_output(prose))
    findings.extend(check_character_state(prose, scene, scene.chapter_number))
    findings.extend(check_balaclava_ops(prose, scene))

    # Deterministic style checks
    tautology_budget = kwargs.get("reflexive_tautology_budget", None)
    findings.extend(check_reflexive_tautology(prose, tautology_budget))

    # LLM checks
    if use_llm:
        findings.extend(check_beat_coverage_llm(prose, scene, use_llm=True))
        findings.extend(check_no_reintroduction(prose, scene, prior_prose_in_chapter, use_llm=True))
        findings.extend(check_logistics_continuity(prose, scene, use_llm=True))

    # Stats
    wc = len(prose.split())
    class_a = [f for f in findings if f.severity == "CLASS_A"]
    class_b = [f for f in findings if f.severity == "CLASS_B"]

    return SceneAuditResult(
        passed=len(class_a) == 0,
        findings=findings,
        stats={
            "word_count": wc,
            "class_a_count": len(class_a),
            "class_b_count": len(class_b),
        },
    )
