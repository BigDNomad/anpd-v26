"""
MA-007 voice_register_adherence — detects voice-register violations against
the series_bible spec.

Two sub-checks:
  A) Intrusion-allocation breach (per-scene-TYPE budget violation)
  B) Forbidden-pattern presence (anaphora, relative time refs, future-tense
     irony, exposition-dump dialogue, AI-isms)

Bias: false-negative over false-positive. Deterministic.
Severity: CLASS_A on hard breach or forbidden-pattern hit, CLASS_B on soft breach.
"""

from __future__ import annotations

import os
import re
import sys

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Configuration ────────────────────────────────────────────────────────────

MA007_INTRUSION_TOLERANCE_PP = 5.0  # percentage-point cushion above budget
MA007_SCENE_TYPE_DEFAULTS = {
    "ACTION": 8.0,
    "SUSPENSE": 5.0,
    "NON-ACTION": 20.0,
    "MIXED": 20.0,  # apply looser budget
}
MA007_ANAPHORA_MIN_CONSECUTIVE = 3
MA007_ANAPHORA_PREFIX_WORDS = 2
MA007_EXPOSITION_DIALOGUE_MIN_CHARS = 300
MA007_EXPOSITION_PROPER_NOUN_COUNT = 3

# ── Scene TYPE extraction from synopsis ──────────────────────────────────────

from audit_checks._lib.synopsis_scene_types import load_scene_type_map


# ── Intrusion detection ──────────────────────────────────────────────────────

_INTRUSION_TENSE_MARKERS = [
    re.compile(r"\bhad\s+been\b", re.IGNORECASE),
    re.compile(r"\bwould\s+have\b", re.IGNORECASE),
    re.compile(r"\bhad\s+already\b", re.IGNORECASE),
    re.compile(r"\bthere\s+is\s+a\b", re.IGNORECASE),
    re.compile(r"\bthere\s+are\s+(?:those|people|things)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:he|she)\s+had\b", re.IGNORECASE),
]

_INTRUSION_ABSTRACT_SUBJECTS = [
    re.compile(r"^The\s+(?:work|decision|thing|question|cost|weight|problem)\b", re.IGNORECASE),
    re.compile(r"^What\s+(?:he|she)\s+had\s+was\b", re.IGNORECASE),
    re.compile(r"^It\s+was\s+(?:the|a)\s+kind\s+of\b", re.IGNORECASE),
    re.compile(r"^There\s+(?:is|was|are|were)\s+a\b", re.IGNORECASE),
]

_INTRUSION_MORAL_NOUNS = re.compile(
    r"\b(?:cost|weight|sacrifice|willingness|discipline|calculation|burden|"
    r"consequence|price|toll|damage|reckoning)\b",
    re.IGNORECASE,
)

# Sentence splitter — split on period/exclamation/question followed by space+uppercase
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z""])')


def _is_intrusion_sentence(sentence: str) -> bool:
    """Heuristic: sentence is intrusion if it meets >= 2 markers."""
    markers = 0

    word_count = len(sentence.split())
    if word_count > 40:
        markers += 1

    if any(p.search(sentence) for p in _INTRUSION_TENSE_MARKERS):
        markers += 1

    if any(p.search(sentence) for p in _INTRUSION_ABSTRACT_SUBJECTS):
        markers += 1

    if _INTRUSION_MORAL_NOUNS.search(sentence) and word_count > 20:
        markers += 1

    return markers >= 2


def compute_intrusion_percentage(scene_text: str) -> float:
    """Return intrusion % (0-100) for a scene."""
    sentences = _SENTENCE_SPLIT_RE.split(scene_text)
    total_words = sum(len(s.split()) for s in sentences) or 1
    intrusion_words = sum(
        len(s.split()) for s in sentences if _is_intrusion_sentence(s)
    )
    return 100.0 * intrusion_words / total_words


# ── Forbidden patterns ───────────────────────────────────────────────────────

_REL_TIME_RE = re.compile(
    r"\b(?:a few|several|some)\s+(?:days?|weeks?|months?|hours?)\s+(?:ago|earlier|later)\b"
    r"|\bsoon\s+after\b"
    r"|\blater\s+that\b",
    re.IGNORECASE,
)

_FUTURE_TENSE_IRONY_PHRASES = [
    re.compile(r"\bwould\s+later\b", re.IGNORECASE),
    re.compile(r"\bwas\s+going\s+to\b", re.IGNORECASE),
    re.compile(r"\bnot\s+yet\b", re.IGNORECASE),
]

_AI_ISMS = [
    re.compile(r"\bit['\u2019]?s\s+not\s+just\s+\w+[,;]?\s+it['\u2019]?s\b", re.IGNORECASE),
    re.compile(r"\ba\s+testament\s+to\b", re.IGNORECASE),
    re.compile(r"\bin\s+a\s+world\s+where\b", re.IGNORECASE),
]


def detect_anaphora(scene_text: str) -> list[tuple[int, str]]:
    """Find 3+ consecutive sentences in a paragraph starting with same 2-word prefix.

    Returns list of (offset_in_scene, excerpt).
    """
    hits: list[tuple[int, str]] = []
    # Split into paragraphs (separated by blank lines or double newlines)
    for para_match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]*)*", scene_text):
        para = para_match.group(0)
        para_start = para_match.start()
        sentences = _SENTENCE_SPLIT_RE.split(para)
        if len(sentences) < MA007_ANAPHORA_MIN_CONSECUTIVE:
            continue
        i = 0
        while i <= len(sentences) - MA007_ANAPHORA_MIN_CONSECUTIVE:
            prefixes = []
            for j in range(MA007_ANAPHORA_MIN_CONSECUTIVE):
                words = sentences[i + j].strip().split()[:MA007_ANAPHORA_PREFIX_WORDS]
                prefixes.append(" ".join(words).lower())
            if len(set(prefixes)) == 1 and prefixes[0]:
                hits.append((para_start, sentences[i][:120]))
                i += MA007_ANAPHORA_MIN_CONSECUTIVE
            else:
                i += 1
    return hits


def detect_future_tense_irony(scene_text: str) -> list[tuple[int, str]]:
    """Paragraph with 2+ future-tense-irony phrases -> hit."""
    hits: list[tuple[int, str]] = []
    for para_match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]*)*", scene_text):
        para = para_match.group(0)
        count = sum(len(p.findall(para)) for p in _FUTURE_TENSE_IRONY_PHRASES)
        if count >= 2:
            hits.append((para_match.start(), para[:120]))
    return hits


def detect_exposition_dialogue(scene_text: str) -> list[tuple[int, str]]:
    """Single dialogue line >300 chars with 3+ proper nouns -> hit."""
    hits: list[tuple[int, str]] = []
    for m in re.finditer(r'[\u201c""]([^\u201d""]+)[\u201d""]', scene_text):
        body = m.group(1)
        if len(body) >= MA007_EXPOSITION_DIALOGUE_MIN_CHARS:
            proper_nouns = re.findall(r"\b[A-Z][a-z]{2,}\b", body)
            if len(proper_nouns) >= MA007_EXPOSITION_PROPER_NOUN_COUNT:
                hits.append((m.start(), body[:120]))
    return hits


# ── Finding builder ──────────────────────────────────────────────────────────

def _finding(severity: str, scene_number: int, description: str,
             evidence: list[str] | None = None) -> Finding:
    return Finding(
        check_id="MA-007-voice-register-adherence",
        severity=severity,
        scene_number=scene_number,
        scene_numbers=[scene_number],
        description=description,
        evidence=evidence or [],
        suggested_fix="Revise scene to bring intrusion within budget, or remove forbidden pattern",
    )


# ── Check class ──────────────────────────────────────────────────────────────

class VoiceRegisterAdherence:
    check_id = "MA-007-voice-register-adherence"
    severity = "CLASS_A"
    description = (
        "Voice register adherence: intrusion-allocation budget enforcement "
        "and forbidden-pattern detection"
    )

    def __init__(self):
        self._scene_type_map: dict[int, str] = {}
        self._budget_map: dict[str, float] = MA007_SCENE_TYPE_DEFAULTS.copy()

    def _init_from_briefs(self, briefs: BriefBundle) -> None:
        """Load scene TYPE map from synopsis and budgets from series_bible."""
        self._scene_type_map = load_scene_type_map(briefs.synopsis_path)

        if not self._scene_type_map:
            print("    WARN: no synopsis found for TYPE map; defaulting all to NON-ACTION",
                  file=sys.stderr)

        # Parse budgets from series_bible if available
        voice_reg = briefs.series_bible.get("voice_register", {})
        allocation = voice_reg.get("intrusion_allocation", "")
        if allocation:
            # Parse "ACTION scenes: 0% intrusion. SUSPENSE scenes: 5%..."
            for m in re.finditer(r"(\w[\w\-]*)\s+scenes?:\s*(\d+)%", allocation, re.IGNORECASE):
                scene_type = m.group(1).upper()
                pct = float(m.group(2))
                self._budget_map[scene_type] = pct

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        self._init_from_briefs(briefs)
        findings: list[Finding] = []

        print(f"    Scene type map: {len(self._scene_type_map)} entries", file=sys.stderr)
        print(f"    Budgets: {self._budget_map}", file=sys.stderr)

        for scene in sorted(manuscript.scenes, key=lambda s: s.scene_number):
            sn = scene.scene_number
            scene_type = self._scene_type_map.get(sn, "NON-ACTION")
            budget = self._budget_map.get(scene_type, 15.0)

            # Sub-check A: intrusion percentage
            intrusion_pct = compute_intrusion_percentage(scene.text)
            if intrusion_pct > budget + MA007_INTRUSION_TOLERANCE_PP:
                findings.append(_finding(
                    severity="CLASS_B",
                    scene_number=sn,
                    description=(
                        f"Intrusion-allocation breach: scene {sn} ({scene_type}) "
                        f"shows {intrusion_pct:.1f}% intrusion voice; "
                        f"budget {budget:.0f}% + {MA007_INTRUSION_TOLERANCE_PP:.0f}pp tolerance"
                    ),
                    evidence=[f"Scene {sn}: TYPE={scene_type}, intrusion={intrusion_pct:.1f}%"],
                ))
            elif intrusion_pct > budget:
                findings.append(_finding(
                    severity="CLASS_B",
                    scene_number=sn,
                    description=(
                        f"Intrusion-allocation soft breach: scene {sn} ({scene_type}) "
                        f"at {intrusion_pct:.1f}%, budget {budget:.0f}% (within tolerance)"
                    ),
                    evidence=[f"Scene {sn}: TYPE={scene_type}, intrusion={intrusion_pct:.1f}%"],
                ))

            # Sub-check B: forbidden patterns
            for hit_pos, excerpt in detect_anaphora(scene.text):
                findings.append(_finding(
                    severity="CLASS_B", scene_number=sn,
                    description=f"Anaphora detected in scene {sn}",
                    evidence=[f"Scene {sn}: ...{excerpt}..."]))

            for hit_pos, excerpt in detect_future_tense_irony(scene.text):
                findings.append(_finding(
                    severity="CLASS_B", scene_number=sn,
                    description=f"Future-tense irony pattern in scene {sn}",
                    evidence=[f"Scene {sn}: ...{excerpt}..."]))

            for hit_pos, excerpt in detect_exposition_dialogue(scene.text):
                findings.append(_finding(
                    severity="CLASS_B", scene_number=sn,
                    description=f"Exposition dump in dialogue (scene {sn})",
                    evidence=[f"Scene {sn}: ...{excerpt}..."]))

            for m in _REL_TIME_RE.finditer(scene.text):
                findings.append(_finding(
                    severity="CLASS_A", scene_number=sn,
                    description=f"Relative time reference: '{m.group(0)}' (scene {sn})",
                    evidence=[f"Scene {sn}: ...{scene.text[max(0,m.start()-40):m.end()+40]}..."]))

            for pattern in _AI_ISMS:
                for m in pattern.finditer(scene.text):
                    findings.append(_finding(
                        severity="CLASS_A", scene_number=sn,
                        description=f"AI-ism pattern (scene {sn})",
                        evidence=[f"Scene {sn}: ...{m.group(0)}..."]))

        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        class_b = sum(1 for f in findings if f.severity == "CLASS_B")
        print(f"    -> Total: {len(findings)} findings ({class_a} A, {class_b} B)",
              file=sys.stderr)

        return findings
