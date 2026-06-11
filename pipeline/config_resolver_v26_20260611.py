# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify master_controller.py to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""Shared effective-config loader for V24 pipeline components.

Reads series_config.json and the named genre template, applies
structural_overrides, returns a dict with resolved values. Used by
synopsis_generator.py and synopsis_auditor.py (and eventually
master_controller.py).

Per Series Config Schema §4.3: series_config.structural_overrides
overlay genre template defaults. Per White Paper §2.1 (single
canonical source): this is the one place effective config is computed.
"""

import json
import os


def resolve_config(series_config_path):
    """Load series_config.json and the named genre template, apply overrides.

    Returns a dict with resolved values for all structural and model fields.
    """
    with open(series_config_path, 'r', encoding='utf-8') as f:
        series_config = json.load(f)

    genre = series_config["genre"]
    genre_template_path = f"/anpd/v26/docs/genre_defaults/{genre}.json"
    with open(genre_template_path, 'r', encoding='utf-8') as f:
        genre_template = json.load(f)

    overrides = series_config.get("structural_overrides", {})

    # Build effective values: genre template as base, series overrides on top
    effective = {}
    for key, value in genre_template.items():
        effective[key] = overrides.get(key, value)

    # Model identifier from series_config (not genre template)
    effective["model_synopsis_generation"] = series_config.get(
        "model_synopsis_generation", genre_template.get("model_synopsis_generation")
    )

    # Carry through series-level identity fields
    effective["pen_name"] = series_config.get("pen_name", "")
    effective["series_name"] = series_config.get("series_name", "")
    effective["series_directory"] = series_config.get("series_directory", "")

    # Read banned names from series banned_phrases.json.
    # Hard failure if missing — the validator backstop was removed in Commit C2.
    banned_phrases_path = series_config.get("banned_phrases_path")
    if not banned_phrases_path or not os.path.exists(banned_phrases_path):
        raise FileNotFoundError(
            f"banned_phrases.json not found at {banned_phrases_path}; "
            f"required for synopsis generation and audit."
        )
    with open(banned_phrases_path, 'r', encoding='utf-8') as f:
        banned_data = json.load(f)
    effective["banned_names"] = banned_data.get("names", [])
    effective["banned_phrases"] = banned_data.get("phrases", [])

    return effective
