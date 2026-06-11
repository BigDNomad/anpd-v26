"""
MA-004: Object State Continuity — detects contradictions in the state of
significant objects across the manuscript.

Two sub-checks:
  A) Object lifecycle violation: terminal state in scene N, functional state
     in scene M, no replacement event between them.
  B) Named-object possession transfer without explanation.

Conservative bias: CLASS_A only on unambiguous contradictions. Generic
objects without unique identifiers are skipped.

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


# ── Constants ────────────────────────────────────────────────────────────────

HAIKU_MODEL = "claude-haiku-4-5"
MAX_RETRIES = 2

# ── Terminal state patterns ──────────────────────────────────────────────────

# Possessive pattern fragment: matches Name's (ASCII or smart quote)
_POSS = "[A-Z][a-z]+['\u2019]s"  # non-raw to allow unicode escape

# Each pattern: (label, compiled_regex)
# These match "object + terminal state" constructions
_TERMINAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("destroyed", re.compile(
        r"\b(the\s+\w+|" + _POSS + r"\s+\w+)\b[^.]{0,30}\b(destroyed|destruction)\b",
        re.IGNORECASE)),
    ("bricked", re.compile(
        r"\b(bricked\s+(?:laptop|tablet|phone|device))\b"
        r"|"
        r"\b(the\s+\w+|" + _POSS + r"\s+\w+)\b[^.]{0,20}\bbricked\b",
        re.IGNORECASE)),
    ("burned", re.compile(
        r"\b(the\s+\w+|" + _POSS + r"\s+\w+)\b[^.]{0,30}\b(burned|burnt|burned\s+through)\b",
        re.IGNORECASE)),
    ("lost", re.compile(
        r"\b(had\s+lost|was\s+lost|gone\s+missing|left\s+behind|abandoned)\b[^.]{0,30}"
        r"\b(the\s+\w+|" + _POSS + r"\s+\w+)\b",
        re.IGNORECASE)),
    ("device_dead", re.compile(
        r"\b(phone|laptop|tablet|radio|comms|battery|line)\s+(?:was\s+|went\s+)?dead\b",
        re.IGNORECASE)),
    ("jammed", re.compile(
        r"\b(" + _POSS + r"\s+(?:rifle|sidearm|pistol|gun|weapon))\b[^.]{0,20}\bjammed\b",
        re.IGNORECASE)),
    ("compromised", re.compile(
        r"\b(the\s+(?:encrypted\s+)?(?:tablet|phone|laptop|device|channel|line))\b[^.]{0,30}"
        r"\b(compromised|exposed)\b",
        re.IGNORECASE)),
]

# Replacement markers — presence between terminal and functional suppresses finding
_REPLACEMENT_RE = re.compile(
    r'\b(?:new\s+(?:phone|laptop|tablet|radio|rifle|sidearm|device)'
    r'|burner'
    r'|replaced'
    r'|swapped'
    r'|switched\s+to'
    r'|spare'
    r'|backup\s+(?:phone|laptop|tablet|radio|rifle|sidearm|device))\b',
    re.IGNORECASE,
)

# Named-object patterns: possessive or uniquely identified objects
_NAMED_OBJECT_RE = [
    # "Hank's rifle", "Cole's sidearm", "Lena's laptop"
    re.compile(r"\b([A-Z][a-záéíóúñ]+)['\u2019]s\s+(saber|rifle|sidearm|pistol|gun|file|envelope|laptop|phone|tablet|knife|map|radio)\b"),
    # "the encrypted tablet", "the sat phone", "the bricked laptop"
    re.compile(r"\b(the\s+(?:encrypted|bricked|cracked|modified|stolen)\s+(?:tablet|phone|laptop|device|sidearm|rifle))\b", re.IGNORECASE),
    # "the envelope from Hank", "Medina's file"
    re.compile(r"\b(the\s+\w+\s+from\s+[A-Z][a-záéíóúñ]+)\b"),
]

# Object classes for replacement matching
_OBJECT_CLASSES = {
    "laptop": "computing", "tablet": "computing", "phone": "computing",
    "rifle": "firearm", "sidearm": "firearm", "pistol": "firearm",
    "gun": "firearm", "weapon": "firearm",
    "radio": "comms", "comms": "comms", "line": "comms", "channel": "comms",
    "saber": "melee", "knife": "melee",
    "file": "document", "envelope": "document", "map": "document",
}


def _object_class(token: str) -> str:
    """Determine the object class from a token for replacement matching."""
    lower = token.lower()
    for key, cls in _OBJECT_CLASSES.items():
        if key in lower:
            return cls
    return "unknown"


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ObjectStateClaim:
    """An object-state claim from a scene."""
    scene_number: int
    object_token: str       # normalized: "coles_rifle", "the_encrypted_tablet"
    state: str              # "terminal" or "functional"
    excerpt: str
    state_label: str        # "bricked", "jammed", "in_use", etc.


# ── Extraction ───────────────────────────────────────────────────────────────

def _normalize_object_token(raw: str) -> str:
    """Normalize an object reference to a canonical token."""
    return re.sub(r'[^a-z0-9]+', '_', raw.lower()).strip('_')


def extract_object_claims(manuscript: ManuscriptArtifact) -> list[ObjectStateClaim]:
    """Extract named-object state claims from all scenes.

    Only extracts objects that have a unique identifier (possessive name,
    descriptive modifier). Generic objects like 'the phone' or 'her laptop'
    are skipped to maintain conservative bias.
    """
    claims: list[ObjectStateClaim] = []
    scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)

    for scene in scenes:
        text = scene.text[:4000]
        sn = scene.scene_number

        # Extract terminal states
        for label, pattern in _TERMINAL_PATTERNS:
            for m in pattern.finditer(text):
                matched = m.group(0)
                # Check if this involves a named/unique object
                is_named = False
                object_token = ""
                for np in _NAMED_OBJECT_RE:
                    nm = np.search(matched)
                    if nm:
                        is_named = True
                        object_token = _normalize_object_token(nm.group(0))
                        break

                # Also check broader context for named object
                if not is_named:
                    context_start = max(0, m.start() - 60)
                    context_end = min(len(text), m.end() + 60)
                    context = text[context_start:context_end]
                    for np in _NAMED_OBJECT_RE:
                        nm = np.search(context)
                        if nm:
                            is_named = True
                            object_token = _normalize_object_token(nm.group(0))
                            break

                if not is_named:
                    continue  # Skip generic objects

                start = max(0, m.start() - 40)
                end = min(len(text), m.end() + 40)
                excerpt = text[start:end].replace("\n", " ").strip()

                claims.append(ObjectStateClaim(
                    scene_number=sn,
                    object_token=object_token,
                    state="terminal",
                    excerpt=excerpt,
                    state_label=label,
                ))

        # Extract functional states for named objects
        for np in _NAMED_OBJECT_RE:
            for nm in np.finditer(text):
                object_token = _normalize_object_token(nm.group(0))

                # Check if this is already captured as terminal
                already_terminal = any(
                    c.scene_number == sn and c.object_token == object_token and c.state == "terminal"
                    for c in claims
                )
                if already_terminal:
                    continue

                # Check surrounding context for functional indicators
                context_start = max(0, nm.start() - 40)
                context_end = min(len(text), nm.end() + 80)
                context = text[context_start:context_end].lower()

                # Functional: used, opened, fired, carried, held, ran, operated
                functional_indicators = re.search(
                    r'\b(opened|fired|used|carried|held|ran|operated|typed|'
                    r'checked|pulled|set|sat with|working|running)\b',
                    context,
                )
                if functional_indicators:
                    start = max(0, nm.start() - 40)
                    end = min(len(text), nm.end() + 40)
                    excerpt = text[start:end].replace("\n", " ").strip()

                    claims.append(ObjectStateClaim(
                        scene_number=sn,
                        object_token=object_token,
                        state="functional",
                        excerpt=excerpt,
                        state_label="in_use",
                    ))

    return claims


def replacement_marker_between(
    manuscript: ManuscriptArtifact,
    start_scene: int,
    end_scene: int,
    obj_class: str,
) -> bool:
    """Check if any scene between start and end contains a replacement marker
    for the given object class."""
    for scene in manuscript.scenes:
        if scene.scene_number <= start_scene or scene.scene_number >= end_scene:
            continue
        text = scene.text[:4000]
        if _REPLACEMENT_RE.search(text):
            # Check if the replacement is for the same object class
            for key, cls in _OBJECT_CLASSES.items():
                if cls == obj_class and re.search(rf'\b{key}\b', text, re.IGNORECASE):
                    return True
            # Generic replacement markers (burner, replaced, spare) also count
            if re.search(r'\b(burner|replaced|spare)\b', text, re.IGNORECASE):
                return True
    return False


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

CONFIRMATION_PROMPT = """You are validating a possible object-state contradiction in a manuscript.

OBJECT: {object_token}
CLAIM A (scene {scene_a}, terminal state '{state_a}'):
  "{excerpt_a}"
CLAIM B (scene {scene_b}, functional state):
  "{excerpt_b}"

INTERVENING SCENE EXCERPTS (relevant lines from scenes {scene_a_plus} to {scene_b_minus}):
{intervening_excerpts}

Decide which of these is true:

(1) CONTRADICTION_CONFIRMED — the same object is described as terminal in claim A
    and functional in claim B, with no intervening replacement, recovery, or
    repair event.

(2) REPLACEMENT_PRESENT — between claim A and claim B, the manuscript shows
    or implies a replacement (new device, recovered backup, etc.) accounting
    for the functional state in claim B.

(3) OBJECT_MISMATCH — claim A and claim B refer to different objects despite
    similar names (e.g., "his phone" in A vs "his phone" in B are different
    devices).

(4) UNCERTAIN — none of the above clearly applies.

Respond with exactly one line: one of CONTRADICTION_CONFIRMED, REPLACEMENT_PRESENT,
OBJECT_MISMATCH, or UNCERTAIN. Then one sentence of reasoning."""


def _get_intervening_excerpts(
    manuscript: ManuscriptArtifact,
    start_scene: int,
    end_scene: int,
    object_token: str,
    max_chars: int = 2000,
) -> str:
    """Get relevant excerpts from scenes between start and end."""
    lines = []
    total = 0
    obj_words = set(object_token.replace("_", " ").split())

    for scene in sorted(manuscript.scenes, key=lambda s: s.scene_number):
        if scene.scene_number <= start_scene or scene.scene_number >= end_scene:
            continue
        # Extract lines mentioning the object or related terms
        for line in scene.text.split("\n"):
            if any(w in line.lower() for w in obj_words):
                excerpt = f"[sc {scene.scene_number}] {line.strip()[:200]}"
                if total + len(excerpt) > max_chars:
                    break
                lines.append(excerpt)
                total += len(excerpt)

    return "\n".join(lines) if lines else "(no relevant mentions found)"


def llm_confirm_contradiction(
    claim_a: ObjectStateClaim,
    claim_b: ObjectStateClaim,
    manuscript: ManuscriptArtifact,
) -> str:
    """Ask LLM to confirm or dismiss a candidate object-state contradiction."""
    intervening = _get_intervening_excerpts(
        manuscript, claim_a.scene_number, claim_b.scene_number, claim_a.object_token
    )

    prompt = CONFIRMATION_PROMPT.format(
        object_token=claim_a.object_token,
        scene_a=claim_a.scene_number,
        scene_b=claim_b.scene_number,
        state_a=claim_a.state_label,
        excerpt_a=claim_a.excerpt,
        excerpt_b=claim_b.excerpt,
        scene_a_plus=claim_a.scene_number + 1,
        scene_b_minus=claim_b.scene_number - 1,
        intervening_excerpts=intervening,
    )

    system = "You are a manuscript continuity auditor. Respond with exactly one verdict on the first line, then one sentence of reasoning on the second line."

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = _call_llm(system, prompt)
            first_line = response.strip().splitlines()[0].strip().upper()
            if first_line in ("CONTRADICTION_CONFIRMED", "REPLACEMENT_PRESENT",
                              "OBJECT_MISMATCH", "UNCERTAIN"):
                return first_line
            return "UNCERTAIN"
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"    WARN: LLM confirmation failed: {e}", file=sys.stderr)
            return "UNCERTAIN"

    return "UNCERTAIN"


# ── Finding builder ──────────────────────────────────────────────────────────

def _build_finding(
    object_token: str,
    claim_a: ObjectStateClaim,
    claim_b: ObjectStateClaim,
    severity: str,
    verdict: str,
) -> Finding:
    return Finding(
        check_id="MA-004-object-state-continuity",
        severity=severity,
        scene_number=None,
        scene_numbers=sorted([claim_a.scene_number, claim_b.scene_number]),
        description=(
            f"Object state contradiction for '{object_token}': "
            f"terminal state '{claim_a.state_label}' in scene {claim_a.scene_number}, "
            f"then functional in scene {claim_b.scene_number} — "
            f"verdict: {verdict}"
        ),
        evidence=[
            f"Scene {claim_a.scene_number} ({claim_a.state_label}): \"{claim_a.excerpt}\"",
            f"Scene {claim_b.scene_number} ({claim_b.state_label}): \"{claim_b.excerpt}\"",
        ],
        suggested_fix=(
            f"Reconcile state of '{object_token}' between scenes "
            f"{claim_a.scene_number} and {claim_b.scene_number}"
        ),
    )


# ── Check module class ───────────────────────────────────────────────────────

class ObjectStateContinuity:
    check_id = "MA-004-object-state-continuity"
    severity = "CLASS_A"
    description = (
        "Object state continuity: detects objects described as destroyed/lost/bricked "
        "in one scene then used in a later scene without replacement, and named-object "
        "possession transfers without explanation"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        findings: list[Finding] = []

        # Phase 1: Deterministic extraction
        print("    Phase 1: object state extraction", file=sys.stderr)
        claims = extract_object_claims(manuscript)
        print(f"    -> {len(claims)} object state claims", file=sys.stderr)

        # Group by object_token
        by_token: dict[str, list[ObjectStateClaim]] = {}
        for c in claims:
            by_token.setdefault(c.object_token, []).append(c)

        # Find terminal → functional transitions
        candidates: list[tuple[ObjectStateClaim, ObjectStateClaim]] = []
        for token, token_claims in by_token.items():
            sorted_claims = sorted(token_claims, key=lambda c: c.scene_number)
            for i, claim_a in enumerate(sorted_claims):
                if claim_a.state != "terminal":
                    continue
                for claim_b in sorted_claims[i + 1:]:
                    if claim_b.state != "functional":
                        continue
                    # Check for replacement marker between them
                    obj_cls = _object_class(token)
                    if replacement_marker_between(manuscript, claim_a.scene_number,
                                                   claim_b.scene_number, obj_cls):
                        print(f"    -> replacement found for '{token}' between "
                              f"sc {claim_a.scene_number} and sc {claim_b.scene_number}",
                              file=sys.stderr)
                        continue
                    candidates.append((claim_a, claim_b))
                    break  # Only pair with the next functional occurrence

        print(f"    -> {len(candidates)} candidate contradictions", file=sys.stderr)

        if not candidates:
            return findings

        # Phase 2: LLM confirmation
        print("    Phase 2: LLM confirmation", file=sys.stderr)
        for claim_a, claim_b in candidates:
            verdict = llm_confirm_contradiction(claim_a, claim_b, manuscript)
            print(f"    -> {claim_a.object_token}: {verdict}", file=sys.stderr)

            if verdict == "CONTRADICTION_CONFIRMED":
                findings.append(_build_finding(
                    claim_a.object_token, claim_a, claim_b, "CLASS_A", verdict))
            elif verdict == "UNCERTAIN":
                findings.append(_build_finding(
                    claim_a.object_token, claim_a, claim_b, "CLASS_B", verdict))
            # REPLACEMENT_PRESENT / OBJECT_MISMATCH → suppressed

        return findings
