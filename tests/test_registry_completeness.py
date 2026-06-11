"""Registry-completeness test: every component name passed to
run_component_subprocess across all phase_handlers MUST exist in
master_controller.COMPONENTS.

This is the systemic fix for the capsule_writer registry crash
(2026-06-12): an unregistered component name causes a ValueError
at runtime. This test catches such gaps statically so they never
reach a production run.
"""

from __future__ import annotations

import ast
import os
import sys

import pytest

# Ensure pipeline is importable.
PIPELINE_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "pipeline")
sys.path.insert(0, PIPELINE_DIR)


def _extract_run_component_subprocess_names(filepath: str) -> set[str]:
    """Parse a Python file's AST and extract every string literal passed
    as the first positional argument to ``run_component_subprocess(...)``
    or ``mc.run_component_subprocess(...)``.
    """
    with open(filepath, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=filepath)

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match: run_component_subprocess("name", ...) or
        #        mc.run_component_subprocess("name", ...)
        is_target = False
        if isinstance(func, ast.Name) and func.id == "run_component_subprocess":
            is_target = True
        elif isinstance(func, ast.Attribute) and func.attr == "run_component_subprocess":
            is_target = True
        if is_target and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                names.add(first_arg.value)
    return names


def _collect_all_referenced_component_names() -> dict[str, set[str]]:
    """Scan all phase_handler files + master_controller for component
    names passed to run_component_subprocess.  Returns {filepath: {names}}.
    """
    results: dict[str, set[str]] = {}
    for fname in os.listdir(PIPELINE_DIR):
        if not fname.endswith(".py"):
            continue
        if "phase_handler" in fname or "master_controller" in fname:
            fpath = os.path.join(PIPELINE_DIR, fname)
            names = _extract_run_component_subprocess_names(fpath)
            if names:
                results[fpath] = names
    return results


def test_all_dispatched_components_are_registered():
    """Every component name passed to run_component_subprocess must
    exist as a key in master_controller.COMPONENTS.
    """
    import master_controller as mc

    referenced = _collect_all_referenced_component_names()
    all_names: set[str] = set()
    for names in referenced.values():
        all_names |= names

    missing = all_names - set(mc.COMPONENTS.keys())
    assert not missing, (
        f"Component name(s) passed to run_component_subprocess but missing "
        f"from COMPONENTS registry: {sorted(missing)}.  "
        f"Sources: { {os.path.basename(k): sorted(v & missing) for k, v in referenced.items() if v & missing} }"
    )


def test_components_registry_is_not_empty():
    """Sanity: COMPONENTS must contain at least the core pipeline modules."""
    import master_controller as mc
    assert len(mc.COMPONENTS) >= 10, (
        f"COMPONENTS has only {len(mc.COMPONENTS)} entries — expected ≥10"
    )


# ─── Inverse test: registered COMPONENTS that are never dispatched ──────────

# Components that are legitimately never called via run_component_subprocess
# because they serve other roles (non-pipeline utilities, manifest-level
# tools, or inline-invoked components).
_ALLOWED_UNDISPATCHED = {
    "manifest_auditor",            # called inline by master_controller, not via run_component_subprocess
    "preflight",                   # invoked via preflight_stub inline or subprocess
    "book_archive",                # operator-invoked utility, no phase dispatch
    "anpd_export",                 # operator-invoked utility, no phase dispatch
    "scene_auditor",               # called internally by scene loop (may not appear in AST scan)
    "character_profile_auditor",   # called internally by character_generator
    "synopsis_summarizer",         # manifest trigger: after_phase_commit:synopsis_generator
    "manuscript_summarizer",       # manifest trigger: after_phase_commit:manuscript_assembler
    "outline_comparator",          # called internally by synopsis gate (not via subprocess in current code)
}


def test_registered_components_are_dispatched():
    """Every COMPONENTS entry must be dispatched by at least one
    phase_handler or master_controller call to run_component_subprocess,
    unless it is in the allowed-undispatched set.

    This catches the manuscript_assembler-class bug: a component
    registered in COMPONENTS and manifest but never called by any
    phase handler.
    """
    import master_controller as mc

    referenced = _collect_all_referenced_component_names()
    all_dispatched: set[str] = set()
    for names in referenced.values():
        all_dispatched |= names

    registered = set(mc.COMPONENTS.keys())
    never_dispatched = registered - all_dispatched - _ALLOWED_UNDISPATCHED

    assert not never_dispatched, (
        f"COMPONENTS entries that are never dispatched by any phase handler: "
        f"{sorted(never_dispatched)}.  Either add dispatch in phase_handlers "
        f"or add to _ALLOWED_UNDISPATCHED with justification."
    )
