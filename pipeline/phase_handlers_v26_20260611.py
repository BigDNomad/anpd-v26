# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V24 Phase Handlers — Phases 1–5 (master_controller Commit 2)

Per master_controller V24 design doc §2, phase orchestration is
master_controller's responsibility. This module separates the per-phase
implementation into named functions — handle_synopsis_gate(),
handle_character_gate(), handle_scene_loop(), etc. — so that
master_controller's run_pipeline() reads as a high-level sequence
rather than a 1000-line procedure.

Each handler:
- Returns a dict with at minimum 'verdict' and 'findings' keys
- Updates pipeline_state in place (gate_verdicts, components_called,
  invocation_timeline)
- Detects component-written STOP_REPORTs and propagates 'halt' verdicts
  per design doc §6
- Does NOT write STOP_REPORTs itself (master_controller writes
  orchestrator-level STOP_REPORTs per §6.1)

Phases 6–9 (chapter assembly, manuscript gate, format, capsule write)
plus full receipt finalization land in Commit 3.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import master_controller as mc


# Per design doc absorbing scene_writer_stub_runner.py logic:
SCENES_PER_BATCH = 12
SCENES_PER_CHAPTER = 4


# ─── Phase 1 — Preflight (delegating wrapper) ─────────────────────────────────

def handle_preflight(args, pipeline_state) -> dict:
    """Run preflight via subprocess if available; fall back to inline stub.

    Per design doc §3.1: when preflight.py is built, master_controller
    invokes it as subprocess. Until then, the inline stub from
    master_controller's preflight_stub() runs.

    Returns:
        dict with 'verdict' (pass | halt), 'findings' (list of dicts).
    """
    preflight_script = mc.COMPONENTS["preflight"]

    if os.path.isfile(preflight_script):
        # Real subprocess invocation when preflight.py exists.
        result = mc.run_component_subprocess(
            "preflight",
            [
                "--book-dir", args.book_dir,
                "--series-dir", args.series_dir,
                "--intake", args.intake,
                "--series-config", args.series_config,
            ],
            args.book_dir,
            pipeline_state,
        )
        if result["stop_report_written_during_call"]:
            return {"verdict": "halt", "findings": [], "via": "subprocess"}
        if result["exit_code"] != 0:
            return {
                "verdict": "halt",
                "findings": [{
                    "class": "A",
                    "component": "preflight",
                    "phase": 1,
                    "message": f"preflight subprocess returned exit code {result['exit_code']}",
                    "suggested_fix": "review preflight output",
                }],
                "via": "subprocess",
            }
        return {"verdict": "pass", "findings": [], "via": "subprocess"}

    # Stub path — inline minimal checks.
    findings = mc.preflight_stub(args, pipeline_state)
    class_a = [f for f in findings if f["class"] == "A"]
    return {
        "verdict": "halt" if class_a else "pass",
        "findings": findings,
        "via": "stub",
    }


# ─── Phase 3 — Synopsis gate ──────────────────────────────────────────────────

def handle_synopsis_gate(args, pipeline_state, effective_config) -> dict:
    """Synopsis gate orchestration loop per design doc §3 / §A3.

    1. Invoke synopsis_generator.py (if not yet run)
    2. Locate produced synopsis_*.md (latest by mtime in {book_dir}/work/)
    3. Invoke synopsis_auditor.py
    4. Parse findings; route per Tier
    5. Repeat until pass or --max-retries-per-gate exhausted

    Tier 1/2 auto-fix handlers don't yet exist — when findings come back
    in those tiers, this handler logs them and halts (per master_controller
    design doc §7). Tier 3 routes to re-running synopsis_generator with
    --skip-structural.
    """
    work_dir = os.path.join(args.book_dir, "work")
    os.makedirs(work_dir, exist_ok=True)

    # Resolve auxiliary paths from series_dir (canonical filenames per
    # Data Standards §10).
    character_profiles_path = os.path.join(args.series_dir, "character_profiles.json")
    series_bible_path = os.path.join(args.series_dir, "series_bible.json")
    twist_library_path = os.path.join(args.series_dir, "twist_library.md")

    # Resolve outline path from intake.json (required field per intake schema).
    try:
        with open(args.intake, "r", encoding="utf-8") as fh:
            intake_data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        intake_data = {}
    outline_path = intake_data.get("outline_path", "outline.md")
    if not os.path.isabs(outline_path):
        outline_path = os.path.join(work_dir, outline_path)

    retries = args.max_retries_per_gate
    skip_structural = False

    for attempt in range(retries + 1):
        # 1. Generator
        gen_args = [
            "--intake", args.intake,
            "--outline", outline_path,
            "--character-profiles", character_profiles_path,
            "--series-bible", series_bible_path,
            "--output-dir", work_dir,
        ]

        gen_result = mc.run_component_subprocess(
            "synopsis_generator", gen_args, args.book_dir, pipeline_state
        )
        if gen_result["stubbed"]:
            return _handle_stubbed("synopsis_generator", "synopsis", pipeline_state)
        if gen_result["stop_report_written_during_call"]:
            return {"verdict": "halt", "findings": [], "via": "component_stop_report"}
        if gen_result["exit_code"] != 0:
            return _wrap_class_a(
                "synopsis_generator",
                f"exit code {gen_result['exit_code']}: {gen_result['stderr'][:200]}",
                phase=3,
            )

        # 2. Locate produced synopsis
        synopsis_path = mc.find_latest_file(work_dir, "synopsis_*.md")
        if synopsis_path is None:
            return _wrap_class_a(
                "synopsis_generator",
                f"no synopsis_*.md produced in {work_dir}",
                phase=3,
            )

        # 3. Auditor
        audit_result = mc.run_component_subprocess(
            "synopsis_auditor",
            [
                "--synopsis", synopsis_path,
                "--intake", args.intake,
                "--series-dir", args.series_dir,
                "--series-config", args.series_config,
            ],
            args.book_dir,
            pipeline_state,
        )
        if audit_result["stubbed"]:
            return _handle_stubbed("synopsis_auditor", "synopsis", pipeline_state)
        if audit_result["stop_report_written_during_call"]:
            return {"verdict": "halt", "findings": [], "via": "component_stop_report"}
        if audit_result["exit_code"] != 0:
            return _wrap_class_a(
                "synopsis_auditor",
                f"exit code {audit_result['exit_code']}: {audit_result['stderr'][:200]}",
                phase=3,
            )

        # 4. Parse findings from auditor stdout. Auditor returns JSON-encoded
        # findings list per V24 finding schema.
        findings = _parse_auditor_findings(audit_result["stdout"])

        # No findings → gate passes.
        if not findings:
            pipeline_state["gate_verdicts"]["synopsis"] = "pass"
            return {"verdict": "pass", "findings": [], "synopsis_path": synopsis_path}

        # 5. Tier-route findings.
        tier_decision = _classify_findings_by_tier(findings)
        if tier_decision == "pass_with_warnings":
            pipeline_state["gate_verdicts"]["synopsis"] = "pass"
            return {"verdict": "pass", "findings": findings, "synopsis_path": synopsis_path}
        if tier_decision == "tier_1_or_2":
            # Auto-fix handlers not yet built; halt with finding listing.
            pipeline_state["gate_verdicts"]["synopsis"] = "fail"
            return _wrap_findings_halt(
                "synopsis_auditor",
                "tier 1/2 findings present; auto-fix handlers not yet built",
                findings,
                phase=3,
            )
        if tier_decision == "tier_3":
            if attempt >= retries:
                pipeline_state["gate_verdicts"]["synopsis"] = "fail"
                return _wrap_findings_halt(
                    "synopsis_auditor",
                    f"tier 3 findings persist after {retries} retries",
                    findings,
                    phase=3,
                )
            skip_structural = True  # next attempt skips structural pass
            continue

    # Exhausted loop without a verdict.
    pipeline_state["gate_verdicts"]["synopsis"] = "fail"
    return _wrap_class_a("synopsis_gate", "exhausted retries without verdict", phase=3)


# ─── Phase 4 — Character profile gate ─────────────────────────────────────────

def handle_character_gate(args, pipeline_state, effective_config) -> dict:
    """Character profile gate per design doc §A4.

    character_generator.py self-audits in-process and bounds its own
    retries. master_controller's job: invoke generator, check for
    component STOP_REPORT, record verdict.
    """
    book_config_path = os.path.join(args.book_dir, "work", "book_config.json")
    if not os.path.isfile(book_config_path):
        # Fallback search in book root
        alt = os.path.join(args.book_dir, "book_config.json")
        if os.path.isfile(alt):
            book_config_path = alt
        else:
            return _wrap_class_a(
                "character_gate",
                f"book_config.json not found at {book_config_path}",
                phase=4,
            )

    result = mc.run_component_subprocess(
        "character_generator",
        [
            "--book-config", book_config_path,
            "--series-config", args.series_config,
            "--series-dir", args.series_dir,
            "--max-retries", str(args.max_retries_per_gate),
        ],
        args.book_dir,
        pipeline_state,
    )

    if result["stubbed"]:
        return _handle_stubbed("character_generator", "character_profiles", pipeline_state)

    if result["stop_report_written_during_call"]:
        # Generator wrote its own STOP_REPORT (retry exhaustion).
        pipeline_state["gate_verdicts"]["character_profiles"] = "fail"
        return {"verdict": "halt", "findings": [], "via": "component_stop_report"}

    if result["exit_code"] != 0:
        pipeline_state["gate_verdicts"]["character_profiles"] = "fail"
        return _wrap_class_a(
            "character_generator",
            f"exit code {result['exit_code']} without STOP_REPORT: {result['stderr'][:200]}",
            phase=4,
        )

    # Verify the produced profile file exists.
    book_profile_path = mc.find_latest_file(
        os.path.join(args.book_dir, "work"), "character_profiles_*.json"
    )
    if book_profile_path is None:
        pipeline_state["gate_verdicts"]["character_profiles"] = "fail"
        return _wrap_class_a(
            "character_generator",
            "subprocess returned 0 but no character_profiles_*.json produced",
            phase=4,
        )

    pipeline_state["gate_verdicts"]["character_profiles"] = "pass"
    return {
        "verdict": "pass",
        "findings": [],
        "character_profiles_path": book_profile_path,
    }


# ─── Phase 5 — Scene generation loop ──────────────────────────────────────────

def handle_scene_loop(
    args,
    pipeline_state,
    effective_config,
    synopsis_path: str | None,
    character_profiles_path: str | None,
) -> dict:
    """Per-scene loop per design doc §A5 + §P6 (V23 inheritance).

    For each scene in scene_map (bounded by --start-scene / --end-scene):
        Step 0 (optional): research_pipeline.py if effective_config["research_enabled"]
        Step 1: assemble bundle, invoke scene_writer.py, write scene file
        Step 2: state_tracker (if built); skip with Class B if not.

    Per-scene retry per --max-retries-per-scene on Class A finding.
    Auto-skip on already-written scenes unless --force.
    Bundle assembly absorbs scene_writer_stub_runner.py logic per §2.12.
    """
    scenes_dir = os.path.join(args.book_dir, "out", "scenes")
    state_dir = os.path.join(args.book_dir, "out", "state")
    chapters_dir = os.path.join(args.book_dir, "out", "chapters")

    # Archive-and-purge on a FULL generation run (no scene bounds). A full
    # run regenerates the whole book, so prior output must not linger —
    # slug-based filenames otherwise leave orphan duplicates that corrupt
    # chapter assembly. Bounded/debug runs (--start-scene/--end-scene) skip
    # this so they don't wipe scenes they aren't regenerating.
    is_full_run = (args.start_scene is None) and (args.end_scene is None)
    if is_full_run:
        import shutil
        from datetime import datetime
        existing = [d for d in (scenes_dir, state_dir, chapters_dir) if os.path.isdir(d) and os.listdir(d)]
        if existing:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_root = os.path.join(args.book_dir, "out", "_archive", f"run_{ts}")
            os.makedirs(archive_root, exist_ok=True)
            for d in (scenes_dir, state_dir, chapters_dir):
                if os.path.isdir(d):
                    dest = os.path.join(archive_root, os.path.basename(d))
                    shutil.move(d, dest)
            print(f"  [scene_loop] full run — archived prior output to {archive_root}", file=sys.stderr)

    os.makedirs(scenes_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(chapters_dir, exist_ok=True)

    # Locate scene_map. Canonical filename per Data Standards §10.
    scene_map_path = os.path.join(args.book_dir, "scene_map.md")
    if not os.path.isfile(scene_map_path):
        # Fallback: glob in book_dir for scene_map_*.md (timestamped variant)
        scene_map_path = mc.find_latest_file(args.book_dir, "scene_map*.md")
        if scene_map_path is None:
            return _wrap_class_a(
                "scene_loop",
                "scene_map.md not found in book_dir",
                phase=5,
            )

    try:
        scene_map = _parse_scene_map(scene_map_path)
    except (ValueError, OSError) as exc:
        return _wrap_class_a(
            "scene_loop",
            f"scene_map parse failed: {exc}",
            phase=5,
        )

    if not scene_map:
        return _wrap_class_a(
            "scene_loop",
            "scene_map produced zero scenes",
            phase=5,
        )

    # Determine bounds.
    available = sorted(scene_map.keys())
    start_scene = args.start_scene if args.start_scene else available[0]
    end_scene = args.end_scene if args.end_scene else available[-1]

    scenes_written = []
    scenes_skipped_existing = []
    scenes_failed = []

    for scene_num in range(start_scene, end_scene + 1):
        if scene_num not in scene_map:
            continue

        scene_info = scene_map[scene_num]
        scene_slug = _slug_from_title(scene_info.get("title", f"scene_{scene_num}"))
        scene_filename = f"sc{scene_num:02d}_{scene_slug}.md"
        scene_path = os.path.join(scenes_dir, scene_filename)

        # Auto-skip if already written and not forcing.
        if os.path.isfile(scene_path) and not args.force:
            scenes_skipped_existing.append(scene_num)
            continue

        # Per-scene retry loop.
        success = False
        for attempt in range(args.max_retries_per_scene + 1):
            try:
                bundle_path = _assemble_scene_bundle(
                    scene_num,
                    scene_info,
                    args,
                    effective_config,
                    synopsis_path,
                    character_profiles_path,
                )
            except (OSError, ValueError) as exc:
                pipeline_state["class_a_failures"] += 1
                if attempt >= args.max_retries_per_scene:
                    scenes_failed.append((scene_num, f"bundle assembly: {exc}"))
                    break
                continue

            result = mc.run_component_subprocess(
                "scene_writer",
                ["--bundle", bundle_path],
                args.book_dir,
                pipeline_state,
                scene_number=scene_num,
            )

            if result["stubbed"]:
                scenes_failed.append((scene_num, "scene_writer stubbed"))
                break
            if result["stop_report_written_during_call"]:
                # scene_writer doesn't normally write STOP_REPORTs but defend
                # against the case anyway.
                return {"verdict": "halt", "findings": [], "via": "component_stop_report"}
            if result["exit_code"] != 0:
                pipeline_state["class_a_failures"] += 1
                if attempt >= args.max_retries_per_scene:
                    scenes_failed.append((
                        scene_num,
                        f"scene_writer exit {result['exit_code']}: {result['stderr'][:200]}",
                    ))
                    break
                continue

            # Locate the scene_writer output bundle. Convention: same
            # directory as input bundle, suffix _output.
            output_bundle_path = bundle_path.replace(".json", "_output.json")
            if not os.path.isfile(output_bundle_path):
                pipeline_state["class_a_failures"] += 1
                scenes_failed.append((
                    scene_num,
                    f"scene_writer returned 0 but no output bundle at {output_bundle_path}",
                ))
                break

            # Extract prose from output bundle and persist to canonical
            # scene file.
            try:
                with open(output_bundle_path, "r", encoding="utf-8") as fh:
                    output_bundle = json.load(fh)
                prose = output_bundle.get("scene_text", "")
                if not prose:
                    raise ValueError("output bundle missing scene_text field")
                with open(scene_path, "w", encoding="utf-8") as fh:
                    fh.write(prose)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                pipeline_state["class_a_failures"] += 1
                if attempt >= args.max_retries_per_scene:
                    scenes_failed.append((scene_num, f"output bundle: {exc}"))
                    break
                continue

            # Step 2 — state extraction (Class A hard stop on failure).
            state_ok = _attempt_state_extraction(scene_num, scene_path, args, pipeline_state, state_dir)
            if not state_ok:
                scenes_failed.append((scene_num, "state_tracker hard failure (Class A)"))
                return {
                    "verdict": "halt",
                    "via": "component_stop_report",
                    "findings": [{
                        "class": "A",
                        "component": "state_tracker",
                        "phase": 5,
                        "message": f"scene {scene_num} state extraction failed (Class A) — halting; subsequent scenes would run on broken continuity",
                        "suggested_fix": "review state_tracker STOP_REPORT for this scene",
                    }],
                    "scenes_written": scenes_written,
                    "scenes_failed": scenes_failed,
                }

            scenes_written.append(scene_num)
            pipeline_state["scenes_generated"] += 1
            success = True
            break

        if not success and not scenes_failed:
            scenes_failed.append((scene_num, "exhausted retries"))

    # Verdict: if any scene_failed was Class A, halt the gate; otherwise pass.
    if scenes_failed:
        return {
            "verdict": "halt",
            "findings": [
                {
                    "class": "A",
                    "component": "scene_writer",
                    "phase": 5,
                    "message": f"scene {sn} failed: {reason}",
                    "suggested_fix": "review scene_writer output and bundle",
                }
                for sn, reason in scenes_failed
            ],
            "scenes_written": scenes_written,
            "scenes_skipped_existing": scenes_skipped_existing,
            "scenes_failed": scenes_failed,
        }

    return {
        "verdict": "pass",
        "findings": [],
        "scenes_written": scenes_written,
        "scenes_skipped_existing": scenes_skipped_existing,
    }


# ─── Phase 6 — Chapter assembly ───────────────────────────────────────────────

def handle_chapter_assembly(args, pipeline_state, effective_config) -> dict:
    """Invoke scene_formatter.py to split scenes into chapters per
    scene_map's chapter assignments.

    Per design doc §3.8: scene_formatter is a Phase 4 fresh-build that
    has not yet shipped. When stubbed, halt subsequent phases (chapters
    are required for manuscript gate + format).
    """
    scenes_dir = os.path.join(args.book_dir, "out", "scenes")
    chapters_dir = os.path.join(args.book_dir, "out", "chapters")
    os.makedirs(chapters_dir, exist_ok=True)

    if not os.path.isdir(scenes_dir):
        return _wrap_class_a(
            "chapter_assembly",
            f"scenes directory missing: {scenes_dir}",
            phase=6,
        )

    scene_files = sorted(_glob_scenes(scenes_dir))
    if not scene_files:
        return _wrap_class_a(
            "chapter_assembly",
            "no sc{NN}_*.md files in scenes directory",
            phase=6,
        )

    result = mc.run_component_subprocess(
        "scene_formatter",
        [
            "--scenes-dir", scenes_dir,
            "--chapters-dir", chapters_dir,
            "--scene-map", _resolve_scene_map(args.book_dir),
            "--target-chapter-count", str(effective_config.get("target_chapter_count", 25)),
        ],
        args.book_dir,
        pipeline_state,
    )

    if result["stubbed"]:
        return _handle_stubbed("scene_formatter", None, pipeline_state)

    if result["stop_report_written_during_call"]:
        return {"verdict": "halt", "findings": [], "via": "component_stop_report"}

    if result["exit_code"] != 0:
        return _wrap_class_a(
            "scene_formatter",
            f"exit code {result['exit_code']}: {result['stderr'][:200]}",
            phase=6,
        )

    chapter_files = sorted(_glob_chapters(chapters_dir))
    if not chapter_files:
        return _wrap_class_a(
            "scene_formatter",
            "subprocess returned 0 but no ch{NN}_*.md files produced",
            phase=6,
        )

    return {
        "verdict": "pass",
        "findings": [],
        "chapters_written": len(chapter_files),
    }


# ─── Phase 7 — Manuscript gate (Gate 3) ───────────────────────────────────────

def handle_manuscript_gate(
    args, pipeline_state, effective_config
) -> dict:
    """Manuscript gate orchestration per design doc §A5 + §7.

    1. Invoke manuscript_auditor.py
    2. Tier-route findings:
       - Tier 1/2 → fix via chapter_editor (auto-fix handlers stubbed)
       - Tier 3 → return signal to caller for scene-regeneration retry
       - Pass → record verdict, advance
    3. Bounded retry per --max-retries-per-gate

    The Tier 3 routing back to scene regeneration is owned by the caller
    (run_pipeline) — this handler signals "tier_3_regen_needed" via the
    result dict and identifies which scenes need re-generating.

    Per design doc §7: when manuscript_auditor isn't built, log Class B
    "Gate 3 stubbed" and proceed with verdict='stubbed'.
    """
    # First check if manuscript_auditor exists at all.
    auditor_script = mc.COMPONENTS["manuscript_auditor"]
    if not os.path.isfile(auditor_script):
        pipeline_state["gate_verdicts"]["manuscript"] = "stubbed"
        pipeline_state["class_b_violations"] += 1
        return {
            "verdict": "pass",  # Pass-through per design doc §7 (proceed to format)
            "findings": [{
                "class": "B",
                "component": "manuscript_auditor",
                "phase": 7,
                "message": "manuscript_auditor not yet built; Gate 3 stubbed",
                "suggested_fix": "no operator action; resolves when manuscript_auditor ships",
            }],
            "via": "stubbed",
        }

    chapters_dir = os.path.join(args.book_dir, "out", "chapters")
    retries = args.max_retries_per_gate

    for attempt in range(retries + 1):
        result = mc.run_component_subprocess(
            "manuscript_auditor",
            [
                "--book-dir", args.book_dir,
                "--series-config", args.series_config,
                "--chapters-dir", chapters_dir,
            ],
            args.book_dir,
            pipeline_state,
        )

        if result["stop_report_written_during_call"]:
            pipeline_state["gate_verdicts"]["manuscript"] = "fail"
            return {"verdict": "halt", "findings": [], "via": "component_stop_report"}

        if result["exit_code"] != 0:
            pipeline_state["gate_verdicts"]["manuscript"] = "fail"
            return _wrap_class_a(
                "manuscript_auditor",
                f"exit code {result['exit_code']}: {result['stderr'][:200]}",
                phase=7,
            )

        findings = _parse_auditor_findings(result["stdout"])

        if not findings:
            pipeline_state["gate_verdicts"]["manuscript"] = "pass"
            return {"verdict": "pass", "findings": []}

        tier_decision = _classify_findings_by_tier(findings)

        if tier_decision == "pass_with_warnings":
            pipeline_state["gate_verdicts"]["manuscript"] = "pass"
            return {"verdict": "pass", "findings": findings}

        if tier_decision == "tier_1_or_2":
            # Try chapter_editor for Tier 1/2 fix. If chapter_editor
            # is stubbed, halt with finding listing.
            fix_result = _attempt_chapter_editor_fix(
                args, pipeline_state, findings
            )
            if fix_result.get("verdict") == "stubbed_no_fix":
                pipeline_state["gate_verdicts"]["manuscript"] = "fail"
                return _wrap_findings_halt(
                    "manuscript_auditor",
                    "tier 1/2 findings present and chapter_editor not yet built",
                    findings,
                    phase=7,
                )
            if fix_result.get("verdict") == "halt":
                pipeline_state["gate_verdicts"]["manuscript"] = "fail"
                return fix_result
            # Fix attempted; loop to re-audit.
            continue

        if tier_decision == "tier_3":
            # F0 (state-chain invariant): in-place scene regeneration is
            # UNSAFE — it forks the state chain (regenerated scene produces
            # new state, but downstream scenes were authored against the old
            # state). Until a state-preserving fixer exists (see Fixer
            # Architecture Spec), the gate must DETECT -> REPORT -> HALT,
            # never delete scenes and restart phase 5. Report the affected
            # scenes in the halt for operator/synopsis review.
            scenes_to_regen = _extract_affected_scenes(findings)
            pipeline_state["gate_verdicts"]["manuscript"] = "fail"
            return _wrap_findings_halt(
                "manuscript_auditor",
                f"tier 3 findings present (scenes {scenes_to_regen}); in-place "
                f"regen disabled per state-chain invariant — fix upstream "
                f"(synopsis) or build state-preserving fixer",
                findings,
                phase=7,
            )

    pipeline_state["gate_verdicts"]["manuscript"] = "fail"
    return _wrap_class_a("manuscript_gate", "exhausted retries without verdict", phase=7)


# ─── Phase 8 — Format (.docx production) ──────────────────────────────────────

def handle_format(args, pipeline_state, effective_config, intake: dict) -> dict:
    """Invoke formatter.py to produce .docx from chapter files.

    Per Data Standards §2.8: output filename is
    `{NN}_{Title}_{YYYYMMDD_HHMM}.docx`. Formatter owns filename
    construction; master_controller passes book metadata.
    """
    chapters_dir = os.path.join(args.book_dir, "out", "chapters")
    output_dir = os.path.join(args.book_dir, "out")

    if not os.path.isdir(chapters_dir):
        return _wrap_class_a(
            "format",
            f"chapters directory missing: {chapters_dir}",
            phase=8,
        )

    book_number = intake.get("book_number", 0)
    book_title = intake.get("book_title") or intake.get("title") or "Untitled"
    pen_name = effective_config.get("pen_name", "Author")

    result = mc.run_component_subprocess(
        "formatter",
        [
            "--chapters-dir", chapters_dir,
            "--output-dir", output_dir,
            "--book-number", str(book_number),
            "--book-title", book_title,
            "--author-name", pen_name,
        ],
        args.book_dir,
        pipeline_state,
    )

    if result["stubbed"]:
        return _handle_stubbed("formatter", None, pipeline_state)

    if result["stop_report_written_during_call"]:
        return {"verdict": "halt", "findings": [], "via": "component_stop_report"}

    if result["exit_code"] != 0:
        return _wrap_class_a(
            "formatter",
            f"exit code {result['exit_code']}: {result['stderr'][:200]}",
            phase=8,
        )

    # Locate produced .docx
    docx_path = mc.find_latest_file(output_dir, "*.docx")
    if docx_path is None:
        return _wrap_class_a(
            "formatter",
            "subprocess returned 0 but no .docx produced",
            phase=8,
        )

    pipeline_state["output_valid"] = True
    return {
        "verdict": "pass",
        "findings": [],
        "docx_path": docx_path,
    }


# ─── Phase 9 — Capsule write ──────────────────────────────────────────────────

def handle_capsule_write(args, pipeline_state, effective_config) -> dict:
    """Invoke capsule_writer.py to produce the forward capsule.

    Per Capsule Schema §4.1: capsule_writer is Workstream 1 territory and
    has not yet shipped. When stubbed, log Class B and proceed (capsule
    is non-blocking; receipt finalization runs regardless).
    """
    receipt_path = os.path.join(args.book_dir, "out", "reports", "PIPELINE_RECEIPT.json")

    result = mc.run_component_subprocess(
        "capsule_writer",
        [
            "--book-dir", args.book_dir,
            "--series-config", args.series_config,
            "--pipeline-receipt", receipt_path,
        ],
        args.book_dir,
        pipeline_state,
    )

    if result["stubbed"]:
        # Non-blocking — record skip and continue.
        pipeline_state["capsule_paths"]["forward"] = None
        pipeline_state["class_b_violations"] += 1
        return {
            "verdict": "pass",
            "findings": [{
                "class": "B",
                "component": "capsule_writer",
                "phase": 9,
                "message": "capsule_writer not yet built; capsule write skipped",
                "suggested_fix": "no operator action; resolves when capsule_writer ships (Workstream 1)",
            }],
            "via": "stubbed",
        }

    if result["stop_report_written_during_call"]:
        return {"verdict": "halt", "findings": [], "via": "component_stop_report"}

    if result["exit_code"] != 0:
        return _wrap_class_a(
            "capsule_writer",
            f"exit code {result['exit_code']}: {result['stderr'][:200]}",
            phase=9,
        )

    # Capsule path follows capsule_schema.capsule_path_forward(series, book_number).
    # We trust capsule_writer to have written there; record canonical path.
    series = effective_config.get("series_directory", "")
    intake = _safe_load_json(args.intake) or {}
    book_number = intake.get("book_number", 0)
    capsule_path = f"/anpd/v26/series/{series}/b{book_number:02d}/capsule_manifest.json"
    pipeline_state["capsule_paths"]["forward"] = capsule_path

    return {
        "verdict": "pass",
        "findings": [],
        "capsule_path": capsule_path,
    }


# ─── Helpers added in Commit 3 ────────────────────────────────────────────────

def _glob_scenes(scenes_dir: str) -> list[str]:
    import glob as _glob
    return _glob.glob(os.path.join(scenes_dir, "sc[0-9][0-9]_*.md"))


def _glob_chapters(chapters_dir: str) -> list[str]:
    import glob as _glob
    return _glob.glob(os.path.join(chapters_dir, "ch[0-9][0-9]_*.md"))


def _resolve_scene_map(book_dir: str) -> str:
    """Find scene_map.md or latest scene_map_*.md in book_dir."""
    canonical = os.path.join(book_dir, "scene_map.md")
    if os.path.isfile(canonical):
        return canonical
    found = mc.find_latest_file(book_dir, "scene_map*.md")
    return found if found else canonical


def _attempt_chapter_editor_fix(
    args, pipeline_state, findings: list[dict]
) -> dict:
    """Try to invoke chapter_editor for Tier 1/2 manuscript findings.

    Per design doc §3.10: chapter_editor takes the chapter file, the
    findings affecting it, and book/series dirs. If chapter_editor is
    stubbed, return verdict='stubbed_no_fix' so manuscript gate can halt
    with a clear listing.
    """
    editor_script = mc.COMPONENTS.get("chapter_editor")
    if not editor_script or not os.path.isfile(editor_script):
        pipeline_state["class_b_violations"] += 1
        return {"verdict": "stubbed_no_fix"}

    # Group findings by chapter (each finding may carry chapter_number).
    by_chapter: dict[int, list[dict]] = {}
    for f in findings:
        ch = f.get("chapter_number")
        if ch is not None:
            by_chapter.setdefault(ch, []).append(f)

    if not by_chapter:
        # Findings without chapter scope — can't route to chapter_editor.
        return {"verdict": "stubbed_no_fix"}

    chapters_dir = os.path.join(args.book_dir, "out", "chapters")
    for chapter_num, chapter_findings in by_chapter.items():
        chapter_glob = mc.find_latest_file(
            chapters_dir, f"ch{chapter_num:02d}_*.md"
        )
        if chapter_glob is None:
            continue

        # Pass findings as JSON via a temp file; chapter_editor reads it.
        findings_path = os.path.join(
            args.book_dir, "work", f"editor_findings_ch{chapter_num:02d}.json"
        )
        os.makedirs(os.path.dirname(findings_path), exist_ok=True)
        with open(findings_path, "w", encoding="utf-8") as fh:
            json.dump(chapter_findings, fh, indent=2)

        result = mc.run_component_subprocess(
            "chapter_editor",
            [
                "--chapter", chapter_glob,
                "--findings", findings_path,
                "--book-dir", args.book_dir,
                "--series-dir", args.series_dir,
            ],
            args.book_dir,
            pipeline_state,
        )
        if result["stop_report_written_during_call"]:
            return {"verdict": "halt", "findings": [], "via": "component_stop_report"}
        if result["exit_code"] != 0:
            pipeline_state["scenes_corrected"] += 0  # editor failed
            continue
        pipeline_state["scenes_corrected"] += 1

    return {"verdict": "fix_attempted"}


def _extract_affected_scenes(findings: list[dict]) -> list[int]:
    """Pull scene numbers from Tier 3 findings.

    Findings may carry 'scene_number' (single) or 'scene_numbers' (list)
    or 'chapter_number' (master_controller maps chapter → scenes).
    Fallback: empty list, signaling regenerate-all is the caller's call.
    """
    scenes: set[int] = set()
    for f in findings:
        if "scene_number" in f:
            scenes.add(int(f["scene_number"]))
        elif "scene_numbers" in f:
            for sn in f["scene_numbers"]:
                scenes.add(int(sn))
        elif "chapter_number" in f:
            # Map chapter to its 4 scenes (per SCENES_PER_CHAPTER).
            ch = int(f["chapter_number"])
            base = (ch - 1) * SCENES_PER_CHAPTER + 1
            for sn in range(base, base + SCENES_PER_CHAPTER):
                scenes.add(sn)
    return sorted(scenes)


# ─── Helpers (private) ────────────────────────────────────────────────────────

def _wrap_class_a(component: str, message: str, phase: int) -> dict:
    return {
        "verdict": "halt",
        "findings": [{
            "class": "A",
            "component": component,
            "phase": phase,
            "message": message,
            "suggested_fix": "review the underlying error and retry",
        }],
    }


def _wrap_findings_halt(
    component: str, summary: str, findings: list[dict], phase: int
) -> dict:
    return {
        "verdict": "halt",
        "findings": [{
            "class": "A",
            "component": component,
            "phase": phase,
            "message": summary,
            "suggested_fix": "address findings listed below",
            "subordinate_findings": findings,
        }],
    }


def _handle_stubbed(component: str, gate_key: str | None, pipeline_state: dict) -> dict:
    """Component is registered but not yet built on disk. Per design doc §10,
    log Class B and halt the gate (downstream phases can't run without it).
    """
    if gate_key:
        pipeline_state["gate_verdicts"][gate_key] = "stubbed"
    pipeline_state["class_b_violations"] += 1
    return {
        "verdict": "halt",
        "findings": [{
            "class": "B",
            "component": component,
            "phase": 0,
            "message": f"{component} not yet built on disk; halting downstream phases",
            "suggested_fix": "ship the component or use --from-phase to skip past it",
        }],
        "via": "stubbed",
    }


def _parse_auditor_findings(stdout: str) -> list[dict]:
    """Auditor stdout format: V24 finding schema JSON. May contain prose
    interleaved with the JSON output. Pull the JSON list out.
    """
    if not stdout.strip():
        return []
    # Try direct parse first (simplest case).
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "findings" in parsed:
            return parsed["findings"]
        return []
    except json.JSONDecodeError:
        pass
    # Fallback: extract JSON block. V24 auditors typically emit findings
    # as a JSON array; look for "[ ... ]" or '"findings": [ ... ]'.
    match = re.search(r"\[\s*\{.*?\}\s*\]", stdout, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return []


def _classify_findings_by_tier(findings: list[dict]) -> str:
    """Return 'pass_with_warnings' | 'tier_1_or_2' | 'tier_3'.

    Per White Paper §3.8: Tier 1 = mechanical fixes; Tier 2 = LLM-mediated
    fixes; Tier 3 = regenerate. Tier is encoded in finding's 'tier' field;
    'class' B/C without explicit tier is pass-with-warnings.
    """
    has_tier_3 = any(f.get("tier") == 3 for f in findings)
    has_tier_1_or_2 = any(f.get("tier") in (1, 2) for f in findings)
    has_class_a = any(f.get("class") == "A" for f in findings)

    if has_tier_3:
        return "tier_3"
    if has_tier_1_or_2 or has_class_a:
        return "tier_1_or_2"
    return "pass_with_warnings"


def _parse_scene_map(scene_map_path: str) -> dict[int, dict]:
    """Parse scene_map.md into {scene_num: {title, ...}} dict.

    Scene map format per Scene Map Schema: markdown with one heading per
    scene. Heading format: "## Scene N: Title" or "### Scene N - Title".
    Scene body is plain text following the heading.

    For Commit 2 we extract scene_num + title. Pressure fields, reveal
    gates, and other rich fields are passed through to the bundle as
    raw section text.
    """
    if not os.path.isfile(scene_map_path):
        raise OSError(f"scene_map not found at {scene_map_path}")

    with open(scene_map_path, "r", encoding="utf-8") as fh:
        content = fh.read()

    scenes = {}
    # Heading pattern: optional level prefix, "Scene", number, separator, title.
    pattern = re.compile(
        r"^#{2,4}\s+Scene\s+(\d+)\s*[—:\-]\s*(.+?)$",
        re.MULTILINE | re.IGNORECASE,
    )
    matches = list(pattern.finditer(content))
    for i, m in enumerate(matches):
        scene_num = int(m.group(1))
        title = m.group(2).strip()
        # Body extends from end of heading to start of next heading (or EOF).
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end].strip()
        scenes[scene_num] = {
            "title": title,
            "body": body,
        }
    return scenes


def _slug_from_title(title: str) -> str:
    """Compute file-safe slug from a scene title.

    Per Data Standards §2.5: 'sc{NN}_{slug}.md' where slug is the
    first-meaningful-word lowercased with non-alphanumerics stripped.
    """
    if not title:
        return "untitled"
    # Take everything before any colon (subtitle separator); first word.
    head = title.split(":")[0].strip()
    words = re.findall(r"[A-Za-z0-9]+", head.lower())
    if not words:
        return "untitled"
    return words[0]


def _assemble_scene_bundle(
    scene_num: int,
    scene_info: dict,
    args,
    effective_config: dict,
    synopsis_path: str | None,
    character_profiles_path: str | None,
) -> str:
    """Build the input bundle scene_writer consumes.

    Absorbs scene_writer_stub_runner.py's bundle assembly per design
    doc §2.12 (master_controller owns assembly). Bundle is written to
    {book_dir}/work/bundles/scene_{NN}_bundle.json. Scene_writer is
    invoked with --bundle <path>.

    The exact bundle schema is owned by scene_writer; this assembler
    populates all fields scene_writer's BundleValidationError checks
    require. Keeping schema fidelity is scene_writer_stub_runner.py's
    territory and the absorbed contract.
    """
    bundle_dir = os.path.join(args.book_dir, "work", "bundles")
    os.makedirs(bundle_dir, exist_ok=True)
    bundle_path = os.path.join(bundle_dir, f"scene_{scene_num:02d}_bundle.json")

    # Load auxiliaries (treat as best-effort; missing fields surface as
    # scene_writer BundleValidationError, which we catch and retry).
    intake = _safe_load_json(args.intake) or {}
    series_bible = _safe_load_json(
        os.path.join(args.series_dir, "series_bible.json")
    ) or {}
    banned_phrases = _safe_load_json(
        "/anpd/v26/shared/banned_ai_phrases.json"
    ) or {"phrases": []}
    character_profiles = _safe_load_json(character_profiles_path) if character_profiles_path else {}
    synopsis_text = _safe_read_text(synopsis_path) if synopsis_path else ""

    # Prior-scene state for continuity.
    prior_state = None
    if scene_num > 1:
        prior_state_path = os.path.join(
            args.book_dir, "out", "state", f"state_after_sc{scene_num - 1:02d}.json"
        )
        prior_state = _safe_load_json(prior_state_path)

    bundle = {
        "schema_version": "1.0.0",
        "scene_number": scene_num,
        "scene_title": scene_info.get("title", ""),
        "scene_body_from_map": scene_info.get("body", ""),
        "intake": intake,
        "series_bible": series_bible,
        "character_profiles": character_profiles,
        "synopsis_text": synopsis_text,
        "banned_phrases": banned_phrases.get("phrases", []),
        "prior_state": prior_state,
        "effective_config": effective_config,
    }

    with open(bundle_path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    return bundle_path


def _attempt_state_extraction(
    scene_num: int,
    scene_path: str,
    args,
    pipeline_state: dict,
    state_dir: str,
) -> bool:
    """Per-scene state extraction. Returns True on success, False on hard failure.

    State is load-bearing for cross-scene continuity (character gender,
    deaths, resource counts, spent beats). A book generated without state
    is known-defective — scenes loop and roster drifts (cf. v15 Black
    Mountain looping defect, 2026-05-20). Per no-silent-failures policy,
    state_tracker failure is now Class A (hard stop), not Class B.

    Args match state_tracker._build_parser exactly: --book-dir, --scene-file,
    --state-dir. state_tracker derives scene_number from the filename and
    resolves prior-state internally from state-dir; do NOT pass --scene-number,
    --output, --prior-state, or --series-dir (state_tracker's argparse rejects
    unknown args and exits non-zero).
    """
    state_script = mc.COMPONENTS["state_tracker"]
    if not os.path.isfile(state_script):
        pipeline_state["class_a_failures"] += 1
        mc.write_stop_report(
            book_dir=args.book_dir,
            component="state_tracker",
            phase=5,
            error_type="Class A",
            error_message=f"state_tracker.py not found at {state_script}",
            suggested_fix="ship state_tracker.py to /anpd/v26/pipeline/",
            pipeline_state_description=f"halted at scene {scene_num} state extraction",
        )
        return False

    state_args = [
        "--book-dir", args.book_dir,
        "--scene-file", scene_path,
        "--state-dir", state_dir,
    ]

    result = mc.run_component_subprocess(
        "state_tracker", state_args, args.book_dir, pipeline_state,
        scene_number=scene_num,
    )

    if result["stubbed"] or result["exit_code"] != 0:
        pipeline_state["class_a_failures"] += 1
        mc.write_stop_report(
            book_dir=args.book_dir,
            component="state_tracker",
            phase=5,
            error_type="Class A",
            error_message=(
                f"state_tracker failed at scene {scene_num} "
                f"(exit {result.get('exit_code')}, stubbed={result.get('stubbed')})"
            ),
            suggested_fix=(
                "state_tracker must produce state_after_sc{NN}.json for each "
                "scene; check state_tracker logs and arg interface"
            ),
            pipeline_state_description=f"halted at scene {scene_num} state extraction",
        )
        return False

    # Verify the expected state file was actually written.
    expected = os.path.join(state_dir, f"state_after_sc{scene_num:02d}.json")
    if not os.path.isfile(expected):
        pipeline_state["class_a_failures"] += 1
        mc.write_stop_report(
            book_dir=args.book_dir,
            component="state_tracker",
            phase=5,
            error_type="Class A",
            error_message=(
                f"state_tracker returned 0 but no state file at {expected}"
            ),
            suggested_fix="check state_tracker output path / filename convention",
            pipeline_state_description=f"halted at scene {scene_num} state extraction",
        )
        return False

    return True


def _safe_load_json(path: str | None) -> Any:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_read_text(path: str | None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""
