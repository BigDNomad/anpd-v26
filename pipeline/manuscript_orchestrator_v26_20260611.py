"""
manuscript_orchestrator.py — V25 Manuscript Orchestrator
ANPD V25 | Version: 20260511

Runs the full manuscript generation pipeline: iterate over all scenes,
generate prose, audit, retry on failures, assemble output.

Usage:
    python3 manuscript_orchestrator.py \\
      --synopsis <path> \\
      --intake <path> \\
      --series-bible <path> \\
      --character-profiles <path> \\
      --principles <path> \\
      --output-dir <path>
"""

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import datetime
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synopsis_parser import parse_synopsis
from scene_writer import write_scene, _calc_cost
from scene_auditor import audit_scene
from manuscript_assembler import assemble_manuscript
from collections import defaultdict


@dataclass
class SceneResult:
    chapter: int
    scene: int
    title: str
    scene_type: str
    pov: str
    word_count: int
    attempts: int
    passed: bool
    findings_summary: list = field(default_factory=list)
    prose_path: str = ""


# ── S-3 gate enforcement helpers ──────────────────────────────────────────


def _build_retry_feedback(
    class_a_items: list,
    attempt: int,
    max_attempts: int,
    target_words: int,
) -> str:
    """Build attempt-aware retry feedback per S-3 spec §4.3.

    Args:
        class_a_items: list of Finding objects from the failed attempt
        attempt: the just-completed attempt number (1-based)
        max_attempts: total attempt budget
        target_words: word count target for this scene

    Returns feedback string for the NEXT attempt.
    """
    next_attempt = attempt + 1
    if next_attempt > max_attempts:
        return ""  # No next attempt

    lines = []

    if next_attempt == max_attempts:
        lines.append(f"ATTEMPT {next_attempt} of {max_attempts} — FINAL. If you cannot satisfy these constraints, output the single line 'CANNOT_GENERATE' followed by one sentence explaining why.")
    else:
        lines.append(f"ATTEMPT {next_attempt} of {max_attempts}. Your previous attempt failed the following gates:")

    target_low = 700
    target_high = 1100

    for f in class_a_items:
        check = f.check
        msg = f.message

        if check == "word_count":
            import re as _re
            wc_match = _re.search(r"Word count (\d+)", msg)
            actual = int(wc_match.group(1)) if wc_match else 0

            if "above" in msg:
                delta = actual - target_high
                lines.append(
                    f"- Word count: your output was {actual} words. "
                    f"Target {target_low}-{target_high}. You must cut at least "
                    f"{delta} words. Reduce scene scope; do not add new beats."
                )
            elif "below" in msg:
                delta = target_low - actual
                lines.append(
                    f"- Word count: your output was {actual} words. "
                    f"Target {target_low}-{target_high}. You must add at least "
                    f"{delta} words. Develop existing beats rather than introduce new ones."
                )
            else:
                lines.append(f"- [{check}] {msg}")

        elif check == "character_state":
            # Extract character name from message
            import re as _re
            char_match = _re.search(r"character '([^']+)'", msg)
            char_name = char_match.group(1) if char_match else "unknown"
            lines.append(
                f"- Character '{char_name}' is dead by this chapter "
                f"but appears in your output. Remove all references."
            )

        elif check == "smell_opener":
            excerpt = f.excerpt if f.excerpt else ""
            lines.append(
                f"- Scene opened with smell description: '{excerpt}'. "
                f"Open with action, position, or dialogue instead."
            )

        elif check == "metadata_leak":
            lines.append(f"- [{check}] {msg}")

        else:
            # LLM-judgment gates and other deterministic gates: pass through verbatim
            lines.append(f"- [{check}] {msg}")

    return "\n".join(lines)


def _build_failure_report(
    scene_results: dict,
    retry_history: dict,
    run_id: str,
) -> dict:
    """Build failure_report.json content per S-3 spec §4.2.

    Args:
        scene_results: dict mapping (ch, sc) -> SceneResult
        retry_history: dict mapping (ch, sc) -> list of attempt dicts
        run_id: identifier for this run (typically manuscript dir basename)
    """
    failing_scenes = []
    total_class_a = 0
    total_class_b = 0

    for key, result in scene_results.items():
        if not result.passed:
            total_class_a += 1
            history = retry_history.get(key, [])
            failing_scenes.append({
                "chapter": result.chapter,
                "scene": result.scene,
                "title": result.title,
                "scene_type": result.scene_type,
                "attempts": result.attempts,
                "final_findings": [
                    s for s in result.findings_summary
                    if s.startswith("CLASS_A")
                ],
                "retry_history": history,
            })
        # Count CLASS_B across all scenes
        for s in result.findings_summary:
            if s.startswith("CLASS_B"):
                total_class_b += 1

    return {
        "run_id": run_id,
        "blocked": True,
        "class_a_failures": total_class_a,
        "class_b_warnings": total_class_b,
        "failing_scenes": failing_scenes,
        "remediation_path": "operator review required — see retry history for failure pattern",
    }


# ── S-8 provenance helpers ───────────────────────────────────────────────


def _write_scene_provenance(
    provenance_dir: str,
    run_provenance_dir: str,
    ch: int,
    sc: int,
    scene,
    model: str,
    generation_params: dict,
    target_words: int,
    passed: bool,
    attempt_records: list,
    run_system_prompt_sha256: str,
    scene_system_prompt: str,
):
    """Write per-scene provenance JSON and handle system-prompt divergence."""
    scene_sha = hashlib.sha256(scene_system_prompt.encode("utf-8")).hexdigest()
    if scene_sha == run_system_prompt_sha256:
        sp_ref = "run_provenance/system_prompt.txt"
    else:
        # Divergent system prompt (corrections injected) — store separately
        sp_filename = f"system_prompt_sc{sc:03d}.txt"
        sp_path = os.path.join(run_provenance_dir, sp_filename)
        with open(sp_path, "w", encoding="utf-8") as f:
            f.write(scene_system_prompt)
        sp_ref = f"run_provenance/{sp_filename}"

    provenance = {
        "chapter": ch,
        "scene": sc,
        "title": scene.title,
        "scene_type": scene.scene_type,
        "pov": scene.pov,
        "model": model,
        "generation_params": generation_params,
        "system_prompt_sha256": scene_sha,
        "system_prompt_ref": sp_ref,
        "target_words": target_words,
        "final_passed": passed,
        "total_attempts": len(attempt_records),
        "attempts": attempt_records,
    }

    filename = f"sc_ch{ch:02d}_sc{sc:02d}_provenance.json"
    path = os.path.join(provenance_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(provenance, f, indent=2)


def generate_manuscript(
    synopsis_path: str,
    intake_path: str,
    series_bible_path: str,
    character_profiles_path: str,
    principles_path: str,
    output_dir: str,
    max_attempts_per_scene: int = 3,
    skip_llm_audit: bool = False,
    chapter_filter: int = None,
):
    """Run full Act 1 manuscript generation."""
    print(f"\n{'='*70}")
    print(f"  ANPD V25 — MANUSCRIPT ORCHESTRATOR")
    print(f"{'='*70}")

    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    manuscript_dir = os.path.join(output_dir, f"manuscript_{timestamp}")
    os.makedirs(manuscript_dir, exist_ok=True)
    audit_dir = os.path.join(manuscript_dir, "scene_audits")
    os.makedirs(audit_dir, exist_ok=True)
    # S-8: provenance directories
    provenance_dir = os.path.join(manuscript_dir, "scene_provenance")
    os.makedirs(provenance_dir, exist_ok=True)
    run_provenance_dir = os.path.join(manuscript_dir, "run_provenance")
    os.makedirs(run_provenance_dir, exist_ok=True)

    # ── Load inputs ──
    print("\n  Loading inputs...")
    synopsis = parse_synopsis(synopsis_path)
    print(f"    Synopsis: {synopsis.scene_count} scenes across {len(synopsis.chapters)} chapters")

    with open(intake_path, 'r', encoding='utf-8') as f:
        intake = json.load(f)
    with open(series_bible_path, 'r', encoding='utf-8') as f:
        series_bible = json.load(f)
    with open(character_profiles_path, 'r', encoding='utf-8') as f:
        character_profiles = json.load(f)
    with open(principles_path, 'r', encoding='utf-8') as f:
        principles_data = json.load(f)

    craft_principles = principles_data.get("principles", principles_data)

    # Load entity ledger (non-fatal if absent or unparseable)
    entity_ledger = None
    entity_ledger_path = os.path.join(os.path.dirname(intake_path), "entity_ledger.json")
    if os.path.isfile(entity_ledger_path):
        try:
            with open(entity_ledger_path, 'r', encoding='utf-8') as f:
                entity_ledger = json.load(f)
            print(f"    Entity ledger: loaded ({len(entity_ledger.get('entities', []))} entities)")
        except json.JSONDecodeError as e:
            print(f"    WARNING: entity_ledger.json present but unparseable: {e}", file=sys.stderr)
            print("    Proceeding without ledger awareness; audit will backstop", file=sys.stderr)
    else:
        print(f"    Note: no entity_ledger.json at {entity_ledger_path}; proceeding without ledger")

    print("    Inputs loaded.")

    # Calculate per-scene word target
    total_target = intake.get("target_word_count", 85000)
    total_chapters = intake.get("total_chapter_count", len(synopsis.chapters))
    # For Act 1 (8 chapters of 25), scale proportionally
    act_fraction = total_chapters / 25 if total_chapters < 25 else 1.0
    act_target = int(total_target * act_fraction)
    words_per_scene = act_target // synopsis.scene_count if synopsis.scene_count else 850
    words_per_scene = max(700, min(1100, words_per_scene))
    print(f"    Target: {act_target} words for Act 1, ~{words_per_scene} words/scene")

    # S-3: log skip-llm-audit scope — scene-level deterministic checks
    # that run inside audit_scene() regardless of use_llm (see scene_auditor.py)
    if skip_llm_audit:
        _DET_CHECKS = [
            "word_count", "smell_opener", "time_refs", "age_refs",
            "base_language", "metadata_leak", "character_state",
            "balaclava_ops", "reflexive_tautology",
        ]
        print(f"    --skip-llm-audit set: LLM-judgment checks bypassed. "
              f"Scene-level deterministic gates still run: {', '.join(_DET_CHECKS)}.")

    # ── Generate scenes ──
    all_scenes = synopsis.all_scenes
    scene_results = {}
    scene_prose = {}
    retry_history: dict[tuple[int, int], list[dict]] = defaultdict(list)
    total_tokens = {"input": 0, "output": 0}
    total_attempts = 0
    class_a_failures = 0
    # S-8: system prompt stored once per run
    run_system_prompt_sha256 = None

    # Support chapter-scoped runs
    chapters_to_run = synopsis.chapters
    if chapter_filter is not None:
        chapters_to_run = [ch for ch in synopsis.chapters if ch.chapter_number == chapter_filter]
        if not chapters_to_run:
            raise ValueError(f"Chapter {chapter_filter} not found in synopsis")

    scenes_to_run = []
    for ch in chapters_to_run:
        scenes_to_run.extend(ch.scenes)

    print(f"\n  Generating {len(scenes_to_run)} scenes...")

    scene_idx = 0
    for chapter in chapters_to_run:
        accumulated_chapter_prose = []  # Reset at chapter boundary

        for scene in chapter.scenes:
            scene_idx += 1
            # Build adjacent context from full scene list
            flat_idx = all_scenes.index(scene) if scene in all_scenes else scene_idx - 1
            prior = all_scenes[flat_idx - 1] if flat_idx > 0 else None
            nxt = all_scenes[flat_idx + 1] if flat_idx < len(all_scenes) - 1 else None
            adjacent = {"prior": prior, "next": nxt}

            ch = scene.chapter_number
            sc = scene.scene_number
            label = f"Ch{ch} Sc{sc}"
            print(f"    [{scene_idx}/{len(scenes_to_run)}] {label} — {scene.title}...", end='', flush=True)

            best_prose = ""
            best_findings = []
            passed = False
            feedback = ""
            # S-8: per-attempt provenance accumulator
            attempt_provenance = []
            scene_system_prompt = ""
            scene_model = ""
            scene_generation_params = {}

            for attempt in range(1, max_attempts_per_scene + 1):
                total_attempts += 1
                t0 = time.time()
                result = write_scene(
                    scene=scene,
                    adjacent=adjacent,
                    series_bible=series_bible,
                    character_profiles=character_profiles,
                    craft_principles=craft_principles,
                    target_words=words_per_scene,
                    failure_feedback=feedback,
                    prior_prose_in_chapter=accumulated_chapter_prose.copy(),
                    entity_ledger=entity_ledger,
                )
                elapsed = time.time() - t0
                wc = len(result.prose.split())

                total_tokens["input"] += result.tokens_used.get("input_tokens", 0)
                total_tokens["output"] += result.tokens_used.get("output_tokens", 0)

                # S-8: capture system prompt / model / params from first attempt
                if attempt == 1:
                    scene_system_prompt = result.system_prompt
                    scene_model = result.model
                    scene_generation_params = result.generation_params

                # Audit
                audit = audit_scene(
                    prose=result.prose,
                    scene=scene,
                    craft_principles=craft_principles,
                    series_bible=series_bible,
                    use_llm=not skip_llm_audit,
                    prior_prose_in_chapter=accumulated_chapter_prose.copy(),
                )

                best_prose = result.prose
                best_findings = audit.findings

                # S-8: compute per-attempt cost
                attempt_model = result.model or os.environ.get("V25_MODEL", "claude-sonnet-4-6")
                attempt_cost = _calc_cost(
                    attempt_model,
                    result.tokens_used.get("input_tokens", 0),
                    result.tokens_used.get("output_tokens", 0),
                )

                if audit.passed:
                    passed = True
                    retry_history[(ch, sc)].append({
                        "attempt": attempt,
                        "word_count": wc,
                        "outcome": "PASS",
                        "trips": [],
                        "elapsed_seconds": round(elapsed, 1),
                    })
                    # S-8: record passing attempt provenance
                    attempt_provenance.append({
                        "attempt": attempt,
                        "user_prompt": result.full_user_prompt,
                        "output": result.prose,
                        "word_count": wc,
                        "tokens": result.tokens_used,
                        "cost_usd": round(attempt_cost, 4),
                        "elapsed_seconds": round(elapsed, 1),
                        "audit_passed": True,
                        "gates_fired": [],
                        "failure_feedback_for_next": "",
                    })
                    print(f" {elapsed:.0f}s, {wc}w, PASS (attempt {attempt})")
                    break
                else:
                    class_a_items = [f for f in audit.findings if f.severity == "CLASS_A"]
                    trips = [f.check for f in class_a_items]
                    retry_history[(ch, sc)].append({
                        "attempt": attempt,
                        "word_count": wc,
                        "outcome": "FAIL",
                        "trips": trips,
                        "elapsed_seconds": round(elapsed, 1),
                    })
                    # S-3: attempt-aware retry feedback
                    feedback = _build_retry_feedback(
                        class_a_items, attempt, max_attempts_per_scene, words_per_scene,
                    )
                    # S-8: record failing attempt provenance
                    gates_fired = [
                        {"check": f.check, "severity": f.severity, "message": f.message}
                        for f in audit.findings
                    ]
                    attempt_provenance.append({
                        "attempt": attempt,
                        "user_prompt": result.full_user_prompt,
                        "output": result.prose,
                        "word_count": wc,
                        "tokens": result.tokens_used,
                        "cost_usd": round(attempt_cost, 4),
                        "elapsed_seconds": round(elapsed, 1),
                        "audit_passed": False,
                        "gates_fired": gates_fired,
                        "failure_feedback_for_next": feedback,
                    })
                    if attempt < max_attempts_per_scene:
                        print(f" {elapsed:.0f}s, {wc}w, FAIL({len(class_a_items)}A) retry...", end='', flush=True)
                    else:
                        print(f" {elapsed:.0f}s, {wc}w, FAIL({len(class_a_items)}A) EXHAUSTED")
                        class_a_failures += 1

            # Only accumulate passed prose (don't condition on broken output)
            if passed:
                accumulated_chapter_prose.append(best_prose)

            # S-8: store run-level system prompt on first scene
            if run_system_prompt_sha256 is None and scene_system_prompt:
                run_system_prompt_sha256 = hashlib.sha256(
                    scene_system_prompt.encode("utf-8")
                ).hexdigest()
                sp_path = os.path.join(run_provenance_dir, "system_prompt.txt")
                with open(sp_path, "w", encoding="utf-8") as f:
                    f.write(scene_system_prompt)

            # S-8: write per-scene provenance (immediately, not accumulated)
            if scene_system_prompt and run_system_prompt_sha256:
                _write_scene_provenance(
                    provenance_dir=provenance_dir,
                    run_provenance_dir=run_provenance_dir,
                    ch=ch,
                    sc=sc,
                    scene=scene,
                    model=scene_model,
                    generation_params=scene_generation_params,
                    target_words=words_per_scene,
                    passed=passed,
                    attempt_records=attempt_provenance,
                    run_system_prompt_sha256=run_system_prompt_sha256,
                    scene_system_prompt=scene_system_prompt,
                )

            # Store results
            key = (ch, sc)
            scene_prose[key] = best_prose
            scene_results[key] = SceneResult(
                chapter=ch,
                scene=sc,
                title=scene.title,
                scene_type=scene.scene_type,
                pov=scene.pov,
                word_count=len(best_prose.split()),
                attempts=attempt,
                passed=passed,
                findings_summary=[f"{f.severity}: {f.message}" for f in best_findings],
            )

            # Save per-scene audit
            audit_path = os.path.join(audit_dir, f"ch{ch:02d}_sc{sc:02d}_audit.json")
            with open(audit_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "chapter": ch,
                    "scene": sc,
                    "passed": passed,
                    "attempts": attempt,
                    "findings": [{"id": fi.id, "check": fi.check, "severity": fi.severity,
                                  "message": fi.message, "excerpt": fi.excerpt} for fi in best_findings],
                    "stats": audit.stats if audit else {},
                }, f, indent=2)

    # ── Assemble manuscript ──
    print(f"\n  Assembling manuscript...")
    paths = assemble_manuscript(scene_prose, manuscript_dir, synopsis,
                                class_a_failures=class_a_failures)
    print(f"    Chapters: {len(paths['chapters'])}")
    print(f"    Full manuscript: {paths['full']}")
    if paths.get("blocked"):
        print(f"    *** BLOCKED — {class_a_failures} Class A failure(s) ***")

    # Word count
    with open(paths['full'], 'r') as f:
        full_text = f.read()
    total_words = len(full_text.split())

    # ── Receipt ──
    receipt = {
        "component": "v25_manuscript_orchestrator",
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "synopsis_path": synopsis_path,
        "model": os.environ.get("V25_MODEL", "claude-sonnet-4-6"),
        "total_scenes": len(all_scenes),
        "total_words": total_words,
        "total_attempts": total_attempts,
        "class_a_failures": class_a_failures,
        "total_tokens": total_tokens,
        "per_scene": {
            f"ch{r.chapter}_sc{r.scene}": {
                "title": r.title, "type": r.scene_type, "pov": r.pov,
                "words": r.word_count, "attempts": r.attempts, "passed": r.passed,
                "findings": r.findings_summary,
            }
            for r in scene_results.values()
        },
        "output_paths": {
            "manuscript_dir": manuscript_dir,
            "full_manuscript": paths["full"],
            "chapters": paths["chapters"],
        },
    }
    receipt_path = os.path.join(manuscript_dir, "manuscript_receipt.json")
    with open(receipt_path, 'w', encoding='utf-8') as f:
        json.dump(receipt, f, indent=2)

    # S-3: write failure_report.json when blocked
    if class_a_failures > 0:
        failure_report = _build_failure_report(
            scene_results, retry_history,
            run_id=os.path.basename(manuscript_dir),
        )
        failure_report_path = os.path.join(manuscript_dir, "failure_report.json")
        with open(failure_report_path, 'w', encoding='utf-8') as f:
            json.dump(failure_report, f, indent=2)
        print(f"    Failure report: {failure_report_path}")

    print(f"\n{'='*70}")
    print(f"  V25 MANUSCRIPT ORCHESTRATOR COMPLETE")
    print(f"  Scenes: {len(all_scenes)} ({class_a_failures} Class A failures)")
    print(f"  Words:  {total_words:,}")
    print(f"  Output: {manuscript_dir}")
    print(f"{'='*70}\n")

    return receipt


def main():
    parser = argparse.ArgumentParser(description='ANPD V25 Manuscript Orchestrator')
    parser.add_argument('--synopsis', required=True, help='Path to approved synopsis')
    parser.add_argument('--intake', required=True, help='Path to intake.json')
    parser.add_argument('--series-bible', required=True, help='Path to series_bible.json')
    parser.add_argument('--character-profiles', required=True, help='Path to character_profiles.json')
    parser.add_argument('--principles', required=True, help='Path to craft_principles.json')
    parser.add_argument('--output-dir', required=True, help='Output directory')
    parser.add_argument('--max-attempts', type=int, default=3, help='Max retry attempts per scene')
    parser.add_argument('--skip-llm-audit', action='store_true', help='Skip LLM-based audit checks')
    parser.add_argument('--chapter', type=int, default=None, help='Generate only this chapter number')
    args = parser.parse_args()

    try:
        receipt = generate_manuscript(
            synopsis_path=args.synopsis,
            intake_path=args.intake,
            series_bible_path=args.series_bible,
            character_profiles_path=args.character_profiles,
            principles_path=args.principles,
            output_dir=args.output_dir,
            max_attempts_per_scene=args.max_attempts,
            skip_llm_audit=args.skip_llm_audit,
            chapter_filter=args.chapter,
        )
    except Exception as e:
        print(f"\n  FATAL ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(0 if receipt.get("class_a_failures", 0) == 0 else 1)


if __name__ == "__main__":
    main()
