"""
MA-003: Character Location Temporal — detects character location contradictions
where physical travel between claimed locations would take longer than the
elapsed story-time between the two claims.

Two-phase detection:
  Phase 1: deterministic regex extraction of (character, location, polarity)
           triples; window-based candidate generation
  Phase 2: LLM confirmation on Phase 1 candidates with narrative-bridge filter

Sub-check A: Same-character in two cities within too-short a window.
Sub-check B: Off-screen location implausibility (explicit "still in X"
             followed by presence in Y with no narrative bridge).

Severity: CLASS_A on CONTRADICTION_CONFIRMED, CLASS_B on UNCERTAIN.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

from audit_checks import ManuscriptArtifact, BriefBundle, Finding
from audit_checks._lib.timeline_extractor import (
    extract_timeline,
    elapsed_days_between,
    SceneTimeline,
)


# ── Configuration ────────────────────────────────────────────────────────────

MA003_WINDOW_SCENES = 3  # Max scene-gap for deterministic pairing
HAIKU_MODEL = "claude-haiku-4-5"
MAX_RETRIES = 2

# Distance class catalog. Keys are frozensets so (A, B) == (B, A).
_LOCATION_DISTANCE: dict[frozenset[str], str] = {
    frozenset({"caracas", "maracaibo"}): "same_country",
    frozenset({"caracas", "madrid"}):    "different_country",
    frozenset({"caracas", "washington"}): "different_country",
    frozenset({"caracas", "langley"}):   "different_country",
    frozenset({"caracas", "bogota"}):    "different_country",
    frozenset({"caracas", "lisbon"}):    "different_country",
    frozenset({"madrid", "lisbon"}):     "same_country",
    frozenset({"washington", "langley"}): "same_city",
}

_DEFAULT_DISTANCE = "different_country"


def distance_class(loc_a: str, loc_b: str) -> str:
    """Classify the travel distance between two locations."""
    a, b = loc_a.lower().strip(), loc_b.lower().strip()
    if a == b:
        return "same_city"
    return _LOCATION_DISTANCE.get(frozenset({a, b}), _DEFAULT_DISTANCE)


# ── Plausibility check ──────────────────────────────────────────────────────

def is_travel_plausible(elapsed_days: float | None, dist_class: str) -> bool:
    """Check if travel is plausible given elapsed time and distance class.

    Returns True if travel IS plausible (no contradiction).
    """
    if elapsed_days is None:
        return False  # Can't assess — conservative, flag it

    if dist_class == "same_city":
        return True  # Always plausible
    elif dist_class == "same_country":
        return elapsed_days > 1.0  # More than 1 day for in-country travel
    elif dist_class == "different_country":
        return elapsed_days > 0.5  # More than 12 hours for international
    return False


# ── Location tokens ──────────────────────────────────────────────────────────

_LOCATION_TOKENS_RE = re.compile(
    r'\b(Caracas|Maracaibo|Madrid|Washington|Langley|Bogot[áa]|Lisbon|'
    r'Venezuela|Spain|Colombia|Portugal|Panama(?:\s+City)?)\b',
    re.IGNORECASE,
)

# ── Regex catalog for location claims ────────────────────────────────────────

# Pattern group 1: "<Name> ... <verb> ... in <Location>"
_PAT_NAME_IN_LOC = re.compile(
    r'\b([A-Z][a-záéíóúñ]+(?:\s+[A-Z][a-záéíóúñ]+)?)\b'
    r'[^.]{0,80}?'
    r'\b(?:still|now|currently|back|was|is|sat|stood|stayed|lived|arrived)\b'
    r'[^.]{0,40}?'
    r'\bin\s+(Caracas|Maracaibo|Madrid|Washington|Langley|Bogot[áa]|Lisbon)\b',
    re.IGNORECASE,
)

# Pattern group 2: "His/Her <relation> ... in <Location>"
_PAT_POSS_REL_IN_LOC = re.compile(
    r'\b((?:His|Her|Their)\s+(?:wife|husband|son|daughter|daughters|family))\b'
    r'[^.]{0,60}?'
    r'\b(?:still|now|is|are|were|was|remained)\b'
    r'[^.]{0,40}?'
    r'\bin\s+(Caracas|Maracaibo|Madrid|Washington|Langley|Bogot[áa]|Lisbon)\b',
    re.IGNORECASE,
)

# Pattern group 3: "<relation> in <Location>" (shorter, catches "wife in Maracaibo")
_PAT_REL_IN_LOC = re.compile(
    r'\b(?:a|the|his|her)\s+(wife|husband)\s+in\s+'
    r'(Caracas|Maracaibo|Madrid|Washington|Langley|Bogot[áa]|Lisbon)\b',
    re.IGNORECASE,
)

# Pattern group 4: "daughters ... in <Location>" / "two daughters in <Location>"
_PAT_FAMILY_IN_LOC = re.compile(
    r'\b((?:two|three|his|her)\s+(?:daughters?|sons?|children))\b'
    r'[^.]{0,40}?'
    r'\bin\s+(Caracas|Maracaibo|Madrid|Washington|Langley|Bogot[áa]|Lisbon)\b',
    re.IGNORECASE,
)


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LocationClaim:
    """A character-location claim extracted from a scene."""
    scene_number: int
    entity_key: str       # normalized: "funes", "funes_wife", "hank"
    location: str         # normalized: "caracas", "maracaibo"
    excerpt: str          # matched text with context
    polarity: str = "present_in"


@dataclass
class CandidatePair:
    """A pair of conflicting location claims to be verified."""
    claim_a: LocationClaim
    claim_b: LocationClaim
    elapsed_days: float | None
    dist_class: str


# ── Entity key normalization ─────────────────────────────────────────────────

_NON_NAME_WORDS = {
    "the", "his", "her", "their", "she", "he", "it", "they", "this", "that",
    "was", "were", "had", "has", "have", "been", "being", "are", "is",
    "not", "but", "and", "for", "with", "from", "into", "about", "after",
    "before", "between", "still", "now", "then", "there", "here", "where",
    "when", "which", "what", "who", "how", "both", "each", "every", "all",
    "spanish", "venezuelan", "colombian", "portuguese", "american", "cuban",
    "mexican", "russian", "chinese", "iranian", "european",
    "scene", "chapter", "book", "page", "section",
    # Location names that appear as proper nouns but aren't character names
    "caracas", "maracaibo", "madrid", "washington", "langley", "lisbon",
    "bogota", "bogotá", "venezuela", "spain", "colombia", "portugal",
    "panama", "emirates", "petare", "guarenas", "cuba", "miami",
    "texas", "florida", "san", "antonio", "america", "bangkok",
    "sichuan", "agency", "cia", "nsa", "sebin", "fbi",
}


def _resolve_possessive_owner(scene_text: str, match_start: int) -> str:
    """Find the nearest preceding proper noun to resolve 'His/Her' possessives.

    Skips common non-name words (adjectives, pronouns, nationalities) to
    find actual character names.
    """
    lookback = scene_text[max(0, match_start - 500):match_start]
    # Find all capitalized words
    names = list(re.finditer(r'\b([A-Z][a-záéíóúñ]+)\b', lookback))
    # Walk backwards, skip non-name words
    for m in reversed(names):
        candidate = m.group(1)
        if candidate.lower() not in _NON_NAME_WORDS:
            return candidate.lower()
    return "unknown"


def normalize_entity_key(matched_subject: str, scene_text: str, match_start: int) -> str:
    """Map a matched subject to a canonical entity key."""
    subject = matched_subject.strip()
    lower = subject.lower()

    # Direct name match
    if re.match(r'^[A-Z][a-záéíóúñ]+(?:\s+[A-Z][a-záéíóúñ]+)?$', subject):
        return lower

    # Possessive relation: "His wife", "Her daughter"
    if lower.startswith(("his ", "her ", "their ")):
        relation = lower.split()[-1]  # "wife", "daughter", etc.
        owner = _resolve_possessive_owner(scene_text, match_start)
        return f"{owner}_{relation}"

    # Bare relation: "wife", "husband"
    if lower in ("wife", "husband"):
        owner = _resolve_possessive_owner(scene_text, match_start)
        return f"{owner}_{lower}"

    # Family reference: "two daughters", "his daughters"
    if "daughter" in lower or "son" in lower or "children" in lower:
        owner = _resolve_possessive_owner(scene_text, match_start)
        relation = "daughters" if "daughter" in lower else ("sons" if "son" in lower else "children")
        return f"{owner}_{relation}"

    return lower


# ── Phase 1: Deterministic extraction ────────────────────────────────────────

def extract_location_claims(manuscript: ManuscriptArtifact) -> list[LocationClaim]:
    """Run regex catalog over each scene, return location claims."""
    claims: list[LocationClaim] = []
    scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)

    for scene in scenes:
        text = scene.text[:3000]
        sn = scene.scene_number

        # Pattern 1: Name ... in Location
        for m in _PAT_NAME_IN_LOC.finditer(text):
            entity = normalize_entity_key(m.group(1), text, m.start())
            location = m.group(2).lower()
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            excerpt = text[start:end].replace("\n", " ").strip()
            claims.append(LocationClaim(sn, entity, location, excerpt))

        # Pattern 2: His/Her <relation> ... in Location
        for m in _PAT_POSS_REL_IN_LOC.finditer(text):
            entity = normalize_entity_key(m.group(1), text, m.start())
            location = m.group(2).lower()
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            excerpt = text[start:end].replace("\n", " ").strip()
            claims.append(LocationClaim(sn, entity, location, excerpt))

        # Pattern 3: <relation> in Location
        for m in _PAT_REL_IN_LOC.finditer(text):
            entity = normalize_entity_key(m.group(1), text, m.start())
            location = m.group(2).lower()
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            excerpt = text[start:end].replace("\n", " ").strip()
            claims.append(LocationClaim(sn, entity, location, excerpt))

        # Pattern 4: Family in Location
        for m in _PAT_FAMILY_IN_LOC.finditer(text):
            entity = normalize_entity_key(m.group(1), text, m.start())
            location = m.group(2).lower()
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            excerpt = text[start:end].replace("\n", " ").strip()
            claims.append(LocationClaim(sn, entity, location, excerpt))

    return claims


def _relation_suffix(entity_key: str) -> str | None:
    """Extract the relation suffix from an entity key, if present.

    'funes_wife' → 'wife', 'hank_daughters' → 'daughters', 'hank' → None
    """
    parts = entity_key.split("_")
    if len(parts) >= 2:
        return parts[-1]
    return None


def build_candidate_pairs(
    claims: list[LocationClaim],
    timelines: list[SceneTimeline] | None,
) -> list[CandidatePair]:
    """Build candidate contradiction pairs from claims.

    Pairs are generated when the same entity_key (or same relation suffix
    like 'wife') appears in two different locations within a scene window
    of MA003_WINDOW_SCENES.
    """
    # Group by entity_key
    by_entity: dict[str, list[LocationClaim]] = {}
    for c in claims:
        by_entity.setdefault(c.entity_key, []).append(c)

    # Also group by relation suffix to catch cross-entity coreference
    # (e.g., "hank_wife" and "prada_wife" may both refer to Funes's wife)
    by_relation: dict[str, list[LocationClaim]] = {}
    for c in claims:
        rel = _relation_suffix(c.entity_key)
        if rel:
            by_relation.setdefault(rel, []).append(c)

    # Merge: add relation groups as additional entity groups
    for rel, rel_claims in by_relation.items():
        group_key = f"_relation_{rel}"
        if group_key not in by_entity:
            by_entity[group_key] = rel_claims

    candidates: list[CandidatePair] = []

    for entity_key, entity_claims in by_entity.items():
        # Sort by scene number
        sorted_claims = sorted(entity_claims, key=lambda c: c.scene_number)
        # Deduplicate: keep one claim per (scene, location) pair
        seen: set[tuple[int, str]] = set()
        deduped: list[LocationClaim] = []
        for c in sorted_claims:
            key = (c.scene_number, c.location)
            if key not in seen:
                seen.add(key)
                deduped.append(c)

        # Check all pairs within the window
        for i in range(len(deduped)):
            for j in range(i + 1, len(deduped)):
                ca, cb = deduped[i], deduped[j]
                if ca.location == cb.location:
                    continue  # Same location — no contradiction
                scene_gap = abs(cb.scene_number - ca.scene_number)
                if scene_gap > MA003_WINDOW_SCENES:
                    continue  # Too far apart

                # Check timeline plausibility
                elapsed = None
                if timelines:
                    elapsed = elapsed_days_between(timelines, ca.scene_number, cb.scene_number)

                dist = distance_class(ca.location, cb.location)

                # Only flag if travel is NOT plausible
                if not is_travel_plausible(elapsed, dist):
                    candidates.append(CandidatePair(
                        claim_a=ca,
                        claim_b=cb,
                        elapsed_days=elapsed,
                        dist_class=dist,
                    ))

    return candidates


# ── LLM helper ───────────────────────────────────────────────────────────────

def _call_llm(system: str, user: str, model: str = HAIKU_MODEL) -> str:
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
        max_tokens=1024,
        temperature=0.0,
    )
    return response.text


# ── Phase 2: LLM confirmation ───────────────────────────────────────────────

CONFIRMATION_PROMPT = """You are validating a possible character location contradiction in a manuscript.

CHARACTER OR RELATION: {entity_key}
CLAIM A (scene {scene_a}): "{excerpt_a}"
CLAIM B (scene {scene_b}): "{excerpt_b}"

In-story elapsed time between scene {scene_a} and scene {scene_b}: {elapsed}
Distance class between locations: {distance_class}

Read the surrounding context (provided below) and decide which of these is true:

(1) CONTRADICTION_CONFIRMED — the claims describe the same entity in two different
    locations with no narrative bridge accounting for travel, and the elapsed time
    is insufficient for plausible travel.

(2) NARRATIVE_BRIDGE_PRESENT — the manuscript itself acknowledges or implies the
    travel (e.g., "the next morning she had moved", "after the flight to Madrid",
    or the locations are mentioned in a context that describes movement).

(3) ENTITY_MISMATCH — Claim A and Claim B refer to different entities (e.g.,
    "his wife" in claim A is a different character than "his wife" in claim B,
    or one of the matches is a coreference error).

(4) UNCERTAIN — none of the above clearly applies.

Respond with exactly one line: one of CONTRADICTION_CONFIRMED, NARRATIVE_BRIDGE_PRESENT,
ENTITY_MISMATCH, or UNCERTAIN. Then on a new line provide one sentence of reasoning.

CONTEXT (scene {scene_a}, ±200 words around the claim):
{context_a}

CONTEXT (scene {scene_b}, ±200 words around the claim):
{context_b}"""


def _get_context(manuscript: ManuscriptArtifact, scene_number: int, excerpt: str, radius: int = 800) -> str:
    """Get surrounding context for an excerpt in a scene."""
    scene = manuscript.scene_by_number(scene_number)
    if not scene:
        return excerpt
    text = scene.text
    # Find the excerpt in the scene text
    idx = text.find(excerpt[:50])
    if idx < 0:
        # Fallback: return first 1600 chars
        return text[:1600]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(excerpt) + radius)
    return text[start:end]


def llm_confirm_contradiction(
    candidate: CandidatePair,
    manuscript: ManuscriptArtifact,
) -> str:
    """Ask LLM to confirm or dismiss a candidate contradiction.

    Returns one of: CONTRADICTION_CONFIRMED, NARRATIVE_BRIDGE_PRESENT,
    ENTITY_MISMATCH, UNCERTAIN.
    """
    elapsed_str = f"~{candidate.elapsed_days:.1f} days" if candidate.elapsed_days is not None else "unknown"

    context_a = _get_context(manuscript, candidate.claim_a.scene_number, candidate.claim_a.excerpt)
    context_b = _get_context(manuscript, candidate.claim_b.scene_number, candidate.claim_b.excerpt)

    prompt = CONFIRMATION_PROMPT.format(
        entity_key=candidate.claim_a.entity_key,
        scene_a=candidate.claim_a.scene_number,
        scene_b=candidate.claim_b.scene_number,
        excerpt_a=candidate.claim_a.excerpt,
        excerpt_b=candidate.claim_b.excerpt,
        elapsed=elapsed_str,
        distance_class=candidate.dist_class,
        context_a=context_a,
        context_b=context_b,
    )

    system = "You are a manuscript continuity auditor. Respond with exactly one verdict on the first line, then one sentence of reasoning on the second line."

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = _call_llm(system, prompt)
            first_line = response.strip().splitlines()[0].strip().upper()
            if first_line in ("CONTRADICTION_CONFIRMED", "NARRATIVE_BRIDGE_PRESENT",
                              "ENTITY_MISMATCH", "UNCERTAIN"):
                return first_line
            return "UNCERTAIN"
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"    WARN: LLM confirmation failed for {candidate.claim_a.entity_key}: {e}",
                  file=sys.stderr)
            return "UNCERTAIN"

    return "UNCERTAIN"


# ── Finding builder ──────────────────────────────────────────────────────────

def _build_finding(candidate: CandidatePair, severity: str, verdict: str) -> Finding:
    """Build a Finding from a confirmed candidate pair."""
    ca, cb = candidate.claim_a, candidate.claim_b
    elapsed_str = f"~{candidate.elapsed_days:.1f} days" if candidate.elapsed_days is not None else "unknown"

    return Finding(
        check_id="MA-003-character-location-temporal",
        severity=severity,
        scene_number=None,
        scene_numbers=sorted([ca.scene_number, cb.scene_number]),
        description=(
            f"Location contradiction for '{ca.entity_key}': "
            f"{ca.location} (scene {ca.scene_number}) vs {cb.location} (scene {cb.scene_number}) — "
            f"elapsed time {elapsed_str}, distance class {candidate.dist_class}, "
            f"verdict: {verdict}"
        ),
        evidence=[
            f"Scene {ca.scene_number}: \"{ca.excerpt}\"",
            f"Scene {cb.scene_number}: \"{cb.excerpt}\"",
        ],
        suggested_fix=(
            f"Reconcile location for '{ca.entity_key}' between scenes "
            f"{ca.scene_number} and {cb.scene_number}"
        ),
    )


# ── Check module class ───────────────────────────────────────────────────────

class CharacterLocationTemporal:
    check_id = "MA-003-character-location-temporal"
    severity = "CLASS_A"
    description = (
        "Character location temporal: detects characters claimed in "
        "physically separated locations within implausibly short story-time windows"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        findings: list[Finding] = []

        # Timeline extraction (same pattern as MA-001)
        print("    Phase 0: timeline extraction", file=sys.stderr)
        try:
            timelines = extract_timeline(manuscript, use_llm=False)
            print(f"    -> {len(timelines)} scene timelines", file=sys.stderr)
        except Exception as e:
            print(f"    -> timeline extraction failed: {e}", file=sys.stderr)
            timelines = None

        # Phase 1: Deterministic regex extraction
        print("    Phase 1: deterministic location extraction", file=sys.stderr)
        claims = extract_location_claims(manuscript)
        print(f"    -> {len(claims)} location claims extracted", file=sys.stderr)

        # Build candidate pairs
        candidates = build_candidate_pairs(claims, timelines)
        print(f"    -> {len(candidates)} candidate contradictions", file=sys.stderr)

        if not candidates:
            return findings

        # Phase 2: LLM confirmation
        print("    Phase 2: LLM confirmation", file=sys.stderr)
        for cand in candidates:
            verdict = llm_confirm_contradiction(cand, manuscript)
            print(f"    -> {cand.claim_a.entity_key}: "
                  f"{cand.claim_a.location} vs {cand.claim_b.location} = {verdict}",
                  file=sys.stderr)

            if verdict == "CONTRADICTION_CONFIRMED":
                findings.append(_build_finding(cand, severity="CLASS_A", verdict=verdict))
            elif verdict == "UNCERTAIN":
                findings.append(_build_finding(cand, severity="CLASS_B", verdict=verdict))
            # NARRATIVE_BRIDGE_PRESENT and ENTITY_MISMATCH → suppressed

        return findings
