# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify master_controller.py to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.
"""V24 audit finding schema module.

Defines the structured finding format used by every V24 audit pass, per
White Paper §3.8.  Provides creation, validation, and serialization helpers.

Design decision: findings are plain dicts rather than dataclass instances.
Dict-based findings keep the module dependency-free beyond the standard
library and make JSON round-tripping trivial.  The ``create_finding``
factory enforces the schema so callers never need to construct dicts by hand.

Load-bearing rule (Finding 19): ``suggested_fix`` is **never** optional and
**never** empty.  An orphan finding — one without a suggested_fix — is itself
a silent failure because the fix path lives in human memory rather than in a
persistent artifact.  When ``fix_action`` is ``"not_applicable"``,
``suggested_fix`` must contain the reason the finding is not actionable
(e.g. "informational only — banned phrase appears inside a quoted source
citation").

See also: White Paper §3.8, Finding 19.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Enum constants
# ---------------------------------------------------------------------------

FINDING_CLASS: set[str] = {"A", "B", "C"}
FINDING_TIER: set[str] = {"1", "2", "3"}
FINDING_GATE: set[str] = {"synopsis", "character_profile", "manuscript"}
FINDING_CONFIDENCE: set[str] = {"HIGH", "MEDIUM", "LOW"}
FIX_ACTION: set[str] = {
    "auto_fix",
    "verify_then_fix",
    "route_to_rewrite",
    "route_to_human",
    "not_applicable",
}
LOCATION_TYPE: set[str] = {
    "scene",
    "chapter",
    "chapter_paragraph",
    "character_offset",
    "field_path",
    "whole_artifact",
}

# ---------------------------------------------------------------------------
# Field ordering — used by create_finding and validate_finding
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: list[str] = [
    "finding_id",
    "auditor",
    "gate",
    "pass_name",
    "class_",
    "tier",
    "category",
    "description",
    "location",
    "fix_action",
    "suggested_fix",
    "timestamp",
]

_OPTIONAL_FIELDS: list[str] = [
    "evidence",
    "confidence",
]

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_finding(finding: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a finding dict against the V24 schema.

    Returns ``(is_valid, errors)`` where *errors* is a list of human-readable
    strings, one per validation failure.  All checks run; the function never
    short-circuits on the first error.
    """
    errors: list[str] = []

    # --- required fields present ---
    for field in _REQUIRED_FIELDS:
        if field not in finding:
            errors.append(f"Missing required field: {field}")

    # --- non-empty string checks ---
    for field in ("finding_id", "auditor", "pass_name", "category", "description"):
        val = finding.get(field)
        if val is not None and (not isinstance(val, str) or not val.strip()):
            errors.append(f"{field} must be a non-empty string")

    # --- suggested_fix: load-bearing check (Finding 19) ---
    sf = finding.get("suggested_fix")
    if sf is not None and (not isinstance(sf, str) or not sf.strip()):
        errors.append(
            "suggested_fix must be a non-empty string (Finding 19: "
            "an orphan finding without a suggested_fix is a silent failure)"
        )

    # --- enum membership ---
    _check_enum(finding, "class_", FINDING_CLASS, errors)
    _check_enum(finding, "tier", FINDING_TIER, errors)
    _check_enum(finding, "gate", FINDING_GATE, errors)
    _check_enum(finding, "fix_action", FIX_ACTION, errors)

    # confidence: None is valid; if present, must be in FINDING_CONFIDENCE
    conf = finding.get("confidence")
    if conf is not None and conf not in FINDING_CONFIDENCE:
        errors.append(
            f"confidence must be one of {sorted(FINDING_CONFIDENCE)} or None, "
            f"got {conf!r}"
        )

    # --- location ---
    loc = finding.get("location")
    if loc is not None:
        if not isinstance(loc, dict):
            errors.append("location must be a dict")
        elif "type" not in loc:
            errors.append("location dict must contain a 'type' key")
        elif loc["type"] not in LOCATION_TYPE:
            errors.append(
                f"location.type must be one of {sorted(LOCATION_TYPE)}, "
                f"got {loc['type']!r}"
            )

    # --- timestamp format ---
    ts = finding.get("timestamp")
    if ts is not None and (not isinstance(ts, str) or not _TIMESTAMP_RE.match(ts)):
        errors.append(
            f"timestamp must match YYYY-MM-DD HH:MM, got {ts!r}"
        )

    return (len(errors) == 0, errors)


def _check_enum(
    finding: dict[str, Any],
    field: str,
    allowed: set[str],
    errors: list[str],
) -> None:
    """Append an error if *field* is present but not in *allowed*."""
    val = finding.get(field)
    if val is not None and val not in allowed:
        errors.append(
            f"{field} must be one of {sorted(allowed)}, got {val!r}"
        )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_finding(**kwargs: Any) -> dict[str, Any]:
    """Construct a validated finding dict.

    This is the primary creation path.  Auditors should use this function
    rather than building finding dicts by hand.

    Does **not** auto-generate ``timestamp`` — the caller must pass it
    explicitly.  This keeps tests deterministic and prevents two findings
    produced in the same minute from being indistinguishable when the caller
    actually had distinct events to record.

    Raises :class:`ValueError` if the resulting finding fails validation.
    """
    # Build the dict in canonical field order.
    finding: dict[str, Any] = {}
    for field in _REQUIRED_FIELDS:
        if field in kwargs:
            finding[field] = kwargs[field]
    for field in _OPTIONAL_FIELDS:
        finding[field] = kwargs.get(field)

    is_valid, errors = validate_finding(finding)
    if not is_valid:
        raise ValueError(
            "Invalid finding:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return finding


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe dict with ``class_`` renamed to ``class``.

    Validates the finding first; raises :class:`ValueError` if invalid.
    """
    is_valid, errors = validate_finding(finding)
    if not is_valid:
        raise ValueError(
            "Cannot serialize invalid finding:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    out: dict[str, Any] = {}
    for key, value in finding.items():
        out_key = "class" if key == "class_" else key
        out[out_key] = value
    return out


def serialize_findings(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Serialize a list of findings into a report envelope.

    Returns::

        {
            "findings": [<serialized finding>, ...],
            "count": N,
            "generated_at": "YYYY-MM-DD HH:MM"
        }

    Validates each finding; raises :class:`ValueError` on the first invalid
    finding encountered.
    """
    from datetime import datetime, timezone

    serialized = [serialize_finding(f) for f in findings]
    now = datetime.now(timezone.utc)
    return {
        "findings": serialized,
        "count": len(serialized),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
    }


def deserialize_finding(d: dict[str, Any]) -> dict[str, Any]:
    """Reverse of :func:`serialize_finding`.

    Renames ``class`` back to ``class_``, validates the result, and returns
    the finding dict.  Raises :class:`ValueError` if the deserialized finding
    is invalid.
    """
    finding: dict[str, Any] = {}
    for key, value in d.items():
        internal_key = "class_" if key == "class" else key
        finding[internal_key] = value
    is_valid, errors = validate_finding(finding)
    if not is_valid:
        raise ValueError(
            "Deserialized finding is invalid:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return finding
