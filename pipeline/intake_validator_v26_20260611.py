"""
intake_validator.py — V25 Intake Validator
ANPD V25 | Version: 20260509

Validates intake.json against the V25 intake schema.
Returns structured ValidationResult with pass/fail, errors, warnings.
"""

import json
import os
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    passed: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    intake: dict = field(default_factory=dict)


REQUIRED_FIELDS = {
    "book_number": int,
    "title": str,
    "series": str,
    "total_chapter_count": int,
    "target_word_count": int,
    "outline_path": str,
    "historical_window": dict,
    "historical_anchors_in_scope": list,
    "historical_anchors_out_of_scope": list,
}

OPTIONAL_FIELDS = {
    "series_bible_path": str,
    "character_profiles_path": str,
    "craft_principles_path": str,
    "voice_register": str,
    "operational_doctrine": list,
    "anti_patterns": list,
    "protagonist": str,
    "antagonist": str,
}


def validate_intake(intake_path: str) -> ValidationResult:
    """Validate intake.json against V25 schema.

    Returns ValidationResult with:
      - passed: bool
      - errors: list[str] (missing required fields, type errors, value errors)
      - warnings: list[str] (recommended-but-missing fields)
      - intake: dict (parsed and validated intake)
    """
    errors = []
    warnings = []

    # File existence
    if not os.path.exists(intake_path):
        return ValidationResult(passed=False, errors=[f"Intake file not found: {intake_path}"])

    # JSON parsing
    try:
        with open(intake_path, 'r', encoding='utf-8') as f:
            intake = json.load(f)
    except json.JSONDecodeError as e:
        return ValidationResult(passed=False, errors=[f"Invalid JSON: {e}"])

    # Required fields
    for field_name, field_type in REQUIRED_FIELDS.items():
        if field_name not in intake:
            errors.append(f"Missing required field: {field_name}")
        elif not isinstance(intake[field_name], field_type):
            errors.append(
                f"Type mismatch for {field_name}: expected {field_type.__name__}, "
                f"got {type(intake[field_name]).__name__}"
            )

    # Optional fields (warnings only)
    for field_name, field_type in OPTIONAL_FIELDS.items():
        if field_name not in intake:
            warnings.append(f"Recommended field missing: {field_name}")

    # Value validations (only if required fields present and correct type)
    if not errors:
        # historical_window must have start_date and end_date
        hw = intake["historical_window"]
        if "start_date" not in hw or "end_date" not in hw:
            errors.append("historical_window must contain start_date and end_date")

        # outline_path must exist (resolve relative to intake file's directory)
        outline_path = intake["outline_path"]
        if not os.path.isabs(outline_path):
            intake_dir = os.path.dirname(os.path.abspath(intake_path))
            outline_path = os.path.join(intake_dir, outline_path)
        if not os.path.exists(outline_path):
            errors.append(f"outline_path file not found: {intake['outline_path']} (resolved: {outline_path})")

    return ValidationResult(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        intake=intake if not errors else {},
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 intake_validator.py <intake.json>")
        sys.exit(1)
    result = validate_intake(sys.argv[1])
    print(f"Passed: {result.passed}")
    for e in result.errors:
        print(f"  ERROR: {e}")
    for w in result.warnings:
        print(f"  WARNING: {w}")
    sys.exit(0 if result.passed else 1)
