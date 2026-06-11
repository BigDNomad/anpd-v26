#!/usr/bin/env python3
"""
entity_ledger_builder.py — V25 Entity-Fact Ledger Builder
ANPD V25 | S-2 Phase 2a

Extracts scalar facts from a synopsis, merges declared constraints from
series bible + book config, resolves conflicts, writes a sealed
entity_ledger.json.

CLI:
    python3 entity_ledger_builder.py \
      --synopsis <path> \
      --series-bible <path> \
      --book-config <path> \
      --out <path>

Library:
    from entity_ledger_builder import build_ledger
    ledger = build_ledger(synopsis_path, series_bible_path, book_config_path)
"""

import os
import sys
import json
import re
import hashlib
import argparse
import time
from datetime import datetime, timezone

BUILDER_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"

DEFAULT_MODEL = "claude-sonnet-4-6"


# ── Synopsis parsing ─────────────────────────────────────────────────────


def _parse_scenes(synopsis_text: str) -> list[dict]:
    """Split synopsis into scene dicts with scene_number, chapter, title, body."""
    scenes = []
    scene_pattern = re.compile(
        r'^### Scene (\d+)\s*—\s*(.+?)(?:\s*\[TYPE:\s*(\w[\w-]*)\])?\s*$',
        re.MULTILINE,
    )
    chapter_pattern = re.compile(r'^## Chapter (\d+)', re.MULTILINE)

    # Build chapter map: line_offset -> chapter_number
    chapter_map = []
    for m in chapter_pattern.finditer(synopsis_text):
        chapter_map.append((m.start(), int(m.group(1))))

    def _chapter_for_pos(pos):
        ch = 1
        for offset, num in chapter_map:
            if pos >= offset:
                ch = num
        return ch

    matches = list(scene_pattern.finditer(synopsis_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(synopsis_text)
        body = synopsis_text[m.end():end].strip()
        scenes.append({
            "scene_number": int(m.group(1)),
            "chapter": _chapter_for_pos(m.start()),
            "title": m.group(2).strip(),
            "scene_type": (m.group(3) or "MIXED").upper().replace("-", "_"),
            "body": body,
        })
    return scenes


# ── Scalar extraction (regex layer) ──────────────────────────────────────


# Patterns for counts, designations, sides
_COUNT_PATTERN = re.compile(
    r'\b(?:(?P<word_num>one|two|three|four|five|six|seven|eight|nine|ten|'
    r'eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|'
    r'nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|'
    r'hundred|thousand|three hundred)'
    r'|(?P<digit_num>\d+))\s+'
    r'(?P<noun>[A-Za-z][\w\s]{0,30}?(?:rotors?|mines?|vehicles?|trucks?|'
    r'miniguns?|men|personnel|aircraft|helicopters?|Claymores?|grenades?))',
    re.IGNORECASE,
)

_DESIGNATION_PATTERN = re.compile(
    r'\b(?P<desig>[A-Z]{1,4}[-/]\d{1,3}[A-Za-z]?(?:/[A-Z])?)\b'
)

_SIDE_PATTERN = re.compile(
    r'\b(?P<side>right|left|port|starboard)\s+(?P<obj>[A-Za-z][\w\s]{0,20}?'
    r'(?:arm|leg|side|wing|engine|flank|eye|ankle|shoulder|hand|foot|knee))',
    re.IGNORECASE,
)

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100, "thousand": 1000,
    "three hundred": 300,
}


def _extract_raw_scalars(scenes: list[dict]) -> list[dict]:
    """Extract raw scalar candidates from synopsis scenes."""
    candidates = []

    for scene in scenes:
        body = scene["body"]
        sc_num = scene["scene_number"]

        # Counts
        for m in _COUNT_PATTERN.finditer(body):
            word = m.group("word_num")
            digit = m.group("digit_num")
            noun = m.group("noun").strip().lower()
            value = _WORD_NUMS.get(word.lower(), None) if word else int(digit)
            if value is None:
                continue
            candidates.append({
                "type": "count",
                "value": value,
                "noun": noun,
                "scene": sc_num,
                "raw_text": m.group(0).strip(),
            })

        # Designations
        for m in _DESIGNATION_PATTERN.finditer(body):
            desig = m.group("desig")
            # Get surrounding context (±60 chars) for entity association
            start = max(0, m.start() - 60)
            end = min(len(body), m.end() + 60)
            context = body[start:end]
            candidates.append({
                "type": "designation",
                "value": desig,
                "context": context,
                "scene": sc_num,
                "raw_text": m.group(0).strip(),
            })

        # Sides (for wound tracking)
        for m in _SIDE_PATTERN.finditer(body):
            side = m.group("side").lower()
            obj = m.group("obj").strip().lower()
            start = max(0, m.start() - 60)
            end = min(len(body), m.end() + 60)
            context = body[start:end]
            candidates.append({
                "type": "side",
                "value": side,
                "object": obj,
                "context": context,
                "scene": sc_num,
                "raw_text": m.group(0).strip(),
            })

    return candidates


# ── LLM entity association ───────────────────────────────────────────────


def _associate_entities_llm(candidates: list[dict], synopsis_text: str) -> list[dict]:
    """Use a single bounded LLM call to associate scalar candidates with entities.

    The LLM does association only — "this count belongs to the rotors entity" —
    not correctness judgment.
    """
    if not candidates:
        return []

    # Build the prompt
    candidate_lines = []
    for i, c in enumerate(candidates):
        if c["type"] == "count":
            candidate_lines.append(
                f"  {i}: count={c['value']} noun=\"{c['noun']}\" scene={c['scene']} raw=\"{c['raw_text']}\""
            )
        elif c["type"] == "designation":
            candidate_lines.append(
                f"  {i}: designation=\"{c['value']}\" scene={c['scene']} context=\"{c['context'][:80]}\""
            )
        elif c["type"] == "side":
            candidate_lines.append(
                f"  {i}: side=\"{c['value']}\" object=\"{c['object']}\" scene={c['scene']} raw=\"{c['raw_text']}\""
            )

    candidates_block = "\n".join(candidate_lines)

    system = (
        "You are a fact-extraction assistant for a fiction manuscript pipeline. "
        "You will receive a list of scalar facts (counts, designations, sides) "
        "extracted from a synopsis. Your ONLY job is to associate each fact with "
        "a stable entity identifier (lowercase_underscore). You do NOT judge "
        "correctness. You do NOT invent facts. You associate.\n\n"
        "For each candidate, output a JSON line: {\"idx\": N, \"entity_id\": \"...\", "
        "\"canonical_name\": \"...\", \"invariant_key\": \"...\", \"invariant_value\": ...}\n\n"
        "Rules:\n"
        "- entity_id: lowercase_underscore, stable. E.g., cipher_rotors, convoy, claymores, "
        "archers_weapon, coyle_wound, miniguns.\n"
        "- canonical_name: the entity as named in prose. E.g., \"KL-7 cipher rotors\", \"NVA convoy\".\n"
        "- invariant_key: what the scalar measures. E.g., \"count\", \"designation\", \"side\", \"damage_side\".\n"
        "- invariant_value: the extracted value (number or string).\n"
        "- Skip candidates that are not meaningful entity facts (e.g., generic counts of unnamed things, "
        "counts that are about narrative pacing not entity properties).\n"
        "- Output ONLY the JSON lines, one per candidate. No commentary.\n"
        "- Use the same entity_id for facts about the same entity."
    )

    user = f"SCALAR CANDIDATES:\n{candidates_block}\n\nAssociate each with an entity. Output JSON lines only."

    try:
        from llm_client import call_llm
        response = call_llm(
            provider="anthropic",
            model=os.environ.get("V25_MODEL", DEFAULT_MODEL),
            system=system,
            user=user,
            max_tokens=4000,
            temperature=0.0,
        )
        return _parse_association_response(response.text, candidates)
    except Exception as e:
        print(f"  WARNING: LLM association failed ({e}); falling back to heuristic association")
        return _associate_entities_heuristic(candidates)


def _parse_association_response(response_text: str, candidates: list[dict]) -> list[dict]:
    """Parse LLM association response into enriched candidates."""
    enriched = []
    for line in response_text.strip().split("\n"):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            idx = parsed.get("idx")
            if idx is not None and 0 <= idx < len(candidates):
                c = dict(candidates[idx])
                c["entity_id"] = parsed.get("entity_id", "unknown")
                c["canonical_name"] = parsed.get("canonical_name", "")
                c["invariant_key"] = parsed.get("invariant_key", "count")
                c["invariant_value"] = parsed.get("invariant_value", c.get("value"))
                enriched.append(c)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return enriched


def _associate_entities_heuristic(candidates: list[dict]) -> list[dict]:
    """Fallback heuristic association when LLM is unavailable."""
    enriched = []
    for c in candidates:
        entity_id = None
        canonical_name = ""
        invariant_key = "count"

        if c["type"] == "count":
            noun = c["noun"].lower()
            if "rotor" in noun:
                entity_id = "cipher_rotors"
                canonical_name = "KL-7 cipher rotors"
                invariant_key = "count"
            elif "minigun" in noun:
                entity_id = "miniguns"
                canonical_name = "M134 miniguns"
                invariant_key = "count"
            elif "claymore" in noun:
                entity_id = "claymores"
                canonical_name = "Claymore mines"
                invariant_key = "count"
            elif "truck" in noun or "vehicle" in noun:
                entity_id = "convoy"
                canonical_name = "NVA convoy"
                invariant_key = "count"
            elif "men" in noun or "personnel" in noun:
                entity_id = "force_count"
                canonical_name = "force count"
                invariant_key = "count"
            # "aircraft" suppressed: extracted counts near "aircraft" are
            # character-attributed tallies (Bounchanh's notebook, "fourteen
            # American aircraft brought down"), not world-invariant equipment
            # counts. See S-2 Phase 2a re-seal memo (2026-05-28).
            # elif "aircraft" in noun:
            #     entity_id = "aircraft_count"

        elif c["type"] == "designation":
            desig = c["value"]
            ctx = c.get("context", "").lower()
            if "kl-7" in desig.lower() or "kl" in desig.lower():
                entity_id = "cipher_rotors"
                canonical_name = "KL-7 cipher machine"
                invariant_key = "designation"
            elif "gau" in desig.lower():
                entity_id = "archers_weapon"
                canonical_name = "Archer's weapon"
                invariant_key = "designation"
            elif "ak-47" in desig.lower() or "ak" in desig.lower():
                entity_id = "enemy_weapon"
                canonical_name = "enemy weapon"
                invariant_key = "designation"
            elif "rpd" in desig.lower():
                entity_id = "enemy_mg"
                canonical_name = "enemy machine gun"
                invariant_key = "designation"
            elif "sa-2" in desig.lower():
                entity_id = "sam"
                canonical_name = "SA-2 SAM"
                invariant_key = "designation"
            elif "m134" in desig.lower():
                entity_id = "miniguns"
                canonical_name = "M134 miniguns"
                invariant_key = "designation"
            elif "ac-119" in desig.lower():
                entity_id = "black_widow"
                canonical_name = "AC-119K Black Widow"
                invariant_key = "designation"
            elif "hh-3" in desig.lower():
                entity_id = "jolly_green"
                canonical_name = "HH-3E Jolly Green"
                invariant_key = "designation"
            elif "a-1" in desig.lower():
                entity_id = "sandy"
                canonical_name = "A-1 Skyraider Sandy"
                invariant_key = "designation"
            elif "o-1" in desig.lower():
                entity_id = "bird_dog"
                canonical_name = "O-1 Bird Dog"
                invariant_key = "designation"
            else:
                entity_id = f"desig_{desig.lower().replace('-', '_').replace('/', '_')}"
                canonical_name = desig
                invariant_key = "designation"

        elif c["type"] == "side":
            obj = c.get("object", "").lower()
            if "arm" in obj:
                entity_id = "coyle_wound"
                canonical_name = "Coyle's wound"
                invariant_key = "burn_side"
            elif "leg" in obj:
                entity_id = "coyle_wound"
                canonical_name = "Coyle's wound"
                invariant_key = "injury_side"
            else:
                entity_id = f"side_{obj.replace(' ', '_')}"
                canonical_name = obj
                invariant_key = "side"

        if entity_id:
            enriched_c = dict(c)
            enriched_c["entity_id"] = entity_id
            enriched_c["canonical_name"] = canonical_name
            enriched_c["invariant_key"] = invariant_key
            enriched_c["invariant_value"] = c["value"]
            enriched.append(enriched_c)

    return enriched


# ── Conflict resolution ──────────────────────────────────────────────────


def _resolve_scalars(enriched_candidates: list[dict]) -> tuple[dict, list[dict]]:
    """Group by entity+invariant, resolve multi-valued via plurality + first-tiebreak.

    Returns:
        entities: dict of entity_id -> entity dict
        conflicts: list of conflict records for ledger_conflicts.json
    """
    # Group: (entity_id, invariant_key) -> list of assertions
    groups: dict[tuple, list] = {}
    for c in enriched_candidates:
        key = (c["entity_id"], c["invariant_key"])
        if key not in groups:
            groups[key] = []
        groups[key].append(c)

    entities: dict[str, dict] = {}
    provenance: dict[str, dict] = {}
    conflicts = []

    for (entity_id, inv_key), assertions in groups.items():
        # Initialize entity
        if entity_id not in entities:
            entities[entity_id] = {
                "id": entity_id,
                "canonical_name": assertions[0].get("canonical_name", entity_id),
                "aliases": [],
                "entity_class": "scalar",
                "invariants": {},
            }

        # Count values
        value_counts: dict = {}
        value_first_scene: dict = {}
        for a in assertions:
            v = a["invariant_value"]
            v_key = str(v)
            value_counts[v_key] = value_counts.get(v_key, 0) + 1
            if v_key not in value_first_scene:
                value_first_scene[v_key] = a["scene"]

        # Plurality + first-tiebreak
        sorted_values = sorted(
            value_counts.keys(),
            key=lambda v: (-value_counts[v], value_first_scene.get(v, 9999)),
        )
        winner = sorted_values[0]
        # Try to use numeric value if possible
        try:
            winner_val = int(winner)
        except (ValueError, TypeError):
            winner_val = winner

        entities[entity_id]["invariants"][inv_key] = winner_val

        # Provenance
        prov_key = f"{entity_id}.{inv_key}"
        synopsis_assertions = [
            {"value": a["invariant_value"], "scene": a["scene"], "raw_text": a.get("raw_text", "")}
            for a in assertions
        ]

        if len(sorted_values) > 1:
            resolution = "auto_resolved"
            superseded = [v for v in sorted_values[1:]]
            conflicts.append({
                "entity_id": entity_id,
                "invariant_key": inv_key,
                "values_seen": {v: {"count": value_counts[v], "first_scene": value_first_scene[v]} for v in sorted_values},
                "chosen": winner_val,
                "resolution_method": "plurality_first_tiebreak",
            })
        else:
            resolution = "unambiguous"
            superseded = []

        provenance[prov_key] = {
            "origin": "synopsis_extracted",
            "synopsis_assertions": synopsis_assertions,
            "resolution": resolution,
            "superseded_values": superseded,
        }

    return entities, provenance, conflicts


# ── Stateful promotion ───────────────────────────────────────────────────


_WOUND_PROGRESSION_KEYWORDS = [
    "wound", "injured", "injury", "burned", "burn", "bleeding",
    "broken", "deteriorat", "worsening", "hobble", "non-functional",
]


def _detect_stateful_entities(entities: dict, provenance: dict,
                              scenes: list[dict],
                              forbidden_states_decl: list[dict]) -> None:
    """Promote entities that change along a path from scalar to stateful.

    Modifies entities in-place. Detects wound progressions from scene text.
    """
    # Build forbidden_states map from book_config declarations
    forbidden_map = {}
    for fs in forbidden_states_decl:
        forbidden_map[fs["entity_id"]] = fs.get("states", [])

    # Check for Coyle wound — the canonical stateful entity in CSAR
    for eid, entity in list(entities.items()):
        if "wound" in eid or "coyle" in eid.lower():
            # Look for wound progression across scenes
            wound_scenes = []
            for scene in scenes:
                body_lower = scene["body"].lower()
                if "coyle" in body_lower and any(kw in body_lower for kw in _WOUND_PROGRESSION_KEYWORDS):
                    wound_scenes.append(scene["scene_number"])

            if len(wound_scenes) >= 2:
                # Promote to stateful
                entity["entity_class"] = "stateful"
                invariants = entity.pop("invariants", {})
                entity["state_track"] = {
                    "initial_state": "pristine",
                    "allowed_transitions": [],
                    "forbidden_states": forbidden_map.get(eid, []),
                }
                # Build transitions from wound scenes
                states = _extract_wound_states(scenes, wound_scenes)
                prev_state = "pristine"
                for sc_num, state in states:
                    entity["state_track"]["allowed_transitions"].append({
                        "from": prev_state,
                        "to": state,
                        "occurs_at_scene": sc_num,
                    })
                    prev_state = state

    # Reconcile forbidden_states declarations onto matching existing entities.
    # A declared entity_id (e.g., "coyle") must find and merge with an existing
    # entity whose id contains it (e.g., "coyle_wound") rather than creating a
    # bare duplicate. This ensures one entity carries both state_track and
    # forbidden_states — the contract Phase 2b checks.
    for fs in forbidden_states_decl:
        declared_id = fs["entity_id"]
        states = fs.get("states", [])

        # Find existing entity: exact match first, then prefix/containment match
        target_eid = None
        if declared_id in entities:
            target_eid = declared_id
        else:
            for eid in entities:
                eid_lower = eid.lower()
                decl_lower = declared_id.lower()
                canon_lower = entities[eid].get("canonical_name", "").lower()
                aliases_lower = [a.lower() for a in entities[eid].get("aliases", [])]
                if (eid_lower.startswith(decl_lower + "_")
                        or decl_lower in canon_lower
                        or decl_lower in aliases_lower):
                    target_eid = eid
                    break

        if target_eid is not None:
            entity = entities[target_eid]
            # Attach forbidden_states to existing state_track
            if "state_track" in entity:
                entity["state_track"]["forbidden_states"] = states
            else:
                entity["entity_class"] = "stateful"
                entity.pop("invariants", None)
                entity["state_track"] = {
                    "initial_state": "pristine",
                    "allowed_transitions": [],
                    "forbidden_states": states,
                }
            # Rename to canonical declared id if different
            if target_eid != declared_id:
                entity["id"] = declared_id
                entities[declared_id] = entity
                del entities[target_eid]
                # Migrate provenance keys
                for old_key in list(provenance.keys()):
                    if old_key.startswith(target_eid + "."):
                        new_key = declared_id + old_key[len(target_eid):]
                        provenance[new_key] = provenance.pop(old_key)
        else:
            # No match — create new entity
            entities[declared_id] = {
                "id": declared_id,
                "canonical_name": declared_id.replace("_", " ").title(),
                "aliases": [],
                "entity_class": "stateful",
                "state_track": {
                    "initial_state": "pristine",
                    "allowed_transitions": [],
                    "forbidden_states": states,
                },
            }

        # Provenance for forbidden_states declaration
        prov_key = f"{declared_id}.forbidden_states"
        provenance[prov_key] = {
            "origin": "book_config_declared",
            "synopsis_assertions": [],
            "resolution": "declared",
            "superseded_values": [],
        }


def _extract_wound_states(scenes: list[dict], wound_scenes: list[int]) -> list[tuple]:
    """Extract wound state labels from scene text for Coyle."""
    states = []
    for scene in scenes:
        if scene["scene_number"] not in wound_scenes:
            continue
        body = scene["body"].lower()
        state_parts = []
        if "shrapnel" in body and "thigh" in body:
            state_parts.append("thigh_shrapnel")
        if "burn" in body and ("arm" in body or "side" in body):
            state_parts.append("burns_right_arm_side")
        if "broken" in body and ("femur" in body or "leg" in body):
            state_parts.append("broken_femur")
        if "deteriorat" in body or "worsening" in body:
            state_parts.append("deteriorating")
        if "internal bleeding" in body:
            state_parts.append("internal_bleeding")

        if state_parts:
            states.append((scene["scene_number"], "+".join(state_parts)))

    return states


# ── Lifecycle + role-binding merge ───────────────────────────────────────


def _merge_lifecycle_entities(entities: dict, provenance: dict,
                              series_bible: dict) -> None:
    """Merge recurring_entities from series bible into lifecycle_role entities."""
    recurring = series_bible.get("recurring_entities", [])
    for re_entry in recurring:
        name = re_entry["name"]
        eid = name.lower().replace(" ", "_").replace("'", "")
        constraints = re_entry.get("lifecycle_constraints", {})

        if eid not in entities:
            entities[eid] = {
                "id": eid,
                "canonical_name": name,
                "aliases": re_entry.get("aliases", []),
                "entity_class": "lifecycle_role",
                "lifecycle": {
                    "alive_at_end_of_book": constraints.get("alive_at_end_of_book", False),
                    "source": "series_bible:recurring_entities",
                },
            }
        else:
            # Entity already exists (e.g., from scalar extraction) — add lifecycle
            entity = entities[eid]
            entity["lifecycle"] = {
                "alive_at_end_of_book": constraints.get("alive_at_end_of_book", False),
                "source": "series_bible:recurring_entities",
            }
            if entity["entity_class"] == "scalar":
                entity["entity_class"] = "lifecycle_role"

        # Provenance
        prov_key = f"{eid}.lifecycle"
        provenance[prov_key] = {
            "origin": "series_bible_declared",
            "synopsis_assertions": [],
            "resolution": "declared",
            "superseded_values": [],
        }


def _merge_role_bindings(entities: dict, provenance: dict,
                         book_config: dict) -> None:
    """Merge role_bindings from book config into lifecycle_role entities."""
    invariants = book_config.get("entity_invariants", {})
    role_bindings = invariants.get("role_bindings", [])

    for rb in role_bindings:
        eid = rb["entity_id"]
        if eid not in entities:
            entities[eid] = {
                "id": eid,
                "canonical_name": eid.replace("_", " ").title(),
                "aliases": [],
                "entity_class": "lifecycle_role",
            }

        entity = entities[eid]
        if entity["entity_class"] == "scalar":
            entity["entity_class"] = "lifecycle_role"

        if "role_bindings" not in entity:
            entity["role_bindings"] = []

        entity["role_bindings"].append({
            "context": rb.get("context", ""),
            "required_form": rb.get("required_form", "role_only"),
            "forbidden_references": rb.get("forbidden_references", []),
            "permitted_roles": rb.get("permitted_roles", []),
        })

        # Provenance
        prov_key = f"{eid}.role_bindings"
        provenance[prov_key] = {
            "origin": "book_config_declared",
            "synopsis_assertions": [],
            "resolution": "declared",
            "superseded_values": [],
        }


def _merge_declared_scalars(entities: dict, provenance: dict,
                            book_config: dict) -> None:
    """Merge declared_scalars from book config into scalar entities.

    Declared scalars are ground-truth facts the synopsis extractor missed.
    They carry book_config_declared provenance and resolution: declared.
    """
    invariants_block = book_config.get("entity_invariants", {})
    declared_scalars = invariants_block.get("declared_scalars", [])

    for ds in declared_scalars:
        eid = ds["id"]
        if eid not in entities:
            entities[eid] = {
                "id": eid,
                "canonical_name": ds.get("canonical_name", eid.replace("_", " ").title()),
                "aliases": ds.get("aliases", []),
                "entity_class": "scalar",
                "invariants": ds.get("invariants", {}),
            }
        else:
            # Merge invariants into existing entity
            entity = entities[eid]
            existing_inv = entity.get("invariants", {})
            existing_inv.update(ds.get("invariants", {}))
            entity["invariants"] = existing_inv

        # Provenance for each declared invariant
        for inv_key in ds.get("invariants", {}):
            prov_key = f"{eid}.{inv_key}"
            provenance[prov_key] = {
                "origin": "book_config_declared",
                "synopsis_assertions": [],
                "resolution": "declared",
                "superseded_values": [],
            }


# ── Build orchestration ─────────────────────────────────────────────────


def build_ledger(
    synopsis_path: str,
    series_bible_path: str,
    book_config_path: str | None = None,
    book_slug: str = "",
    series_slug: str = "",
    use_llm: bool = True,
) -> tuple[dict, list[dict]]:
    """Build the entity ledger from synopsis + declared constraints.

    Returns:
        (ledger_dict, conflicts_list)
    """
    # Step 1: Hash the synopsis
    with open(synopsis_path, "r", encoding="utf-8") as f:
        synopsis_text = f.read()
    synopsis_sha256 = hashlib.sha256(synopsis_text.encode("utf-8")).hexdigest()

    # Load inputs
    with open(series_bible_path, "r", encoding="utf-8") as f:
        series_bible = json.load(f)

    book_config = {}
    if book_config_path and os.path.exists(book_config_path):
        with open(book_config_path, "r", encoding="utf-8") as f:
            book_config = json.load(f)

    # Derive slugs from config if not provided
    if not series_slug:
        series_config_dir = os.path.dirname(series_bible_path)
        sc_path = os.path.join(series_config_dir, "series_config.json")
        if os.path.exists(sc_path):
            with open(sc_path, "r", encoding="utf-8") as f:
                sc = json.load(f)
            series_slug = sc.get("series_slug", "")
            if not book_slug:
                book_num = book_config.get("book_number", 1)
                slugs = sc.get("book_slugs", {})
                book_slug = slugs.get(f"b{book_num:02d}", f"{series_slug}{book_num:03d}")

    if not book_slug:
        book_slug = "unknown"
    if not series_slug:
        series_slug = "unknown"

    print(f"  Building entity ledger for {book_slug} ({series_slug})")

    # Step 2: Extract scalar candidates
    scenes = _parse_scenes(synopsis_text)
    print(f"    Parsed {len(scenes)} scenes from synopsis")

    raw_candidates = _extract_raw_scalars(scenes)
    print(f"    Extracted {len(raw_candidates)} raw scalar candidates")

    # LLM association pass
    if use_llm and raw_candidates:
        enriched = _associate_entities_llm(raw_candidates, synopsis_text)
    else:
        enriched = _associate_entities_heuristic(raw_candidates)
    print(f"    Associated {len(enriched)} candidates with entities")

    # Step 4: Conflict resolution
    entities, provenance, conflicts = _resolve_scalars(enriched)
    print(f"    Resolved {len(entities)} scalar entities ({len(conflicts)} conflicts)")

    # Step 5: Stateful promotion
    forbidden_states_decl = book_config.get("entity_invariants", {}).get("forbidden_states", [])
    _detect_stateful_entities(entities, provenance, scenes, forbidden_states_decl)

    # Step 3: Merge declared constraints
    _merge_lifecycle_entities(entities, provenance, series_bible)
    _merge_role_bindings(entities, provenance, book_config)
    _merge_declared_scalars(entities, provenance, book_config)
    print(f"    Total entities after merge: {len(entities)}")

    # Step 6: Seal
    ledger = {
        "ledger_meta": {
            "book_slug": book_slug,
            "series_slug": series_slug,
            "source_synopsis_path": synopsis_path,
            "source_synopsis_sha256": synopsis_sha256,
            "builder_version": BUILDER_VERSION,
            "sealed": True,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": SCHEMA_VERSION,
        },
        "entities": list(entities.values()),
        "provenance": provenance,
    }

    return ledger, conflicts


def write_ledger(
    ledger: dict,
    conflicts: list[dict],
    out_path: str | None = None,
    work_dir: str | None = None,
    book_slug: str = "",
) -> str:
    """Write the ledger to disk with timestamped name + canonical symlink.

    Returns the path to the canonical symlink.
    """
    if out_path:
        ledger_path = out_path
        ledger_dir = os.path.dirname(out_path)
    elif work_dir:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        slug = book_slug or ledger.get("ledger_meta", {}).get("book_slug", "unknown")
        filename = f"entity_ledger_{slug}_{ts}.json"
        ledger_path = os.path.join(work_dir, filename)
        ledger_dir = work_dir
    else:
        raise ValueError("Either --out or work_dir must be provided")

    os.makedirs(ledger_dir, exist_ok=True)

    # Write timestamped file
    with open(ledger_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2)
    print(f"    Ledger written: {ledger_path}")

    # Create canonical symlink
    symlink_path = os.path.join(ledger_dir, "entity_ledger.json")
    if os.path.islink(symlink_path):
        os.unlink(symlink_path)
    elif os.path.exists(symlink_path):
        os.unlink(symlink_path)
    os.symlink(os.path.basename(ledger_path), symlink_path)
    print(f"    Symlink: {symlink_path} -> {os.path.basename(ledger_path)}")

    # Write conflicts
    conflicts_path = os.path.join(ledger_dir, "ledger_conflicts.json")
    with open(conflicts_path, "w", encoding="utf-8") as f:
        json.dump(conflicts, f, indent=2)
    if conflicts:
        print(f"    Conflicts: {len(conflicts)} logged to {conflicts_path}")
    else:
        print(f"    Conflicts: none (clean synopsis)")

    return symlink_path


# ── CLI ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="ANPD V25 Entity Ledger Builder — S-2 Phase 2a"
    )
    parser.add_argument("--synopsis", required=True, help="Path to synopsis")
    parser.add_argument("--series-bible", required=True, help="Path to series_bible.json")
    parser.add_argument("--book-config", default=None, help="Path to book config JSON (intake.json)")
    parser.add_argument("--out", default=None, help="Output path for ledger JSON")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM association; use heuristic only")
    args = parser.parse_args()

    # Determine output path
    work_dir = os.path.dirname(args.synopsis) if not args.out else None

    print(f"\n{'='*70}")
    print(f"  ANPD V25 — ENTITY LEDGER BUILDER (S-2 Phase 2a)")
    print(f"{'='*70}\n")

    ledger, conflicts = build_ledger(
        synopsis_path=args.synopsis,
        series_bible_path=args.series_bible,
        book_config_path=args.book_config,
        use_llm=not args.no_llm,
    )

    # Determine book_slug for timestamped filename
    book_slug = ledger["ledger_meta"]["book_slug"]

    symlink = write_ledger(
        ledger=ledger,
        conflicts=conflicts,
        out_path=args.out,
        work_dir=work_dir,
        book_slug=book_slug,
    )

    # Summary
    print(f"\n  Sealed: {ledger['ledger_meta']['sealed']}")
    print(f"  Entities: {len(ledger['entities'])}")
    entity_classes = {}
    for e in ledger["entities"]:
        cls = e["entity_class"]
        entity_classes[cls] = entity_classes.get(cls, 0) + 1
    for cls, count in sorted(entity_classes.items()):
        print(f"    {cls}: {count}")
    print(f"  Conflicts: {len(conflicts)}")
    print(f"  Ledger: {symlink}")
    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    main()
