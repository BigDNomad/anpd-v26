"""
MA-006: Reintroduction Detection — detects characterization-tic stutter and
thematic-beat echo.

Two sub-checks:
  A) Characterization-tic stutter: a verb/phrase distinctively used for one
     character that recurs above a frequency threshold.
  B) Thematic-beat echo: the same emotional statement repeated across scenes
     without development.

Bias: false-negative over false-positive. Deterministic candidate generation,
LLM Phase 2 confirmation.

Severity: CLASS_A on confirmed stutter/echo, CLASS_B on uncertain.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Configuration ────────────────────────────────────────────────────────────

MA006_STUTTER_THRESHOLD = 8       # Min occurrences for sub-check A candidate
MA006_ECHO_THRESHOLD = 4          # Min occurrences for sub-check B candidate
MA006_STUTTER_DOMINANCE = 0.6     # Fraction of occurrences that must be one character

HAIKU_MODEL = "claude-haiku-4-5"
MAX_RETRIES = 2


# ── Thematic-echo phrases (sub-check B) ─────────────────────────────────────

_THEMATIC_PHRASES = [
    (r"\bthe\s+weight\s+of\b", "the weight of"),
    (r"\bthe\s+cost\s+of\b", "the cost of"),
    (r"\bcarried\s+it\s+forward\b", "carried it forward"),
    (r"\bcarried\s+the\s+weight\b", "carried the weight"),
    (r"\bthe\s+work\s+continued\b", "the work continued"),
    (r"\bshe\s+had\s+learned\s+to\b", "she had learned to"),
    (r"\bhe\s+had\s+learned\s+to\b", "he had learned to"),
    (r"\bdid\s+not\s+name\s+it\b", "did not name it"),
    (r"\bwithout\s+letting\s+it\s+(?:show|slow)\b", "without letting it show/slow"),
]

_THEMATIC_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in _THEMATIC_PHRASES]


# ── Verbs to exclude ────────────────────────────────────────────────────────

_COMMON_VERBS = {
    "said", "asked", "looked", "watched", "saw", "thought", "knew", "felt",
    "moved", "went", "came", "took", "gave", "made", "did", "had", "was",
    "were", "got", "let", "kept", "found", "began", "started", "stopped",
    "stood", "sat", "walked", "turned", "opened", "closed", "held", "put",
    "set", "ran", "left", "heard", "read", "called", "told", "pulled",
    "pushed", "waited", "needed", "wanted", "tried", "used", "worked",
    "reached", "passed", "picked", "brought", "crossed", "entered",
    "finished", "continued", "returned", "paused", "noticed",
}


# ── Character gender mapping — pronoun → character resolution ───────────────
# TODO: source from character_profiles.json when schema includes gender field
_CHARACTER_GENDERS_FALLBACK = {
    "lena": "she",
    "mia": "she",
    "hank": "he",
    "cole": "he",
    "eddie": "he",
    "funes": "he",
    "prada": "he",
    "marco": "he",
    "torres": "he",
    "vega": "he",
    "ortega": "he",
    "medina": "he",
    "castellano": "he",
    "fuentes": "he",
    "hale": "he",
    "douglas": "he",
    "kuznetsov": "he",
    "volkov": "he",
}

# Pronoun resolution window — chars to look back for named subjects
_PRONOUN_WINDOW = 200


def _character_gender(name: str) -> str | None:
    """Look up character gender from hardcoded mapping."""
    return _CHARACTER_GENDERS_FALLBACK.get(name.lower())


# ── Subject-verb extraction ─────────────────────────────────────────────────

# Pattern 1: "<Name> <past-tense verb>"
_NAME_VERB_RE = re.compile(
    r'\b([A-Z][a-záéíóúñ]+)\s+([a-z]{3,}ed)\b'
)

# Pattern 2: "<pronoun> <past-tense verb>" — for pronoun resolution
_PRONOUN_VERB_RE = re.compile(
    r'\b([Ss]he|[Hh]e)\s+([a-z]{3,}ed)\b'
)

# Pattern for finding named characters (proper nouns) in text
_PRECEDING_NAME_RE = re.compile(r'\b([A-Z][a-záéíóúñ]+)\b')


_ARTICLES = {"the", "a", "an"}


def _load_character_roster(briefs: BriefBundle) -> set[str]:
    """Load canonical character first names from briefs."""
    names: set[str] = set()

    # From character_profiles
    for char in briefs.character_profiles.get("characters", []):
        name = char.get("name", "")
        if name:
            parts = name.strip().split()
            # Skip leading articles ("The Chief of Station" → skip)
            if parts and parts[0].lower() not in _ARTICLES:
                names.add(parts[0])  # First name only

    # From series_bible
    for char in briefs.series_bible.get("recurring_characters", []):
        name = char.get("name", "")
        if name:
            parts = name.strip().split()
            if parts and parts[0].lower() not in _ARTICLES:
                names.add(parts[0])

    return names


def extract_subject_verb_pairs(
    scene_text: str,
    scene_number: int,
    characters: set[str],
) -> list[tuple[str, str, int, str]]:
    """Extract (character, verb, scene_number, excerpt) tuples.

    Single-pass stateful scan per dispatch 35.1 Rule 1.1:
    - Maintains last_named_subject per gender (sticky until displaced)
    - Ambiguity check: if multiple same-gender characters appeared within
      200 chars of the pronoun, the pronoun is dropped (conservative)
    - Processes the scene in position order
    """
    pairs: list[tuple[str, str, int, str]] = []

    # Track the most recent named character per gender (sticky — persists
    # across the whole scene until displaced by another same-gender name)
    last_named_by_gender: dict[str, str] = {}  # gender → character name

    # Track ALL named-character mention positions for the 200-char ambiguity check
    all_name_mentions: list[tuple[int, str]] = []
    for m in _PRECEDING_NAME_RE.finditer(scene_text):
        candidate = m.group(1)
        if candidate in characters:
            all_name_mentions.append((m.start(), candidate))

    # Collect all subject-verb matches (named + pronoun) sorted by position
    all_matches: list[tuple[int, str, str]] = []  # (pos, subject, verb)

    for m in _NAME_VERB_RE.finditer(scene_text):
        name = m.group(1)
        verb = m.group(2).lower()
        if name in characters and verb not in _COMMON_VERBS:
            all_matches.append((m.start(), name, verb))

    for m in _PRONOUN_VERB_RE.finditer(scene_text):
        pronoun = m.group(1)
        verb = m.group(2).lower()
        if verb not in _COMMON_VERBS:
            all_matches.append((m.start(), pronoun, verb))

    all_matches.sort(key=lambda x: x[0])

    # Index into all_name_mentions for updating last_named_by_gender
    name_idx = 0

    for pos, subject, verb in all_matches:
        # Update last_named_by_gender with all character mentions up to this pos
        while name_idx < len(all_name_mentions) and all_name_mentions[name_idx][0] < pos:
            mention_pos, mention_name = all_name_mentions[name_idx]
            gender = _character_gender(mention_name)
            if gender:
                last_named_by_gender[gender] = mention_name
            name_idx += 1

        start = max(0, pos - 30)
        end = min(len(scene_text), pos + 60)
        excerpt = scene_text[start:end].replace("\n", " ").strip()

        if subject in characters:
            # Named subject — emit directly
            pairs.append((subject, verb, scene_number, excerpt))
        elif subject in ("She", "she", "He", "he"):
            # Pronoun resolution
            pronoun_gender = "she" if subject.lower() == "she" else "he"

            # Check if last_named has a binding for this gender
            resolved = last_named_by_gender.get(pronoun_gender)
            if not resolved:
                continue  # No named subject of this gender seen yet

            # Ambiguity check: are there multiple DIFFERENT same-gender
            # characters mentioned within 200 chars before this pronoun?
            window_start = pos - _PRONOUN_WINDOW
            names_in_window = [
                c for p, c in all_name_mentions
                if window_start <= p < pos and _character_gender(c) == pronoun_gender
            ]
            unique_in_window = set(names_in_window)

            if len(unique_in_window) > 1:
                # Ambiguous — multiple same-gender characters in recent context
                continue

            # Unambiguous — emit
            pairs.append((resolved, verb, scene_number, excerpt))

    return pairs


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class StutterCandidate:
    """A characterization-tic stutter candidate."""
    character: str
    verb: str
    total_occurrences: int            # total across all characters
    character_occurrences: int        # occurrences for this character
    dominance: float                  # character_occurrences / total_occurrences
    scene_examples: list[tuple[int, str]]  # (scene_number, excerpt)


@dataclass
class EchoCandidate:
    """A thematic-beat echo candidate."""
    phrase_label: str
    total_occurrences: int
    occurrences: list[tuple[int, str]]  # (scene_number, excerpt)


# ── Candidate building ───────────────────────────────────────────────────────

def build_stutter_candidates(
    pairs_by_char: dict[str, list[tuple[str, int, str]]],
    all_verb_counts: Counter,
    threshold: int = MA006_STUTTER_THRESHOLD,
    dominance: float = MA006_STUTTER_DOMINANCE,
) -> list[StutterCandidate]:
    """Build stutter candidates from extracted subject-verb pairs."""
    candidates: list[StutterCandidate] = []

    # For each character, count their verbs
    for char, verb_entries in pairs_by_char.items():
        char_verb_counts: Counter = Counter()
        char_verb_examples: dict[str, list[tuple[int, str]]] = defaultdict(list)

        for verb, sn, excerpt in verb_entries:
            char_verb_counts[verb] += 1
            char_verb_examples[verb].append((sn, excerpt))

        for verb, char_count in char_verb_counts.items():
            total = all_verb_counts[verb]
            if char_count < threshold:
                continue
            dom = char_count / total if total > 0 else 0
            if dom < dominance:
                continue

            examples = char_verb_examples[verb][:5]  # Max 5 examples
            candidates.append(StutterCandidate(
                character=char,
                verb=verb,
                total_occurrences=total,
                character_occurrences=char_count,
                dominance=dom,
                scene_examples=examples,
            ))

    return candidates


def build_echo_candidates(
    manuscript: ManuscriptArtifact,
    threshold: int = MA006_ECHO_THRESHOLD,
) -> list[EchoCandidate]:
    """Build thematic-echo candidates by scanning for recurring phrases."""
    candidates: list[EchoCandidate] = []

    for pattern, label in _THEMATIC_COMPILED:
        occurrences: list[tuple[int, str]] = []
        for scene in sorted(manuscript.scenes, key=lambda s: s.scene_number):
            for m in pattern.finditer(scene.text):
                start = max(0, m.start() - 40)
                end = min(len(scene.text), m.end() + 40)
                excerpt = scene.text[start:end].replace("\n", " ").strip()
                occurrences.append((scene.scene_number, excerpt))

        if len(occurrences) >= threshold:
            candidates.append(EchoCandidate(
                phrase_label=label,
                total_occurrences=len(occurrences),
                occurrences=occurrences,
            ))

    return candidates


# ── LLM helper ───────────────────────────────────────────────────────────────

def _call_llm(system: str, user: str, model: str = HAIKU_MODEL) -> str:
    pipeline_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from llm_client import call_llm
    response = call_llm(
        provider="anthropic",
        model=model,
        system=system,
        user=user,
        max_tokens=512,
        temperature=0.0,
    )
    return response.text


# ── Phase 2: LLM confirmation ───────────────────────────────────────────────

STUTTER_PROMPT = """You are validating whether a verb is being used as a characterization stutter.

CHARACTER: {character}
VERB: {verb}
TOTAL OCCURRENCES: {total} across the manuscript
OCCURRENCES ATTRIBUTED TO {character}: {char_count}

SAMPLE EXCERPTS:
{excerpts}

Decide:
(1) CHARACTERIZATION_STUTTER — this verb is repeatedly used to characterize
    {character} specifically, in narration about them, with little variation.
    The repetition reads as a tic the reader will notice.
(2) LEGITIMATE_VOCABULARY — the verb is reasonable narrative vocabulary that
    happens to attach to {character} because they are a frequent POV/subject.
    The repetition does not read as a tic.
(3) UNCERTAIN — neither clearly applies.

Respond with one line: CHARACTERIZATION_STUTTER, LEGITIMATE_VOCABULARY, or
UNCERTAIN. Then one sentence of reasoning."""

ECHO_PROMPT = """You are validating whether a thematic phrase is being repeated with development
or echoed without development.

PHRASE: {phrase}
TOTAL OCCURRENCES: {total} across the manuscript

OCCURRENCES (scene -> excerpt):
{occurrences}

Decide:
(1) THEMATIC_ECHO — the phrase recurs in nearly the same emotional/conceptual
    context each time. The repetition reinforces but does not develop. A reader
    would experience this as the writer leaning on a refrain.
(2) DEVELOPED_THEME — the phrase recurs but each occurrence advances the theme,
    adds a new facet, or marks a turning point. The repetition is intentional
    motif work.
(3) UNCERTAIN — neither clearly applies.

Respond with one line: THEMATIC_ECHO, DEVELOPED_THEME, or UNCERTAIN. Then one
sentence of reasoning."""


def llm_confirm_stutter(candidate: StutterCandidate) -> str:
    """Phase 2: confirm sub-check A. Returns CHARACTERIZATION_STUTTER /
    LEGITIMATE_VOCABULARY / UNCERTAIN."""
    excerpts = "\n".join(
        f"  sc {sn}: \"{ex}\"" for sn, ex in candidate.scene_examples[:5]
    )
    prompt = STUTTER_PROMPT.format(
        character=candidate.character,
        verb=candidate.verb,
        total=candidate.total_occurrences,
        char_count=candidate.character_occurrences,
        excerpts=excerpts,
    )
    system = "You are a manuscript craft auditor. Respond with one verdict line then one sentence of reasoning."

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = _call_llm(system, prompt)
            first_line = response.strip().splitlines()[0].strip().upper()
            for verdict in ("CHARACTERIZATION_STUTTER", "LEGITIMATE_VOCABULARY", "UNCERTAIN"):
                if verdict in first_line:
                    return verdict
            return "UNCERTAIN"
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"    WARN: stutter LLM failed: {e}", file=sys.stderr)
            return "UNCERTAIN"
    return "UNCERTAIN"


def llm_confirm_echo(candidate: EchoCandidate) -> str:
    """Phase 2: confirm sub-check B. Returns THEMATIC_ECHO /
    DEVELOPED_THEME / UNCERTAIN."""
    occ_text = "\n".join(
        f"  sc {sn}: \"{ex}\"" for sn, ex in candidate.occurrences[:10]
    )
    prompt = ECHO_PROMPT.format(
        phrase=candidate.phrase_label,
        total=candidate.total_occurrences,
        occurrences=occ_text,
    )
    system = "You are a manuscript craft auditor. Respond with one verdict line then one sentence of reasoning."

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = _call_llm(system, prompt)
            first_line = response.strip().splitlines()[0].strip().upper()
            for verdict in ("THEMATIC_ECHO", "DEVELOPED_THEME", "UNCERTAIN"):
                if verdict in first_line:
                    return verdict
            return "UNCERTAIN"
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"    WARN: echo LLM failed: {e}", file=sys.stderr)
            return "UNCERTAIN"
    return "UNCERTAIN"


# ── Finding builders ─────────────────────────────────────────────────────────

def _build_stutter_finding(candidate: StutterCandidate, severity: str, verdict: str) -> Finding:
    example_scenes = sorted(set(sn for sn, _ in candidate.scene_examples))
    evidence = [f"Scene {sn}: \"{ex}\"" for sn, ex in candidate.scene_examples[:3]]
    return Finding(
        check_id="MA-006-reintroduction",
        severity=severity,
        scene_number=None,
        scene_numbers=example_scenes,
        description=(
            f"Characterization stutter: '{candidate.character}' + '{candidate.verb}' "
            f"({candidate.character_occurrences}/{candidate.total_occurrences} occurrences, "
            f"dominance {candidate.dominance:.0%}) — verdict: {verdict}"
        ),
        evidence=evidence,
        suggested_fix=(
            f"Vary the verb '{candidate.verb}' for {candidate.character} or "
            f"reduce frequency below {MA006_STUTTER_THRESHOLD}"
        ),
    )


def _build_echo_finding(candidate: EchoCandidate, severity: str, verdict: str) -> Finding:
    example_scenes = sorted(set(sn for sn, _ in candidate.occurrences[:5]))
    evidence = [f"Scene {sn}: \"{ex}\"" for sn, ex in candidate.occurrences[:3]]
    return Finding(
        check_id="MA-006-reintroduction",
        severity=severity,
        scene_number=None,
        scene_numbers=example_scenes,
        description=(
            f"Thematic echo: '{candidate.phrase_label}' "
            f"({candidate.total_occurrences} occurrences) — verdict: {verdict}"
        ),
        evidence=evidence,
        suggested_fix=(
            f"Develop or vary the '{candidate.phrase_label}' motif across scenes"
        ),
    )


# ── Check module class ───────────────────────────────────────────────────────

class ReintroductionDetection:
    check_id = "MA-006-reintroduction"
    severity = "CLASS_A"
    description = (
        "Reintroduction detection: characterization-tic stutter and "
        "thematic-beat echo across manuscript scenes"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        findings: list[Finding] = []

        # Load character roster
        characters = _load_character_roster(briefs)
        print(f"    Character roster: {len(characters)} names", file=sys.stderr)

        # ─── Sub-check A: Characterization-tic stutter ───
        print("    Sub-check A: characterization-tic stutter", file=sys.stderr)

        pairs_by_char: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
        all_verb_counts: Counter = Counter()

        for scene in sorted(manuscript.scenes, key=lambda s: s.scene_number):
            pairs = extract_subject_verb_pairs(scene.text, scene.scene_number, characters)
            for char, verb, sn, excerpt in pairs:
                pairs_by_char[char].append((verb, sn, excerpt))
                all_verb_counts[verb] += 1

        stutter_candidates = build_stutter_candidates(
            pairs_by_char, all_verb_counts,
            MA006_STUTTER_THRESHOLD, MA006_STUTTER_DOMINANCE,
        )
        print(f"    -> {len(stutter_candidates)} stutter candidates", file=sys.stderr)

        for candidate in stutter_candidates:
            print(f"    -> candidate: {candidate.character}+{candidate.verb} "
                  f"({candidate.character_occurrences}/{candidate.total_occurrences})",
                  file=sys.stderr)
            verdict = llm_confirm_stutter(candidate)
            print(f"       verdict: {verdict}", file=sys.stderr)

            if verdict == "CHARACTERIZATION_STUTTER":
                findings.append(_build_stutter_finding(candidate, "CLASS_A", verdict))
            elif verdict == "UNCERTAIN":
                findings.append(_build_stutter_finding(candidate, "CLASS_B", verdict))

        # ─── Sub-check B: Thematic-beat echo ───
        print("    Sub-check B: thematic-beat echo", file=sys.stderr)

        echo_candidates = build_echo_candidates(manuscript, MA006_ECHO_THRESHOLD)
        print(f"    -> {len(echo_candidates)} echo candidates", file=sys.stderr)

        for candidate in echo_candidates:
            print(f"    -> candidate: '{candidate.phrase_label}' "
                  f"({candidate.total_occurrences} occ)", file=sys.stderr)
            verdict = llm_confirm_echo(candidate)
            print(f"       verdict: {verdict}", file=sys.stderr)

            if verdict == "THEMATIC_ECHO":
                findings.append(_build_echo_finding(candidate, "CLASS_A", verdict))
            elif verdict == "UNCERTAIN":
                findings.append(_build_echo_finding(candidate, "CLASS_B", verdict))

        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        class_b = sum(1 for f in findings if f.severity == "CLASS_B")
        print(f"    -> Total: {len(findings)} findings ({class_a} A, {class_b} B)",
              file=sys.stderr)

        return findings
