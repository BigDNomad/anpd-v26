#!/usr/bin/env python3
# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify master_controller.py to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""character_generator.py — Phase 3 Character Profile Generator

ANPD V24 | Produces book-level character_profiles.json from book_config + series profiles.

Per Character Profile Schema v1.1.0 §3.2: book-specific character profiles
file holds antagonist + supporting cast for one book. Recurring characters
are inherited from the series-level file at runtime; the protagonist is
defined at series level.

This component generates ONLY antagonist and supporting characters. It does
not generate or modify protagonist, recurring, or any other series-level
character profile.

Per Book Config Schema v0.1: input artifact is `book_config.json`, authored
by Dave per book at `/anpd/v25/series/{series}/{bNN}/work/book_config.json`.

Anti-pattern-matching discipline (per Character Profile Schema §4):
  Prompts contain NO populated character examples. The generator is
  instructed to derive each character from book_config + series profiles
  alone, not from any prior fiction or training-data exemplars.

Phase 3f Commit 3 scope: skeleton + single-pass generation + auditor integration
+ bounded retry on Class A findings.

After audit, if Class A findings are present, the generator builds a corrective
prompt (showing the model its prior output and the specific findings it failed)
and retries up to N times (default 2). Each retry overwrites the prior attempt's
output file. If after N retries Class A findings remain, write STOP_REPORT with
the full retry trail and exit 1.

Input:
  - book_config.json (per Book Config Schema v0.1)
  - series_config.json (for effective_config)
  - series-level character_profiles.json (read for cross-checks, not modified)
  - series_bible.json (read for protagonist context, not modified)
  - banned_phrases.json (for name validation; loaded via effective_config)

Output:
  - {book_dir}/work/character_profiles_{YYYYMMDD_HHMM}.json
  - {book_dir}/work/character_profiles.json (canonical symlink)
  - {book_dir}/out/reports/STOP_REPORT.json on Class A failures

Usage:
    python3 character_generator.py \\
      --book-config <path/to/book_config.json> \\
      --series-config <path/to/series_config.json> \\
      --series-dir <path/to/series_dir>

Copyright (c) 2026 Endeavor Publishing LLC
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from config_resolver import resolve_config
from character_profile_auditor import audit_character_profiles
from findings import serialize_findings


# ── File loaders ──────────────────────────────────────────────────────────────

def find_latest(directory, pattern):
    """Return the most-recently-modified file matching pattern in directory, or None."""
    matches = glob.glob(os.path.join(directory, pattern))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def load_json(path):
    with open(path) as f:
        return json.load(f)


def clean_json_response(raw):
    """Strip markdown fences and parse JSON from model response."""
    clean = raw.strip()
    if clean.startswith('```'):
        clean = '\n'.join(clean.split('\n')[1:])
    if clean.endswith('```'):
        clean = '\n'.join(clean.split('\n')[:-1])
    clean = clean.strip()
    return json.loads(clean)


# ── Path derivation ───────────────────────────────────────────────────────────

def derive_book_dir(book_config_path):
    """Derive {book_dir} from book_config.json path.

    Per Book Config Schema v0.1 §1: book_config.json lives at
    {book_dir}/work/book_config.json. Strip the /work/book_config.json
    suffix to get book_dir.

    If book_config is not in a directory named 'work', fall back to the
    parent of the containing directory (best-effort) so STOP_REPORT can
    still be written somewhere predictable.
    """
    abspath = os.path.abspath(book_config_path)
    work_dir = os.path.dirname(abspath)
    if os.path.basename(work_dir) == 'work':
        return os.path.dirname(work_dir)
    return os.path.dirname(work_dir)


# ── Series artifact loading ───────────────────────────────────────────────────

def load_series_artifacts(series_dir):
    """Load series_bible.json and series-level character_profiles.json.

    Returns a dict with keys 'series_bible' and 'series_profiles'.

    Raises FileNotFoundError if series_bible.json is absent (always required).
    Returns empty dict for series_profiles if no character_profiles file is
    found (a new series may not have one yet).
    """
    bible_path = find_latest(series_dir, '*series_bible*.json')
    if not bible_path:
        bible_path = os.path.join(series_dir, 'series_bible.json')
    if not os.path.exists(bible_path):
        raise FileNotFoundError(
            f"series_bible.json not found in {series_dir} "
            f"(searched both glob *series_bible*.json and exact filename)"
        )
    series_bible = load_json(bible_path)

    profiles_path = find_latest(series_dir, '*character_profiles*.json')
    series_profiles = load_json(profiles_path) if profiles_path else {}

    return {
        'series_bible': series_bible,
        'series_profiles': series_profiles,
    }


# ── Preflight validation ──────────────────────────────────────────────────────

def validate_book_config_preflight(book_config, series_artifacts, banned_data, effective_series_dir):
    """Preflight validation per Book Config Schema v0.1 §6.

    Returns (errors, warnings) where errors are Class A (block generation)
    and warnings are Class B (proceed but flag).

    `effective_series_dir` is the directory used for I/O (typically args.series_dir);
    book_config.series_directory must point to the same place after path normalization.
    """
    errors = []
    warnings = []

    # Required top-level fields
    for field in ('title', 'book_number', 'series_directory', 'antagonist_concept', 'supporting_cast_needs'):
        if field not in book_config:
            errors.append(f"Missing required field: {field}")

    # antagonist_concept structure
    ac = book_config.get('antagonist_concept', {})
    if not isinstance(ac, dict):
        errors.append("antagonist_concept must be an object")
    else:
        core_threat = ac.get('core_threat')
        if not core_threat or not isinstance(core_threat, str) or not core_threat.strip():
            errors.append(
                "antagonist_concept.core_threat is required "
                "(one-sentence threat statement, non-empty string)"
            )

    # book_number positive integer
    bn = book_config.get('book_number')
    if bn is not None and (not isinstance(bn, int) or bn < 1 or isinstance(bn, bool)):
        errors.append(f"book_number must be a positive integer, got {bn!r}")

    # series_directory consistency
    sd = book_config.get('series_directory')
    if sd:
        if not os.path.isdir(sd):
            errors.append(f"series_directory does not exist: {sd}")
        else:
            # Required series-level artifacts
            for fname in ('series_bible.json', 'series_config.json'):
                if not os.path.exists(os.path.join(sd, fname)):
                    errors.append(f"{fname} not found in series_directory: {sd}")
            # character_profiles.json may be absent for new series — warn only
            if not os.path.exists(os.path.join(sd, 'character_profiles.json')):
                warnings.append(
                    f"character_profiles.json not found in series_directory: {sd} "
                    f"(acceptable for a new series; the generator will proceed with no series-level cast)"
                )
        # Consistency check vs CLI --series-dir
        if effective_series_dir and os.path.abspath(sd) != os.path.abspath(effective_series_dir):
            errors.append(
                f"book_config.series_directory ({os.path.abspath(sd)}) does not match "
                f"--series-dir ({os.path.abspath(effective_series_dir)})"
            )

    # Banned-name checks
    banned_names_lower = {n.lower() for n in (banned_data.get('names', []) if banned_data else [])}
    if isinstance(ac, dict) and ac.get('name'):
        if ac['name'].lower() in banned_names_lower:
            errors.append(
                f"antagonist_concept.name {ac['name']!r} appears in banned_phrases.json"
            )

    sup_needs = book_config.get('supporting_cast_needs', [])
    if isinstance(sup_needs, list):
        for i, sup in enumerate(sup_needs):
            if not isinstance(sup, dict):
                errors.append(f"supporting_cast_needs[{i}] must be an object")
                continue
            nf = sup.get('narrative_function')
            if not nf or not isinstance(nf, str) or not nf.strip():
                errors.append(
                    f"supporting_cast_needs[{i}].narrative_function is required "
                    f"(non-empty string per Book Config Schema §3.5)"
                )
            if sup.get('name') and sup['name'].lower() in banned_names_lower:
                errors.append(
                    f"supporting_cast_needs[{i}].name {sup['name']!r} appears in banned_phrases.json"
                )

    # do_not_appear referent check (typo guard)
    series_profile_names = set(series_artifacts['series_profiles'].keys())
    for name in book_config.get('do_not_appear', []):
        if name not in series_profile_names:
            errors.append(
                f"do_not_appear name {name!r} is not a recurring character "
                f"in series-level character_profiles.json (typo guard)"
            )

    # recurring_appearances referent check
    if 'recurring_appearances' in book_config:
        recurring_names = {
            name for name, profile in series_artifacts['series_profiles'].items()
            if profile.get('character_role') == 'recurring'
        }
        for name in book_config.get('recurring_appearances', []):
            if name not in recurring_names:
                errors.append(
                    f"recurring_appearances name {name!r} is not a recurring character "
                    f"in series-level character_profiles.json"
                )

    # Warnings
    if 'supporting_cast_needs' in book_config and isinstance(book_config['supporting_cast_needs'], list) and len(book_config['supporting_cast_needs']) == 0:
        warnings.append("supporting_cast_needs is empty — book has no supporting cast (unusual but permitted)")
    if not book_config.get('book_subtext') and not book_config.get('protagonist_per_book_drive'):
        warnings.append(
            "book_subtext and protagonist_per_book_drive both omitted — "
            "generator falls back on series-level only"
        )

    return errors, warnings


# ── API client (streaming) ────────────────────────────────────────────────────

def call_sonnet_streaming(prompt, system_prompt, model, max_tokens=12000):
    """Call the Anthropic API with streaming for character generation via llm_client.

    Streaming keeps the connection alive for long generations (>1 min)
    that can time out with non-streaming requests.
    """
    from llm_client import call_llm
    response = call_llm(
        provider="anthropic",
        model=model,
        system=system_prompt,
        user=prompt,
        max_tokens=max_tokens,
        stream=True,
    )
    return response.text


# ── Prompt building (anti-pattern-matching discipline) ────────────────────────

def build_corrective_prompt(prior_cast, class_a_findings, original_user_prompt):
    """Build a corrective user prompt for a retry attempt.

    Shows the model its prior output and the specific findings it failed,
    instructs it to fix those findings while preserving everything else.
    The system prompt is unchanged from the original generation call (same
    anti-pattern-matching discipline applies).

    Args:
        prior_cast: dict — the previous attempt's generated cast (will be
            shown to the model as JSON)
        class_a_findings: list of finding dicts — the Class A findings the
            previous attempt failed
        original_user_prompt: str — the original generation prompt, included
            verbatim so the model has the full original context

    Returns:
        str: corrective user prompt
    """
    findings_lines = []
    for f in class_a_findings:
        finding_id = f.get('finding_id', '?')
        pass_name = f.get('pass_name', '?')
        description = f.get('description', '?')
        suggested_fix = f.get('suggested_fix', '?')
        findings_lines.append(
            f"- {finding_id} ({pass_name}): {description}\n"
            f"    Suggested fix: {suggested_fix}"
        )
    findings_block = "\n".join(findings_lines)

    prior_cast_json = json.dumps(prior_cast, indent=2, ensure_ascii=False)

    corrective_prompt = f"""Your previous attempt produced character profiles that failed the following Class A audit checks:

{findings_block}

Your previous output was:

{prior_cast_json}

Produce a corrected version that fixes the listed Class A findings while preserving everything else that was correct. Use the suggested fixes as guidance. The original generation requirements are below; honor them all.

ORIGINAL REQUIREMENTS:

{original_user_prompt}"""

    return corrective_prompt


def build_generation_prompt(book_config, series_bible, series_profiles, effective_config):
    """Build the system + user prompt for character generation.

    Per Character Profile Schema §4 anti-pattern-matching discipline:
      - NO populated character examples in either prompt
      - Field requirements conveyed as prose constraints
      - Explicit instruction not to derive characters from prior fiction
      - Series context provided for cross-reference and exclusion (recurring
        characters, protagonist) but never as patterns to copy

    Returns (system_prompt, user_prompt) tuple.
    """

    # Series context: protagonist (defined; do not modify) + appearing recurring chars
    series_context_parts = []

    protagonist_entry = None
    for name, profile in series_profiles.items():
        if profile.get('character_role') == 'protagonist':
            protagonist_entry = (name, profile)
            break

    if protagonist_entry:
        name, profile = protagonist_entry
        series_context_parts.append(
            f"PROTAGONIST (already defined at series level — DO NOT modify or include in output):\n"
            f"  Name: {name}\n"
            f"  Primary trait: {profile.get('primary_trait', '?')}\n"
            f"  Secondary trait: {profile.get('secondary_trait', '?')}\n"
            f"  Psychological wound: {profile.get('psychological_wound', '?')}\n"
            f"  Character purpose: {profile.get('character_purpose', '?')}\n"
            f"  Plot flaw connection: {profile.get('plot_flaw_connection', '?')}"
        )

    # Recurring characters that may appear in this book (for relationships awareness)
    recurring = {
        name: profile for name, profile in series_profiles.items()
        if profile.get('character_role') == 'recurring'
    }
    do_not_appear = set(book_config.get('do_not_appear', []))
    recurring_appearances = book_config.get('recurring_appearances')  # may be None

    appearing_recurring = {}
    for name, profile in recurring.items():
        if name in do_not_appear:
            continue
        if recurring_appearances is not None and name not in recurring_appearances:
            continue
        appearing_recurring[name] = profile

    if appearing_recurring:
        lines = ["RECURRING CHARACTERS THAT MAY APPEAR (already defined; DO NOT include in output, but may be referenced in `relationships`):"]
        for name, profile in appearing_recurring.items():
            lines.append(f"  - {name} (role: {profile.get('narrative_function', 'recurring')})")
        series_context_parts.append("\n".join(lines))

    series_context = "\n\n".join(series_context_parts) if series_context_parts else "(no series context provided)"

    # Antagonist seed
    ac = book_config['antagonist_concept']
    antagonist_seed_lines = [f"  Core threat: {ac['core_threat']}"]
    if ac.get('name'):
        antagonist_seed_lines.append(f"  Name (provided, use verbatim): {ac['name']}")
    else:
        antagonist_seed_lines.append("  Name: (not provided — propose a culturally and contextually appropriate name)")
    if ac.get('world_position'):
        antagonist_seed_lines.append(f"  World position: {ac['world_position']}")
    if ac.get('notes'):
        antagonist_seed_lines.append(f"  Additional creative direction: {ac['notes']}")
    antagonist_seed = "\n".join(antagonist_seed_lines)

    # Supporting cast seeds
    supporting_seed_lines = []
    for i, sup in enumerate(book_config.get('supporting_cast_needs', []), 1):
        line = f"  Supporting character {i}:\n    Narrative function: {sup['narrative_function']}"
        if sup.get('name'):
            line += f"\n    Name (provided, use verbatim): {sup['name']}"
        else:
            line += "\n    Name: (not provided — propose a culturally and contextually appropriate name)"
        if sup.get('notes'):
            line += f"\n    Additional creative direction: {sup['notes']}"
        supporting_seed_lines.append(line)
    supporting_seed = "\n\n".join(supporting_seed_lines) if supporting_seed_lines else "  (no supporting cast — antagonist only)"

    # Banned names list
    banned_names = effective_config.get('banned_names', [])
    banned_list = ", ".join(repr(n) for n in banned_names) if banned_names else "(none)"

    # Optional fields
    book_subtext = book_config.get('book_subtext', '')
    per_book_drive = book_config.get('protagonist_per_book_drive', '')

    # System prompt — anti-pattern-matching discipline
    system_prompt = (
        "You are a character profile generator for novel production. You generate "
        "character profiles in the ANPD V24 Character Profile Schema v1.1.0 format. "
        "You return ONLY a JSON object, with no preamble, explanation, or markdown fencing.\n"
        "\n"
        "ANTI-PATTERN-MATCHING DISCIPLINE (CRITICAL):\n"
        "Do NOT derive characters from any prior fiction or training-data exemplars. "
        "Each character must originate from the seed inputs provided in this prompt — "
        "the antagonist's core threat, the supporting characters' narrative functions, "
        "and the series context. Do not produce characters who feel like recognizable "
        "archetypes or who recall specific characters from existing books, films, or shows. "
        "Traits must be specific to the seed, not generic. Voice specifications must be "
        "distinct from each other and from anything that reads as a default 'thriller "
        "antagonist voice' or 'wise mentor voice.' If a character feels familiar, that "
        "is a signal to revise toward the seed inputs."
    )

    user_prompt = f"""Generate book-level character profiles per the ANPD V24 Character Profile Schema v1.1.0.

SERIES CONTEXT:
{series_context}

BOOK SEED — ANTAGONIST:
{antagonist_seed}

BOOK SEED — SUPPORTING CAST:
{supporting_seed}

BOOK SUBTEXT (thematic frame):
{book_subtext or '(none provided)'}

PROTAGONIST PER-BOOK DRIVE (what the protagonist wants in this book):
{per_book_drive or '(none provided)'}

BANNED NAMES (do not use any of these):
{banned_list}

OUTPUT REQUIREMENTS:

Produce a JSON object keyed by character canonical name. The object must contain:
  - One antagonist (character_role: "antagonist") with all common fields plus the antagonist-specific block: justification, specific_threat, escalation_capacity, what_they_want, relationship_to_protagonist.
  - One profile per entry in the supporting cast seed (character_role: "supporting") with all common fields plus the supporting-specific block: narrative_function, relationship_to_protagonist, optional arc_in_book, optional what_they_will_not_or_cannot_do.

Common fields for every character: name (must equal the top-level key), character_role, aliases (array, may be empty), primary_trait (single trait, 3-7 words, bold and distinctive), secondary_trait (for antagonist must oppose primary; for supporting must be distinct from primary but need not oppose), psychological_wound (one sentence), gender (one of: male | female | nonbinary — required, never null, the canonical gender the manuscript must render this character as), defining_image (required for antagonist; optional for supporting — omit if the character is one-scene), voice_specification (object with vocabulary_register, sentence_structure_tendencies, signature_expressions array, stress_state_shifts; optional dialogue_constraints sub-object), skills (optional array of strings; omit or empty if not relevant), physical_description (1-3 sentences).

Cross-reference fields for every character: series_bible_match (boolean — false for book-specific antagonist and supporting), relationships (object mapping other character names to relationship descriptions; if you reference a recurring character, the relationship is this character's view of that relationship).

DO NOT produce profiles for the protagonist or for any recurring character — those are inherited from the series level.

DO NOT include a top-level envelope (no "characters" wrapper, no "version" field, no "title" field — the file is the raw per-character map only).

DO NOT use any name in the banned list.

Return only the JSON object."""

    return system_prompt, user_prompt


# ── Output writing ────────────────────────────────────────────────────────────

def write_output(generated_cast, book_config, book_dir):
    """Write the generated cast to {book_dir}/work/character_profiles_{TS}.json
    and update the canonical symlink.

    Returns the absolute path of the timestamped output file.
    """
    work_dir = os.path.join(book_dir, 'work')
    os.makedirs(work_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    fname = f'character_profiles_{timestamp}.json'
    out_path = os.path.join(work_dir, fname)
    with open(out_path, 'w') as f:
        json.dump(generated_cast, f, indent=2, ensure_ascii=False)

    # Update canonical symlink
    canonical = os.path.join(work_dir, 'character_profiles.json')
    if os.path.islink(canonical) or os.path.exists(canonical):
        os.unlink(canonical)
    os.symlink(fname, canonical)

    return out_path


# ── STOP_REPORT writer ────────────────────────────────────────────────────────

def write_stop_report(book_dir, error_message, suggested_fix, pipeline_state):
    reports_dir = os.path.join(book_dir, 'out', 'reports')
    os.makedirs(reports_dir, exist_ok=True)
    stop_report = {
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "component": "character_generator.py",
        "phase": 3,
        "scene_number": None,
        "error_type": "Class A",
        "error_message": error_message,
        "file_path": os.path.abspath(__file__),
        "suggested_fix": suggested_fix,
        "pipeline_state": pipeline_state,
    }
    stop_path = os.path.join(reports_dir, 'STOP_REPORT.json')
    with open(stop_path, 'w') as f:
        json.dump(stop_report, f, indent=2)
    return stop_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='ANPD V24 Character Generator — Phase 3 (book-level antagonist + supporting cast)'
    )
    parser.add_argument('--book-config', required=True, help='Path to book_config.json (per Book Config Schema v0.1)')
    parser.add_argument('--series-config', required=True, help='Path to series_config.json (drives effective_config and model identifier)')
    parser.add_argument('--series-dir', required=True, help='Path to series root directory')
    parser.add_argument(
        '--max-retries', type=int, default=2,
        help='Max number of retry attempts on Class A audit findings (default: 2). '
             'Total attempts = 1 + max_retries. Set to 0 to disable retry.',
    )
    args = parser.parse_args()

    book_dir = derive_book_dir(args.book_config)

    # Load effective config
    try:
        effective_config = resolve_config(args.series_config)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        msg = f"Failed to load effective config from {args.series_config}: {e}"
        print(f"  FATAL: {msg}", file=sys.stderr)
        write_stop_report(
            book_dir, msg,
            "Verify series_config.json exists, is valid JSON, and references a "
            "valid genre template + banned_phrases.json.",
            "Effective config load failed; generation not attempted",
        )
        sys.exit(1)

    # Load book_config
    if not os.path.exists(args.book_config):
        msg = f"book_config.json not found: {args.book_config}"
        print(f"  FATAL: {msg}", file=sys.stderr)
        write_stop_report(
            book_dir, msg,
            "Provide a valid book_config.json per Book Config Schema v0.1 at the path specified.",
            "book_config load failed; generation not attempted",
        )
        sys.exit(1)

    try:
        book_config = load_json(args.book_config)
    except json.JSONDecodeError as e:
        msg = f"book_config.json is not valid JSON: {e}"
        print(f"  FATAL: {msg}", file=sys.stderr)
        write_stop_report(
            book_dir, msg,
            "Fix the JSON syntax in book_config.json. Use a JSON validator if needed.",
            "book_config parse failed; generation not attempted",
        )
        sys.exit(1)

    # Load series artifacts
    try:
        series_artifacts = load_series_artifacts(args.series_dir)
    except FileNotFoundError as e:
        msg = str(e)
        print(f"  FATAL: {msg}", file=sys.stderr)
        write_stop_report(
            book_dir, msg,
            "Verify series_dir contains series_bible.json. character_profiles.json may be absent for new series.",
            "Series artifact load failed; generation not attempted",
        )
        sys.exit(1)

    # Banned data for preflight (sourced from config_resolver)
    banned_data = {
        "names": effective_config.get('banned_names', []),
        "phrases": effective_config.get('banned_phrases', []),
    }

    # Preflight validation
    errors, warnings = validate_book_config_preflight(
        book_config, series_artifacts, banned_data, args.series_dir
    )
    if errors:
        msg = "book_config preflight failed:\n  - " + "\n  - ".join(errors)
        print(f"  FATAL: {msg}", file=sys.stderr)
        write_stop_report(
            book_dir, msg,
            "Fix the listed book_config issues per Book Config Schema v0.1 §6 (Validation Rules) and re-run.",
            "Preflight failed; generation not attempted",
        )
        sys.exit(1)

    # Header
    print(f"\n{'='*70}")
    print(f"  ANPD V24 — CHARACTER GENERATION")
    print(f"{'='*70}")
    print(f"  Book:           {book_config['title']} (book {book_config['book_number']})")
    print(f"  Series:         {args.series_dir}")
    antag_name = book_config['antagonist_concept'].get('name', '(name TBD by generator)')
    print(f"  Antagonist:     {antag_name}")
    print(f"  Supporting:     {len(book_config.get('supporting_cast_needs', []))} character(s)")
    print(f"  Book dir:       {book_dir}")

    if warnings:
        print(f"\n  Preflight warnings:")
        for w in warnings:
            print(f"    - {w}")

    # Build prompt
    system_prompt, user_prompt = build_generation_prompt(
        book_config,
        series_artifacts['series_bible'],
        series_artifacts['series_profiles'],
        effective_config,
    )

    # Generate with bounded retry on Class A findings
    gen_model = (
        effective_config.get('model_character_generation')
        or effective_config.get('model_synopsis_generation')
        or 'claude-sonnet-4-5'
    )
    series_profiles_path = os.path.join(args.series_dir, 'character_profiles.json')
    book_profiles_path = os.path.join(book_dir, 'work', 'character_profiles.json')
    findings_report_path = os.path.join(book_dir, 'character_generation_findings.json')

    max_attempts = 1 + max(0, args.max_retries)
    all_attempts_findings = []  # list of {attempt, findings} for the report
    final_cast = None
    final_findings = []
    final_class_a = []
    out_path = None
    last_user_prompt = user_prompt  # corrective prompts replace this on retry

    for attempt in range(1, max_attempts + 1):
        attempt_label = f"attempt {attempt}/{max_attempts}"
        print(f"\n  Generating cast — {attempt_label} (model: {gen_model})...")
        start = time.time()
        try:
            raw = call_sonnet_streaming(last_user_prompt, system_prompt, model=gen_model, max_tokens=12000)
            elapsed = time.time() - start
            print(f"  Generation complete — {elapsed:.0f}s")
        except Exception as e:
            msg = f"API error during character generation ({attempt_label}): {e}"
            print(f"  FATAL: {msg}", file=sys.stderr)
            write_stop_report(
                book_dir, msg,
                "Verify API key file exists at /home/anpd/.anthropic/api_key, network is reachable, "
                "and model identifier is valid. If error persists, check Anthropic API status.",
                f"API call failed on {attempt_label}; no output written for this attempt",
            )
            sys.exit(1)

        # Parse output
        try:
            generated_cast = clean_json_response(raw)
        except json.JSONDecodeError as e:
            raw_preview = (raw or '')[:500]
            msg = (
                f"Generated output is not valid JSON ({attempt_label}): {e}. "
                f"Raw output (first 500 chars): {raw_preview}"
            )
            print(f"  FATAL: {msg}", file=sys.stderr)
            write_stop_report(
                book_dir, msg,
                "Re-run generation. If error persists, the prompt may need stricter JSON-only instructions "
                "or the model identifier may need to change.",
                f"{attempt_label}: generation succeeded; JSON parse failed",
            )
            sys.exit(1)

        if not isinstance(generated_cast, dict):
            msg = f"Generated output is not a JSON object on {attempt_label} (got {type(generated_cast).__name__})"
            print(f"  FATAL: {msg}", file=sys.stderr)
            write_stop_report(
                book_dir, msg,
                "Re-run generation. The prompt may need clarification that the output is a top-level "
                "object keyed by character name.",
                f"{attempt_label}: generation succeeded; output type wrong",
            )
            sys.exit(1)

        # Write output (overwrites prior attempts)
        out_path = write_output(generated_cast, book_config, book_dir)
        print(f"  Output:         {out_path}")
        print(f"  Characters:     {len(generated_cast)} ({', '.join(generated_cast.keys())})")

        # Audit
        print(f"  Auditing generated cast ({attempt_label})...")
        if not os.path.exists(series_profiles_path):
            print(f"  (Skipping audit — no series-level character_profiles.json at {series_profiles_path})")
            findings = []
        else:
            try:
                findings = audit_character_profiles(
                    Path(series_profiles_path),
                    Path(book_profiles_path),
                    effective_config,
                )
            except Exception as e:
                msg = f"Auditor error during character_generator integration ({attempt_label}): {type(e).__name__}: {e}"
                print(f"  FATAL: {msg}", file=sys.stderr)
                write_stop_report(
                    book_dir, msg,
                    "Inspect the auditor module's error. The generator wrote its output successfully "
                    f"to {out_path}; the failure occurred during audit. If the auditor's contract has "
                    "changed, character_generator's call site may need updating.",
                    f"{attempt_label}: generation succeeded; auditor failed",
                )
                sys.exit(1)

        # Categorize
        class_a = [f for f in findings if f.get('class_') == 'A']
        class_b = [f for f in findings if f.get('class_') == 'B']
        class_c = [f for f in findings if f.get('class_') == 'C']
        print(f"  Findings:       Class A={len(class_a)}  Class B={len(class_b)}  Class C={len(class_c)}")

        # Validate finding shapes (defensive — auditor contract requires conformance)
        try:
            attempt_envelope = serialize_findings(findings)
        except ValueError as e:
            msg = f"Auditor returned invalid findings on {attempt_label} (failed serialize_findings validation): {e}"
            print(f"  FATAL: {msg}", file=sys.stderr)
            write_stop_report(
                book_dir, msg,
                "Auditor returned a finding that does not conform to the V24 finding schema. "
                "Inspect the auditor module's check function output.",
                f"{attempt_label}: generation + audit succeeded; finding serialization failed",
            )
            sys.exit(1)

        all_attempts_findings.append({
            "attempt": attempt,
            "class_a_count": len(class_a),
            "class_b_count": len(class_b),
            "class_c_count": len(class_c),
            "findings": attempt_envelope["findings"],
        })

        final_cast = generated_cast
        final_findings = findings
        final_class_a = class_a

        # If clean (no Class A), break out — success
        if not class_a:
            break

        # Class A present — retry if attempts remain
        if attempt < max_attempts:
            print(f"  Class A findings present — building corrective prompt for next attempt...")
            last_user_prompt = build_corrective_prompt(generated_cast, class_a, user_prompt)
        # else: loop will exit; STOP_REPORT below.

    # Write the consolidated findings report (covers all attempts)
    consolidated_report = {
        "attempts_total": len(all_attempts_findings),
        "max_attempts_allowed": max_attempts,
        "final_class_a_count": len(final_class_a),
        "attempts": all_attempts_findings,
    }
    with open(findings_report_path, 'w') as f:
        json.dump(consolidated_report, f, indent=2)
    print(f"\n  Findings report: {findings_report_path}")

    # If final attempt still has Class A → STOP
    if final_class_a:
        finding_summary = "\n  - ".join(
            f"{f.get('finding_id', '?')} ({f.get('pass_name', '?')}): {f.get('description', '?')[:200]}"
            for f in final_class_a
        )
        msg = (
            f"Generated cast failed audit after {len(all_attempts_findings)} attempt(s) "
            f"with {len(final_class_a)} Class A finding(s) on the final attempt:\n"
            f"  - {finding_summary}"
        )
        print(f"  FATAL: {msg}", file=sys.stderr)
        write_stop_report(
            book_dir, msg,
            "Inspect findings report for the per-attempt history. Class A findings persisting "
            "across all retry attempts indicate the generator cannot produce a valid cast for the "
            "given book_config + series context. Possible fixes: "
            "(a) revise book_config to provide more specific creative direction; "
            "(b) raise --max-retries (current default: 2); "
            "(c) if findings repeat with the same root cause, the generator's prompt may need adjustment.",
            f"Generation + audit succeeded across {len(all_attempts_findings)} attempt(s); Class A findings persist",
        )
        sys.exit(1)

    # Success — Class A clear
    final_class_b = [f for f in final_findings if f.get('class_') == 'B']
    final_class_c = [f for f in final_findings if f.get('class_') == 'C']
    print(f"\n{'='*70}")
    if len(all_attempts_findings) > 1:
        print(f"  CHARACTER GENERATION COMPLETE — passed audit on attempt {len(all_attempts_findings)} of {max_attempts}")
    if final_class_b or final_class_c:
        print(f"  Audit: {len(final_class_b) + len(final_class_c)} non-blocking finding(s) on final attempt")
    else:
        print(f"  Audit: clean")
    print(f"{'='*70}\n")

    sys.exit(0)


if __name__ == '__main__':
    main()
