"""
MA-047 entity_consistency — checks manuscript prose against sealed entity_ledger.

Phase 2b of S-2: the audit-time half of the entity-fact control loop.
Reads the manuscript directly against the sealed ledger; MUST NOT consume
state_tracker output (that would collapse the loop).

Dispatch by entity_class:
  scalar      → deterministic designation/count/side comparison (T1)
  stateful    → forbidden-state scan (T2, CLASS_B)
  lifecycle_role → death-vs-canon + role-binding (T2/T3)

Severity policy (F-INT-9): designation and death_assertion are CLASS_A
(hard-block); count/side/forbidden_state/role_violation are CLASS_B
(advisory, pending LLM rebuild — F-INT-9 Part 2). Provenance sorts
triage order only; never suppresses block.

See: ANPD_V25_Check_Module_Spec_S2_Phase2b_check_entity_consistency_20260528_T2230.md
"""

from __future__ import annotations

import re
import sys
from typing import Any

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Number parsing ───────────────────────────────────────────────────────

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}

_COMPOUND_NUMS: dict[str, int] = {}
for _tens_word, _tens_val in [
    ("two", 2), ("three", 3), ("four", 4), ("five", 5),
    ("six", 6), ("seven", 7), ("eight", 8), ("nine", 9),
]:
    _COMPOUND_NUMS[f"{_tens_word} hundred"] = _tens_val * 100


def _parse_number_token(text: str) -> int | None:
    """Parse a number word or digit string into an integer."""
    text = text.strip().lower()
    for compound, val in _COMPOUND_NUMS.items():
        if text == compound:
            return val
    if text in _WORD_NUMS:
        return _WORD_NUMS[text]
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


# Tokens that, when immediately following a number, bind the number to
# something other than the entity head-noun. Prevents "eight thousand feet"
# matching cipher_rotors, "three times" matching claymores, etc.
_COUNT_REBIND_WORDS = {
    "thousand", "hundred", "million", "billion",
    "feet", "meters", "miles", "kilometers", "yards",
    "times", "hours", "minutes", "seconds", "days", "weeks", "months", "years",
    "percent", "degrees", "rounds", "shots", "pounds", "kilograms",
    "o'clock", "oclock",
}


# ── Designation scanner ──────────────────────────────────────────────────


def _build_designation_family_regex(designation: str) -> re.Pattern:
    """Build a regex that matches the designation's shape-family."""
    base = designation.rstrip("s")
    m = re.match(r'^([A-Za-z]+)-(\d+)(/[A-Za-z])?([A-Za-z])?$', base)
    if m:
        prefix = m.group(1)
        has_slash_suffix = m.group(3) is not None
        has_letter_suffix = m.group(4) is not None
        if has_slash_suffix:
            pattern = rf'\b{re.escape(prefix)}-\d+(/[A-Za-z])?\b'
        elif has_letter_suffix:
            pattern = rf'\b{re.escape(prefix)}-\d+[A-Za-z]?\b'
        else:
            pattern = rf'\b{re.escape(prefix)}-\d+\b'
        pattern = pattern.rstrip(r'\b') + r's?\b'
        return re.compile(pattern, re.IGNORECASE)
    return re.compile(re.escape(designation), re.IGNORECASE)


def _scan_designations(designation: str, lines: list[str]) -> list[dict]:
    """Scan prose lines for designation-family matches."""
    family_re = _build_designation_family_regex(designation)
    results = []
    for i, line in enumerate(lines):
        for m in family_re.finditer(line):
            results.append({
                "fact_type": "designation",
                "asserted": m.group(0),
                "canonical": designation,
                "line_number": i + 1,
                "context": line.strip(),
            })
    return results


# ── Count scanner (calibration-2: adjacency-tightened) ───────────────────


def _derive_head_nouns(canonical_name: str) -> list[str]:
    """Extract searchable head nouns from canonical_name."""
    tokens = canonical_name.split()
    significant = [t for t in tokens if not re.match(r'^[A-Z]{1,4}[-/]\d', t)]
    if not significant:
        significant = tokens
    tail = significant[-2:] if len(significant) >= 2 else significant
    nouns = set()
    for t in tail:
        t_lower = t.lower().rstrip("s")
        nouns.add(t_lower)
        nouns.add(t_lower + "s")
        nouns.add(t.lower())
    return list(nouns)


_NUM_WORD_PATTERN = (
    r'(?:three hundred|two hundred|four hundred|five hundred|'
    r'one|two|three|four|five|six|seven|eight|nine|ten|'
    r'eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|'
    r'nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|'
    r'hundred|\d+)'
)


def _scan_counts(count_value: int, canonical_name: str, lines: list[str]) -> list[dict]:
    """Scan prose lines for count assertions near head nouns.

    Calibration-2: rejects matches where the number binds to a different
    noun (e.g. 'eight thousand feet' does not match 'rotors').
    """
    head_nouns = _derive_head_nouns(canonical_name)
    if not head_nouns:
        return []

    results = []
    noun_alt = "|".join(re.escape(n) for n in head_nouns)
    head_noun_set = {n.lower() for n in head_nouns}

    # Pattern A: NUMBER ... NOUN (tight: ≤2 intervening tokens)
    pattern_a = re.compile(
        rf'(?P<num>{_NUM_WORD_PATTERN})\s+(?P<gap>(?:\w+\s+){{0,2}})(?P<noun>{noun_alt})',
        re.IGNORECASE,
    )
    # Pattern B: NOUN ... NUMBER
    pattern_b = re.compile(
        rf'(?P<noun>{noun_alt})\s*(?:,\s*)?(?:\w+\s+){{0,3}}(?P<num>{_NUM_WORD_PATTERN})',
        re.IGNORECASE,
    )

    for i, line in enumerate(lines):
        for m in pattern_a.finditer(line):
            num = _parse_number_token(m.group("num"))
            if num is None:
                continue
            # Calibration-2: check if the token after the number rebinds it
            gap_text = m.group("gap").strip().lower()
            gap_tokens = gap_text.split() if gap_text else []
            if gap_tokens and gap_tokens[0] in _COUNT_REBIND_WORDS:
                continue
            results.append({
                "fact_type": "count",
                "asserted": num,
                "canonical": count_value,
                "line_number": i + 1,
                "context": line.strip(),
                "noun_matched": m.group("noun"),
            })
        for m in pattern_b.finditer(line):
            num = _parse_number_token(m.group("num"))
            if num is None:
                continue
            already = any(
                r["line_number"] == i + 1 and r["asserted"] == num
                for r in results
            )
            if not already:
                results.append({
                    "fact_type": "count",
                    "asserted": num,
                    "canonical": count_value,
                    "line_number": i + 1,
                    "context": line.strip(),
                    "noun_matched": m.group("noun"),
                })
    return results


# ── Side scanner (M-3 damage_side) ──────────────────────────────────────

_SIDE_VALUES = {"right", "left", "port", "starboard"}

# Calibration-3: wound-context gate. A left/right mention counts as a
# damage-side assertion ONLY when a wound/injury/burn term is also present
# in the proximity window. Without this gate, aircraft references ("left
# wing", "left side of the flight deck") near Coyle produce ~20 false
# positives. Terms drawn from actual M-3 defect lines (L5251, L5375).
_WOUND_CONTEXT = {
    "burn", "burned", "burns", "burning",
    "wound", "wounded", "wounds",
    "scar", "scarred", "scars",
    "injury", "injured",
    "blister", "blistered",
    "skin", "flesh",
    "blood", "bleeding", "bled",
    "face",
    "thigh",
}


def _scan_sides(side_value: str, canonical_name: str, lines: list[str]) -> list[dict]:
    """Scan prose for side assertions near entity-related body parts.

    For damage_side (Coyle burn/damage side), looks for 'right/left'
    adjacent to wound-related terms near the entity's character.

    Calibration-3: requires a wound-context term in the proximity window
    to distinguish wound-side references from aircraft/direction references.
    """
    char_name = canonical_name.split()[0].lower() if canonical_name else ""
    wound_terms = r'(?:burn|wound|injur|scar|damage|arm|side|face|thigh|leg)'

    results = []
    # Pattern: SIDE + wound-term within tight window
    side_pattern = re.compile(
        rf'\b(?P<side>right|left)\b\s+(?:\w+\s+){{0,2}}{wound_terms}',
        re.IGNORECASE,
    )
    # Reverse: wound-term ... on the SIDE
    reverse_pattern = re.compile(
        rf'{wound_terms}\w*\s+(?:\w+\s+){{0,3}}(?:on\s+(?:the\s+)?)?(?P<side>right|left)\b',
        re.IGNORECASE,
    )

    for i, line in enumerate(lines):
        # Only match near the character
        if char_name:
            window_start = max(0, i - 3)
            window_end = min(len(lines), i + 4)
            window_text = " ".join(lines[window_start:window_end]).lower()
            if char_name not in window_text:
                continue

        # Calibration-3: wound-context gate — require at least one wound
        # term in the proximity window to distinguish wound references from
        # aircraft/direction references
        window_start = max(0, i - 2)
        window_end = min(len(lines), i + 3)
        window_raw = " ".join(lines[window_start:window_end]).lower()
        # Strip punctuation for word matching
        window_words = re.findall(r'[a-z]+', window_raw)
        if not any(w in _WOUND_CONTEXT for w in window_words):
            continue

        for pat in (side_pattern, reverse_pattern):
            for m in pat.finditer(line):
                asserted = m.group("side").lower()
                already = any(
                    r["line_number"] == i + 1 and r["asserted"] == asserted
                    for r in results
                )
                if not already:
                    results.append({
                        "fact_type": "side",
                        "asserted": asserted,
                        "canonical": side_value,
                        "line_number": i + 1,
                        "context": line.strip(),
                    })
    return results


# ── Main matcher function ────────────────────────────────────────────────


def find_asserted_facts(entity: dict, manuscript_text: str) -> list[dict]:
    """Find prose references that assert a checkable fact about the entity.

    Declaration-anchored: operates only on the passed declared entity; never
    mints new entities. Empty list if no references found.

    Returns:
        [{'fact_type': 'designation'|'count'|'side'|'forbidden_state',
          'asserted': <str|int>,
          'canonical': <str|int|'forbidden'>,
          'line_number': int,
          'context': str}, ...]
    """
    entity_class = entity.get("entity_class", "")
    lines = manuscript_text.split("\n")
    results = []

    if entity_class == "scalar":
        invariants = entity.get("invariants", {})
        canonical_name = entity.get("canonical_name", "")

        for inv_key, inv_value in invariants.items():
            if inv_key == "designation":
                results.extend(_scan_designations(str(inv_value), lines))
            elif inv_key == "count":
                results.extend(_scan_counts(inv_value, canonical_name, lines))
            elif inv_key == "side":
                results.extend(_scan_sides(str(inv_value), canonical_name, lines))

    elif entity_class == "stateful":
        forbidden = entity.get("state_track", {}).get("forbidden_states", [])
        for state_label in forbidden:
            terms = state_label.replace("_injury", "").replace("_", " ")
            term_re = re.compile(rf'\b{re.escape(terms)}\b', re.IGNORECASE)
            hyphen_terms = terms.replace(" ", "-")

            entity_name = entity.get("canonical_name", "").split("'")[0].strip()
            entity_id = entity.get("id", "")

            for i, line in enumerate(lines):
                window_start = max(0, i - 3)
                window_end = min(len(lines), i + 4)
                window_text = " ".join(lines[window_start:window_end]).lower()
                if entity_id not in window_text and entity_name.lower() not in window_text:
                    continue

                if term_re.search(line) or hyphen_terms.lower() in line.lower():
                    results.append({
                        "fact_type": "forbidden_state",
                        "asserted": state_label,
                        "canonical": "forbidden",
                        "line_number": i + 1,
                        "context": line.strip(),
                    })

    return results


# ── Death-language scanner (lifecycle) ───────────────────────────────────

_DEATH_PATTERNS = [
    r'{NAME}\s+(?:was\s+)?dead\b',
    r'{NAME}\s+died\b',
    r'{NAME}\s+was\s+killed',
    r'killed\s+{NAME}\b',
    r'{NAME}.*(?:body|corpse)\s+(?:lay|settled|fell)',
    r'{NAME}\s+(?:fell\s+dead|collapsed\s+dead|bled\s+out)',
]


def _scan_deaths(entity_name: str, aliases: list[str], lines: list[str]) -> list[dict]:
    """Scan for death assertions about a named entity."""
    names = [entity_name] + aliases
    results = []
    for name in names:
        escaped = re.escape(name)
        for pattern_template in _DEATH_PATTERNS:
            pattern = pattern_template.replace(r'{NAME}', escaped)
            pat_re = re.compile(pattern, re.IGNORECASE)
            for i, line in enumerate(lines):
                if pat_re.search(line):
                    already = any(r["line_number"] == i + 1 for r in results)
                    if not already:
                        results.append({
                            "fact_type": "death_assertion",
                            "asserted": name,
                            "line_number": i + 1,
                            "context": line.strip(),
                        })
    return results


def _scan_role_violations(role_binding: dict, lines: list[str]) -> list[dict]:
    """Scan for forbidden-name appearances in role-binding contexts."""
    context_kw = role_binding.get("context", "").lower()
    forbidden = role_binding.get("forbidden_references", [])
    if not forbidden:
        return []

    # Context keywords to identify relevant passages
    context_terms = [w for w in context_kw.split() if len(w) > 3]

    results = []
    for i, line in enumerate(lines):
        line_lower = line.lower()
        # Check if we're in a relevant context (gunship / Black Widow scenes)
        window_start = max(0, i - 5)
        window_end = min(len(lines), i + 6)
        window_text = " ".join(lines[window_start:window_end]).lower()
        in_context = any(term in window_text for term in context_terms) or \
                     "black widow" in window_text or "gunship" in window_text

        if not in_context:
            continue

        for name in forbidden:
            if re.search(rf'\b{re.escape(name)}\b', line, re.IGNORECASE):
                results.append({
                    "fact_type": "role_violation",
                    "asserted": name,
                    "context_label": role_binding.get("context", ""),
                    "line_number": i + 1,
                    "context": line.strip(),
                })
    return results


# ── LLM helper (follows character_detail_consistency pattern) ────────────

HAIKU_MODEL = "claude-haiku-4-5"


def _call_llm(system: str, user: str, model: str = HAIKU_MODEL) -> str:
    """Call LLM via the pipeline's llm_client."""
    try:
        from llm_client import call_llm
        response = call_llm(
            provider="anthropic",
            model=model,
            system=system,
            user=user,
            max_tokens=500,
            temperature=0.0,
        )
        return response.text
    except Exception as e:
        print(f"    MA-047: LLM call failed ({e}), falling back to regex-only", file=sys.stderr)
        return ""


def _llm_coda_death_check(entity_name: str, coda_text: str) -> bool:
    """Targeted LLM call scoped to the coda: is this character described as dead?"""
    if not coda_text.strip():
        return False

    system = (
        "You are a manuscript fact-checker. You will be given a passage from "
        "the final section of a novel. Answer ONLY 'YES' or 'NO'."
    )
    user = (
        f"In the following passage, is the character '{entity_name}' described as "
        f"dead, killed, or having died?\n\n"
        f"---\n{coda_text[:3000]}\n---\n\n"
        f"Answer YES or NO only."
    )
    response = _call_llm(system, user)
    return response.strip().upper().startswith("YES")


# ── Provenance helpers ───────────────────────────────────────────────────


def _get_provenance(ledger: dict, entity_id: str, inv_key: str) -> dict:
    """Look up provenance for an entity.invariant_key."""
    prov = ledger.get("provenance", {})
    return prov.get(f"{entity_id}.{inv_key}", {})


def _provenance_evidence(prov: dict) -> str:
    """Build a human-readable provenance evidence string."""
    origin = prov.get("origin", "unknown")
    resolution = prov.get("resolution", "unknown")
    if resolution == "auto_resolved":
        return f"[provenance: {origin}, {resolution} — lower-confidence — verify ledger before prose]"
    return f"[provenance: {origin}, {resolution}]"


# ── Suggested tier assignment ────────────────────────────────────────────

_TIER_MAP = {
    "scalar": "Tier 1",
    "stateful_forbidden": "Tier 2",
    "role_binding": "Tier 2",
    "lifecycle_canon": "Tier 3",
}


# ── Line → scene mapping ────────────────────────────────────────────────


def _build_line_to_scene(manuscript: 'ManuscriptArtifact') -> list[tuple[int, int]]:
    """Build sorted (first_line, scene_number) boundaries for line→scene lookup.

    Lines are 1-based, matching fact['line_number'] from scanners.
    full_text() joins scenes with '\\n\\n', so each separator adds one empty line.
    """
    boundaries: list[tuple[int, int]] = []
    current_line = 1
    sorted_scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)
    for i, scene in enumerate(sorted_scenes):
        boundaries.append((current_line, scene.scene_number))
        scene_line_count = scene.text.count("\n") + 1
        current_line += scene_line_count
        if i < len(sorted_scenes) - 1:
            current_line += 1  # empty line from "\n\n" join
    return boundaries


def _resolve_scene_number(boundaries: list[tuple[int, int]], line_number: int | None) -> int | None:
    """Given a line number in the full text, return the scene_number it belongs to."""
    if line_number is None:
        return None
    result = None
    for start, sn in boundaries:
        if start <= line_number:
            result = sn
        else:
            break
    return result


# ── State-transition timeline builder ───────────────────────────────────


def _build_expected_state_timeline(
    entity_id: str, initial_state: str, transitions: list[dict],
) -> tuple[list[tuple[int, str]] | None, Finding | None]:
    """Build an ordered timeline of (scene_number, state_in_effect) from the
    ledger's allowed_transitions.

    Returns (timeline, None) on success or (None, finding) if the chain is
    malformed.  The timeline is a sorted list of (occurs_at_scene, new_state)
    tuples; the caller can derive expected_state(scene_i) by scanning forward.
    """
    if not transitions:
        return None, None  # no transitions declared — nothing to check

    # Validate chain: occurs_at_scene strictly increasing, each from ==
    # state in effect immediately before.
    state_in_effect = initial_state
    prev_scene: int | None = None
    timeline: list[tuple[int, str]] = []

    for idx, t in enumerate(transitions):
        t_from = t.get("from", "")
        t_to = t.get("to", "")
        t_scene = t.get("occurs_at_scene")

        if t_scene is None or not isinstance(t_scene, int):
            finding = Finding(
                check_id="MA-047-entity-consistency",
                severity="CLASS_A",
                scene_number=None,
                description=(
                    f"Ledger chain malformed: {entity_id} — transition #{idx} "
                    f"has non-integer occurs_at_scene ({t_scene!r})."
                ),
            )
            return None, finding

        if prev_scene is not None and t_scene <= prev_scene:
            finding = Finding(
                check_id="MA-047-entity-consistency",
                severity="CLASS_A",
                scene_number=t_scene,
                description=(
                    f"Ledger chain malformed: {entity_id} — transition #{idx} "
                    f"occurs_at_scene {t_scene} is not strictly after {prev_scene}."
                ),
            )
            return None, finding

        if t_from != state_in_effect:
            finding = Finding(
                check_id="MA-047-entity-consistency",
                severity="CLASS_A",
                scene_number=t_scene,
                description=(
                    f"Ledger chain malformed: {entity_id} — transition #{idx} "
                    f"expects from='{t_from}' but state in effect is '{state_in_effect}'."
                ),
            )
            return None, finding

        timeline.append((t_scene, t_to))
        state_in_effect = t_to
        prev_scene = t_scene

    return timeline, None


def _expected_state_at_scene(
    initial_state: str, timeline: list[tuple[int, str]], scene_number: int,
) -> str:
    """Return the expected state at a given scene number."""
    state = initial_state
    for t_scene, t_to in timeline:
        if scene_number >= t_scene:
            state = t_to
        else:
            break
    return state


# ── State-transition LLM extraction ───────────────────────────────────


def _extract_asserted_state_llm(
    entity_name: str, state_vocab: list[str], scene_text: str,
) -> str:
    """Call Haiku to extract the single state label asserted in scene_text.

    Returns a label from state_vocab or "indeterminate".
    """
    vocab_str = ", ".join(f'"{s}"' for s in state_vocab)
    system = (
        "You are a manuscript fact-checker. You will be given a scene from a "
        "novel and a list of possible physical-state labels for an entity. "
        "Return ONLY the single state label from the list that the scene's "
        "prose asserts for the entity, or \"indeterminate\" if the entity is "
        "not mentioned, its state is unclear, or the scene does not assert a "
        "specific physical state. Do NOT return any other text."
    )
    user = (
        f"Entity: \"{entity_name}\"\n"
        f"Allowed state labels: [{vocab_str}, \"indeterminate\"]\n\n"
        f"Scene text:\n---\n{scene_text[:4000]}\n---\n\n"
        f"Which single state label does this scene assert for the entity? "
        f"Reply with ONLY the label."
    )
    response = _call_llm(system, user)
    label = response.strip().strip('"').strip("'")

    # Constrain to vocab ∪ {"indeterminate"}
    if label in state_vocab:
        return label
    # Fuzzy match: lowercase comparison
    label_lower = label.lower()
    for v in state_vocab:
        if v.lower() == label_lower:
            return v
    return "indeterminate"


# ── State-transition contradiction detection ──────────────────────────


def _detect_transition_violations(
    entity_id: str,
    initial_state: str,
    timeline: list[tuple[int, str]],
    state_vocab: list[str],
    scene_assertions: list[tuple[int, str, str]],  # (scene_number, asserted_state, scene_excerpt)
) -> list[Finding]:
    """Detect retrograde, premature, and off-vocabulary state violations.

    scene_assertions: [(scene_number, asserted_state, excerpt), ...]
    """
    # Build ordered state chain for positional comparison
    state_order: dict[str, int] = {initial_state: 0}
    for idx, (_, t_to) in enumerate(timeline):
        if t_to not in state_order:
            state_order[t_to] = idx + 1

    findings: list[Finding] = []
    for scene_num, asserted, excerpt in scene_assertions:
        if asserted == "indeterminate":
            continue

        expected = _expected_state_at_scene(initial_state, timeline, scene_num)
        if asserted == expected:
            continue

        # Determine violation type
        if asserted not in state_vocab:
            violation_type = "off-vocabulary"
        elif state_order.get(asserted, 999) < state_order.get(expected, 999):
            violation_type = "retrograde"
        else:
            violation_type = "premature"

        findings.append(Finding(
            check_id="MA-047-entity-consistency",
            severity=_SEVERITY_BY_FACT["state_transition_violation"],
            scene_number=scene_num,
            description=(
                f"State-transition violation ({violation_type}): {entity_id} — "
                f"scene {scene_num} asserts '{asserted}' but expected "
                f"'{expected}' per ledger timeline."
            ),
            evidence=[excerpt[:300]],
            suggested_fix=(
                f"Tier 2: regenerate scene {scene_num} so that {entity_id} "
                f"reflects state '{expected}', not '{asserted}'."
            ),
        ))

    return findings


# ── Check class ──────────────────────────────────────────────────────────


_SEVERITY_BY_FACT = {
    "designation":              "CLASS_A",
    "death_assertion":          "CLASS_A",
    "state_transition_violation":"CLASS_A",
    "count":                    "CLASS_B",
    "side":                     "CLASS_B",
    "forbidden_state":          "CLASS_B",
    "role_violation":           "CLASS_B",
}


class EntityConsistencyCheck:
    """MA-047: Entity-fact consistency check against sealed entity_ledger."""

    check_id = "MA-047-entity-consistency"
    severity = "CLASS_A"
    description = "Entity-fact consistency: checks manuscript prose against sealed entity ledger"

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        ledger = briefs.entity_ledger
        if not ledger:
            return []
        if not ledger.get("ledger_meta", {}).get("sealed"):
            print("    MA-047: ledger not sealed — skipping", file=sys.stderr)
            return []

        entities = {e["id"]: e for e in ledger.get("entities", [])}
        text = manuscript.full_text()
        lines = text.split("\n")
        self._scene_boundaries = _build_line_to_scene(manuscript)
        findings: list[Finding] = []

        # Get coda text (last ~15% of manuscript) for lifecycle checks
        total_lines = len(lines)
        coda_start = int(total_lines * 0.85)
        coda_text = "\n".join(lines[coda_start:])

        for eid, entity in entities.items():
            ec = entity.get("entity_class", "")
            if ec == "scalar":
                findings.extend(self._handle_scalar(entity, text, ledger))
            elif ec == "stateful":
                findings.extend(self._handle_stateful(entity, text, ledger))
                findings.extend(self._handle_stateful_transitions(entity, manuscript, ledger))
            elif ec == "lifecycle_role":
                findings.extend(self._handle_lifecycle(entity, text, lines, coda_text, ledger))

        return findings

    def _handle_scalar(self, entity: dict, text: str, ledger: dict) -> list[Finding]:
        """Scalar handler: deterministic designation/count/side comparison."""
        facts = find_asserted_facts(entity, text)
        findings = []
        eid = entity["id"]
        invariants = entity.get("invariants", {})

        for fact in facts:
            ft = fact["fact_type"]
            asserted = fact["asserted"]
            canonical = fact["canonical"]

            if ft == "designation":
                # Normalize: strip trailing 's', compare case-insensitive
                asserted_norm = str(asserted).rstrip("s").upper()
                canonical_norm = str(canonical).rstrip("s").upper()
                if asserted_norm != canonical_norm:
                    prov = _get_provenance(ledger, eid, "designation")
                    prov_str = _provenance_evidence(prov)
                    findings.append(Finding(
                        check_id=self.check_id,
                        severity=_SEVERITY_BY_FACT["designation"],
                        scene_number=_resolve_scene_number(self._scene_boundaries, fact["line_number"]),
                        description=(
                            f"Designation mismatch: {eid} — manuscript says '{asserted}', "
                            f"ledger says '{canonical}'. {prov_str}"
                        ),
                        evidence=[fact["context"]],
                        suggested_fix=(
                            f"Tier 1: replace '{asserted}' with '{canonical}' at line {fact['line_number']}."
                        ),
                        line_number=fact["line_number"],
                    ))

            elif ft == "count":
                if asserted != canonical:
                    prov = _get_provenance(ledger, eid, "count")
                    prov_str = _provenance_evidence(prov)
                    findings.append(Finding(
                        check_id=self.check_id,
                        severity=_SEVERITY_BY_FACT["count"],
                        scene_number=_resolve_scene_number(self._scene_boundaries, fact["line_number"]),
                        description=(
                            f"Count mismatch: {eid} — manuscript says {asserted}, "
                            f"ledger says {canonical}. {prov_str}"
                        ),
                        evidence=[fact["context"]],
                        suggested_fix=(
                            f"Tier 1: correct count from {asserted} to {canonical} at line {fact['line_number']}."
                        ),
                        line_number=fact["line_number"],
                    ))

            elif ft == "side":
                if str(asserted).lower() != str(canonical).lower():
                    prov = _get_provenance(ledger, eid, "side")
                    prov_str = _provenance_evidence(prov)
                    findings.append(Finding(
                        check_id=self.check_id,
                        severity=_SEVERITY_BY_FACT["side"],
                        scene_number=_resolve_scene_number(self._scene_boundaries, fact["line_number"]),
                        description=(
                            f"Side mismatch: {eid} — manuscript says '{asserted}', "
                            f"ledger says '{canonical}'. {prov_str}"
                        ),
                        evidence=[fact["context"]],
                        suggested_fix=(
                            f"Tier 1: correct side reference from '{asserted}' to '{canonical}' "
                            f"at line {fact['line_number']}."
                        ),
                        line_number=fact["line_number"],
                    ))

        return findings

    def _handle_stateful(self, entity: dict, text: str, ledger: dict) -> list[Finding]:
        """Stateful handler: forbidden-state scan (CLASS_B, Tier 2)."""
        facts = find_asserted_facts(entity, text)
        findings = []
        eid = entity["id"]

        for fact in facts:
            if fact["fact_type"] == "forbidden_state":
                prov = _get_provenance(ledger, eid, "forbidden_states")
                prov_str = _provenance_evidence(prov)
                findings.append(Finding(
                    check_id=self.check_id,
                    severity=_SEVERITY_BY_FACT["forbidden_state"],
                    scene_number=_resolve_scene_number(self._scene_boundaries, fact["line_number"]),
                    description=(
                        f"Forbidden state: {eid} — manuscript references '{fact['asserted']}', "
                        f"which is a forbidden state per the entity ledger. {prov_str}"
                    ),
                    evidence=[fact["context"]],
                    suggested_fix=(
                        f"Tier 2: regenerate scene containing line {fact['line_number']} with "
                        f"correct wound profile (allowed states only; '{fact['asserted']}' is forbidden)."
                    ),
                    line_number=fact["line_number"],
                ))

        return findings

    def _handle_stateful_transitions(
        self, entity: dict, manuscript: ManuscriptArtifact, ledger: dict,
    ) -> list[Finding]:
        """Stateful transition handler: validates prose state against the
        expected-state timeline derived from allowed_transitions (CLASS_A).

        Runs *after* the existing forbidden-state scan — both run, they catch
        different things.
        """
        eid = entity["id"]
        state_track = entity.get("state_track", {})
        transitions = state_track.get("allowed_transitions")
        initial_state = state_track.get("initial_state")

        if not transitions or initial_state is None:
            return []

        # Build timeline; bail with finding if chain is malformed
        timeline, chain_finding = _build_expected_state_timeline(
            eid, initial_state, transitions,
        )
        if chain_finding:
            return [chain_finding]
        if not timeline:
            return []

        # State vocabulary = {initial_state} ∪ {t.to for all transitions}
        state_vocab = list({initial_state} | {t_to for _, t_to in timeline})

        # Identify scenes that mention the entity
        canonical_name = entity.get("canonical_name", "")
        aliases = entity.get("aliases", [])
        search_terms = [canonical_name] + aliases
        # Also match on entity id (e.g. "coyle")
        entity_name_short = canonical_name.split("'")[0].strip()
        if entity_name_short and entity_name_short not in search_terms:
            search_terms.append(entity_name_short)

        scene_assertions: list[tuple[int, str, str]] = []
        sorted_scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)

        for scene in sorted_scenes:
            scene_lower = scene.text.lower()
            mentioned = any(
                term.lower() in scene_lower for term in search_terms if term
            )
            if not mentioned:
                continue

            # LLM extraction: what state does this scene assert?
            asserted = _extract_asserted_state_llm(
                canonical_name, state_vocab, scene.text,
            )
            # Grab a short excerpt for evidence
            excerpt_lines = scene.text.split("\n")[:5]
            excerpt = " ".join(l.strip() for l in excerpt_lines if l.strip())

            scene_assertions.append((scene.scene_number, asserted, excerpt))

        if not scene_assertions:
            return []

        return _detect_transition_violations(
            eid, initial_state, timeline, state_vocab, scene_assertions,
        )

    def _handle_lifecycle(self, entity: dict, text: str, lines: list[str],
                          coda_text: str, ledger: dict) -> list[Finding]:
        """Lifecycle handler: death-vs-canon (C-3) + role-binding (C-5)."""
        findings = []
        eid = entity["id"]
        canonical_name = entity.get("canonical_name", "")
        aliases = entity.get("aliases", [])

        # Death-vs-canon check
        lifecycle = entity.get("lifecycle", {})
        if lifecycle.get("alive_at_end_of_book"):
            # Regex pass for death language
            death_hits = _scan_deaths(canonical_name, aliases, lines)

            # LLM coda check (precision-biased, narrow scope)
            llm_death = _llm_coda_death_check(canonical_name, coda_text)

            if death_hits or llm_death:
                prov = _get_provenance(ledger, eid, "lifecycle")
                prov_str = _provenance_evidence(prov)
                evidence_lines = [h["context"] for h in death_hits[:3]]
                if llm_death:
                    evidence_lines.append("[LLM coda check: character described as dead in final section]")

                death_line = death_hits[0]["line_number"] if death_hits else None
                findings.append(Finding(
                    check_id=self.check_id,
                    severity=_SEVERITY_BY_FACT["death_assertion"],
                    scene_number=_resolve_scene_number(self._scene_boundaries, death_line),
                    description=(
                        f"Lifecycle violation: {eid} ('{canonical_name}') is declared "
                        f"alive_at_end_of_book but manuscript contains death assertion(s). {prov_str}"
                    ),
                    evidence=evidence_lines,
                    suggested_fix=(
                        f"Tier 3: {canonical_name} must survive per series canon "
                        f"({lifecycle.get('source', 'series_bible')}). Operator updates concept "
                        f"artifacts; never auto-regenerate."
                    ),
                    line_number=death_line,
                ))

        # Role-binding check
        role_bindings = entity.get("role_bindings", [])
        for rb in role_bindings:
            violations = _scan_role_violations(rb, lines)
            for v in violations:
                findings.append(Finding(
                    check_id=self.check_id,
                    severity=_SEVERITY_BY_FACT["role_violation"],
                    scene_number=_resolve_scene_number(self._scene_boundaries, v["line_number"]),
                    description=(
                        f"Role-binding violation: '{v['asserted']}' named in "
                        f"'{v['context_label']}' context — forbidden per entity ledger "
                        f"(entity: {eid}, required_form: {rb.get('required_form', 'role_only')}). "
                        f"Permitted roles: {rb.get('permitted_roles', [])}."
                    ),
                    evidence=[v["context"]],
                    suggested_fix=(
                        f"Tier 2: replace '{v['asserted']}' with role reference "
                        f"({', '.join(rb.get('permitted_roles', []))}) at line {v['line_number']}."
                    ),
                    line_number=v["line_number"],
                ))

        return findings
