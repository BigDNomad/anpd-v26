"""
scene_writer.py — V25 Scene Writer
ANPD V25 | Version: 20260511

Generates manuscript prose for a single scene given full context.

Library mode:
    from scene_writer import write_scene
    result = write_scene(scene, adjacent, series_bible, ...)

CLI mode (subprocess invocation by phase_handlers):
    python3 scene_writer.py --bundle /path/to/scene_NN_bundle.json

    Reads bundle JSON, calls write_scene(), writes output bundle
    (scene_NN_bundle_output.json) with scene_text + token stats.

Standalone CLI mode (direct invocation):
    python3 scene_writer.py \\
      --intake intake.json --synopsis synopsis.md \\
      --series-bible series_bible.json \\
      --character-profiles character_profiles.json \\
      --scene-number 1 --output-dir /path/to/scenes/
"""

import os
import sys
import json
import time
import argparse
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field


@dataclass
class SceneProse:
    prose: str
    tokens_used: dict = field(default_factory=dict)
    prompt_excerpt: str = ""
    # S-8 provenance fields
    full_user_prompt: str = ""
    system_prompt: str = ""
    model: str = ""
    generation_params: dict = field(default_factory=dict)


DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192


def _call_api(system_prompt, user_prompt, model=DEFAULT_MODEL, max_tokens=MAX_TOKENS, retries=3, timeout_seconds=300):
    """Call Anthropic API with retry logic via llm_client."""
    from llm_client import call_llm
    for attempt in range(retries):
        try:
            response = call_llm(
                provider="anthropic",
                model=model,
                system=system_prompt,
                user=user_prompt,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
            if response.stop_reason == "max_tokens":
                raise RuntimeError(f"Response truncated by max_tokens ({max_tokens})")
            return response.text, {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            }
        except Exception as e:
            error_str = str(e).lower()
            transient = ["rate_limit", "timeout", "read timed out", "timed out",
                         "529", "overloaded",
                         "connect error", "connection reset", "disconnect",
                         "broken pipe", "eof occurred", "502", "503"]
            if any(t in error_str for t in transient):
                if attempt < retries - 1:
                    wait = (attempt + 1) * 30
                    print(f"    Transient error — retrying in {wait}s ({attempt+1}/{retries})")
                    time.sleep(wait)
                    continue
            raise
    raise RuntimeError(f"API call failed after {retries} attempts")


SYSTEM_PROMPT = """You are a novelist writing prose in emulation of {prose_emulation_author}.

PROSE EMULATION TARGET:
Author: {prose_emulation_author}
Reference works: {prose_emulation_works}
Voice notes: {prose_emulation_voice_notes}

Write as {prose_emulation_author} would write this scene. Do NOT blend other authorial voices into the prose unless specifically directed by the voice register below.

VOICE REGISTER:
{voice_register}

OPERATIONAL DOCTRINE (every scene must respect these rules):
{doctrine}

ANTI-PATTERNS (NEVER include these in your prose):
{anti_patterns}

HARD CONSTRAINTS (these are not suggestions — violating any of these produces an invalid scene):
{hard_constraints}

POV DISCIPLINE:
- Stay in the scene's specified POV throughout. No head-hopping.
- For limited-POV scenes: only report what the POV character can see, hear, know.
- For Omniscient Historical POV: panoramic observation permitted, no single character's interiority.
- Use "said" for dialogue tags. No alternatives (muttered, exclaimed, hissed, etc.) unless the physical action is literally occurring.

OUTPUT FORMAT:
- Pure prose. No scene headers, chapter markers, or metadata.
- No "Scene 1" or "Chapter" labels. Just the prose.
- No author's notes, no commentary, no explanations.
- Begin the prose directly. End it cleanly."""


_CORRECTIONS_BLOCK = """
========================================
CORRECTIONS REQUIRED
========================================

The following corrections must be satisfied in the regenerated scene. These
are mandatory constraints, not suggestions. Failure to satisfy them produces
an invalid output.

{corrections_text}

========================================
CONSTRAINT RESOLUTION RULES
========================================

1. Each correction must be reflected in the scene's prose. The scene's
   narrative function, structural beats, and POV remain unchanged — only
   the specific factual or stylistic elements named in the corrections are
   altered.

2. Where a correction conflicts with the synopsis sub-scene's literal text,
   the correction wins. The synopsis is the structural specification; the
   correction is the factual ground truth.

3. Maintain voice continuity with the original scene (provided as reference,
   if included). Do not introduce stylistic shifts.

4. If two corrections appear mutually incompatible, satisfy both to the
   maximum extent possible and proceed. Do not refuse the generation.

5. Do not add commentary about the corrections in the scene's prose. The
   reader should see no trace of the correction process — only the corrected
   prose."""


_ENTITY_INVARIANTS_BLOCK = """

========================================
ENTITY INVARIANTS
========================================

Facts that must be true in every scene where the named entity appears.
These are sealed declarations from the book's entity ledger. They are not
suggestions. If a scene's synopsis spec contradicts an invariant, the
invariant wins.

{invariants_text}

Do not mention the invariant ledger in the prose. The invariants constrain
content, not commentary."""


def _format_entity_invariants(entity_ledger: dict | None) -> str:
    """Format the entity ledger into a prompt block for scene generation.

    Returns formatted invariants text, or empty string if ledger is empty/None.
    """
    if not entity_ledger:
        return ""

    entities = entity_ledger.get("entities", [])
    if not entities:
        return ""

    scalars = []
    stateful = []
    lifecycle = []

    for entity in entities:
        ec = entity.get("entity_class", "scalar")
        if ec == "scalar":
            scalars.append(entity)
        elif ec == "stateful":
            stateful.append(entity)
        elif ec == "lifecycle_role":
            lifecycle.append(entity)

    sections = []

    if scalars:
        lines = ["Scalar invariants:"]
        for e in scalars:
            name = e.get("canonical_name", e.get("id", ""))
            invariants = e.get("invariants", {})
            for inv_key, inv_value in invariants.items():
                lines.append(f"- {name} — {inv_key}: {inv_value}")
        sections.append("\n".join(lines))

    if stateful:
        lines = ["Stateful entities (state changes at scene boundaries):"]
        for e in stateful:
            name = e.get("canonical_name", e.get("id", ""))
            track = e.get("state_track", {})
            initial = track.get("initial_state", "")
            transitions = track.get("allowed_transitions", [])
            forbidden = track.get("forbidden_states", [])
            lines.append(f"- {name}: initial state = {initial}")
            if transitions:
                for t in transitions:
                    lines.append(f"  Scene {t['occurs_at_scene']}: {t['from']} → {t['to']}")
            if forbidden:
                lines.append(f"  Forbidden states: {', '.join(forbidden)}")
        sections.append("\n".join(lines))

    if lifecycle:
        lines = ["Lifecycle and role invariants:"]
        for e in lifecycle:
            name = e.get("canonical_name", e.get("id", ""))
            lc = e.get("lifecycle", {})
            rb = e.get("role_bindings", [])
            if lc:
                alive = lc.get("alive_at_end_of_book")
                if alive is True:
                    lines.append(f"- {name}: survives the book. Do not write {name} as dying or dead.")
                elif alive is False:
                    lines.append(f"- {name}: dies during the book.")
            if rb:
                for binding in rb:
                    ctx = binding.get("context", "")
                    form = binding.get("required_form", "")
                    forbidden = binding.get("forbidden_references", [])
                    permitted = binding.get("permitted_roles", [])
                    line = f"- {name} ({ctx}): use {form} only."
                    if forbidden:
                        line += f" Do not name: {', '.join(forbidden)}."
                    if permitted:
                        line += f" Permitted roles: {', '.join(permitted)}."
                    lines.append(line)
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _build_system_prompt(series_bible: dict, craft_principles: list,
                         prose_emulation: dict | None = None,
                         corrections: str | None = None,
                         entity_ledger: dict | None = None) -> str:
    """Build the system prompt from series bible and principles."""
    voice = series_bible.get("voice_register", {})
    voice_text = (
        f"Base: {voice.get('base_voice', 'Short declarative sentences, ground-level observation.')}\n"
        f"Intrusion: {voice.get('intrusion_voice', 'Extended sentences for thematic weight.')}\n"
        f"Allocation: {voice.get('intrusion_allocation', 'ACTION: 0-5% McCarthy. NON-ACTION: 10-20%.')}\n"
    )
    forbidden = voice.get("forbidden_patterns", [])
    if forbidden:
        voice_text += "Forbidden: " + "; ".join(forbidden)

    doctrine = series_bible.get("operational_doctrine", [])
    doctrine_text = "\n".join(f"- {d}" for d in doctrine) if doctrine else "None specified."

    ap_entries = []
    for p in craft_principles:
        if p.get("category") in ("PROSE", "DOCTRINE") and "scene_writer" in p.get("components_inject_into_prompt", []):
            ap_entries.append(f"- [{p['id']}] {p['description']}")
    anti_patterns_text = "\n".join(ap_entries) if ap_entries else "None specified."

    # Prose emulation block — source of truth is intake.prose_emulation
    if prose_emulation:
        prose_emulation_author = prose_emulation.get("author", "")
        works_list = prose_emulation.get("primary_works", [])
        prose_emulation_works = ", ".join(works_list) if works_list else "(not specified)"
        prose_emulation_voice_notes = prose_emulation.get("voice_notes", "")
    else:
        prose_emulation_author = "(not specified)"
        prose_emulation_works = "(not specified)"
        prose_emulation_voice_notes = "(not specified)"

    # Hard constraints block — pulled from series_bible.hard_constraints
    hard_constraints_dict = series_bible.get("hard_constraints", {})
    if hard_constraints_dict:
        hc_lines = []
        for key, value in hard_constraints_dict.items():
            if isinstance(value, list):
                hc_lines.append(f"- {key}: {', '.join(str(v) for v in value)}")
            else:
                hc_lines.append(f"- {key}: {value}")
        hard_constraints_text = "\n".join(hc_lines)
    else:
        hard_constraints_text = "(none specified)"

    prompt = SYSTEM_PROMPT.format(
        prose_emulation_author=prose_emulation_author,
        prose_emulation_works=prose_emulation_works,
        prose_emulation_voice_notes=prose_emulation_voice_notes,
        voice_register=voice_text,
        doctrine=doctrine_text,
        anti_patterns=anti_patterns_text,
        hard_constraints=hard_constraints_text,
    )

    invariants_text = _format_entity_invariants(entity_ledger)
    if invariants_text:
        prompt += _ENTITY_INVARIANTS_BLOCK.format(invariants_text=invariants_text)

    if corrections:
        prompt += _CORRECTIONS_BLOCK.format(corrections_text=corrections)

    return prompt


def _format_continuity_state(prior_state: dict | None) -> str:
    """Format prior-scene state into a CONTINUITY STATE block for the prompt.

    prior_state is the state_after_sc{N-1}.json dict produced by state_tracker.
    Returns "" when there is no prior state (scene 1). The block is binding:
    facts established in prior scenes must not be contradicted (deaths stay
    dead, established physical/identity attributes hold, resource counts only
    decrease as consumed).
    """
    if not prior_state or not isinstance(prior_state, dict):
        return ""

    lines = []
    deaths = prior_state.get("deaths") or []
    if deaths:
        lines.append("DECEASED (these characters are dead — they do NOT appear alive, act, or speak):")
        for d in deaths:
            lines.append(f"  - {d}")

    locs = prior_state.get("character_locations") or {}
    if locs:
        lines.append("CHARACTER LOCATIONS (as of the prior scene):")
        for name, loc in locs.items():
            lines.append(f"  - {name}: {loc}")

    phys = prior_state.get("character_physical_state") or {}
    if phys:
        lines.append("CHARACTER STATE (identity, condition — must remain consistent):")
        for name, st in phys.items():
            lines.append(f"  - {name}: {st}")

    injuries = prior_state.get("injuries") or {}
    if injuries:
        lines.append("INJURIES (carry forward — do not heal without cause):")
        for name, inj in injuries.items():
            lines.append(f"  - {name}: {inj}")

    equip = prior_state.get("equipment") or {}
    if equip:
        lines.append("EQUIPMENT / RESOURCES (counts only decrease as consumed):")
        for name, items in equip.items():
            lines.append(f"  - {name}: {items}")

    revealed = prior_state.get("information_revealed") or []
    if revealed:
        lines.append("ALREADY REVEALED (do NOT re-reveal as if new):")
        for r in revealed:
            lines.append(f"  - {r}")

    timeline = prior_state.get("timeline")
    if timeline:
        lines.append(f"TIMELINE: {timeline}")

    if not lines:
        return ""

    body = "\n".join(lines)
    return f"""

=== CONTINUITY STATE (BINDING — carried from prior scenes) ===

The following facts are established. Your scene MUST NOT contradict them.
Deceased characters stay dead. Established identity and physical attributes
hold. Resource counts do not increase. Already-revealed information is not
re-revealed as if new.

{body}

=== END CONTINUITY STATE ===
"""


def _build_user_prompt(
    scene,
    adjacent: dict,
    character_profiles: dict,
    target_words: int,
    failure_feedback: str,
    prior_prose_in_chapter: list = None,
    prior_state: dict | None = None,
) -> str:
    """Build the per-scene user prompt."""
    continuity_text = _format_continuity_state(prior_state)

    # Filter character profiles to those mentioned in scene body
    chars = character_profiles.get("characters", [])
    scene_text_lower = scene.body.lower()
    relevant_chars = []
    for c in chars:
        name = c.get("name", "")
        if name.lower().split()[0] in scene_text_lower:
            relevant_chars.append(c)

    char_text = ""
    if relevant_chars:
        char_sections = []
        for c in relevant_chars[:6]:  # Limit to 6 most relevant
            section = f"**{c['name']}** ({c.get('role', '')}):"
            if c.get("voice_characteristics"):
                section += f" Voice: {c['voice_characteristics']}"
            arc_tag = c.get("arc_tags", {}).get(f"ch{scene.chapter_number}", "")
            if arc_tag:
                section += f" Arc state this chapter: {arc_tag}"
            char_sections.append(section)
        char_text = "\n".join(char_sections)

    # Adjacent scene context
    prior_text = ""
    if adjacent.get("prior"):
        p = adjacent["prior"]
        prior_text = f"\nPRIOR SCENE SYNOPSIS (for transition continuity):\n{p.body[:1500]}"

    next_text = ""
    if adjacent.get("next"):
        n = adjacent["next"]
        next_text = f"\nNEXT SCENE SYNOPSIS (for setup — plant seeds, don't execute):\n{n.body[:800]}"

    feedback_text = ""
    if failure_feedback:
        feedback_text = f"\n\nPREVIOUS ATTEMPT FAILED. Address these issues:\n{failure_feedback}\n"

    scene_mode = getattr(scene, "mode", "") or ""
    mode_clause = f" Narrative mode: {scene_mode}." if scene_mode else ""

    # Determine the perspective clause. FOCUS (scene.pov) may name a character,
    # a vessel/organization, "omniscient", or be absent. Absent FOCUS means the
    # outline declared no single anchor — render from the natural perspective
    # of the synopsis content; do NOT force a character.
    if scene.pov and "omniscient" in scene.pov.lower():
        focus_clause = "OMNISCIENT — panoramic, no single character's interiority. McCarthy register permitted heavily."
    elif scene.pov:
        focus_clause = (
            f"FOCUS: {scene.pov}. Anchor the scene to this — it may be a "
            f"character, a vessel, a unit, or an operation. If it is not a "
            f"person, do NOT invent a character viewpoint; render the scene "
            f"observationally around the named focus."
        )
    else:
        focus_clause = (
            "No single FOCUS declared. Render from the natural perspective the "
            "synopsis content implies. Do NOT force a single character's "
            "interiority if the content does not call for one."
        )

    if scene.scene_type == "ACTION":
        pov_note = f"\nThis is an ACTION scene.{mode_clause} {focus_clause} Leonard register dominant (0-5% McCarthy). Short sentences. Ground-level. Physical events only."
    elif scene.scene_type == "NON_ACTION":
        pov_note = f"\nThis is a NON-ACTION scene.{mode_clause} {focus_clause} Leonard base with McCarthy intrusions permitted (10-20%)."
    else:
        pov_note = f"\nThis is a MIXED scene.{mode_clause} {focus_clause} Leonard base with moderate McCarthy intrusions (5-10%)."

    # Prior prose injection (scenes already written in this chapter)
    prior_prose_text = ""
    if prior_prose_in_chapter:
        prior_scenes_joined = "\n\n***\n\n".join(prior_prose_in_chapter)
        prior_prose_text = f"""

=== PRIOR SCENES IN THIS CHAPTER (ALREADY WRITTEN) ===

The following scenes have already been written in this chapter. Their prose is
below in order. You are continuing the chapter from where these end.

CRITICAL CONSTRAINTS:

1. DO NOT restate plot points, images, character introductions, settings, or
   thematic elements already established below. The reader has read every word.

2. DO NOT re-anchor your scene with imagery from prior scenes. If the prologue's
   first scene depicted a specific historical moment in vivid detail, your scene
   does not depict it again — it references it only by allusion if at all, and
   only when load-bearing.

3. DO build FORWARD. Your scene continues, develops, or pivots from what has
   been established. It does not recapitulate.

4. DO use callbacks sparingly and deliberately when they carry meaning. A single
   echo of a prior image at a structural moment is allowed when it lands a beat.
   Restating context that the reader already has is not allowed.

--- BEGIN PRIOR SCENES ---

{prior_scenes_joined}

--- END PRIOR SCENES ---

Now write the current scene, building forward from what has been established.
"""

    prompt = f"""Write manuscript prose for this scene.

SCENE: Chapter {scene.chapter_number}, Scene {scene.scene_number} — {scene.title}
TYPE: {scene.scene_type}
FOCUS: {scene.pov if scene.pov else "(none — natural perspective)"}
TARGET LENGTH: {target_words} words (range {target_words - 150} to {target_words + 250})
{pov_note}

SCENE SYNOPSIS (the structural truth — cover EVERY beat):
{scene.body}
{continuity_text}
CHARACTER PROFILES FOR THIS SCENE:
{char_text or "No specific profiles filtered."}
{prior_prose_text}{prior_text}{next_text}{feedback_text}

INSTRUCTIONS:
- Cover EVERY beat in the scene synopsis above.
- Do NOT invent plot content not in the synopsis.
- The synopsis is the structural truth — render it as manuscript prose.
- Pure prose output. No headers, no metadata, no scene markers.
- Begin immediately with the prose. End cleanly.
- Target {target_words} words."""

    return prompt


def write_scene(
    scene,
    adjacent: dict,
    series_bible: dict,
    character_profiles: dict,
    craft_principles: list,
    target_words: int = 850,
    failure_feedback: str = "",
    prior_prose_in_chapter: list = None,
    prior_state: dict | None = None,
    prose_emulation: dict | None = None,
    corrections: str | None = None,
    entity_ledger: dict | None = None,
) -> SceneProse:
    """Generate manuscript prose for a single scene."""
    system = _build_system_prompt(series_bible, craft_principles, prose_emulation=prose_emulation, corrections=corrections, entity_ledger=entity_ledger)
    user = _build_user_prompt(scene, adjacent, character_profiles, target_words, failure_feedback, prior_prose_in_chapter, prior_state=prior_state)

    model = os.environ.get("V25_MODEL", DEFAULT_MODEL)
    prose, tokens = _call_api(system, user, model=model)

    return SceneProse(
        prose=prose,
        tokens_used=tokens,
        prompt_excerpt=user[:500],
        full_user_prompt=user,
        system_prompt=system,
        model=model,
        generation_params={"temperature": "model_default", "max_tokens": MAX_TOKENS},
    )


# ── Pricing (matches synopsis_generator) ────────────────────────────────────

PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
}


def _calc_cost(model, input_tokens, output_tokens):
    prices = PRICING.get(model, PRICING[DEFAULT_MODEL])
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


def _atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.rename(tmp, path)


def _atomic_write_json(path, data):
    _atomic_write(path, json.dumps(data, indent=2))


# ── Scene object from bundle ────────────────────────────────────────────────

@dataclass
class _BundleScene:
    """Lightweight scene object constructed from bundle JSON for write_scene()."""
    chapter_number: int
    scene_number: int
    title: str
    scene_type: str
    pov: str
    body: str
    position_in_chapter: int = 1
    mode: str = ""


def _scene_from_bundle(bundle: dict) -> _BundleScene:
    """Extract a SceneEntry-compatible object from a phase_handlers bundle."""
    body = bundle.get("scene_body_from_map", "")

    # Parse scene_type and pov from body annotations if present
    scene_type = "MIXED"
    pov = ""
    type_match = re.search(r'\[(?:TYPE:\s*)?(ACTION|MIXED|NON-ACTION|NON_ACTION)\]', body, re.IGNORECASE)
    if type_match:
        scene_type = type_match.group(1).upper().replace("-", "_")
    # Prefer [FOCUS: ...]; fall back to legacy [POV: ...]. Either may be absent.
    mode = ""
    focus_match = re.search(r'\[FOCUS:\s*([^\]]+)\]', body, re.IGNORECASE)
    if focus_match:
        pov = focus_match.group(1).strip()
    else:
        pov_match = re.search(r'\[POV:\s*([^\]]+)\]', body, re.IGNORECASE)
        if pov_match:
            pov = pov_match.group(1).strip()
    mode_match = re.search(r'\[MODE:\s*([^\]]+)\]', body, re.IGNORECASE)
    if mode_match:
        mode = mode_match.group(1).strip()

    return _BundleScene(
        chapter_number=(bundle.get("scene_number", 1) - 1) // 4 + 1,  # mechanical: 4 scenes per chapter
        scene_number=bundle.get("scene_number", 1),
        title=bundle.get("scene_title", ""),
        scene_type=scene_type,
        pov=pov,
        body=body,
        mode=mode,
    )


def _scene_from_synopsis(synopsis_path: str, scene_number: int):
    """Extract a scene from a parsed synopsis file. Returns SceneEntry or None."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from synopsis_parser import parse_synopsis

    structure = parse_synopsis(synopsis_path)
    for ch in structure.chapters:
        for sc in ch.scenes:
            if sc.scene_number == scene_number:
                return sc
    return None


# ── Bundle mode ─────────────────────────────────────────────────────────────

def _run_bundle_mode(bundle_path: str, dry_run: bool = False, corrections: str | None = None) -> int:
    """Read bundle JSON, call write_scene(), write output bundle.

    Output bundle path: {bundle_path} with .json replaced by _output.json
    Output bundle schema: {"scene_text": str, "tokens": dict, "cost_usd": float, ...}
    """
    print(f"  scene_writer: loading bundle {bundle_path}")

    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    scene = _scene_from_bundle(bundle)
    series_bible = bundle.get("series_bible", {})
    character_profiles = bundle.get("character_profiles", {})
    prose_emulation = bundle.get("intake", {}).get("prose_emulation")
    effective_config = bundle.get("effective_config", {})
    prior_state = bundle.get("prior_state")

    # Load craft principles
    craft_principles = []
    principles_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "principles", "craft_principles.json"
    )
    if os.path.exists(principles_path):
        with open(principles_path, "r", encoding="utf-8") as f:
            pdata = json.load(f)
        craft_principles = pdata.get("principles", pdata if isinstance(pdata, list) else [])

    target_words = effective_config.get("target_words_per_scene", 850)

    print(f"  Scene {scene.scene_number}: {scene.title} [{scene.scene_type}] [FOCUS: {scene.pov or 'none'}]")
    print(f"  Target: {target_words} words")

    if dry_run:
        system = _build_system_prompt(series_bible, craft_principles, prose_emulation=prose_emulation, corrections=corrections)
        user = _build_user_prompt(scene, {"prior": None, "next": None}, character_profiles, target_words, "", prior_state=prior_state)
        print(f"  DRY-RUN: system prompt {len(system)} chars, user prompt {len(user)} chars")
        print(f"  System prompt sample: {system[:200]}...")
        print(f"  User prompt sample: {user[:200]}...")
        return 0

    start_time = time.time()
    result = write_scene(
        scene=scene,
        adjacent={"prior": None, "next": None},
        series_bible=series_bible,
        character_profiles=character_profiles,
        craft_principles=craft_principles,
        target_words=target_words,
        prior_state=prior_state,
        prose_emulation=prose_emulation,
        corrections=corrections,
    )
    elapsed = time.time() - start_time

    model = os.environ.get("V25_MODEL", DEFAULT_MODEL)
    input_tokens = result.tokens_used.get("input_tokens", 0)
    output_tokens = result.tokens_used.get("output_tokens", 0)
    cost = _calc_cost(model, input_tokens, output_tokens)
    word_count = len(result.prose.split())

    print(f"  Generated: {word_count} words, {elapsed:.0f}s, ${cost:.4f}")

    # Write output bundle (phase_handlers expects _output.json)
    output_bundle_path = bundle_path.replace(".json", "_output.json")
    output_bundle = {
        "scene_text": result.prose,
        "scene_number": scene.scene_number,
        "word_count": word_count,
        "tokens": result.tokens_used,
        "cost_usd": round(cost, 4),
        "wall_seconds": round(elapsed, 1),
        "model": model,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(output_bundle_path, output_bundle)
    print(f"  Output bundle: {output_bundle_path}")

    return 0


# ── Standalone mode ─────────────────────────────────────────────────────────

def _run_standalone_mode(args) -> int:
    """Run scene_writer with individual file arguments (direct invocation)."""
    # Load inputs
    with open(args.intake, "r", encoding="utf-8") as f:
        intake = json.load(f)
    with open(args.series_bible, "r", encoding="utf-8") as f:
        series_bible = json.load(f)
    with open(args.character_profiles, "r", encoding="utf-8") as f:
        character_profiles = json.load(f)
    prose_emulation = intake.get("prose_emulation")

    # Load craft principles
    craft_principles = []
    principles_path = intake.get("craft_principles_path")
    if principles_path and not os.path.isabs(principles_path):
        principles_path = os.path.join(os.path.dirname(os.path.abspath(args.intake)), principles_path)
    if principles_path and os.path.exists(principles_path):
        with open(principles_path, "r", encoding="utf-8") as f:
            pdata = json.load(f)
        craft_principles = pdata.get("principles", pdata if isinstance(pdata, list) else [])

    # Build scene object from synopsis file
    scene = _scene_from_synopsis(args.synopsis, args.scene_number)
    if scene is None:
        # Fallback: treat entire synopsis text as the scene body
        with open(args.synopsis, "r", encoding="utf-8") as f:
            synopsis_text = f.read()
        scene = _BundleScene(
            chapter_number=(args.scene_number - 1) // 4 + 1,  # mechanical: 4 scenes per chapter
            scene_number=args.scene_number,
            title=f"Scene {args.scene_number}",
            scene_type="MIXED",
            pov="",
            body=synopsis_text,
        )
        # Try to parse annotations from body
        type_match = re.search(r'\[(?:TYPE:\s*)?(ACTION|MIXED|NON-ACTION|NON_ACTION)\]', synopsis_text, re.IGNORECASE)
        if type_match:
            scene.scene_type = type_match.group(1).upper().replace("-", "_")
        focus_match = re.search(r'\[FOCUS:\s*([^\]]+)\]', synopsis_text, re.IGNORECASE)
        if focus_match:
            scene.pov = focus_match.group(1).strip()
        else:
            pov_match = re.search(r'\[POV:\s*([^\]]+)\]', synopsis_text, re.IGNORECASE)
            if pov_match:
                scene.pov = pov_match.group(1).strip()
        mode_match = re.search(r'\[MODE:\s*([^\]]+)\]', synopsis_text, re.IGNORECASE)
        if mode_match and hasattr(scene, 'mode'):
            scene.mode = mode_match.group(1).strip()

    target_words = args.target_words
    corrections = None
    if getattr(args, "corrections_file", None) and os.path.exists(args.corrections_file):
        with open(args.corrections_file, "r", encoding="utf-8") as f:
            corrections = f.read().strip() or None

    print(f"\n  scene_writer: Scene {scene.scene_number} — {scene.title} [{scene.scene_type}] [FOCUS: {scene.pov or 'none'}]")
    print(f"  Target: {target_words} words")

    if args.dry_run:
        system = _build_system_prompt(series_bible, craft_principles, prose_emulation=prose_emulation, corrections=corrections)
        user = _build_user_prompt(scene, {"prior": None, "next": None}, character_profiles, target_words, "")
        print(f"  DRY-RUN: system prompt {len(system)} chars, user prompt {len(user)} chars")
        print(f"  System prompt sample:\n    {system[:300]}...")
        print(f"  User prompt sample:\n    {user[:300]}...")
        return 0

    start_time = time.time()
    result = write_scene(
        scene=scene,
        adjacent={"prior": None, "next": None},
        series_bible=series_bible,
        character_profiles=character_profiles,
        craft_principles=craft_principles,
        target_words=target_words,
        prose_emulation=prose_emulation,
        corrections=corrections,
    )
    elapsed = time.time() - start_time

    model = os.environ.get("V25_MODEL", DEFAULT_MODEL)
    input_tokens = result.tokens_used.get("input_tokens", 0)
    output_tokens = result.tokens_used.get("output_tokens", 0)
    cost = _calc_cost(model, input_tokens, output_tokens)
    word_count = len(result.prose.split())

    print(f"  Generated: {word_count} words, {elapsed:.0f}s, ${cost:.4f}")

    # Write scene file
    os.makedirs(args.output_dir, exist_ok=True)
    scene_filename = f"sc_{args.scene_number:03d}.md"
    scene_path = os.path.join(args.output_dir, scene_filename)
    _atomic_write(scene_path, result.prose)
    print(f"  Scene file: {scene_path}")

    # Write receipt
    receipt = {
        "component": "scene_writer",
        "scene_number": args.scene_number,
        "word_count": word_count,
        "tokens": result.tokens_used,
        "cost_usd": round(cost, 4),
        "wall_seconds": round(elapsed, 1),
        "model": model,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = os.path.join(args.output_dir, f"sc_{args.scene_number:03d}_receipt.json")
    _atomic_write_json(receipt_path, receipt)

    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ANPD V25 Scene Writer — generate manuscript prose for a single scene"
    )

    # Bundle mode (phase_handlers invocation)
    parser.add_argument("--bundle", help="Path to scene bundle JSON (phase_handlers mode)")

    # Standalone mode flags
    parser.add_argument("--intake", help="Path to intake.json")
    parser.add_argument("--synopsis", help="Path to synopsis file (full or per-scene)")
    parser.add_argument("--series-bible", help="Path to series_bible.json")
    parser.add_argument("--character-profiles", help="Path to character_profiles.json")
    parser.add_argument("--scene-number", type=int, help="Scene number to generate")
    parser.add_argument("--output-dir", help="Output directory for scene file")
    parser.add_argument("--target-words", type=int, default=850, help="Target word count per scene (default: 850)")

    # Corrections (fixer Tier 2 regeneration)
    parser.add_argument("--corrections-file", help="Path to corrections text file (optional, for fixer regeneration)")

    # Common flags
    parser.add_argument("--dry-run", action="store_true", help="Build prompts and print stats without calling API")

    args = parser.parse_args()

    # Load corrections from file if specified
    corrections = None
    if args.corrections_file and os.path.exists(args.corrections_file):
        with open(args.corrections_file, "r", encoding="utf-8") as f:
            corrections = f.read().strip() or None

    if args.bundle:
        # Bundle mode
        if not os.path.exists(args.bundle):
            print(f"  FATAL: bundle not found: {args.bundle}", file=sys.stderr)
            sys.exit(1)
        sys.exit(_run_bundle_mode(args.bundle, dry_run=args.dry_run, corrections=corrections))

    elif args.intake and args.synopsis and args.series_bible and args.character_profiles and args.scene_number and args.output_dir:
        # Standalone mode
        sys.exit(_run_standalone_mode(args))

    else:
        parser.print_help()
        print("\nError: provide either --bundle OR all standalone flags "
              "(--intake, --synopsis, --series-bible, --character-profiles, --scene-number, --output-dir)",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
