"""
MA-010 character_gender_conformance — detects characters whose rendered gender in
the manuscript contradicts the canonical `gender` field in character_profiles.json.

Two violation classes:
  CLASS_A profile-drift   — a character's dominant rendered gender (pronouns /
                            gendered nouns) disagrees with their profile gender.
                            (Margaret Hale: profile female, rendered male.)
  CLASS_A pronoun-flip    — a single scene renders one character with conflicting
                            gendered pronouns (e.g. "her" for a male character at
                            ch92 Vera), regardless of profile.

Characters with no `gender` in their profile are skipped — presence of the field
is enforced separately by character_profile_auditor (_UNIVERSAL_REQUIRED).

LLM extraction phase (Sonnet) reads gendered references per character per scene;
comparison is deterministic.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


SONNET_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 5
MAX_RETRIES = 2

# Profile gender values normalized to {male, female, nonbinary}
_PROFILE_GENDER_MAP = {
    "male": "male", "m": "male", "man": "male",
    "female": "female", "f": "female", "woman": "female",
    "nonbinary": "nonbinary", "non-binary": "nonbinary", "nb": "nonbinary",
}

EXTRACTION_SYSTEM = """You are a gender-reference extractor for a novel manuscript. For each NAMED character who appears in the given scenes, report the gendered references used for them.

For each character in each scene, output one JSON object per line:
{"name": "Full Name", "scene_number": N, "male_refs": <int>, "female_refs": <int>, "evidence": "exact quote <=120 chars showing a gendered reference"}

Count as male_refs: he, him, his, himself, and male gendered nouns (man, men, father, brother, son, husband, sir, Mr.) used to refer to that character.
Count as female_refs: she, her, hers, herself, and female gendered nouns (woman, women, mother, sister, daughter, wife, ma'am, Mrs., Ms., Miss) used to refer to that character.

Only count references you are confident point to that specific named character. Do not guess. If a character appears but has no gendered reference in the scene, omit them.

Use the character's canonical full name (resolve titles: "Capitán Vera" -> "Vera" or full name if known from context).

Output one JSON object per line. Nothing else."""

EXTRACTION_PROMPT = """Extract gendered references per character from these scenes.

SCENES:
{scenes_block}

Output one JSON object per line."""


def _call_llm(system: str, user: str) -> str:
    pipeline_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from llm_client import call_llm
    resp = call_llm(provider="anthropic", model=SONNET_MODEL,
                    system=system, user=user, max_tokens=4096, temperature=0.0)
    return resp.text


def _normalize_name(name: str) -> str:
    name = re.sub(r'^(Capitán|Captain|Major|Colonel|General|Teniente|Comandante|Dr\.?|Mr\.?|Mrs\.?|Ms\.?|Miss)\s+',
                  '', name, flags=re.IGNORECASE)
    return name.strip().lower()


def _profile_gender_lookup(briefs: BriefBundle) -> dict[str, str]:
    """Map normalized name (full + each name-part) -> canonical gender."""
    out: dict[str, str] = {}
    profiles = briefs.character_profiles or {}
    # character_profiles is the raw per-character map (no 'characters' wrapper in
    # book-level files) OR a {'characters': [...]} envelope (series-level files).
    chars = []
    if isinstance(profiles, dict) and "characters" in profiles and isinstance(profiles["characters"], list):
        chars = profiles["characters"]
    elif isinstance(profiles, dict):
        # raw per-character map: key -> profile object
        for k, v in profiles.items():
            if isinstance(v, dict):
                v = dict(v)
                v.setdefault("name", k)
                chars.append(v)
    for c in chars:
        g_raw = (c.get("gender") or "").strip().lower()
        g = _PROFILE_GENDER_MAP.get(g_raw)
        if not g:
            continue
        name = c.get("name", "")
        if not name:
            continue
        out[_normalize_name(name)] = g
        for part in name.split():
            if len(part) > 1:
                out[part.lower()] = g
    return out


def _resolve_profile_gender(rendered_name: str, lookup: dict[str, str]) -> str | None:
    norm = _normalize_name(rendered_name)
    if norm in lookup:
        return lookup[norm]
    for part in norm.split():
        if part in lookup:
            return lookup[part]
    return None


def _extract(manuscript: ManuscriptArtifact) -> list[dict]:
    rows: list[dict] = []
    scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)
    for i in range(0, len(scenes), BATCH_SIZE):
        batch = scenes[i:i + BATCH_SIZE]
        block = "\n\n".join(f"--- SCENE {s.scene_number} ---\n{s.text[:3500]}" for s in batch)
        prompt = EXTRACTION_PROMPT.format(scenes_block=block)
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = _call_llm(EXTRACTION_SYSTEM, prompt)
                for line in resp.strip().splitlines():
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                break
            except Exception as e:
                if attempt < MAX_RETRIES:
                    time.sleep(5 * (attempt + 1))
                    continue
                print(f"    WARN: gender extraction failed scenes "
                      f"{batch[0].scene_number}-{batch[-1].scene_number}: {e}", file=sys.stderr)
    return rows


def _build_findings(rows: list[dict], lookup: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []

    # ── Pass A: per-scene pronoun-flip (conflicting genders, same char, one scene)
    for r in rows:
        m = int(r.get("male_refs", 0) or 0)
        f = int(r.get("female_refs", 0) or 0)
        if m >= 2 and f >= 2:  # both genders strongly present for one char in one scene
            findings.append(Finding(
                check_id="MA-010-character-gender-conformance",
                severity="CLASS_A",
                scene_number=int(r.get("scene_number", 0) or 0),
                description=(f"Character '{r.get('name','?')}' rendered with conflicting "
                             f"gendered pronouns in a single scene "
                             f"({m} male refs, {f} female refs)"),
                evidence=[f"Scene {r.get('scene_number')}: \"{r.get('evidence','')}\""],
                suggested_fix=(f"Regenerate scene; render '{r.get('name','?')}' with a single "
                               f"consistent gender matching the character profile."),
            ))

    # ── Pass B: dominant-gender vs profile drift (aggregate across scenes)
    agg: dict[str, dict] = {}
    for r in rows:
        key = _normalize_name(r.get("name", ""))
        if not key:
            continue
        a = agg.setdefault(key, {"name": r.get("name", ""), "m": 0, "f": 0,
                                 "scenes": set(), "ev": []})
        a["m"] += int(r.get("male_refs", 0) or 0)
        a["f"] += int(r.get("female_refs", 0) or 0)
        sc = int(r.get("scene_number", 0) or 0)
        a["scenes"].add(sc)
        if r.get("evidence"):
            a["ev"].append(f"Scene {sc}: \"{r.get('evidence')}\"")

    for key, a in sorted(agg.items()):
        prof_gender = _resolve_profile_gender(key, lookup)
        if prof_gender is None:
            continue  # not in profiles or profile has no gender — skip (auditor enforces presence)
        if a["m"] == 0 and a["f"] == 0:
            continue
        rendered = "male" if a["m"] > a["f"] else "female" if a["f"] > a["m"] else None
        if rendered is None:
            continue  # tie handled by Pass A pronoun-flip if within a scene
        # nonbinary profile: any strongly-gendered rendering is reportable
        if prof_gender == "nonbinary":
            if max(a["m"], a["f"]) >= 3:
                findings.append(Finding(
                    check_id="MA-010-character-gender-conformance",
                    severity="CLASS_A",
                    scene_number=None,
                    scene_numbers=sorted(a["scenes"]),
                    description=(f"'{a['name']}' profile gender is nonbinary but manuscript "
                                 f"renders gendered ({a['m']} male / {a['f']} female refs)"),
                    evidence=a["ev"][:3],
                    suggested_fix=(f"Reconcile: update profile gender or regenerate scenes to "
                                   f"render '{a['name']}' per profile."),
                ))
            continue
        if rendered != prof_gender:
            findings.append(Finding(
                check_id="MA-010-character-gender-conformance",
                severity="CLASS_A",
                scene_number=None,
                scene_numbers=sorted(a["scenes"]),
                description=(f"'{a['name']}' profile gender is {prof_gender} but manuscript "
                             f"renders {rendered} ({a['m']} male / {a['f']} female refs)"),
                evidence=a["ev"][:3],
                suggested_fix=(f"Regenerate affected scenes to render '{a['name']}' as "
                               f"{prof_gender}, matching the character profile."),
            ))
    return findings


class CharacterGenderConformance:
    check_id = "MA-010-character-gender-conformance"
    severity = "CLASS_A"
    description = ("Character gender conformance: rendered gender (pronouns/gendered "
                   "nouns) must match the canonical profile gender field.")

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        lookup = _profile_gender_lookup(briefs)
        print(f"    MA-010: {len(lookup)} profile gender entries", file=sys.stderr)
        if not lookup:
            print("    MA-010: no profile gender data — skipping", file=sys.stderr)
            return []
        rows = _extract(manuscript)
        print(f"    MA-010: {len(rows)} gender-reference rows extracted", file=sys.stderr)
        findings = _build_findings(rows, lookup)
        print(f"    MA-010: {len(findings)} findings", file=sys.stderr)
        return findings
