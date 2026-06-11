"""
MA-005: Pipeline Note Leak Detection — detects editorial scaffolding language
that leaked from pipeline artifacts into manuscript prose.

Five sub-checks:
  A) Bracketed editorial markers ([NOTE:], [TODO], [POV:], etc.)
  B) Meta-narrative references to books/chapters/scenes as artifacts
  C) Synopsis scaffolding phrases ("as described in the synopsis")
  D) Generation-time stage directions ([end of scene], <scene>, etc.)
  E) LLM artifact leaks (sentence-initial LLM tics)

Single deterministic phase; no LLM confirmation required.

Severity: CLASS_A for sub-checks A-D and hard LLM artifacts.
          CLASS_B for sentence-initial LLM tics (false-positive risk).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Sub-check A: Bracketed editorial markers ─────────────────────────────────

_BRACKETED_MARKERS = re.compile(
    r"\[(?:NOTE|TODO|TBD|FIXME|XXX|PLACEHOLDER|INSERT|CHECK|TK|"
    r"ACTION|NON-ACTION|MIXED|POV|TYPE|CHAPTER|SCENE)"
    r"(?:[:\s][^\]]*)?\]",
    re.IGNORECASE,
)


# ── Sub-check B: Meta-narrative references ───────────────────────────────────

_BOOK_NUMBER_WORDS = r"(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)"

_META_BOOK_REFS = [
    # "Book Two's problem" (the Mandate leak) — possessive
    re.compile(
        r"\bBook\s+" + _BOOK_NUMBER_WORDS + "['\u2019]s\\s+\\w+",
        re.IGNORECASE,
    ),
    # "Book 2's problem" — numeric possessive
    re.compile(
        r"\bBook\s+\d+['\u2019]s\s+\w+",
        re.IGNORECASE,
    ),
    # "in Book Two" / "for Book Two" / "of Book Two"
    re.compile(
        r"\b(?:in|for|of)\s+Book\s+(?:" + _BOOK_NUMBER_WORDS + r"|\d+)\b",
        re.IGNORECASE,
    ),
    # "the next book", "the sequel", "the prequel"
    re.compile(r"\bthe\s+(?:next\s+book|sequel|prequel)\b", re.IGNORECASE),
    # "next chapter" / "previous chapter"
    re.compile(r"\b(?:next|previous|the\s+previous|the\s+next)\s+chapter\b", re.IGNORECASE),
    # structural verbs + Chapter N
    re.compile(
        r"\b(?:plants?|sets?\s+up|seeds?|establishes?)\s+(?:in\s+)?Chapter\s+(?:" + _BOOK_NUMBER_WORDS + r"|\d+)\b",
        re.IGNORECASE,
    ),
    # "to be continued"
    re.compile(r"\bto\s+be\s+continued\b", re.IGNORECASE),
]


# ── Sub-check C: Synopsis scaffolding phrases ────────────────────────────────

_SYNOPSIS_PHRASES = [
    re.compile(r"\bas\s+described\s+in\s+the\s+synopsis\b", re.IGNORECASE),
    re.compile(r"\bper\s+the\s+outline\b", re.IGNORECASE),
    re.compile(r"\bas\s+the\s+synopsis\s+indicates\b", re.IGNORECASE),
    re.compile(r"\bas\s+established\s+in\s+scene\b", re.IGNORECASE),
    re.compile(r"\bseeds?\s+for\s+chapter\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bplant\s+for\s+(?:chapter|scene)\s+\d+\b", re.IGNORECASE),
    re.compile(r"\bsetup\s+for\s+(?:chapter|scene)\s+\d+\b", re.IGNORECASE),
]


# ── Sub-check D: Stage directions ────────────────────────────────────────────

_STAGE_DIRECTIONS = [
    re.compile(r"\[\s*(?:continued|end\s+of\s+scene|end\s+scene|scene\s+break|chapter\s+break)\s*\]", re.IGNORECASE),
    re.compile(r"</?(?:scene|chapter)>", re.IGNORECASE),
]


# ── Sub-check E: LLM artifact leaks ─────────────────────────────────────────

_LLM_ARTIFACTS_CLASS_A = [
    re.compile(r"\bAs\s+an\s+AI\b"),
    re.compile(r"\bI\s+cannot\s+generate\b", re.IGNORECASE),
    re.compile(r"\bHere\s+is\s+the\s+(?:next\s+)?scene\b", re.IGNORECASE),
    re.compile(r"\[Generated\s+content\]", re.IGNORECASE),
    re.compile(r"\bNote:\s+I\s+have\b", re.IGNORECASE),
]

# CLASS_B: sentence-initial LLM tics in narration (not dialogue)
# Match "Of course," / "Certainly," / "Indeed," at paragraph start or after ". "
# Exclude matches inside quotation marks (dialogue)
_LLM_TICS_CLASS_B = re.compile(
    r'(?:^|\n\n|\.\s+)((?:Of\s+course|Certainly|Indeed),\s)',
    re.MULTILINE,
)


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LeakHit:
    """A single leak pattern match."""
    scene_number: int
    subcheck: str       # A, B, C, D, E
    pattern_label: str  # human-readable label
    severity: str       # CLASS_A or CLASS_B
    excerpt: str
    char_offset: int


# ── Scanning ─────────────────────────────────────────────────────────────────

def _is_inside_quotes(text: str, pos: int) -> bool:
    """Heuristic check: is the character at pos inside a quoted string?

    Counts opening/closing double quotes before pos. Odd count = inside quotes.
    """
    # Count unescaped double quotes before pos
    preceding = text[:pos]
    # Simple approach: count '"' chars
    quote_count = preceding.count('"') + preceding.count('\u201c') + preceding.count('\u201d')
    return quote_count % 2 == 1


def scan_scene(scene_text: str, scene_number: int) -> list[LeakHit]:
    """Scan a single scene for all leak patterns. Returns one hit per match."""
    hits: list[LeakHit] = []

    # Sub-check A: Bracketed markers
    for m in _BRACKETED_MARKERS.finditer(scene_text):
        start = max(0, m.start() - 40)
        end = min(len(scene_text), m.end() + 40)
        excerpt = scene_text[start:end].replace("\n", " ").strip()
        hits.append(LeakHit(
            scene_number=scene_number, subcheck="A",
            pattern_label="bracketed_editorial_marker",
            severity="CLASS_A", excerpt=excerpt, char_offset=m.start(),
        ))

    # Sub-check B: Meta-narrative references
    for pat in _META_BOOK_REFS:
        for m in pat.finditer(scene_text):
            start = max(0, m.start() - 40)
            end = min(len(scene_text), m.end() + 40)
            excerpt = scene_text[start:end].replace("\n", " ").strip()
            hits.append(LeakHit(
                scene_number=scene_number, subcheck="B",
                pattern_label="meta_narrative_reference",
                severity="CLASS_A", excerpt=excerpt, char_offset=m.start(),
            ))

    # Sub-check C: Synopsis scaffolding
    for pat in _SYNOPSIS_PHRASES:
        for m in pat.finditer(scene_text):
            start = max(0, m.start() - 40)
            end = min(len(scene_text), m.end() + 40)
            excerpt = scene_text[start:end].replace("\n", " ").strip()
            hits.append(LeakHit(
                scene_number=scene_number, subcheck="C",
                pattern_label="synopsis_scaffolding",
                severity="CLASS_A", excerpt=excerpt, char_offset=m.start(),
            ))

    # Sub-check D: Stage directions
    for pat in _STAGE_DIRECTIONS:
        for m in pat.finditer(scene_text):
            start = max(0, m.start() - 40)
            end = min(len(scene_text), m.end() + 40)
            excerpt = scene_text[start:end].replace("\n", " ").strip()
            hits.append(LeakHit(
                scene_number=scene_number, subcheck="D",
                pattern_label="stage_direction",
                severity="CLASS_A", excerpt=excerpt, char_offset=m.start(),
            ))

    # Sub-check E: LLM artifacts (CLASS_A)
    for pat in _LLM_ARTIFACTS_CLASS_A:
        for m in pat.finditer(scene_text):
            start = max(0, m.start() - 40)
            end = min(len(scene_text), m.end() + 40)
            excerpt = scene_text[start:end].replace("\n", " ").strip()
            hits.append(LeakHit(
                scene_number=scene_number, subcheck="E",
                pattern_label="llm_artifact",
                severity="CLASS_A", excerpt=excerpt, char_offset=m.start(),
            ))

    # Sub-check E: LLM tics (CLASS_B) — only in narration, not dialogue
    for m in _LLM_TICS_CLASS_B.finditer(scene_text):
        match_start = m.start(1) if m.lastindex else m.start()
        if _is_inside_quotes(scene_text, match_start):
            continue  # Inside dialogue — skip
        start = max(0, match_start - 40)
        end = min(len(scene_text), m.end() + 40)
        excerpt = scene_text[start:end].replace("\n", " ").strip()
        hits.append(LeakHit(
            scene_number=scene_number, subcheck="E",
            pattern_label="llm_tic_narration",
            severity="CLASS_B", excerpt=excerpt, char_offset=match_start,
        ))

    return hits


# ── Check module class ───────────────────────────────────────────────────────

class PipelineNoteLeak:
    check_id = "MA-005-pipeline-note-leak"
    severity = "CLASS_A"
    description = (
        "Pipeline note leak detection: bracketed markers, meta-narrative references, "
        "synopsis scaffolding, stage directions, LLM artifact leaks"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        findings: list[Finding] = []

        print("    Scanning for pipeline note leaks", file=sys.stderr)

        total_hits = 0
        for scene in sorted(manuscript.scenes, key=lambda s: s.scene_number):
            hits = scan_scene(scene.text, scene.scene_number)
            total_hits += len(hits)

            for hit in hits:
                findings.append(Finding(
                    check_id=self.check_id,
                    severity=hit.severity,
                    scene_number=hit.scene_number,
                    scene_numbers=[hit.scene_number],
                    description=(
                        f"Pipeline note leak ({hit.pattern_label}, sub-check {hit.subcheck}) "
                        f"in scene {hit.scene_number}"
                    ),
                    evidence=[f"Scene {hit.scene_number}: ...{hit.excerpt}..."],
                    suggested_fix=f"Remove or rephrase the '{hit.pattern_label}' construction",
                ))

        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        class_b = sum(1 for f in findings if f.severity == "CLASS_B")
        print(f"    -> {total_hits} hits ({class_a} CLASS_A, {class_b} CLASS_B)", file=sys.stderr)

        return findings
