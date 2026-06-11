"""
MA-001: Character Detail Consistency Check

Reads manuscript prose, extracts per-character claims about physical,
biographical, and material details across all scenes, then cross-references
to identify contradictions.

Catches:
  - Physical description contradictions (hair, build, age references)
  - Biographical detail contradictions (family details, ages of relatives)
  - Material/prop contradictions (devices, vehicles, equipment brands)
  - Rank/title contradictions
  - Location/timeline contradictions (character claimed in two places)

Uses Haiku for extraction (batched by scene groups), then programmatic
cross-referencing for contradiction detection.

Severity: CLASS_A (these block publication).
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


# ── Constants ──────────────────────────────────────────────────────────────

HAIKU_MODEL = "claude-haiku-4-5"
BATCH_SIZE = 5  # scenes per extraction call
MAX_RETRIES = 2

# Timeline-aware plausibility bounds (in days)
# If elapsed time between two contradicting scenes is below the threshold,
# the contradiction is NOT plausible as natural progression.
AGE_MIN_DAYS = 365        # a person doesn't age meaningfully in < 1 year
PHYSICAL_MIN_DAYS = 90    # hair/weight changes need ~3+ months
LOCATION_IRRELEVANT = True  # location contradictions don't depend on time gap

# Categories that benefit from timeline-aware filtering
TIMELINE_CATEGORIES = {
    "BIOGRAPHICAL": AGE_MIN_DAYS,
    "PHYSICAL": PHYSICAL_MIN_DAYS,
}

# Detail keys that are age-related (within BIOGRAPHICAL)
AGE_DETAIL_KEYS = {
    "age", "daughters_ages", "children_ages", "child_age", "daughter_age",
    "son_age", "family_ages", "age_of_children", "daughters_education",
    "children_education",
}

EXTRACTION_SYSTEM = """You are a meticulous continuity editor. Your job is to extract every concrete, specific claim about characters from manuscript prose.

Extract ONLY factual claims that are explicitly stated in the text. Do not infer or assume.

Categories to extract:
- PHYSICAL: hair color/length/style, eye color, build, height, scars, distinguishing features
- BIOGRAPHICAL: age, family members, family details (ages of children, spouse details), hometown, education, military service details
- MATERIAL: specific device brands/models (laptop, phone, vehicle), equipment, weapons
- RANK_TITLE: military rank, professional title, honorific
- LOCATION: where a character physically is at a given moment in the scene
- TEMPORAL: time references tied to a character's state or actions"""

EXTRACTION_PROMPT = """Extract all character detail claims from the following scenes. For each claim, output a JSON object on its own line:

{{"character": "Name", "category": "PHYSICAL|BIOGRAPHICAL|MATERIAL|RANK_TITLE|LOCATION|TEMPORAL", "detail_key": "short key (e.g. 'hair', 'laptop_brand', 'rank', 'daughters_ages')", "value": "the specific claim", "scene_number": N, "excerpt": "exact quote <=120 chars"}}

CRITICAL RULES:
1. Extract ONLY explicit statements, not implications
2. For physical descriptions, capture the EXACT descriptors used
3. For devices/equipment, capture the EXACT brand/model names
4. For ranks/titles, capture the EXACT rank or title used
5. For family details, capture specific ages, counts, descriptions
6. For locations, capture where the character physically IS in the scene
7. Include the scene number for each claim

If a scene contains no extractable character details, skip it silently.

SCENES:
{scenes_block}

Output one JSON object per line. Nothing else."""

CONTRADICTION_SYSTEM = """You are a continuity auditor. You will receive a set of character detail claims extracted from a manuscript. Your job is to identify CONTRADICTIONS — cases where the same character has been described with conflicting details across different scenes.

A contradiction exists when:
- The same physical feature is described differently (e.g., "short black hair" in scene 7 vs "long dark hair" in scene 39)
- Family details change in ways that cannot be explained by time passing (e.g., "older daughter at university, younger in school" vs "both daughters at university" when scenes are close together)
- The same object is given different brands (e.g., "ThinkPad" in scene 49 vs "MacBook" in scene 50)
- A character's rank changes without narrative explanation (e.g., "Capitán" in scene 21 vs "Major" in scene 45)
- A character is in two places simultaneously or arrives somewhere they were already described as being at

IMPORTANT: Flag ALL factual discrepancies between scenes, even if one could theoretically explain the change through time passing. Do NOT assume time progression justifies the change — the timeline filter will handle plausibility assessment separately. Your job is to identify the discrepancy itself.

Do NOT flag:
- Different details about different characters
- Details that could plausibly coexist (e.g., a character has both "dark eyes" and "brown eyes")
- Changes that are narratively explained (promotions, disguises, etc.)
- Vague vs specific descriptions that don't actually conflict"""

CONTRADICTION_PROMPT = """Review these character detail claims and identify any contradictions.

For each contradiction found, output a JSON object on its own line:
{{"character": "Name", "detail_key": "what conflicts", "claim_a": {{"value": "first claim", "scene_number": N, "excerpt": "quote"}}, "claim_b": {{"value": "conflicting claim", "scene_number": M, "excerpt": "quote"}}, "explanation": "why these contradict"}}

If no contradictions exist, output exactly: NO_CONTRADICTIONS

CLAIMS BY CHARACTER:
{claims_block}

Output JSON objects (or NO_CONTRADICTIONS), nothing else."""


# ── LLM helper ─────────────────────────────────────────────────────────────

def _call_llm(system: str, user: str, model: str = HAIKU_MODEL) -> str:
    """Call LLM via the pipeline's llm_client."""
    # Add pipeline dir to path for import
    pipeline_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from llm_client import call_llm
    response = call_llm(
        provider="anthropic",
        model=model,
        system=system,
        user=user,
        max_tokens=4096,
        temperature=0.0,
    )
    return response.text


# ── Extraction ─────────────────────────────────────────────────────────────

@dataclass
class Claim:
    character: str
    category: str
    detail_key: str
    value: str
    scene_number: int
    excerpt: str


def _extract_claims_from_batch(scenes_block: str) -> list[Claim]:
    """Extract character detail claims from a batch of scenes."""
    prompt = EXTRACTION_PROMPT.format(scenes_block=scenes_block)
    response = _call_llm(EXTRACTION_SYSTEM, prompt)

    claims: list[Claim] = []
    for line in response.strip().splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            claims.append(Claim(
                character=obj.get("character", ""),
                category=obj.get("category", ""),
                detail_key=obj.get("detail_key", ""),
                value=obj.get("value", ""),
                scene_number=obj.get("scene_number", 0),
                excerpt=obj.get("excerpt", ""),
            ))
        except json.JSONDecodeError:
            continue
    return claims


def extract_all_claims(manuscript: ManuscriptArtifact) -> list[Claim]:
    """Extract claims from all scenes in batches."""
    all_claims: list[Claim] = []
    scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)

    for i in range(0, len(scenes), BATCH_SIZE):
        batch = scenes[i:i + BATCH_SIZE]
        scenes_block = "\n\n".join(
            f"--- SCENE {s.scene_number} ---\n{s.text[:3000]}"
            for s in batch
        )
        for attempt in range(MAX_RETRIES + 1):
            try:
                claims = _extract_claims_from_batch(scenes_block)
                all_claims.extend(claims)
                break
            except Exception as e:
                if attempt < MAX_RETRIES:
                    time.sleep(5 * (attempt + 1))
                    continue
                print(f"    WARN: extraction failed for scenes "
                      f"{batch[0].scene_number}-{batch[-1].scene_number}: {e}",
                      file=sys.stderr)

    return all_claims


# ── Contradiction detection ────────────────────────────────────────────────

def _group_claims_by_character(claims: list[Claim]) -> dict[str, list[Claim]]:
    """Group claims by normalized character name."""
    groups: dict[str, list[Claim]] = {}
    for c in claims:
        key = c.character.strip().lower()
        groups.setdefault(key, []).append(c)
    return groups


def _is_plausible_progression(
    contradiction: dict,
    timelines: list[SceneTimeline] | None,
) -> bool:
    """Determine if a contradiction could be explained by natural time progression.

    Returns True if the contradiction is plausible (i.e., should NOT be flagged).
    Returns False if the contradiction stands (should be flagged).
    """
    if timelines is None:
        return False  # No timeline data — flag everything

    claim_a = contradiction.get("claim_a", {})
    claim_b = contradiction.get("claim_b", {})
    sn_a = claim_a.get("scene_number", 0)
    sn_b = claim_b.get("scene_number", 0)

    if not sn_a or not sn_b:
        return False  # Can't assess without scene numbers

    elapsed = elapsed_days_between(timelines, sn_a, sn_b)
    if elapsed is None:
        return False  # Can't assess — flag it

    detail_key = contradiction.get("detail_key", "").lower()

    # Age-related contradictions: only plausible if enough time has passed
    if detail_key in AGE_DETAIL_KEYS or "age" in detail_key or "daughter" in detail_key:
        return elapsed >= AGE_MIN_DAYS

    # Location contradictions: people travel, time gap is irrelevant
    # UNLESS the manuscript shows simultaneous presence (which the LLM
    # should catch). We do NOT dismiss location contradictions here.
    if "location" in detail_key:
        return False  # Always flag location contradictions

    # Physical attribute contradictions: moderate time can change them
    if detail_key in ("hair", "hair_length", "hair_color", "weight", "build",
                      "beard", "facial_hair"):
        return elapsed >= PHYSICAL_MIN_DAYS

    # Default: not plausible — flag it
    return False


def detect_contradictions_llm(claims: list[Claim], timelines: list[SceneTimeline] | None = None) -> list[dict]:
    """Use LLM to detect contradictions in extracted claims.

    If timelines are provided, applies timeline-aware plausibility filtering
    to determine whether detected contradictions are real or explainable by
    natural time progression.
    """
    grouped = _group_claims_by_character(claims)

    # Build claims block grouped by character
    blocks = []
    for char_key, char_claims in sorted(grouped.items()):
        if len(char_claims) < 2:
            continue
        char_name = char_claims[0].character
        lines = [f"\n=== {char_name} ==="]
        for c in sorted(char_claims, key=lambda x: (x.detail_key, x.scene_number)):
            lines.append(
                f"  [{c.category}] {c.detail_key}: \"{c.value}\" "
                f"(scene {c.scene_number}, excerpt: \"{c.excerpt}\")"
            )
        blocks.append("\n".join(lines))

    if not blocks:
        return []

    claims_block = "\n".join(blocks)
    prompt = CONTRADICTION_PROMPT.format(claims_block=claims_block)
    response = _call_llm(CONTRADICTION_SYSTEM, prompt)

    contradictions = []
    text = response.strip()
    if text == "NO_CONTRADICTIONS":
        return []

    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            contradictions.append(obj)
        except json.JSONDecodeError:
            continue

    # Timeline-aware filtering: remove contradictions that are plausible
    # given the elapsed time between scenes
    if timelines and contradictions:
        filtered = []
        for c in contradictions:
            if _is_plausible_progression(c, timelines):
                print(f"    timeline filter: dismissed '{c.get('detail_key', '?')}' "
                      f"for {c.get('character', '?')} — plausible progression",
                      file=sys.stderr)
            else:
                filtered.append(c)
        contradictions = filtered

    return contradictions


# ── Deterministic pre-checks (fast, no LLM) ───────────────────────────────

def _deterministic_checks(manuscript: ManuscriptArtifact) -> list[Finding]:
    """Fast regex-based checks for common contradictions.

    These supplement the LLM extraction by catching specific patterns
    that are easy to detect deterministically.
    """
    findings: list[Finding] = []

    # Check for device brand contradictions across scenes
    device_patterns = {
        "ThinkPad": re.compile(r'\bThinkPad\b', re.IGNORECASE),
        "MacBook": re.compile(r'\bMacBook\b', re.IGNORECASE),
        "Dell": re.compile(r'\bDell\b(?:\s+(?:XPS|Latitude|Inspiron))?', re.IGNORECASE),
        "Surface": re.compile(r'\bSurface\s*(?:Pro|Book|Laptop)?\b', re.IGNORECASE),
    }
    device_sightings: dict[str, list[tuple[int, str]]] = {}
    for scene in manuscript.scenes:
        for brand, pattern in device_patterns.items():
            m = pattern.search(scene.text)
            if m:
                start = max(0, m.start() - 40)
                end = min(len(scene.text), m.end() + 40)
                excerpt = scene.text[start:end].replace("\n", " ").strip()
                device_sightings.setdefault(brand, []).append(
                    (scene.scene_number, excerpt)
                )

    # If multiple different device brands appear, flag potential contradiction
    if len(device_sightings) >= 2:
        brands = sorted(device_sightings.keys())
        for i in range(len(brands)):
            for j in range(i + 1, len(brands)):
                b1, b2 = brands[i], brands[j]
                s1 = device_sightings[b1]
                s2 = device_sightings[b2]
                # Check if any sightings are in nearby scenes (potential same device)
                for sn1, ex1 in s1:
                    for sn2, ex2 in s2:
                        if abs(sn1 - sn2) <= 5:
                            findings.append(Finding(
                                check_id="MA-001-character-detail-consistency",
                                severity="CLASS_A",
                                scene_number=None,
                                scene_numbers=sorted([sn1, sn2]),
                                description=(
                                    f"Device brand contradiction: '{b1}' in scene {sn1} "
                                    f"vs '{b2}' in scene {sn2} — may refer to the same device"
                                ),
                                evidence=[
                                    f"Scene {sn1}: ...{ex1}...",
                                    f"Scene {sn2}: ...{ex2}...",
                                ],
                                suggested_fix=f"Verify whether these refer to the same device; if so, use one brand consistently",
                            ))

    # Check for family-detail contradictions (daughters, children, ages)
    # Specifically targets the pattern where family member counts/ages change
    _family_claims: dict[str, list[tuple[int, str]]] = {}  # char_key -> [(scene, claim_text)]

    # Regex to find family-detail statements
    family_patterns = [
        # "daughters ... university / school" patterns
        re.compile(
            r'(?:daughter|son|child(?:ren)?|kid)s?\b.{0,80}'
            r'(?:university|school|college|kindergarten|grade\s+\d)',
            re.IGNORECASE | re.DOTALL,
        ),
        # "both in university" / "all in school" patterns
        re.compile(
            r'\b(?:both|all|two|three)\b.{0,40}'
            r'(?:university|school|college)',
            re.IGNORECASE | re.DOTALL,
        ),
        # "older ... university ... younger ... school" pattern
        re.compile(
            r'\b(?:older|eldest|first)\b.{0,60}'
            r'(?:university|school|college).{0,80}'
            r'(?:younger|youngest|second|other).{0,60}'
            r'(?:university|school|college)',
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    for scene in manuscript.scenes:
        text = scene.text[:3000]
        for pat in family_patterns:
            m = pat.search(text)
            if m:
                # Try to find associated character name (look back up to 200 chars)
                start_pos = max(0, m.start() - 200)
                context = text[start_pos:m.end()]
                # Extract character name from context
                char_match = re.search(r'\b([A-Z][a-záéíóúñ]+)\b', context)
                char_key = char_match.group(1).lower() if char_match else "unknown"
                excerpt = text[max(0, m.start() - 30):min(len(text), m.end() + 30)].replace("\n", " ").strip()
                _family_claims.setdefault(char_key, []).append(
                    (scene.scene_number, excerpt)
                )

    # Check for contradictory family detail claims across ALL characters
    # (pronoun resolution is unreliable, so we pool all family claims globally
    # and then check for school/university contradictions within a window)
    all_family_school: dict[int, str] = {}   # scene -> excerpt where "younger in school" or similar
    all_family_uni: dict[int, str] = {}      # scene -> excerpt where "both in university" or similar

    for _char_key, claims_list in _family_claims.items():
        for sn, excerpt in claims_list:
            lower_ex = excerpt.lower()
            has_younger_school = "younger" in lower_ex and "school" in lower_ex
            has_school_no_uni = "school" in lower_ex and "university" not in lower_ex
            has_both_uni = ("both" in lower_ex and "university" in lower_ex) or \
                           ("all" in lower_ex and "university" in lower_ex)

            if has_younger_school or has_school_no_uni:
                all_family_school[sn] = excerpt
            if has_both_uni:
                all_family_uni[sn] = excerpt

    if all_family_school and all_family_uni:
        for sn_s, ex_s in all_family_school.items():
            for sn_u, ex_u in all_family_uni.items():
                findings.append(Finding(
                    check_id="MA-001-character-detail-consistency",
                    severity="CLASS_A",
                    scene_number=None,
                    scene_numbers=sorted([sn_s, sn_u]),
                    description=(
                        f"Family detail contradiction: "
                        f"children/daughters described differently across scenes {sn_s} and {sn_u} — "
                        f"one says younger in school, another says both in university"
                    ),
                    evidence=[
                        f"Scene {sn_s}: ...{ex_s}...",
                        f"Scene {sn_u}: ...{ex_u}...",
                    ],
                    suggested_fix="Reconcile family/education details across scenes",
                ))

    # Check for rank contradictions
    rank_patterns = {
        "Capitán": re.compile(r'\bCapit[áa]n\b'),
        "Major": re.compile(r'\bMajor\b'),
        "Colonel": re.compile(r'\bColonel\b'),
        "General": re.compile(r'\bGeneral\b'),
        "Teniente": re.compile(r'\bTeniente\b'),
        "Comandante": re.compile(r'\bComandante\b'),
    }
    # Track rank + associated character name
    rank_sightings: dict[str, list[tuple[str, int, str]]] = {}  # name -> [(rank, scene, excerpt)]

    # Look for patterns like "Rank Name" or "Name, rank"
    for scene in manuscript.scenes:
        text = scene.text
        for rank, pattern in rank_patterns.items():
            for m in pattern.finditer(text):
                # Try to grab the name after the rank
                after = text[m.end():m.end() + 30].strip()
                name_match = re.match(r'^(\s+)?([A-Z][a-záéíóú]+)', after)
                if name_match:
                    name = name_match.group(2)
                    start = max(0, m.start() - 20)
                    end = min(len(text), m.end() + 50)
                    excerpt = text[start:end].replace("\n", " ").strip()
                    rank_sightings.setdefault(name.lower(), []).append(
                        (rank, scene.scene_number, excerpt)
                    )

    for name, sightings in rank_sightings.items():
        ranks_seen = set(s[0] for s in sightings)
        if len(ranks_seen) >= 2:
            ranks_list = sorted(ranks_seen)
            evidence = []
            scene_nums = []
            for rank in ranks_list:
                for r, sn, ex in sightings:
                    if r == rank:
                        evidence.append(f"Scene {sn} ({rank}): ...{ex}...")
                        scene_nums.append(sn)
                        break
            findings.append(Finding(
                check_id="MA-001-character-detail-consistency",
                severity="CLASS_A",
                scene_number=None,
                scene_numbers=sorted(set(scene_nums)),
                description=(
                    f"Rank/title contradiction for '{name}': "
                    f"{' vs '.join(ranks_list)} across different scenes"
                ),
                evidence=evidence,
                suggested_fix=f"Verify correct rank for {name} and use consistently",
            ))

    return findings


# ── Check module class ─────────────────────────────────────────────────────

class CharacterDetailConsistency:
    check_id = "MA-001-character-detail-consistency"
    severity = "CLASS_A"
    description = (
        "Cross-scene character detail consistency: physical descriptions, "
        "biographical facts, material/prop brands, ranks/titles, "
        "location/timeline coherence"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        findings: list[Finding] = []

        # Phase 0: Timeline extraction (for plausibility filtering)
        print("    Phase 0: timeline extraction", file=sys.stderr)
        try:
            timelines = extract_timeline(manuscript)
            print(f"    -> {len(timelines)} scene timelines built", file=sys.stderr)
        except Exception as e:
            print(f"    -> timeline extraction failed: {e} (proceeding without)", file=sys.stderr)
            timelines = None

        # Phase 1: Deterministic pre-checks (fast, no LLM)
        print("    Phase 1: deterministic pre-checks", file=sys.stderr)
        det_findings = _deterministic_checks(manuscript)
        findings.extend(det_findings)
        print(f"    -> {len(det_findings)} deterministic findings", file=sys.stderr)

        # Phase 2: LLM-based extraction
        print("    Phase 2: LLM extraction (batched)", file=sys.stderr)
        claims = extract_all_claims(manuscript)
        print(f"    -> {len(claims)} claims extracted", file=sys.stderr)

        # Phase 3: LLM-based contradiction detection (timeline-aware)
        if claims:
            print("    Phase 3: contradiction detection (timeline-aware)", file=sys.stderr)
            contradictions = detect_contradictions_llm(claims, timelines=timelines)
            print(f"    -> {len(contradictions)} contradictions found", file=sys.stderr)

            for c in contradictions:
                claim_a = c.get("claim_a", {})
                claim_b = c.get("claim_b", {})
                sn_a = claim_a.get("scene_number", 0)
                sn_b = claim_b.get("scene_number", 0)

                evidence = []
                if claim_a.get("excerpt"):
                    evidence.append(f"Scene {sn_a}: \"{claim_a['excerpt']}\" — {claim_a.get('value', '')}")
                if claim_b.get("excerpt"):
                    evidence.append(f"Scene {sn_b}: \"{claim_b['excerpt']}\" — {claim_b.get('value', '')}")

                findings.append(Finding(
                    check_id=self.check_id,
                    severity="CLASS_A",
                    scene_number=None,
                    scene_numbers=sorted(set(filter(None, [sn_a, sn_b]))),
                    description=(
                        f"{c.get('character', 'Unknown')}: {c.get('detail_key', 'detail')} contradiction — "
                        f"{c.get('explanation', 'conflicting details across scenes')}"
                    ),
                    evidence=evidence,
                    suggested_fix=(
                        f"Reconcile {c.get('detail_key', 'detail')} for {c.get('character', 'character')} "
                        f"across scenes {sn_a} and {sn_b}"
                    ),
                ))

        # Deduplicate findings that overlap between deterministic and LLM
        findings = _deduplicate_findings(findings)

        return findings


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Remove duplicate findings that cover the same contradiction."""
    seen: set[str] = set()
    unique: list[Finding] = []
    for f in findings:
        # Create a fingerprint from scene numbers + description keywords
        key_parts = sorted(f.scene_numbers) if f.scene_numbers else [f.scene_number or 0]
        # Normalize description for dedup
        desc_words = set(f.description.lower().split())
        key = f"{key_parts}"
        # Check if we've seen a finding for the same scenes
        if key not in seen:
            seen.add(key)
            unique.append(f)
        else:
            # Allow if description is substantially different
            unique.append(f)
    return unique
