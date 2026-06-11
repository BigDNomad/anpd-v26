# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V25 Master Controller — V24 architecture, V25 component set, with manifest_auditor preflight gate

Evolves master_controller_20260430_0500.py (Commit 3 structural skeleton) with:

1. Manifest-driven dispatch awareness — loads pipeline_manifest.json at run start,
   uses manifest metadata for component ordering and stub/optional detection.
2. RuntimeVerifier integration — instantiated once per run after preflight,
   invoked between component invocations (R-rules) and after final phase (C-rules).
3. STOP_REPORT written and run halted on any Class A finding from preflight,
   runtime_verifier R-rules, or runtime_verifier C-rules.

Phase execution logic remains in phase_handlers.py (55 tests, unchanged).
RuntimeVerifier sits downstream: after each component invocation completes,
master_controller feeds exit_code + runtime into the verifier, which checks
outputs exist, are fresh, parse as valid JSON, and conform to schema (R001-R007).

Authority: ANPD_V24_runtime_verifier_Component_Design §10,
           ANPD_V24_Verification_Rules §6-§8.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config_resolver import resolve_config


# ─── Phase identifiers (per design doc §2) ────────────────────────────────────

PHASE_PREFLIGHT = "preflight"
PHASE_EFFECTIVE_CONFIG = "effective_config"
PHASE_SYNOPSIS = "synopsis"
PHASE_CHARACTER = "character"
PHASE_SCENES = "scenes"
PHASE_CHAPTERS = "chapters"
PHASE_MANUSCRIPT = "manuscript"
PHASE_FORMAT = "format"
PHASE_CAPSULE = "capsule"
PHASE_RECEIPT = "receipt"

PHASES_IN_ORDER = [
    PHASE_PREFLIGHT,
    PHASE_EFFECTIVE_CONFIG,
    PHASE_SYNOPSIS,
    PHASE_CHARACTER,
    PHASE_SCENES,
    PHASE_CHAPTERS,
    PHASE_MANUSCRIPT,
    PHASE_FORMAT,
    PHASE_CAPSULE,
    PHASE_RECEIPT,
]

# --from-phase only accepts user-resumable phases (effective_config + receipt
# are not resumption entry points).
RESUMABLE_PHASES = [
    PHASE_PREFLIGHT,
    PHASE_SYNOPSIS,
    PHASE_CHARACTER,
    PHASE_SCENES,
    PHASE_CHAPTERS,
    PHASE_MANUSCRIPT,
    PHASE_FORMAT,
    PHASE_CAPSULE,
]


# ─── COMPONENTS registry (per design doc §3) ──────────────────────────────────

PIPELINE_DIR = "/anpd/v26/pipeline"

COMPONENTS: dict[str, str] = {
    "preflight":                 os.path.join(PIPELINE_DIR, "preflight.py"),
    "synopsis_generator":        os.path.join(PIPELINE_DIR, "synopsis_generator.py"),
    "synopsis_auditor":          os.path.join(PIPELINE_DIR, "synopsis_auditor.py"),
    "synopsis_summarizer":       os.path.join(PIPELINE_DIR, "synopsis_summarizer.py"),
    "character_generator":       os.path.join(PIPELINE_DIR, "character_generator.py"),
    "character_profile_auditor": os.path.join(PIPELINE_DIR, "character_profile_auditor.py"),
    "scene_writer":              os.path.join(PIPELINE_DIR, "scene_writer.py"),
    "scene_auditor":             os.path.join(PIPELINE_DIR, "scene_auditor.py"),
    "state_tracker":             os.path.join(PIPELINE_DIR, "state_tracker.py"),
    "scene_formatter":           os.path.join(PIPELINE_DIR, "scene_formatter.py"),
    "manuscript_auditor":        os.path.join(PIPELINE_DIR, "manuscript_auditor.py"),
    "manuscript_assembler":      os.path.join(PIPELINE_DIR, "manuscript_assembler.py"),
    "outline_comparator":        os.path.join(PIPELINE_DIR, "outline_comparator.py"),
    "manuscript_summarizer":     os.path.join(PIPELINE_DIR, "manuscript_summarizer.py"),
    "manifest_auditor":          os.path.join(PIPELINE_DIR, "manifest_auditor.py"),
}

# Mode flag — only new_book is handled by this controller.
SUPPORTED_MODES = {"new_book"}

# Manifest path (canonical).
MANIFEST_PATH = "/anpd/v26/pipeline/pipeline_manifest.json"


# ─── STOP_REPORT helpers (per design doc §6) ──────────────────────────────────

def stop_report_path(book_dir: str) -> str:
    """Canonical STOP_REPORT location per Data Standards §2.7."""
    return os.path.join(book_dir, "out", "reports", "STOP_REPORT.json")


def find_latest_file(directory: str, pattern: str) -> str | None:
    """Return path to the most-recently-modified file matching pattern."""
    import glob as _glob
    matches = _glob.glob(os.path.join(directory, pattern))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def detect_stop_report(book_dir: str, since_mtime: float) -> dict | None:
    """Check for a STOP_REPORT.json written after the given mtime."""
    path = stop_report_path(book_dir)
    if not os.path.isfile(path):
        return None
    if os.path.getmtime(path) <= since_mtime:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"_unparseable": True, "path": path}


def write_stop_report(
    book_dir: str,
    component: str,
    phase: int,
    error_type: str,
    error_message: str,
    suggested_fix: str,
    pipeline_state_description: str,
    file_path: str | None = None,
    scene_number: int | None = None,
) -> str:
    """Write a STOP_REPORT.json per Data Standards §4.6 schema.

    Does NOT overwrite existing component STOP_REPORTs (per §6.3).
    """
    path = stop_report_path(book_dir)
    if os.path.isfile(path):
        return path

    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "component": component,
        "phase": phase,
        "scene_number": scene_number,
        "error_type": error_type,
        "error_message": error_message,
        "file_path": file_path,
        "suggested_fix": suggested_fix,
        "pipeline_state": pipeline_state_description,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


# ─── Subprocess invocation helper (per design doc §9) ─────────────────────────

def run_component_subprocess(
    component_name: str,
    args: list[str],
    book_dir: str,
    pipeline_state: dict,
    scene_number: int | None = None,
) -> dict:
    """Invoke a registered component as subprocess.

    Returns dict with exit_code, stdout, stderr, duration_seconds, stubbed,
    stop_report_written_during_call, stop_report_payload.
    """
    if component_name not in COMPONENTS:
        raise ValueError(
            f"unknown component: {component_name!r} "
            f"(not in COMPONENTS registry)"
        )

    script_path = COMPONENTS[component_name]
    started_at = datetime.now(timezone.utc).isoformat()
    started_at_epoch = datetime.now(timezone.utc).timestamp()

    # Stub case: component script doesn't exist on disk.
    if not os.path.isfile(script_path):
        ended_at = datetime.now(timezone.utc).isoformat()
        pipeline_state["invocation_timeline"].append({
            "component": component_name,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": "stubbed",
            "finding_count": 0,
            "stop_report_written": False,
            "scene_number": scene_number,
        })
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"component {component_name!r} not yet built at {script_path}",
            "duration_seconds": 0.0,
            "stubbed": True,
            "stop_report_written_during_call": False,
            "stop_report_payload": None,
        }

    # Real invocation.
    pre_invocation_mtime = (
        os.path.getmtime(stop_report_path(book_dir))
        if os.path.isfile(stop_report_path(book_dir))
        else 0.0
    )

    try:
        result = subprocess.run(
            [sys.executable, script_path] + args,
            capture_output=True,
            text=True,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        launch_failed = False
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = -2
        stdout = ""
        stderr = f"subprocess launch failure: {exc}"
        launch_failed = True

    ended_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = datetime.now(timezone.utc).timestamp() - started_at_epoch

    # Detect component-written STOP_REPORT.
    stop_report = detect_stop_report(book_dir, pre_invocation_mtime)
    stop_report_written = stop_report is not None

    # Update pipeline_state.
    pipeline_state["components_called"][component_name] = True
    if launch_failed:
        status = "failed"
    elif exit_code == 0 and not stop_report_written:
        status = "succeeded"
    else:
        status = "failed"
    pipeline_state["invocation_timeline"].append({
        "component": component_name,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": status,
        "finding_count": 0,
        "stop_report_written": stop_report_written,
        "scene_number": scene_number,
    })

    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": duration_seconds,
        "stubbed": False,
        "stop_report_written_during_call": stop_report_written,
        "stop_report_payload": stop_report,
    }


# ─── Pipeline state init (per design doc §5 PIPELINE_RECEIPT schema) ──────────

def _read_git_commit_hash() -> str:
    """Capture current git commit hash per Data Standards §9.2."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd="/anpd/v26",
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def init_pipeline_state(
    args: argparse.Namespace,
    effective_config_snapshot: dict,
) -> dict:
    """Initialize pipeline_state dict to Data Standards §4.5 schema + V24 extensions."""
    intake_data = {}
    try:
        with open(args.intake, "r", encoding="utf-8") as fh:
            intake_data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "run_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "git_commit_hash": _read_git_commit_hash(),
        "pipeline_mode": args.mode,
        "series": effective_config_snapshot.get("series_directory", ""),
        "book_number": intake_data.get("book_number"),
        "title": intake_data.get("book_title", intake_data.get("title", "")),
        "components_called": {name: False for name in COMPONENTS},
        "scenes_generated": 0,
        "scenes_audited": 0,
        "scenes_corrected": 0,
        "correction_rate": 0.0,
        "class_a_failures": 0,
        "class_b_violations": 0,
        "hard_stop": False,
        "output_valid": False,
        "effective_config_snapshot": effective_config_snapshot,
        "gate_verdicts": {
            "synopsis":            "not_yet_run",
            "character_profiles":  "not_yet_run",
            "manuscript":          "not_yet_run",
        },
        "capsule_paths": {
            "forward": None,
        },
        "advisory_phase_failures": [],
        "cost_log": [],
        "invocation_timeline": [],
        "_args": vars(args),
        "_started_at_epoch": datetime.now(timezone.utc).timestamp(),
    }


# ─── Phase prerequisite stubs ────────────────────────────────────────────────

def phase_prerequisites_satisfied(
    phase_name: str,
    pipeline_state: dict,
    args: argparse.Namespace,
) -> tuple[bool, str]:
    """Check whether prerequisites for a phase are met on disk."""
    _ = (phase_name, pipeline_state, args)
    return (True, "")


# ─── Preflight stub (inline until preflight.py ships) ─────────────────────────

def preflight_stub(args: argparse.Namespace, pipeline_state: dict) -> list[dict]:
    """Minimal inline preflight pending preflight.py."""
    findings: list[dict] = []

    required = [
        (args.intake, "intake.json"),
        (args.series_config, "series_config.json"),
    ]
    for path, label in required:
        if not os.path.isfile(path):
            findings.append({
                "class": "A",
                "component": "master_controller",
                "phase": 1,
                "message": f"required file missing: {label} at {path}",
                "suggested_fix": f"verify {label} exists at the configured path",
            })

    if not os.path.isdir(args.book_dir):
        findings.append({
            "class": "A",
            "component": "master_controller",
            "phase": 1,
            "message": f"--book-dir does not exist: {args.book_dir}",
            "suggested_fix": "verify --book-dir path or create the directory",
        })
    if not os.path.isdir(args.series_dir):
        findings.append({
            "class": "A",
            "component": "master_controller",
            "phase": 1,
            "message": f"--series-dir does not exist: {args.series_dir}",
            "suggested_fix": "verify --series-dir path",
        })

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd="/anpd/v26",
        )
        if result.returncode == 0 and result.stdout.strip():
            findings.append({
                "class": "B",
                "component": "master_controller",
                "phase": 1,
                "message": "git working tree is dirty (Data Standards §9.2)",
                "suggested_fix": (
                    "commit or stash uncommitted changes before production run "
                    "(Class A once preflight.py ships; Class B during stub period)"
                ),
            })
    except (OSError, subprocess.SubprocessError):
        pass

    findings.append({
        "class": "B",
        "component": "master_controller",
        "phase": 1,
        "message": "preflight.py not yet built — stubbed inline minimal checks",
        "suggested_fix": "no operator action; resolves when preflight.py ships",
    })

    for f in findings:
        if f["class"] == "A":
            pipeline_state["class_a_failures"] += 1
        elif f["class"] == "B":
            pipeline_state["class_b_violations"] += 1

    return findings


# ─── Manifest loading ────────────────────────────────────────────────────────

def load_manifest(manifest_path: str = MANIFEST_PATH) -> dict:
    """Load and return pipeline_manifest.json.

    Returns the full manifest dict. Raises on parse failure.
    """
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_manifest_components_sorted(manifest: dict) -> list[dict]:
    """Return pipeline_components sorted by phase ascending, then execution_order ascending."""
    components = manifest.get("pipeline_components", [])
    return sorted(components, key=lambda c: (c.get("phase", 0), c.get("execution_order", 0)))


def manifest_component_should_run(entry: dict) -> bool:
    """Determine if a manifest component should be executed.

    Returns False for stubs. Returns True for required non-stubs.
    Optional (required=false) non-stubs return True (caller may further
    filter based on book_config flags via optional_by_config).
    """
    if entry.get("is_stub", False):
        return False
    return True


def get_failure_mode_for_phase(manifest: dict | None, phase_name: str) -> str:
    """Look up failure_mode for components active in a given phase.

    If ANY component in the phase has failure_mode='advisory', the phase
    is treated as advisory. Default is 'halt'.

    This is consulted after phase dispatch to decide whether a failure
    halts the pipeline or is logged as an advisory warning.
    """
    if manifest is None:
        return "halt"
    # Map phase names to phase numbers used in manifest.
    phase_name_to_components: dict[str, list[str]] = {
        "synopsis": ["synopsis_generator", "synopsis_auditor", "synopsis_summarizer"],
        "character": ["character_generator", "character_profile_auditor"],
        "scenes": ["scene_writer", "state_tracker"],
        "chapters": ["scene_formatter"],
        "manuscript": ["manuscript_auditor"],
        "format": ["formatter"],
        "capsule": ["capsule_writer"],
    }
    relevant_components = phase_name_to_components.get(phase_name, [])
    for entry in manifest.get("pipeline_components", []):
        if entry.get("component_name") in relevant_components:
            if entry.get("failure_mode") == "advisory":
                return "advisory"
    return "halt"


def get_failure_mode_for_component(manifest: dict | None, component_name: str) -> str:
    """Look up failure_mode for a specific manifest component entry.

    Returns 'advisory' or 'halt' (default).
    """
    if manifest is None:
        return "halt"
    for entry in manifest.get("pipeline_components", []):
        if entry.get("component_name") == component_name:
            return entry.get("failure_mode", "halt")
    return "halt"


# ─── RuntimeVerifier integration helpers ─────────────────────────────────────

def _verify_new_invocations(
    pipeline_state: dict,
    verifier: Any,
    timeline_offset: int,
    iteration_counters: dict[str, int],
) -> tuple[int, bool, str | None]:
    """Verify all new invocations since timeline_offset.

    Returns (new_offset, had_class_a_failure, failed_component_name).
    If Class A found, writes STOP_REPORT via verifier and returns True
    with the name of the component that triggered the failure.
    """
    timeline = pipeline_state["invocation_timeline"]
    new_entries = timeline[timeline_offset:]
    new_offset = len(timeline)

    for entry in new_entries:
        component_name = entry["component"]
        status = entry["status"]

        # Skip stubbed components — no verification needed.
        if status == "stubbed":
            continue

        # Determine exit code from status.
        if status == "succeeded":
            exit_code = 0
        else:
            exit_code = 1  # failed

        # Calculate duration from timestamps.
        try:
            started = datetime.fromisoformat(entry["started_at"])
            ended = datetime.fromisoformat(entry["ended_at"])
            runtime_seconds = (ended - started).total_seconds()
        except (ValueError, KeyError):
            runtime_seconds = 0.0

        # Determine iteration_index for multi-instance components.
        # Prefer the actual scene_number recorded on the invocation (robust
        # against re-entry / skipped scenes / --force). Fall back to the
        # legacy counter only if scene_number was not recorded.
        manifest_entry = verifier._component_entries.get(component_name)
        if manifest_entry and manifest_entry.get("multi_instance", False):
            recorded_scene = entry.get("scene_number")
            if recorded_scene is not None:
                iteration_index = recorded_scene - 1  # verifier maps idx+1 → scene
            else:
                iteration_index = iteration_counters.get(component_name, 0)
                iteration_counters[component_name] = iteration_index + 1
        else:
            iteration_index = None

        # Set context and verify.
        verifier.set_component_context(
            component_name, exit_code, runtime_seconds, iteration_index
        )
        result = verifier.verify_component_completion(
            component_name, iteration_index
        )

        # Print verification results per P003.
        for finding in result.findings:
            severity_label = "FAIL" if finding.severity == "A" else "WARN"
            print(f"  [{severity_label}] {finding.rule_id} {finding.error_code}: "
                  f"{finding.suggested_fix[:100]}")

        if result.has_class_a_failure:
            verifier.write_stop_report(result)
            return new_offset, True, component_name

    return new_offset, False, None


# ─── Main orchestration ──────────────────────────────────────────────────────

def run_pipeline(args: argparse.Namespace) -> int:
    """Main orchestration loop with manifest-driven dispatch and runtime verification.

    Returns process exit code (0 success, 1 hard stop, 2 fatal error).
    """
    # Resolve effective_config first.
    try:
        effective_config = resolve_config(args.series_config)
    except (OSError, FileNotFoundError, json.JSONDecodeError) as exc:
        os.makedirs(os.path.join(args.book_dir, "out", "reports"), exist_ok=True)
        write_stop_report(
            book_dir=args.book_dir,
            component="master_controller",
            phase=2,
            error_type="Class A",
            error_message=f"effective_config resolution failed: {exc}",
            suggested_fix=(
                "verify series_config.json, genre template, and "
                "banned_phrases.json all exist and are valid JSON"
            ),
            pipeline_state_description="halted before pipeline_state init",
            file_path=args.series_config,
        )
        print(f"FATAL: effective_config resolution failed: {exc}", file=sys.stderr)
        return 2

    pipeline_state = init_pipeline_state(args, effective_config)

    # Preflight.
    print("=" * 70)
    print(f"  ANPD V25 master_controller — {args.mode}")
    print(f"  book: {args.book_dir}")
    print(f"  series: {args.series_dir}")
    print(f"  git: {pipeline_state['git_commit_hash']}")
    print("=" * 70)
    # ── Manifest audit (V25 addition) ─────────────────────────────────────
    print("\nPHASE 1a — manifest_auditor")
    try:
        from manifest_auditor import run_manifest_audit
        audit_result = run_manifest_audit(
            manifest_path=MANIFEST_PATH,
            master_controller_path=os.path.abspath(__file__),
            report_dir=os.path.join(args.book_dir, "out", "manifest_audits"),
        )
        if not audit_result["passed"]:
            print(f"  [manifest_auditor] FAIL — {len(audit_result['class_a_findings'])} Class A findings")
            for f in audit_result["class_a_findings"][:5]:
                print(f"    [A] {f.get('check', '?')}: {f.get('message', '')}")
            write_stop_report(
                book_dir=args.book_dir,
                component="manifest_auditor",
                phase=1,
                error_type="Class A",
                error_message=f"manifest_auditor: {len(audit_result['class_a_findings'])} Class A findings",
                suggested_fix=f"review {audit_result['report_path']}",
                pipeline_state_description="halted at manifest_auditor preflight gate",
            )
            pipeline_state["hard_stop"] = True
            _finalize_receipt(pipeline_state, args.book_dir)
            return 1
        print(f"  [manifest_auditor] PASS")
    except Exception as exc:
        print(f"  [manifest_auditor] FATAL: {exc}")
        write_stop_report(
            book_dir=args.book_dir,
            component="manifest_auditor",
            phase=1,
            error_type="Class A",
            error_message=f"manifest_auditor crashed: {exc}",
            suggested_fix="check manifest_auditor.py implementation",
            pipeline_state_description="halted at manifest_auditor preflight gate",
        )
        pipeline_state["hard_stop"] = True
        _finalize_receipt(pipeline_state, args.book_dir)
        return 1

    print("\nPHASE 1b — preflight (stubbed)")
    findings = preflight_stub(args, pipeline_state)
    class_a = [f for f in findings if f["class"] == "A"]
    for f in findings:
        print(f"  [{f['class']}] {f['message']}")

    if class_a:
        write_stop_report(
            book_dir=args.book_dir,
            component="master_controller",
            phase=1,
            error_type="Class A",
            error_message=(
                f"preflight Class A finding(s): "
                f"{[f['message'] for f in class_a]}"
            ),
            suggested_fix="address Class A findings listed above",
            pipeline_state_description="halted at preflight",
        )
        pipeline_state["hard_stop"] = True
        _finalize_receipt(pipeline_state, args.book_dir)
        return 1

    # Dry-run early exit.
    if args.dry_run:
        print("\n--dry-run: preflight passed; exiting without phase execution.")
        _finalize_receipt(pipeline_state, args.book_dir)
        return 0

    # ─── RuntimeVerifier instantiation (Stage 8) ─────────────────────────
    # Per runtime_verifier Component Design §10: instantiate once per run,
    # after preflight passes, before component dispatch begins.
    verifier = None
    try:
        from runtime_verifier import RuntimeVerifier
        manifest_path = Path(MANIFEST_PATH)
        if manifest_path.exists():
            run_id = f"{datetime.now(timezone.utc).isoformat()}_{pipeline_state['series']}_{pipeline_state.get('book_number', 'unknown')}"
            run_start_time = datetime.now(timezone.utc)

            book_dir_path = Path(args.book_dir)
            receipt_path = book_dir_path / "out" / "reports" / "PIPELINE_RECEIPT.json"
            stop_rpt_path = book_dir_path / "out" / "reports" / "STOP_REPORT.json"

            verifier = RuntimeVerifier(
                manifest_path=manifest_path,
                run_id=run_id,
                run_start_time=run_start_time,
                receipt_path=receipt_path,
                stop_report_path=stop_rpt_path,
                log_file_access=False,
                series_name=os.path.basename(args.series_dir),
                book_number=pipeline_state.get("book_number") or 1,
            )
            print("\n  [runtime_verifier] instantiated — R-rules active between components")
        else:
            print("\n  [runtime_verifier] SKIPPED — pipeline_manifest.json not found")
    except (ImportError, OSError, json.JSONDecodeError) as exc:
        print(f"\n  [runtime_verifier] SKIPPED — instantiation failed: {exc}")
        verifier = None

    # Track verification state.
    timeline_offset = len(pipeline_state["invocation_timeline"])
    iteration_counters: dict[str, int] = {}

    # Load manifest dict for failure_mode lookup (advisory vs halt).
    loaded_manifest: dict | None = None
    try:
        loaded_manifest = load_manifest()
    except (OSError, json.JSONDecodeError):
        pass

    # ─── --from-phase resumption ─────────────────────────────────────────
    from_phase = args.from_phase or PHASE_PREFLIGHT
    skip_until = PHASES_IN_ORDER.index(from_phase) if from_phase in PHASES_IN_ORDER else 0

    # ─── Phase dispatch via phase_handlers ───────────────────────────────
    import phase_handlers

    intake_data = {}
    try:
        with open(args.intake, "r", encoding="utf-8") as fh:
            intake_data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass

    synopsis_path: str | None = None
    character_profiles_path: str | None = None
    docx_path: str | None = None
    capsule_path: str | None = None

    manuscript_regen_attempts = 0

    phase_idx = 0
    while phase_idx < len(PHASES_IN_ORDER):
        phase_name = PHASES_IN_ORDER[phase_idx]

        if phase_idx < skip_until:
            print(f"\nPHASE {phase_idx + 1} — {phase_name}: SKIPPED (--from-phase {from_phase})")
            phase_idx += 1
            continue
        if phase_name in (PHASE_PREFLIGHT, PHASE_EFFECTIVE_CONFIG):
            phase_idx += 1
            continue
        if phase_name == PHASE_RECEIPT:
            phase_idx += 1
            continue

        ok, missing_reason = phase_prerequisites_satisfied(
            phase_name, pipeline_state, args
        )
        if not ok:
            print(f"\nPHASE {phase_idx + 1} — {phase_name}: PREREQUISITE FAILURE — {missing_reason}")
            write_stop_report(
                book_dir=args.book_dir,
                component="master_controller",
                phase=phase_idx + 1,
                error_type="Class A",
                error_message=f"{phase_name} prerequisite missing: {missing_reason}",
                suggested_fix="address missing prerequisite or use --from-phase",
                pipeline_state_description=f"halted at {phase_name}",
            )
            pipeline_state["hard_stop"] = True
            _finalize_receipt(pipeline_state, args.book_dir)
            return 1

        # Phase dispatch.
        print(f"\nPHASE {phase_idx + 1} — {phase_name}: starting")
        phase_result: dict
        if phase_name == PHASE_SYNOPSIS:
            phase_result = phase_handlers.handle_synopsis_gate(
                args, pipeline_state, effective_config
            )
            if phase_result.get("synopsis_path"):
                synopsis_path = phase_result["synopsis_path"]
        elif phase_name == PHASE_CHARACTER:
            phase_result = phase_handlers.handle_character_gate(
                args, pipeline_state, effective_config
            )
            if phase_result.get("character_profiles_path"):
                character_profiles_path = phase_result["character_profiles_path"]
        elif phase_name == PHASE_SCENES:
            phase_result = phase_handlers.handle_scene_loop(
                args, pipeline_state, effective_config,
                synopsis_path, character_profiles_path,
            )
        elif phase_name == PHASE_CHAPTERS:
            phase_result = phase_handlers.handle_chapter_assembly(
                args, pipeline_state, effective_config
            )
        elif phase_name == PHASE_MANUSCRIPT:
            phase_result = phase_handlers.handle_manuscript_gate(
                args, pipeline_state, effective_config
            )
            if phase_result.get("verdict") == "tier_3_regen_needed":
                # F0 guard: in-place regen is disabled (state-chain invariant).
                # The gate no longer emits this verdict; if it somehow does,
                # do NOT delete scenes / restart phase 5 — hard stop instead.
                print("  tier_3_regen_needed received but in-place regen is "
                      "DISABLED (state-chain invariant) — halting")
                pipeline_state["gate_verdicts"]["manuscript"] = "fail"
                write_stop_report(
                    book_dir=args.book_dir,
                    component="master_controller",
                    phase=phase_idx + 1,
                    error_type="Class A",
                    error_message="manuscript gate requested in-place regen, which is disabled (state-chain invariant)",
                    suggested_fix="fix findings upstream (synopsis) or build the state-preserving fixer; do not regenerate scenes in place",
                    pipeline_state_description="halted at manuscript gate — in-place regen disabled",
                )
                pipeline_state["hard_stop"] = True
                _finalize_receipt(pipeline_state, args.book_dir)
                return 1
                manuscript_regen_attempts += 1
                if manuscript_regen_attempts > args.max_retries_per_gate:
                    print(f"  Tier 3 regen attempts exhausted ({args.max_retries_per_gate})")
                    pipeline_state["gate_verdicts"]["manuscript"] = "fail"
                    write_stop_report(
                        book_dir=args.book_dir,
                        component="master_controller",
                        phase=phase_idx + 1,
                        error_type="Class A",
                        error_message=f"Tier 3 regen attempts exhausted",
                        suggested_fix="review manuscript audit findings",
                        pipeline_state_description="halted at manuscript gate after tier 3 regen exhaustion",
                    )
                    pipeline_state["hard_stop"] = True
                    _finalize_receipt(pipeline_state, args.book_dir)
                    return 1
                affected_scenes = phase_result.get("scenes_to_regenerate", [])
                if affected_scenes:
                    print(f"  Regenerating scenes {affected_scenes}; restarting from phase 5")
                    _delete_scene_files(args.book_dir, affected_scenes)
                else:
                    print(f"  No specific scenes identified; restarting full scene loop from phase 5")
                phase_idx = PHASES_IN_ORDER.index(PHASE_SCENES)
                continue
        elif phase_name == PHASE_FORMAT:
            phase_result = phase_handlers.handle_format(
                args, pipeline_state, effective_config, intake_data
            )
            if phase_result.get("docx_path"):
                docx_path = phase_result["docx_path"]
        elif phase_name == PHASE_CAPSULE:
            phase_result = phase_handlers.handle_capsule_write(
                args, pipeline_state, effective_config
            )
            if phase_result.get("capsule_path"):
                capsule_path = phase_result["capsule_path"]
        else:
            phase_idx += 1
            continue

        # Process phase result.
        verdict = phase_result.get("verdict", "halt")
        phase_findings = phase_result.get("findings", [])
        for f in phase_findings:
            print(f"  [{f.get('class', '?')}] {f.get('message', '')}")

        if verdict == "halt":
            # Determine which specific component caused the failure for
            # per-component failure_mode lookup (fixes advisory leak where
            # an advisory component like synopsis_summarizer would make the
            # entire phase advisory, masking real failures in synopsis_auditor).
            failed_component = phase_result.get("failed_component", phase_name)
            if failed_component == phase_name and phase_findings:
                failed_component = phase_findings[0].get("component", phase_name)
            component_failure_mode = get_failure_mode_for_component(loaded_manifest, failed_component)
            if component_failure_mode == "advisory":
                via = phase_result.get("via", "phase_logic")
                print(f"\nPHASE {phase_idx + 1} — {phase_name}: ADVISORY FAILURE (component={failed_component}, via {via}) — continuing pipeline")
                pipeline_state["advisory_phase_failures"].append({
                    "phase": phase_name,
                    "phase_index": phase_idx + 1,
                    "component": failed_component,
                    "via": via,
                    "findings": phase_findings,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                })
                phase_idx += 1
                continue

            via = phase_result.get("via", "phase_logic")
            print(f"\nPHASE {phase_idx + 1} — {phase_name}: HALT (via {via})")
            if via != "component_stop_report":
                write_stop_report(
                    book_dir=args.book_dir,
                    component="master_controller",
                    phase=phase_idx + 1,
                    error_type="Class A",
                    error_message=f"{phase_name} halted: {phase_findings[0].get('message', 'no detail') if phase_findings else 'no findings'}",
                    suggested_fix=phase_findings[0].get("suggested_fix", "review findings") if phase_findings else "review run output",
                    pipeline_state_description=f"halted at {phase_name}",
                )
            pipeline_state["hard_stop"] = True
            _finalize_receipt(pipeline_state, args.book_dir)
            return 1

        print(f"PHASE {phase_idx + 1} — {phase_name}: PASS")

        # ─── Runtime verification between phases (Stage 8) ───────────────
        # After each successful phase, verify all new component invocations
        # via RuntimeVerifier R-rules. Halt on Class A finding (unless advisory).
        if verifier is not None:
            timeline_offset, had_failure, failed_verifier_comp = _verify_new_invocations(
                pipeline_state, verifier, timeline_offset, iteration_counters
            )
            if had_failure:
                # Per-component failure_mode lookup (not phase-level) to avoid
                # advisory leak from co-phase advisory components.
                rv_comp = failed_verifier_comp or phase_name
                rv_failure_mode = get_failure_mode_for_component(loaded_manifest, rv_comp)
                if rv_failure_mode == "advisory":
                    print(f"\n  [runtime_verifier] Class A finding after {phase_name} (component={rv_comp}) — ADVISORY (continuing)")
                    pipeline_state["advisory_phase_failures"].append({
                        "phase": phase_name,
                        "phase_index": phase_idx + 1,
                        "component": rv_comp,
                        "via": "runtime_verifier_class_a",
                        "findings": [],
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    })
                    phase_idx += 1
                    continue
                print(f"\n  [runtime_verifier] Class A finding after {phase_name} — HALT")
                pipeline_state["hard_stop"] = True
                pipeline_state["class_a_failures"] += 1
                _finalize_receipt(pipeline_state, args.book_dir)
                return 1

        phase_idx += 1

    # ─── Post-run verification (C-rules) ─────────────────────────────────
    if verifier is not None:
        # Verify any remaining unverified invocations.
        timeline_offset, had_failure, _ = _verify_new_invocations(
            pipeline_state, verifier, timeline_offset, iteration_counters
        )
        if had_failure:
            print("\n  [runtime_verifier] Class A finding (final R-rules) — HALT")
            pipeline_state["hard_stop"] = True
            pipeline_state["class_a_failures"] += 1
            _finalize_receipt(pipeline_state, args.book_dir)
            return 1

        # C-rules: verify run completion.
        print("\n  [runtime_verifier] running C-rules (post-run verification)")
        final_result = verifier.verify_run_completion()

        for finding in final_result.findings:
            severity_label = "FAIL" if finding.severity == "A" else "WARN"
            print(f"  [{severity_label}] {finding.rule_id} {finding.error_code}: "
                  f"{finding.suggested_fix[:100]}")

        if final_result.has_class_a_failure:
            verifier.write_stop_report(final_result)
            print("\n  [runtime_verifier] Class A finding (C-rules) — HALT")
            pipeline_state["hard_stop"] = True
            pipeline_state["class_a_failures"] += 1
            _finalize_receipt(pipeline_state, args.book_dir)
            return 1

        print("  [runtime_verifier] all C-rules PASS")

    # Receipt finalization.
    _finalize_receipt(pipeline_state, args.book_dir)
    print("\n" + "=" * 70)
    print("  master_controller run complete.")
    print(f"  PIPELINE_RECEIPT: {os.path.join(args.book_dir, 'out', 'reports', 'PIPELINE_RECEIPT.json')}")
    print("=" * 70)
    return 0


def _finalize_receipt(pipeline_state: dict, book_dir: str) -> None:
    """Hand off to pipeline_receipt_writer for the actual write."""
    from pipeline_receipt_writer import write_receipt
    write_receipt(pipeline_state, book_dir)


def _delete_scene_files(book_dir: str, scene_numbers: list[int]) -> None:
    """Delete scene files so the scene loop re-generates them."""
    import glob as _glob
    scenes_dir = os.path.join(book_dir, "out", "scenes")
    state_dir = os.path.join(book_dir, "out", "state")
    for sn in scene_numbers:
        scene_pattern = os.path.join(scenes_dir, f"sc{sn:02d}_*.md")
        for path in _glob.glob(scene_pattern):
            try:
                os.remove(path)
            except OSError:
                pass
        state_path = os.path.join(state_dir, f"state_after_sc{sn:02d}.json")
        if os.path.isfile(state_path):
            try:
                os.remove(state_path)
            except OSError:
                pass


# ─── CLI (per design doc §4) ──────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="master_controller.py",
        description=(
            "ANPD V25 master_controller — new-book pipeline orchestrator. "
            "For rewrite mode, use rewrite_master_controller.py."
        ),
    )
    parser.add_argument("--book-dir", required=True,
                        help="Path to book directory")
    parser.add_argument("--series-dir", required=True,
                        help="Path to series directory")
    parser.add_argument("--intake", required=True,
                        help="Path to intake.json")
    parser.add_argument("--series-config", required=True,
                        help="Path to series_config.json")
    parser.add_argument("--mode", default="new_book", choices=sorted(SUPPORTED_MODES),
                        help="Pipeline mode")
    parser.add_argument("--from-phase", default=None, choices=RESUMABLE_PHASES,
                        help="Resume pipeline from a named phase")
    parser.add_argument("--start-scene", type=int, default=None,
                        help="Scene loop start bound (inclusive)")
    parser.add_argument("--end-scene", type=int, default=None,
                        help="Scene loop end bound (inclusive)")
    parser.add_argument("--max-retries-per-gate", type=int, default=2,
                        help="Per-gate regeneration retry cap (default: 2)")
    parser.add_argument("--max-retries-per-scene", type=int, default=1,
                        help="Per-scene write retry cap (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run preflight only and exit")
    parser.add_argument("--force", action="store_true",
                        help="Re-write scenes even if files already exist")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
