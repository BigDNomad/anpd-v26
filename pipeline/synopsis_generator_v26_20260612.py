"""
synopsis_generator.py — V26 Synopsis Generator
ANPD V26 | Version: 20260612

Generates scene-by-scene synopsis from operator outline + intake + series_bible
+ character_profiles + craft_principles.

Core architectural difference from V24: the operator's outline is the structural
blueprint. The generator executes against it, not around it. Every outline beat
must appear in the synopsis. No content is invented beyond the outline.

Usage:
    python3 synopsis_generator.py \\
      --outline <path> \\
      --intake <path> \\
      --series-bible <path> \\
      --character-profiles <path> \\
      --output-dir <path>
"""

import os
import sys
import json
import time
import argparse
import re
import hashlib
import uuid
import shutil
from datetime import datetime, timezone

# ── V25 pipeline imports ────────────────────────────────────────────────────
# Add pipeline directory to path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intake_validator import validate_intake
from outline_parser import parse_outline
from principles_loader import load_principles_for_component
from outline_comparator import compare_outline_to_synopsis


# ── Constants ────────────────────────────────────────────────────────────────
MAX_TOKENS_PER_CHAPTER = 16384
DEFAULT_MODEL = "claude-sonnet-4-6"
SCENES_PER_CHAPTER_DEFAULT = 4

# Pricing: USD per million tokens
PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
}

CHAPTERS_DIR_NAME = "synopsis_chapters"
STATE_FILE_NAME = "synopsis_generation_state.json"

# ── Per-beat proportional sizing constants (D24) ───────────────────────────
MIN_WORDS_PER_BEAT = 5
MAX_WORDS_PER_BEAT = 15
ABSOLUTE_MIN_WORDS = 100
ABSOLUTE_MAX_WORDS = 1500


def compute_scene_word_target(beat_count: int) -> tuple:
    """Compute per-scene synopsis word target proportional to beat count.

    Returns (min_words, max_words, hard_ceiling).
    """
    min_words = max(ABSOLUTE_MIN_WORDS, beat_count * MIN_WORDS_PER_BEAT)
    max_words = min(ABSOLUTE_MAX_WORDS, max(280, beat_count * MAX_WORDS_PER_BEAT))
    hard_ceiling = int(max_words * 1.2)
    return min_words, max_words, hard_ceiling


# ── File hashing ─────────────────────────────────────────────────────────────

def _sha256(path):
    """Return hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_input_hashes(outline_path, intake_path, series_bible_path, character_profiles_path):
    return {
        "outline.md": _sha256(outline_path),
        "intake.json": _sha256(intake_path),
        "series_bible.json": _sha256(series_bible_path),
        "character_profiles.json": _sha256(character_profiles_path),
    }


# ── Atomic file writing ─────────────────────────────────────────────────────

def _atomic_write(path, content, binary=False):
    """Write content to path atomically via tmp+rename."""
    tmp = path + ".tmp"
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    with open(tmp, mode, **kwargs) as f:
        f.write(content)
    os.rename(tmp, path)


def _atomic_write_json(path, data):
    _atomic_write(path, json.dumps(data, indent=2))


# ── Cost calculation ─────────────────────────────────────────────────────────

def _calc_cost(model, input_tokens, output_tokens):
    """Return cost in USD for given token counts."""
    prices = PRICING.get(model, PRICING[DEFAULT_MODEL])
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


# ── State file management ───────────────────────────────────────────────────

def _load_state(state_path):
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _new_state(input_hashes, model, total_chapters):
    return {
        "run_id": str(uuid.uuid4()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "input_hashes": input_hashes,
        "config": {
            "model": model,
            "temperature": 0.3,
            "max_tokens_per_chapter": MAX_TOKENS_PER_CHAPTER,
        },
        "total_chapters": total_chapters,
        "chapters": {},
        "totals": {
            "completed_chapters": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_usd": 0.0,
            "total_wall_seconds": 0.0,
        },
    }


def _update_chapter_state(state, ch_key, chapter_data):
    """Update state with completed chapter data and recompute totals."""
    state["chapters"][ch_key] = chapter_data
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    # Recompute totals from all chapters
    totals = state["totals"]
    totals["completed_chapters"] = sum(
        1 for c in state["chapters"].values() if c.get("status") == "completed"
    )
    totals["total_input_tokens"] = sum(
        c.get("input_tokens", 0) for c in state["chapters"].values()
    )
    totals["total_output_tokens"] = sum(
        c.get("output_tokens", 0) for c in state["chapters"].values()
    )
    totals["total_cost_usd"] = round(sum(
        c.get("cost_usd", 0) for c in state["chapters"].values()
    ), 4)
    totals["total_wall_seconds"] = round(sum(
        c.get("wall_seconds", 0) for c in state["chapters"].values()
    ), 1)
    return state


# ── API client ───────────────────────────────────────────────────────────────

def call_api(system_prompt, user_prompt, model=DEFAULT_MODEL, max_tokens=MAX_TOKENS_PER_CHAPTER, retries=3, timeout_seconds=300):
    """Call the LLM API via llm_client with retry logic. Returns LLMResponse."""
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
                raise RuntimeError(
                    f"Response truncated by max_tokens limit ({max_tokens}). "
                    f"Output is incomplete."
                )
            return response
        except Exception as e:
            error_str = str(e).lower()
            transient_patterns = [
                "rate_limit", "timeout", "read timed out", "timed out",
                "529", "overloaded",
                "connect error", "connection reset", "disconnect",
                "broken pipe", "eof occurred", "502", "503",
            ]
            if any(t in error_str for t in transient_patterns):
                if attempt < retries - 1:
                    wait = (attempt + 1) * 30
                    print(f"    Transient error — retrying in {wait}s ({attempt+1}/{retries})")
                    time.sleep(wait)
                    continue
            raise
    raise RuntimeError(f"API call failed after {retries} attempts")


# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are generating a SYNOPSIS SECTION for one scene of a novel.

A SYNOPSIS SECTION IS:
- A list of structural beats translated into brief sentences
- An INSTRUCTION SET for a downstream writer (scene_writer) to expand into finished prose
- One or two sentences per beat — terse, specific, concrete
- What HAPPENS, what is REVEALED, what SHIFTS
- Operational/tradecraft specifics: yes
- Character actions and decisions: yes
- Internal states as instructions: 'Lena registers X internally' — yes

A SYNOPSIS SECTION IS NOT:
- Prose
- Paragraphs of literary description
- Thematic interpretation ('this contains a structural fault')
- Interior narration ('she files this the way she files everything')
- Atmospheric description ('the room held its breath')
- Finished dialogue
- Anything the downstream scene_writer should be authoring

EXAMPLE — RIGHT vs WRONG for the same scene beats:

WRONG (this is prose; scene_writer's job):
  'Lena and Marco are in their apartment. Lena tells Marco she has been
  offered a field assignment — operational, extended, location she
  cannot specify, work she cannot fully describe. She gives him the
  shape of it: the duration, the risk category, the fact that she
  will be unreachable for stretches.'

RIGHT (this is instruction; synopsis's job):
  'Lena tells Marco about field assignment in their apartment.
  Conveys: duration, risk category, will be unreachable in stretches.
  Withholds: operational name, country, target nature.'

WRONG: 'The yes also contains a structural fault neither of them can
  locate. It is a yes made before the work has changed her.'

RIGHT: 'Marco says yes immediately. Yes carries trust, pride, willingness
  to absorb absence. Lena registers what yes doesn't say (effort behind
  it, things held back) — files without acting.'

The downstream writer needs INSTRUCTIONS to know WHAT to write. They
do not need the WRITING DONE for them at the synopsis layer.

YOUR ROLE: Execute the operator's outline exactly. You are a laborer, not an architect.
The operator has authored the structural blueprint. Your job is to expand each chapter's
outline beats into a terse instruction synopsis — NOT finished prose.

HARD CONSTRAINTS:
1. EVERY beat in the operator's outline for this chapter MUST appear in your output.
2. You MUST NOT invent beats, events, characters, or locations not in the outline.
3. You MUST NOT reorder the outline's beats.
4. You MUST produce the exact number of scenes requested.
5. You MUST tag each scene with [TYPE: ACTION|MIXED|NON-ACTION]. Include [MODE: ...] and [FOCUS: ...] when provided.
6. You MUST stay within the historical window specified.
7. You MUST follow the voice register specified.
8. You MUST follow ALL craft principles provided.
9. You MUST stay within the per-scene word target specified in the user prompt.

SCENE FORMAT:
### Scene N — Title [TYPE: ACTION|MIXED|NON-ACTION] [PILLAR: <if provided>] [MODE: <if provided>] [FOCUS: <if provided>]
- Beat instruction (1-2 sentences, terse)
- Beat instruction
- ...

If the outline marks a scene with a structural pillar (TWIST1, TWIST2, TWIST3, LOWEST_POINT, FINAL_BATTLE),
you MUST include the [PILLAR: X] tag in the scene header exactly as provided. Do NOT add pillar tags to
scenes that are not marked in the outline.

Return only the synopsis scenes. No preamble. No commentary. No meta-discussion."""


# ── Per-chapter prompt builder ───────────────────────────────────────────────

def build_chapter_prompt(
    chapter,
    intake,
    series_bible,
    character_profiles,
    principles_text,
    prior_chapter_synopsis="",
    next_chapter_outline="",
    per_scene_synopsis_min=ABSOLUTE_MIN_WORDS,
    per_scene_synopsis_max=280,
    hard_ceiling=336,
    is_scene_organized=False,
    correction=None,
):
    """Build the generation prompt for one chapter."""
    # Scene-organized if format flag says so OR chapter carries a scene_type annotation
    if not is_scene_organized and chapter.annotations.get("scene_type"):
        is_scene_organized = True

    # Character profiles — NO TRUNCATION (per no-silent-failures policy)
    char_text = json.dumps(character_profiles, indent=2)
    CHAR_PROFILES_BUDGET = 30000
    if len(char_text) > CHAR_PROFILES_BUDGET:
        raise RuntimeError(
            f"character_profiles.json exceeds budget ({len(char_text)} > "
            f"{CHAR_PROFILES_BUDGET} chars). Truncation disabled per "
            f"no-silent-failures policy. Either increase budget or split "
            f"character_profiles into per-book scope."
        )

    # Series bible context — NO TRUNCATION
    bible_text = json.dumps(series_bible, indent=2)
    BIBLE_BUDGET = 12000
    if len(bible_text) > BIBLE_BUDGET:
        raise RuntimeError(
            f"series_bible.json exceeds budget ({len(bible_text)} > "
            f"{BIBLE_BUDGET} chars). Truncation disabled per "
            f"no-silent-failures policy."
        )

    # Hard constraints — extract and present as load-bearing
    hard_constraints = series_bible.get("hard_constraints", {})
    hc_text = ""
    if hard_constraints:
        hc_lines = [
            "",
            "========================================",
            "HARD CONSTRAINTS — VIOLATING ANY OF THESE IS A CLASS A FAILURE",
            "========================================",
            "These constraints are NON-NEGOTIABLE. The synopsis must honor every",
            "constraint in this block. Any synopsis content that violates these",
            "will fail the audit gate and require regeneration.",
            "",
        ]
        for key, value in hard_constraints.items():
            if isinstance(value, list):
                if value:
                    hc_lines.append(f"{key.upper()}:")
                    for item in value:
                        hc_lines.append(f"  - {item}")
                else:
                    hc_lines.append(f"{key.upper()}: (none)")
            elif isinstance(value, dict):
                hc_lines.append(f"{key.upper()}:")
                for k2, v2 in value.items():
                    hc_lines.append(f"  - {k2}: {v2}")
            else:
                hc_lines.append(f"{key.upper()}: {value}")
            hc_lines.append("")
        hc_lines.append("========================================")
        hc_lines.append("")
        hc_text = "\n".join(hc_lines)

    # Voice register from series_bible
    voice = series_bible.get("voice_register", {})
    voice_text = ""
    if voice:
        voice_text = f"""
VOICE REGISTER:
- Base: {voice.get('base_voice', 'Short declarative sentences, ground-level observation')}
- Intrusion: {voice.get('intrusion_voice', 'Extended sentences when thematically appropriate')}
- Allocation: {voice.get('intrusion_allocation', 'ACTION: minimal intrusion. NON-ACTION: moderate intrusion.')}
"""

    # Operational doctrine from series_bible
    doctrine = series_bible.get("operational_doctrine", [])
    doctrine_text = ""
    if doctrine:
        doctrine_text = "\nOPERATIONAL DOCTRINE (the unit follows these rules — synopsis must reflect them):\n"
        doctrine_text += "\n".join(f"- {d}" for d in doctrine)

    # Historical window
    hw = intake.get("historical_window", {})
    hw_text = f"\nHISTORICAL WINDOW: {hw.get('start_date', 'unspecified')} to {hw.get('end_date', 'unspecified')}"
    hw_text += "\nDo NOT include scenes or events outside this window."

    # Out-of-scope anchors
    oos = intake.get("historical_anchors_out_of_scope", [])
    if oos:
        hw_text += "\nOUT-OF-SCOPE EVENTS (do NOT reference): " + ", ".join(oos)

    # Scene type guidance from annotations (omit for UNKNOWN — no hint is better than "lean UNKNOWN")
    scene_type_hint = ""
    ann_type = chapter.annotations.get("scene_type", "")
    if ann_type and ann_type != "UNKNOWN":
        scene_type_hint = f"\nSCENE TYPE GUIDANCE: This chapter should lean {ann_type}."

    # MODE — from outline annotation or series_bible default
    mode_from_outline = (chapter.annotations or {}).get("mode")
    mode_default = series_bible.get("default_narrative_mode")
    scene_mode = mode_from_outline or mode_default  # None if neither set
    mode_hint = ""
    if scene_mode:
        mode_hint = f"\nNARRATIVE MODE: {scene_mode}"

    # FOCUS — mapped from outline [POV: ...] annotation (preserves verbatim)
    focus_from_outline = (chapter.annotations or {}).get("pov")
    focus_hint = ""
    if focus_from_outline:
        focus_hint = f"\nFOCUS (narrative anchor): {focus_from_outline}"

    # Anti-patterns from intake
    anti_patterns = intake.get("anti_patterns", [])
    ap_text = ""
    if anti_patterns:
        ap_text = "\nANTI-PATTERNS (do NOT include in synopsis prose):\n"
        ap_text += "\n".join(f"- {ap}" for ap in anti_patterns)

    # Prior chapter context
    context_text = ""
    if prior_chapter_synopsis:
        # Truncate to last 2000 chars for context
        truncated = prior_chapter_synopsis[-2000:] if len(prior_chapter_synopsis) > 2000 else prior_chapter_synopsis
        context_text = f"\nPRIOR CHAPTER SYNOPSIS (for continuity — do not repeat these events):\n{truncated}"

    # Next chapter outline peek
    next_text = ""
    if next_chapter_outline:
        next_text = f"\nNEXT CHAPTER OUTLINE (for setup — plant seeds but do not execute these beats):\n{next_chapter_outline[:1000]}"

    # ── Scene count guidance ───────────────────────────────────────────
    # Scene-organized inputs (one outline scene per ChapterSpec) produce
    # exactly one synopsis scene — no decomposition. Chapter-organized
    # inputs use the legacy beat-count heuristic for backward compat.
    # is_scene_organized is passed in from the outline format flag (top_matter),
    # NOT derived from per-scene type-tag presence (SG-4).
    beat_count = len(chapter.beats)

    if is_scene_organized:
        scene_guidance = "exactly 1 scene"
        target_scene_count = 1
    else:
        if beat_count <= 2:
            scene_guidance = "1-2 scenes"
            target_scene_count = 2
        elif beat_count <= 5:
            scene_guidance = "2-3 scenes"
            target_scene_count = 3
        elif beat_count <= 10:
            scene_guidance = "3-5 scenes"
            target_scene_count = 5
        else:
            scene_guidance = "4-6 scenes"
            target_scene_count = 6

    # ── Schema declaration (scene-organized only) ─────────────────────
    schema_block = ""
    if is_scene_organized:
        outline_type = chapter.annotations.get("scene_type", "UNKNOWN")
        schema_block = f"""
========================================
INPUT-OUTPUT SCHEMA
========================================

The operator outline you are processing is SCENE-ORGANIZED. Each input unit
represents ONE numbered scene from the operator's outline. Your output for
this unit must be EXACTLY ONE synopsis scene, with the same scene number as
the input.

Hierarchy:
- The outline contains numbered SCENES
- Each scene has a TYPE (ACTION, NON-ACTION, SUSPENSE, MIXED)
- Each scene contains BEATS (paragraph-level narrative actions)

You MUST NOT:
- Decompose one outline scene into multiple synopsis sub-scenes (e.g. "Scene 25.1, 25.2, 25.3")
- Generate fewer or more synopsis scenes than the outline scene count for this unit
- Change the scene number from the input
- Change the scene TYPE from the input

You MUST:
- Produce exactly {target_scene_count} synopsis scene(s) for this unit
- Preserve the scene number ({chapter.chapter_number})
- Preserve the scene TYPE ({outline_type})
- Cover every beat in the outline within this single synopsis scene's content
========================================

"""

    # ── Scene count and type instructions ─────────────────────────────
    if is_scene_organized:
        outline_type = chapter.annotations.get("scene_type", "UNKNOWN")
        scene_count_line = (
            f"SCENE COUNT: Generate EXACTLY 1 scene with number {chapter.chapter_number}. "
            f"Do NOT decompose into sub-scenes (no 'Scene N.1, N.2, N.3'). "
            f"Cover all {beat_count} outline beats within this single synopsis scene."
        )
        # V26: pillar marker from outline
        pillar_from_outline = (chapter.annotations or {}).get("pillar")
        pillar_tag = f" [PILLAR: {pillar_from_outline}]" if pillar_from_outline else ""
        type_line = (
            f"Tag with the outline-specified TYPE: ### Scene {chapter.chapter_number} — Title "
            f"[TYPE: {outline_type}]"
            f"{pillar_tag}"
            f"{f' [MODE: {scene_mode}]' if scene_mode else ''}"
            f"{f' [FOCUS: {focus_from_outline}]' if focus_from_outline else ''}"
        )
    else:
        scene_count_line = (
            f"SCENE COUNT: Generate {scene_guidance} for this chapter based on "
            f"the {beat_count} outline beats above."
        )
        type_line = "Tag each: ### Scene N — Title [TYPE: ACTION|MIXED|NON-ACTION] [PILLAR: <if provided>] [MODE: <if provided>] [FOCUS: <if provided>]"

    prompt = f"""{schema_block}Generate scenes for Chapter {chapter.chapter_number} of the synopsis.

OPERATOR OUTLINE FOR CHAPTER {chapter.chapter_number}:
{chapter.content}

OUTLINE BEATS TO COVER (every beat below MUST appear in your output):
{chr(10).join(f"  {i+1}. {beat}" for i, beat in enumerate(chapter.beats))}

{scene_count_line}
{type_line}

OUTPUT FORMAT FOR THIS SCENE SYNOPSIS:
- One bullet per beat from the scene outline
- 1-2 sentences per beat MAX
- Use sentence fragments where natural ('Withholds: X, Y, Z')
- No literary phrasing
- No paragraph-style prose
- Brief, terse, specific

Target: {per_scene_synopsis_min}-{per_scene_synopsis_max} words total for this scene.
Hard ceiling: {hard_ceiling} words.

If you find yourself writing in prose register (paragraphs, literary
description, thematic observation), STOP and rewrite that beat as a
terse instruction sentence.

CHARACTER PROFILES:
{char_text}

{hc_text}
SERIES CONTEXT:
{bible_text}
{voice_text}{doctrine_text}{hw_text}{scene_type_hint}{mode_hint}{focus_hint}{ap_text}
{principles_text}
{context_text}{next_text}

INSTRUCTIONS:
- Cover ALL {len(chapter.beats)} outline beats
- Do NOT invent events, characters, or locations not in the outline
- Do NOT reorder the outline's beats
- Generate {scene_guidance} based on the outline beat density
- {per_scene_synopsis_min}-{per_scene_synopsis_max} words total (hard ceiling: {hard_ceiling})
- Tag every scene with TYPE; include MODE and FOCUS when provided
- 1-2 sentences per outline beat — this is INSTRUCTION, not prose
- No atmospheric detail, no dialogue, no thematic interpretation
- Sentence fragments are preferred over full paragraphs

Begin with ### Scene {chapter.chapter_number if is_scene_organized else 1}"""

    if correction:
        prompt += f"""

=== AUDITOR-DRIVEN CORRECTION (this chapter only) ===
{correction}
=== END CORRECTION ===
Apply this correction while maintaining all other requirements above."""

    return prompt


# ── Targeted regeneration helpers ────────────────────────────────────────────

def _mark_chapters_for_regen(output_dir: str, chapter_numbers: list[int]) -> None:
    """Mark specified chapters as incomplete in state and delete their chapter files."""
    state_path = os.path.join(output_dir, STATE_FILE_NAME)
    chapters_dir = os.path.join(output_dir, CHAPTERS_DIR_NAME)

    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        for ch_num in chapter_numbers:
            ch_key = f"{ch_num:03d}"
            if ch_key in state.get("chapters", {}):
                state["chapters"][ch_key]["status"] = "incomplete"
                state["chapters"][ch_key]["attempts"] = 0

        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    # Delete chapter files
    for ch_num in chapter_numbers:
        ch_key = f"{ch_num:03d}"
        ch_file = os.path.join(chapters_dir, f"sc_{ch_key}.md")
        if os.path.exists(ch_file):
            os.remove(ch_file)
            print(f"  [targeted-regen] Deleted {ch_file}")

    print(f"  [targeted-regen] Marked chapters {chapter_numbers} for regeneration")


# ── Generator ────────────────────────────────────────────────────────────────

def generate_synopsis(
    outline_path: str,
    intake_path: str,
    series_bible_path: str,
    character_profiles_path: str,
    output_dir: str,
    regenerate_failed_chapters: bool = True,
    max_regeneration_attempts: int = 3,
    force_clean: bool = False,
    correction: str | None = None,
    targeted_chapters: list[int] | None = None,
    series_config_path: str = "",
):
    """Generate scene-by-scene synopsis from operator outline + inputs.

    Returns dict with synopsis_path, comparator_result, generation_log, tokens_used.
    """
    print(f"\n{'='*70}")
    print(f"  ANPD V25 — SYNOPSIS GENERATOR")
    print(f"{'='*70}")

    generation_log = []

    chapters_dir = os.path.join(output_dir, CHAPTERS_DIR_NAME)
    state_path = os.path.join(output_dir, STATE_FILE_NAME)

    # ── Force-clean if requested ──
    if force_clean:
        if os.path.isdir(chapters_dir):
            shutil.rmtree(chapters_dir)
        if os.path.exists(state_path):
            os.remove(state_path)
        for f in os.listdir(output_dir):
            if f == "synopsis.md" or (f.startswith("synopsis_") and f.endswith(".md")):
                os.remove(os.path.join(output_dir, f))
        print("  FORCE-CLEAN: All prior partial state removed. Starting fresh.")
        generation_log.append("Force-clean: all prior state removed")

    # ── Step 1: Validate intake ──
    print("\n  Step 1: Validating intake...")
    validation = validate_intake(intake_path)
    if not validation.passed:
        for e in validation.errors:
            print(f"    ERROR: {e}")
        raise RuntimeError(f"Intake validation failed: {validation.errors}")
    intake = validation.intake
    for w in validation.warnings:
        print(f"    WARNING: {w}")
    print("    Intake validated.")
    generation_log.append("Step 1: Intake validated")

    # ── Step 2: Parse outline ──
    print("\n  Step 2: Parsing outline...")
    outline = parse_outline(outline_path)
    content_chapters = [ch for ch in outline.chapters if ch.content.strip()]
    outline_is_scene_organized = outline.top_matter.get("format") == "scene-organized"
    print(f"    Parsed {len(content_chapters)} chapters with content (of {len(outline.chapters)} total, format={outline.top_matter.get('format', 'unknown')})")
    for ch in content_chapters:
        print(f"      Chapter {ch.chapter_number}: {len(ch.beats)} beats, {len(ch.content)} chars")
    generation_log.append(f"Step 2: Outline parsed — {len(content_chapters)} chapters with content")

    # ── Step 2b: Per-beat proportional sizing (D24) ──
    # Word targets are now computed per-chapter based on beat count.
    # Log the range across all chapters for visibility.
    beat_counts = [len(ch.beats) for ch in content_chapters]
    sample_min_lo, sample_max_lo, _ = compute_scene_word_target(min(beat_counts))
    sample_min_hi, sample_max_hi, _ = compute_scene_word_target(max(beat_counts))
    print(f"    Per-beat proportional sizing: {MIN_WORDS_PER_BEAT}-{MAX_WORDS_PER_BEAT} words/beat")
    print(f"    Range across chapters: {sample_min_lo}-{sample_max_lo} (lowest beat ch) to {sample_min_hi}-{sample_max_hi} (highest beat ch)")
    generation_log.append(f"Step 2b: Per-beat sizing {MIN_WORDS_PER_BEAT}-{MAX_WORDS_PER_BEAT} w/beat, range {sample_min_lo}-{sample_max_lo} to {sample_min_hi}-{sample_max_hi}")

    # ── Step 3: Load series_bible + character_profiles ──
    print("\n  Step 3: Loading series bible and character profiles...")
    with open(series_bible_path, 'r', encoding='utf-8') as f:
        series_bible = json.load(f)
    with open(character_profiles_path, 'r', encoding='utf-8') as f:
        character_profiles = json.load(f)
    print(f"    Series bible loaded: {len(series_bible)} keys")
    print(f"    Character profiles loaded: {len(character_profiles.get('characters', character_profiles))} entries")
    generation_log.append("Step 3: Series bible + character profiles loaded")

    # ── Step 4: Load craft principles ──
    print("\n  Step 4: Loading craft principles...")
    principles_path = intake.get("craft_principles_path")
    if principles_path and not os.path.isabs(principles_path):
        principles_path = os.path.join(os.path.dirname(os.path.abspath(intake_path)), principles_path)
    try:
        principles_text = load_principles_for_component(
            "synopsis_generator",
            scope_filter=["GENERIC", "WAR-FICTION"],
            principles_path=principles_path,
        )
        print(f"    Principles loaded: {len(principles_text)} chars")
    except FileNotFoundError:
        print("    No craft_principles.json found — proceeding without principles")
        principles_text = ""
    generation_log.append("Step 4: Craft principles loaded")

    # ── Step 4b: Resume detection ──
    title = intake.get("title", "Unknown")
    model = os.environ.get("V25_MODEL", DEFAULT_MODEL)
    total_chapters = len(content_chapters)
    input_hashes = _compute_input_hashes(
        outline_path, intake_path, series_bible_path, character_profiles_path
    )

    completed_chapters = set()
    state = _load_state(state_path)

    if state is not None:
        # Compare hashes
        old_hashes = state.get("input_hashes", {})
        mismatches = []
        for key in input_hashes:
            if old_hashes.get(key) != input_hashes[key]:
                mismatches.append(
                    f"  {key}: {old_hashes.get(key, 'missing')[:12]}... → {input_hashes[key][:12]}..."
                )
        if mismatches:
            print("\n  HASH MISMATCH — input files changed since last run:")
            for m in mismatches:
                print(m)
            print("  REFUSED TO RESUME — inputs changed. Use --force-clean to start")
            print("  over, or revert the input changes to resume.")
            sys.exit(2)

        # Validate completed chapters have files on disk
        for ch_key, ch_data in state.get("chapters", {}).items():
            if ch_data.get("status") == "completed":
                ch_file = os.path.join(chapters_dir, f"sc_{ch_key}.md")
                if os.path.exists(ch_file):
                    completed_chapters.add(int(ch_key))
                else:
                    ch_data["status"] = "incomplete"
                    ch_data["attempts"] = 0
                    print(f"    WARNING: Chapter {ch_key} marked complete but sc_{ch_key}.md missing — will regenerate")

        if completed_chapters:
            prior_cost = state["totals"]["total_cost_usd"]
            prior_wall = state["totals"]["total_wall_seconds"]
            first_incomplete = None
            for ch in content_chapters:
                if ch.chapter_number not in completed_chapters:
                    first_incomplete = ch.chapter_number
                    break
            if first_incomplete is None:
                print(f"\n  RESUME: {len(completed_chapters)} of {total_chapters} chapters already complete")
                print(f"  PRIOR COST: ${prior_cost:.2f} ({prior_wall/60:.1f} min)")
                print("  All chapters complete — skipping to assembly.")
                generation_log.append(f"Resume: all {len(completed_chapters)} chapters already complete")
            else:
                print(f"\n  RESUME: {len(completed_chapters)} of {total_chapters} chapters already complete, resuming from chapter {first_incomplete:03d}")
                print(f"  PRIOR COST: ${prior_cost:.2f} ({prior_wall/60:.1f} min)")
                generation_log.append(f"Resume: {len(completed_chapters)} complete, resuming from {first_incomplete:03d}")
    else:
        # Fresh run
        state = _new_state(input_hashes, model, total_chapters)
        _atomic_write_json(state_path, state)
        generation_log.append("Fresh run: state file created")

    # Ensure chapters directory exists
    os.makedirs(chapters_dir, exist_ok=True)

    # ── Step 5: Generate per-chapter ──
    chapters_to_generate = [
        ch for ch in content_chapters
        if ch.chapter_number not in completed_chapters
    ]
    print(f"\n  Step 5: Generating synopsis ({len(chapters_to_generate)} chapters to generate, {len(completed_chapters)} already done)...")
    print(f"    Model: {model}")

    # Build prior_synopsis from last completed chapter before the first incomplete one
    prior_synopsis = ""
    if completed_chapters:
        # Find the highest completed chapter before the first incomplete
        sorted_completed = sorted(completed_chapters)
        if sorted_completed:
            last_key = f"{sorted_completed[-1]:03d}"
            last_file = os.path.join(chapters_dir, f"sc_{last_key}.md")
            if os.path.exists(last_file):
                with open(last_file, "r", encoding="utf-8") as f:
                    prior_synopsis = f.read()

    for idx, chapter in enumerate(content_chapters):
        ch_num = chapter.chapter_number
        ch_key = f"{ch_num:03d}"

        if ch_num in completed_chapters:
            # Load from disk for prior_synopsis continuity
            ch_file = os.path.join(chapters_dir, f"sc_{ch_key}.md")
            with open(ch_file, "r", encoding="utf-8") as f:
                prior_synopsis = f.read()
            continue

        # Per-beat proportional sizing for this chapter
        scene_min, scene_max, scene_ceiling = compute_scene_word_target(len(chapter.beats))
        api_max_tokens = int(scene_ceiling * 2.0)  # tokens — 2.0x ceiling for dense-scene headroom

        print(f"    Generating Chapter {ch_num} ({len(chapter.beats)} beats, target {scene_min}-{scene_max}w, api_max_tokens {api_max_tokens})...", end='', flush=True)
        start_time = time.time()

        # Next chapter outline for setup planting
        next_outline = ""
        if idx + 1 < len(content_chapters):
            next_outline = content_chapters[idx + 1].content[:1000]

        # Inject correction for targeted chapters only
        chapter_correction = None
        if correction and targeted_chapters and ch_num in targeted_chapters:
            chapter_correction = correction

        prompt = build_chapter_prompt(
            chapter=chapter,
            intake=intake,
            series_bible=series_bible,
            character_profiles=character_profiles,
            principles_text=principles_text,
            prior_chapter_synopsis=prior_synopsis,
            next_chapter_outline=next_outline,
            per_scene_synopsis_min=scene_min,
            per_scene_synopsis_max=scene_max,
            hard_ceiling=scene_ceiling,
            is_scene_organized=outline_is_scene_organized,
            correction=chapter_correction,
        )

        # Per-chapter comparator regen loop
        # The comparator now supports per-scene scope for scene-organized
        # outlines — no longer bypassed. Beat-coverage findings are Class B
        # (informational) and do not trigger regeneration; only Class A
        # structural failures drive regen.
        effective_max_regen = max_regeneration_attempts

        best_text = None
        best_findings = 999
        comparator_passed = False
        total_attempts = 0
        total_input_tokens = 0
        total_output_tokens = 0

        for regen_attempt in range(effective_max_regen + 1):
            try:
                response = call_api(SYSTEM_PROMPT, prompt, model=model, max_tokens=api_max_tokens)
                text = response.text
                total_input_tokens += response.input_tokens
                total_output_tokens += response.output_tokens
                total_attempts += 1
            except Exception as e:
                print(f" FAILED ({e})")
                generation_log.append(f"Chapter {ch_num}: FAILED — {e}")
                total_attempts += 1
                break

            # Write chapter to disk immediately (even before comparator)
            _atomic_write(os.path.join(chapters_dir, f"sc_{ch_key}.md"), text)

            # Run comparator on this chapter
            if regen_attempt < max_regeneration_attempts:
                try:
                    # Write temp synopsis for comparator
                    tmp_synopsis = os.path.join(output_dir, f".comparator_tmp_{ch_key}.md")
                    ch_header = f"## Chapter {ch_num}\n\n"
                    _atomic_write(tmp_synopsis, ch_header + text)

                    comp = compare_outline_to_synopsis(
                        outline_path=outline_path,
                        synopsis_path=tmp_synopsis,
                        intake_path=intake_path,
                        principles_path=principles_path,
                        use_llm=True,
                    )
                    os.remove(tmp_synopsis)

                    # Judge this chapter on ITS OWN per-scene result, not the
                    # global comp.passed. The comparator is handed the full
                    # outline vs a single-chapter temp synopsis, so the global
                    # result is polluted by "scene N missing" Class A findings
                    # for every other scene. comp.chapter_results[ch_num] holds
                    # the Class-A-only pass status for THIS scene.
                    scene_result = comp.chapter_results.get(ch_num) if hasattr(comp, 'chapter_results') and comp.chapter_results else None
                    scene_findings = scene_result.findings if scene_result else comp.findings
                    n_findings = len(scene_findings)
                    if n_findings < best_findings:
                        best_findings = n_findings
                        best_text = text

                    if scene_result is not None and scene_result.passed:
                        comparator_passed = True
                        best_text = text
                        break

                    # Build regen feedback for next attempt
                    if hasattr(comp, 'chapter_results') and comp.chapter_results:
                        # Get missed beats from comparator
                        missed = []
                        for cr in comp.chapter_results.values():
                            if hasattr(cr, 'beat_coverage'):
                                missed = [b for b, c in cr.beat_coverage.items() if not c]
                                break
                        if missed:
                            feedback = f"\nPREVIOUS GENERATION FAILED. The following beats were NOT covered:\n"
                            feedback += "\n".join(f"  - {b}" for b in missed)
                            feedback += "\n\nYou MUST cover ALL of these beats in the regenerated output.\n"
                            prompt = build_chapter_prompt(
                                chapter=chapter,
                                intake=intake,
                                series_bible=series_bible,
                                character_profiles=character_profiles,
                                principles_text=principles_text + feedback,
                                per_scene_synopsis_min=scene_min,
                                per_scene_synopsis_max=scene_max,
                                hard_ceiling=scene_ceiling,
                                is_scene_organized=outline_is_scene_organized,
                            )

                except Exception as comp_err:
                    # Comparator failed — accept the chapter as-is
                    best_text = text
                    comparator_passed = False
                    generation_log.append(f"Chapter {ch_num}: comparator error — {comp_err}")
                    break
            else:
                # Last attempt — keep best
                if best_text is None:
                    best_text = text

        elapsed = time.time() - start_time

        if best_text is None:
            # All attempts failed — mark as failed
            ch_data = {
                "status": "failed",
                "attempts": total_attempts,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "cost_usd": round(_calc_cost(model, total_input_tokens, total_output_tokens), 4),
                "wall_seconds": round(elapsed, 1),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "comparator_passed": False,
                "comparator_findings": -1,
            }
            state = _update_chapter_state(state, ch_key, ch_data)
            _atomic_write_json(state_path, state)
            print(f" FAILED after {total_attempts} attempts ({elapsed:.0f}s)")
            generation_log.append(f"Chapter {ch_num}: FAILED after {total_attempts} attempts")
            continue

        # Write best result to disk
        _atomic_write(os.path.join(chapters_dir, f"sc_{ch_key}.md"), best_text)

        word_count = len(best_text.split())
        cost = _calc_cost(model, total_input_tokens, total_output_tokens)
        print(f" {elapsed:.0f}s — {word_count:,} words — ${cost:.4f} — {'PASS' if comparator_passed else f'{best_findings} findings'}")

        ch_data = {
            "status": "completed",
            "attempts": total_attempts,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": round(cost, 4),
            "wall_seconds": round(elapsed, 1),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "comparator_passed": comparator_passed,
            "comparator_findings": best_findings if best_findings < 999 else 0,
        }
        state = _update_chapter_state(state, ch_key, ch_data)
        _atomic_write_json(state_path, state)

        prior_synopsis = best_text
        generation_log.append(f"Chapter {ch_num}: {word_count} words, {total_attempts} attempts, ${cost:.4f}, {elapsed:.0f}s")

    # ── Step 6: Assemble full synopsis from disk ──
    print("\n  Step 6: Assembling synopsis from chapter files...")
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    synopsis_filename = f"synopsis_{timestamp}.md"
    synopsis_path = os.path.join(output_dir, synopsis_filename)

    header = f"# Synopsis — {title}\nGenerated: {timestamp}\nOutline: {outline_path}\n\n"
    full_synopsis = header
    _prev_disp_ch = 0  # 4 scenes per chapter
    for ch in content_chapters:
        ch_num = ch.chapter_number
        ch_key = f"{ch_num:03d}"
        ch_file = os.path.join(chapters_dir, f"sc_{ch_key}.md")

        _disp_ch = (ch_num - 1) // 4 + 1  # mechanical: 4 scenes per chapter
        if _disp_ch != _prev_disp_ch:
            full_synopsis += f"\n## Chapter {_disp_ch}\n\n"
            _prev_disp_ch = _disp_ch

        ch_state = state.get("chapters", {}).get(ch_key, {})
        if os.path.exists(ch_file) and ch_state.get("status") != "failed":
            with open(ch_file, "r", encoding="utf-8") as f:
                full_synopsis += f.read() + "\n\n"
        elif ch_state.get("status") == "failed":
            full_synopsis += f"[CHAPTER {ch_num} GENERATION FAILED — see state file for details]\n\n"
        else:
            full_synopsis += f"[CHAPTER {ch_num} NOT GENERATED]\n\n"

    _atomic_write(synopsis_path, full_synopsis)

    # ── Verify outline-fidelity before declaring success ──
    from outline_comparator import verify_scene_count_match
    passed, message = verify_scene_count_match(outline_path, synopsis_path)
    if not passed:
        print(f"\n  FAIL: {message}")
        raise RuntimeError(f"Synopsis fidelity check failed: {message}")
    print(f"    {message}")

    # Write canonical synopsis.md
    canonical_path = os.path.join(output_dir, "synopsis.md")
    _atomic_write(canonical_path, full_synopsis)

    total_words = len(full_synopsis.split())
    print(f"    Synopsis saved: {synopsis_path} ({total_words:,} words)")
    print(f"    Canonical copy: {canonical_path}")
    generation_log.append(f"Step 6: Synopsis assembled — {total_words} words")

    # ── Print cost summary ──
    totals = state.get("totals", {})
    print(f"\n  COST SUMMARY:")
    print(f"    Total input tokens:  {totals.get('total_input_tokens', 0):,}")
    print(f"    Total output tokens: {totals.get('total_output_tokens', 0):,}")
    print(f"    Total cost:          ${totals.get('total_cost_usd', 0):.4f}")
    print(f"    Total wall time:     {totals.get('total_wall_seconds', 0)/60:.1f} min")

    # ── Save receipt ──
    comparator_result = None
    receipt = {
        "component": "v25_synopsis_generator",
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "title": title,
        "series": intake.get("series", "Unknown"),
        "book_number": intake.get("book_number", 0),
        "model": model,
        "outline_path": outline_path,
        "chapters_generated": totals.get("completed_chapters", 0),
        "total_words": total_words,
        "total_cost_usd": totals.get("total_cost_usd", 0),
        "generation_log": generation_log,
    }
    receipt_path = os.path.join(output_dir, 'synopsis_generator_receipt.json')
    with open(receipt_path, 'w', encoding='utf-8') as f:
        json.dump(receipt, f, indent=2)

    # ── Run synopsis_auditor (SA-1: rubric checks + integrity) ──
    print("\n  Step 7: Running synopsis_auditor (12 rubric checks + integrity)...")
    from synopsis_auditor import audit_synopsis
    if series_config_path:
        series_dir = os.path.dirname(series_config_path)
    else:
        series_dir = os.path.dirname(os.path.dirname(canonical_path))
    try:
        audit_result = audit_synopsis(
            synopsis_path=canonical_path,
            intake_path=intake_path,
            series_dir=series_dir,
            series_config_path=series_config_path,
        )
    except Exception as e:
        print(f"    WARNING: synopsis_auditor failed to run: {e}")
        audit_result = {"verdict": "ERROR", "fails": [], "weaks": [], "total_scenes": 0}

    audit_verdict = audit_result.get("verdict", "UNKNOWN")
    if audit_verdict == "FAIL":
        fails = audit_result.get("fails", [])
        print(f"    FAIL: synopsis_auditor verdict=FAIL on: {fails}")
        raise RuntimeError(
            f"Synopsis audit failed on: {fails}. "
            f"Reports at {output_dir}/synopsis_audit_report.json. "
            f"Pipeline halt — operator review required."
        )
    elif audit_verdict == "WEAK":
        weaks = audit_result.get("weaks", [])
        print(f"    WEAK: synopsis_auditor flagged (non-blocking): {weaks}")
    elif audit_verdict == "PASS":
        print(f"    PASS: synopsis_auditor verdict=PASS "
              f"(scenes parsed: {audit_result.get('total_scenes', '?')})")
    else:
        # Fix B: ERROR / UNKNOWN / unrecognized verdict means the synopsis was NOT audited. Halt.
        error_detail = audit_result.get("error") or "no error detail captured"
        print(f"    {audit_verdict}: synopsis_auditor verdict={audit_verdict} "
              f"(scenes parsed: {audit_result.get('total_scenes', '?')})")
        raise RuntimeError(
            f"Synopsis audit did not complete (verdict={audit_verdict}); synopsis was NOT audited. "
            f"Auditor error: {error_detail}. Pipeline halt — operator review required."
        )

    print(f"\n{'='*70}")
    print(f"  V25 SYNOPSIS GENERATOR COMPLETE")
    print(f"  Output: {synopsis_path}")
    print(f"  Cost: ${totals.get('total_cost_usd', 0):.4f}")
    print(f"{'='*70}\n")

    return {
        "synopsis_path": synopsis_path,
        "comparator_result": comparator_result,
        "generation_log": generation_log,
        "tokens_used": totals.get("total_input_tokens", 0) + totals.get("total_output_tokens", 0),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='ANPD V25 Synopsis Generator')
    parser.add_argument('--outline', required=True, help='Path to operator outline (PDF, markdown, or docx)')
    parser.add_argument('--intake', required=True, help='Path to intake.json')
    parser.add_argument('--series-bible', required=True, help='Path to series_bible.json')
    parser.add_argument('--character-profiles', required=True, help='Path to character_profiles.json')
    parser.add_argument('--output-dir', required=True, help='Output directory')
    parser.add_argument('--no-regenerate', action='store_true', help='Skip auto-regeneration of failed chapters')
    parser.add_argument('--max-regen-attempts', type=int, default=3, help='Max regeneration attempts (default: 3)')
    parser.add_argument('--force-clean', action='store_true', help='Delete all prior partial state and start fresh')
    parser.add_argument('--chapters', type=str, default=None,
                        help='Comma-separated chapter numbers to regenerate (e.g. "97,98,99,100")')
    parser.add_argument('--correction', type=str, default=None,
                        help='Correction text injected into the prompt for targeted chapters only')
    parser.add_argument('--series-config', type=str, default="",
                        help='Path to series_config.json (passed to embedded auditor)')
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # Handle targeted regeneration: mark specified chapters as incomplete
    targeted_chapters = None
    if args.chapters:
        targeted_chapters = [int(x.strip()) for x in args.chapters.split(',')]
        _mark_chapters_for_regen(args.output_dir, targeted_chapters)

    try:
        result = generate_synopsis(
            outline_path=args.outline,
            intake_path=args.intake,
            series_bible_path=args.series_bible,
            character_profiles_path=args.character_profiles,
            output_dir=args.output_dir,
            regenerate_failed_chapters=not args.no_regenerate,
            max_regeneration_attempts=args.max_regen_attempts,
            force_clean=args.force_clean,
            correction=args.correction,
            targeted_chapters=targeted_chapters,
            series_config_path=args.series_config,
        )
    except Exception as e:
        print(f"\n  FATAL ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
