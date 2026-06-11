# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V24 State Tracker — per-scene state extraction

Phase 5 helper component. Invoked once per scene from master_controller's
scene loop after a scene file is written. Reads the scene prose plus the
prior state file (state_after_sc{N-1}.json), produces the new state file
(state_after_sc{N}.json) per Data Standards §4.4 schema.

Class B if missing per master_controller's stub-handling discipline:
state files are inputs to manuscript_auditor's Pass 3 (state continuity)
but the auditor handles their absence with a Class B coverage note.
The pipeline doesn't halt if state_tracker fails or isn't built.

DATA STANDARDS §4.4 SCHEMA

```json
{
  "scene_number":              integer,
  "character_locations":       {character_name: location_string},
  "character_physical_state":  {character_name: state_string},
  "deaths":                    [character_names_cumulative],
  "injuries":                  {character_name: injury_description},
  "equipment":                 {character_name: [equipment_strings]},
  "information_revealed":      [strings_cumulative],
  "timeline":                  string
}
```

CUMULATIVE VS SNAPSHOT FIELDS

Snapshot (overwritten each scene): scene_number, character_locations,
character_physical_state, injuries, equipment, timeline.

Cumulative (append-only, never decremented): deaths, information_revealed.

Cumulative fields propagate forward — once a character dies they stay
dead in every subsequent state file; once information is revealed it
remains in the cumulative list. The Haiku prompt encodes this rule.

IDEMPOTENT

Re-running state_tracker for an already-written state file overwrites it.
master_controller may invoke state_tracker after Tier 3 regen (when a
scene is regenerated, its state file must be regenerated too).

SCENE FILE LOCATION

state_tracker accepts the canonical scene path directly via --scene-file.
master_controller resolves the canonical path (e.g. resolves sc{NN}_*.md
glob to the most recent match per scene_formatter's convention).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    from llm_client import call_llm as _llm_call
except ImportError:
    _llm_call = None


# ─── Constants ────────────────────────────────────────────────────────────────

DEFAULT_HAIKU_MODEL = "claude-haiku-4-5"
HAIKU_MAX_TOKENS = 2048

SCENE_FILENAME_PATTERN = re.compile(r"^sc(\d{2,3})_(.+)\.md$")

# §4.4 schema field names — used for validation
REQUIRED_FIELDS = {
    "scene_number",
    "character_locations",
    "character_physical_state",
    "deaths",
    "injuries",
    "equipment",
    "information_revealed",
    "timeline",
}

CUMULATIVE_FIELDS = {"deaths", "information_revealed"}

# Delta schema: what Haiku emits per scene (before Python merge).
# new_deaths / new_information_revealed are THIS SCENE's additions only.
DELTA_REQUIRED_FIELDS = {
    "scene_number",
    "character_locations",
    "character_physical_state",
    "new_deaths",
    "injuries",
    "equipment",
    "new_information_revealed",
    "timeline",
}


# ─── STOP_REPORT helper ───────────────────────────────────────────────────────

def write_stop_report(
    book_dir: str,
    error_message: str,
    suggested_fix: str,
    scene_number: int | None = None,
    file_path: str | None = None,
) -> str:
    """Write Class A STOP_REPORT.json per Data Standards §4.6."""
    reports_dir = os.path.join(book_dir, "out", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, "STOP_REPORT.json")
    payload = {
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "component":     "state_tracker",
        "phase":         5,
        "scene_number":  scene_number,
        "error_type":    "Class A",
        "error_message": error_message,
        "file_path":     file_path,
        "suggested_fix": suggested_fix,
        "pipeline_state": "halted at state extraction",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


# ─── Scene number derivation ──────────────────────────────────────────────────

def scene_number_from_path(scene_file_path: str) -> int | None:
    """Extract scene number from a sc{NN}_{slug}.md filename."""
    basename = os.path.basename(scene_file_path)
    m = SCENE_FILENAME_PATTERN.match(basename)
    if not m:
        return None
    return int(m.group(1))


def state_file_path_for(state_dir: str, scene_number: int) -> str:
    """Canonical state file path for a given scene number."""
    return os.path.join(state_dir, f"state_after_sc{scene_number:02d}.json")


# ─── Prior-state loading ──────────────────────────────────────────────────────

def load_prior_state(state_dir: str, scene_number: int) -> dict:
    """Load state_after_sc{N-1}.json. For scene 1, loads state_after_sc00.json
    (the seed state, created manually before first run per Data Standards §2.6).

    Returns empty seed dict if prior file is missing — caller decides whether
    that's a Class A failure.
    """
    prior_scene = scene_number - 1
    prior_path = state_file_path_for(state_dir, prior_scene)
    if not os.path.isfile(prior_path):
        return {}
    try:
        with open(prior_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


# ─── Extraction prompt ────────────────────────────────────────────────────────

def build_extraction_prompt(
    scene_number: int,
    scene_text: str,
    prior_state: dict,
) -> str:
    """Compose the Haiku prompt that extracts THIS SCENE'S DELTA only.

    The LLM does NOT maintain cumulative state. It reports only what changed
    in this scene. state_tracker merges the delta into prior cumulative state
    in Python (merge_delta_into_state). This keeps the LLM task bounded and
    reliable regardless of book position.
    """
    # Give the LLM a SMALL context: who's already dead (so it doesn't
    # re-report a death), and current locations (so it can report moves).
    # Do NOT dump the full cumulative information_revealed — the LLM doesn't
    # need prior reveals to report this scene's new ones.
    prior_deaths = prior_state.get("deaths", []) if prior_state else []
    prior_locations = prior_state.get("character_locations", {}) if prior_state else {}
    context = {
        "already_deceased": prior_deaths,
        "last_known_locations": prior_locations,
    }
    context_json = json.dumps(context, indent=2)
    return f"""You are extracting world-state CHANGES from a single manuscript scene of an action-thriller series.

Report ONLY what this scene establishes or changes. Do NOT repeat prior history.

Output a JSON object with EXACTLY these eight fields:
  - scene_number:             integer (use {scene_number})
  - character_locations:      object mapping character_name -> location (SNAPSHOT — every character whose location is known AS OF this scene; carry forward unchanged ones you can infer, omit unknowns)
  - character_physical_state: object mapping character_name -> state (SNAPSHOT — current state as of this scene)
  - new_deaths:               array of character names who DIE IN THIS SCENE (deltas only — do NOT include anyone already in already_deceased)
  - injuries:                 object mapping character_name -> injury (SNAPSHOT — current injuries; omit healed; omit anyone deceased)
  - equipment:                object mapping character_name -> array of equipment (SNAPSHOT)
  - new_information_revealed:  array of strings — facts REVEALED TO THE READER IN THIS SCENE ONLY (deltas only — do NOT repeat facts from earlier scenes)
  - timeline:                 string describing current story time (SNAPSHOT)

CONTEXT (for reference only — do NOT echo back):
{context_json}

CURRENT SCENE TEXT (scene {scene_number}):
{scene_text[:8000]}

Output ONLY the JSON object. No prose, no markdown fences. Start with {{ and end with }}."""


# ─── LLM invocation ───────────────────────────────────────────────────────────

def call_haiku(client, prompt: str) -> str:
    """Invoke Haiku via llm_client and return the response text.

    The client parameter is retained for call-site compatibility but is
    ignored; llm_client manages its own client.
    """
    response = _llm_call(
        provider="anthropic",
        model=DEFAULT_HAIKU_MODEL,
        system="You are a state tracker for novel production.",
        user=prompt,
        max_tokens=HAIKU_MAX_TOKENS,
    )
    return response.text


# ─── Response parsing & validation ────────────────────────────────────────────

def _repair_json(text: str) -> str:
    """Best-effort repair of common LLM JSON malformations.

    Handles: trailing commas before } or ], and extraction of the outermost
    JSON object when the model adds prose around it. Conservative — only
    fixes well-known patterns; does not attempt structural reconstruction.
    """
    # Extract outermost {...} if there's leading/trailing prose
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        text = text[first:last + 1]
    # Remove trailing commas before closing brace/bracket
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def parse_state_response(response_text: str) -> dict:
    """Parse Haiku response into a state dict.

    Strips markdown fences, attempts a direct parse, then a repaired parse.
    Raises ValueError only if both fail (caller handles retry).
    """
    text = response_text.strip()
    # Strip markdown fences if Haiku added them despite instructions
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Second attempt: repair common malformations
    try:
        return json.loads(_repair_json(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Haiku response is not valid JSON: {exc}") from exc


def validate_state_schema(state: dict, expected_scene_number: int) -> list[str]:
    """Verify the state dict has all §4.4 required fields with correct types.

    Returns list of validation error strings (empty if valid).
    """
    errors: list[str] = []

    missing = REQUIRED_FIELDS - set(state.keys())
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")
        return errors  # don't continue — type checks will compound errors

    if state["scene_number"] != expected_scene_number:
        errors.append(
            f"scene_number is {state['scene_number']!r}, expected {expected_scene_number}"
        )

    if not isinstance(state["character_locations"], dict):
        errors.append("character_locations must be an object")
    if not isinstance(state["character_physical_state"], dict):
        errors.append("character_physical_state must be an object")
    if not isinstance(state["deaths"], list):
        errors.append("deaths must be an array")
    if not isinstance(state["injuries"], dict):
        errors.append("injuries must be an object")
    if not isinstance(state["equipment"], dict):
        errors.append("equipment must be an object")
    if not isinstance(state["information_revealed"], list):
        errors.append("information_revealed must be an array")
    if not isinstance(state["timeline"], str):
        errors.append("timeline must be a string")

    return errors


def validate_delta_schema(delta: dict, expected_scene_number: int) -> list[str]:
    """Validate the per-scene delta Haiku emits (before merge)."""
    errors: list[str] = []
    missing = DELTA_REQUIRED_FIELDS - set(delta.keys())
    if missing:
        errors.append(f"delta missing required fields: {sorted(missing)}")
        return errors
    if delta["scene_number"] != expected_scene_number:
        errors.append(f"scene_number is {delta['scene_number']!r}, expected {expected_scene_number}")
    for f in ("character_locations", "character_physical_state", "injuries", "equipment"):
        if not isinstance(delta[f], dict):
            errors.append(f"{f} must be an object")
    for f in ("new_deaths", "new_information_revealed"):
        if not isinstance(delta[f], list):
            errors.append(f"{f} must be an array")
    if not isinstance(delta["timeline"], str):
        errors.append("timeline must be a string")
    return errors


def merge_delta_into_state(prior_state: dict, delta: dict, scene_number: int) -> dict:
    """Merge this scene's delta into prior cumulative state, in Python.

    Cumulative fields (deaths, information_revealed): prior + new, deduped,
    order-preserving (append-only — never shrinks).
    Snapshot fields (locations, physical_state, injuries, equipment, timeline):
    overwritten by the delta's values.
    Returns a full §4.4-compliant state dict.
    """
    def dedup_append(prior_list, new_list):
        out = list(prior_list)
        seen = set(prior_list)
        for item in new_list:
            if item not in seen:
                out.append(item)
                seen.add(item)
        return out

    prior_deaths = prior_state.get("deaths", []) if prior_state else []
    prior_reveals = prior_state.get("information_revealed", []) if prior_state else []

    merged = {
        "scene_number": scene_number,
        "character_locations": delta["character_locations"],
        "character_physical_state": delta["character_physical_state"],
        "deaths": dedup_append(prior_deaths, delta["new_deaths"]),
        "injuries": delta["injuries"],
        "equipment": delta["equipment"],
        "information_revealed": dedup_append(prior_reveals, delta["new_information_revealed"]),
        "timeline": delta["timeline"],
    }
    # Safety: a deceased character must not appear in injuries.
    for name in merged["deaths"]:
        merged["injuries"].pop(name, None)
    return merged


# ─── State file writing ───────────────────────────────────────────────────────

def write_state_file(state_path: str, state: dict) -> None:
    """Write state file atomically (write to .tmp, then rename)."""
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp_path, state_path)


# ─── Orchestration ────────────────────────────────────────────────────────────

def run_state_tracker(
    book_dir: str,
    scene_file_path: str,
    state_dir: str | None = None,
) -> tuple[int, dict | None]:
    """Main orchestration. Returns (exit_code, state_dict_or_None).

    exit_code:
        0 — success, state file written
        1 — Class A failure, STOP_REPORT written
    """
    if state_dir is None:
        state_dir = os.path.join(book_dir, "out", "state")

    print(f"=== state_tracker ===", file=sys.stderr)
    print(f"  scene_file: {scene_file_path}", file=sys.stderr)
    print(f"  state_dir:  {state_dir}", file=sys.stderr)

    # Resolve scene number
    scene_number = scene_number_from_path(scene_file_path)
    if scene_number is None:
        write_stop_report(
            book_dir,
            error_message=f"could not derive scene number from path: {scene_file_path}",
            suggested_fix="ensure scene file follows sc{NN}_{slug}.md naming convention",
            file_path=scene_file_path,
        )
        return (1, None)
    print(f"  scene_number: {scene_number}", file=sys.stderr)

    # Read scene prose
    if not os.path.isfile(scene_file_path):
        write_stop_report(
            book_dir,
            error_message=f"scene file not found: {scene_file_path}",
            suggested_fix="verify scene file exists before invoking state_tracker",
            scene_number=scene_number,
            file_path=scene_file_path,
        )
        return (1, None)

    try:
        with open(scene_file_path, "r", encoding="utf-8") as fh:
            scene_text = fh.read()
    except OSError as exc:
        write_stop_report(
            book_dir,
            error_message=f"scene file read failed: {exc}",
            suggested_fix="check file permissions",
            scene_number=scene_number,
            file_path=scene_file_path,
        )
        return (1, None)

    if not scene_text.strip():
        write_stop_report(
            book_dir,
            error_message="scene file is empty",
            suggested_fix="regenerate scene file",
            scene_number=scene_number,
            file_path=scene_file_path,
        )
        return (1, None)

    # Load prior state (allowed to be empty for scene 1 if seed missing)
    prior_state = load_prior_state(state_dir, scene_number)
    print(f"  prior_state: {'loaded' if prior_state else 'empty (no prior file)'}", file=sys.stderr)

    # Initialize Haiku client
    client = _init_haiku_client()
    if client is None:
        write_stop_report(
            book_dir,
            error_message="Haiku client unavailable (anthropic not installed or API key missing)",
            suggested_fix="ensure anthropic package installed and ANTHROPIC_API_KEY set",
            scene_number=scene_number,
        )
        return (1, None)

    # Build delta-only prompt and call Haiku, with retry on call failure,
    # unparseable JSON, or delta-schema validation failure. Each attempt
    # re-calls Haiku. STOP_REPORT only after all attempts exhaust.
    prompt = build_extraction_prompt(scene_number, scene_text, prior_state)
    STATE_MAX_ATTEMPTS = 3
    delta = None
    last_error = None
    for attempt in range(1, STATE_MAX_ATTEMPTS + 1):
        try:
            response_text = call_haiku(client, prompt)
        except Exception as exc:
            last_error = f"Haiku call failed: {exc}"
            print(f"  state_tracker: scene {scene_number} call attempt {attempt}/{STATE_MAX_ATTEMPTS} failed: {exc}", file=sys.stderr)
            continue
        try:
            delta = parse_state_response(response_text)
        except ValueError as exc:
            last_error = str(exc)
            print(f"  state_tracker: scene {scene_number} parse attempt {attempt}/{STATE_MAX_ATTEMPTS} failed: {exc}", file=sys.stderr)
            continue
        # Validate delta schema before accepting
        delta_errors = validate_delta_schema(delta, scene_number)
        if delta_errors:
            last_error = f"delta schema: {'; '.join(delta_errors)}"
            print(f"  state_tracker: scene {scene_number} delta validation attempt {attempt}/{STATE_MAX_ATTEMPTS} failed: {last_error}", file=sys.stderr)
            delta = None
            continue
        break  # delta parsed and validated

    if delta is None:
        write_stop_report(
            book_dir,
            error_message=f"state extraction failed after {STATE_MAX_ATTEMPTS} attempts: {last_error}",
            suggested_fix="check Haiku output format / API connectivity; state is load-bearing and cannot be skipped",
            scene_number=scene_number,
        )
        return (1, None)

    # Merge delta into prior cumulative state (deterministic, in Python)
    new_state = merge_delta_into_state(prior_state, delta, scene_number)

    # Validate merged state against §4.4 schema
    errors = validate_state_schema(new_state, scene_number)
    if errors:
        write_stop_report(
            book_dir,
            error_message=f"merged state schema validation failed: {'; '.join(errors)}",
            suggested_fix="re-run; if persistent, merge logic may need fixing",
            scene_number=scene_number,
        )
        return (1, None)

    # Write state file
    state_path = state_file_path_for(state_dir, scene_number)
    try:
        write_state_file(state_path, new_state)
    except OSError as exc:
        write_stop_report(
            book_dir,
            error_message=f"state file write failed: {exc}",
            suggested_fix="check write permissions on state_dir",
            scene_number=scene_number,
            file_path=state_path,
        )
        return (1, None)

    print(f"  wrote: {state_path}", file=sys.stderr)
    print(f"  characters tracked: {len(new_state['character_locations'])}", file=sys.stderr)
    print(f"  deaths cumulative:  {len(new_state['deaths'])}", file=sys.stderr)
    print(f"  reveals cumulative: {len(new_state['information_revealed'])}", file=sys.stderr)

    return (0, new_state)


def _init_haiku_client():
    """Check if llm_client is available. Returns truthy sentinel if so, None if not."""
    if _llm_call is None:
        return None
    try:
        from pathlib import Path
        if not Path("/home/anpd/.anthropic/api_key").exists():
            return None
        return True  # sentinel — call_haiku uses llm_client directly
    except Exception:
        return None


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="state_tracker.py",
        description="ANPD V24 state_tracker — per-scene state extraction (§4.4)",
    )
    parser.add_argument("--book-dir", required=True,
                        help="Path to book directory (for STOP_REPORT location)")
    parser.add_argument("--scene-file", required=True,
                        help="Path to the scene file to extract state from "
                             "(sc{NN}_{slug}.md)")
    parser.add_argument("--state-dir", default=None,
                        help="State file directory; defaults to {book_dir}/out/state/")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    exit_code, _ = run_state_tracker(
        book_dir=args.book_dir,
        scene_file_path=args.scene_file,
        state_dir=args.state_dir,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
