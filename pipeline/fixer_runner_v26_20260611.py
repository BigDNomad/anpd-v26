"""
fixer_runner — Convergence loop wrapping audit + fixer.

Runs audit → fix → re-audit iteratively until convergence
(CLASS_A == 0), max iterations, no progress, or error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from audit_checks import Finding, BriefBundle
from manuscript_auditor_v25 import run_audit, load_briefs, load_manuscript
from manuscript_fixer import ManuscriptFixer, FixerResult


# ── Configuration ──────────────────────────────────────────────────────

MAX_ITERATIONS = 5
PROGRESS_THRESHOLD = 1


# ── Result types ───────────────────────────────────────────────────────

@dataclass
class IterationResult:
    iteration: int
    audit_report_path: str
    class_a_count: int
    class_b_count: int
    class_c_count: int
    total_findings: int
    fixer_summary: dict | None
    audit_wall_sec: float
    fixer_wall_sec: float


@dataclass
class RunnerResult:
    book_dir: str
    manuscript_path: str
    iterations: list[IterationResult] = field(default_factory=list)
    termination_reason: str = ""
    final_class_a: int = -1
    total_wall_sec: float = 0.0
    total_cost_usd: float = 0.0
    log_path: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────

def _bookslug_from_book_dir(book_dir: Path) -> str:
    """Infer book slug from intake.json or fall back to directory name."""
    intake = book_dir / "work" / "intake.json"
    if intake.exists():
        try:
            data = json.loads(intake.read_text(encoding="utf-8"))
            slug = data.get("book_slug") or data.get("slug")
            if slug:
                return slug
        except Exception:
            pass
    return book_dir.name


def _load_audit_summary(report_path: Path) -> tuple[int, int, int, int]:
    """Returns (class_a, class_b, class_c, total) from a report."""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    s = data.get("summary", {})
    return (
        int(s.get("class_a", 0)),
        int(s.get("class_b", 0)),
        int(s.get("class_c", 0)),
        int(s.get("total_findings", s.get("total", 0))),
    )


def _extract_class_a_findings(report_path: Path) -> list[Finding]:
    """Convert the JSON report's all_findings into Finding objects, CLASS_A only."""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    findings = []
    for f in data.get("all_findings", []):
        if f.get("severity") != "CLASS_A":
            continue
        findings.append(Finding(
            check_id=f["check_id"],
            severity=f["severity"],
            description=f.get("description", ""),
            evidence=f.get("evidence", []),
            scene_number=f.get("scene_number"),
            scene_numbers=f.get("scene_numbers", []),
            line_number=f.get("line_number"),
            suggested_fix=f.get("suggested_fix", ""),
        ))
    return findings


def _load_briefs_from_paths(book_dir: Path, briefs_dir: Path | None) -> BriefBundle:
    """Load BriefBundle by constructing paths from book_dir and series dir."""
    series_dir = briefs_dir or book_dir.parent
    synopsis_file = book_dir / "work" / "synopsis.md"
    return load_briefs(
        series_bible_path=str(series_dir / "series_bible.json"),
        character_profiles_path=str(series_dir / "character_profiles.json"),
        book_config_path=str(book_dir / "work" / "intake.json"),
        entity_ledger_path=str(book_dir / "work" / "entity_ledger.json"),
        synopsis_path=str(synopsis_file) if synopsis_file.is_file() else None,
    )


def _fixer_summary(fix_result: FixerResult) -> dict:
    """Extract a JSON-serializable summary from FixerResult."""
    return {
        "tier_1_applied": fix_result.tier_1_applied,
        "tier_1_skipped": fix_result.tier_1_skipped,
        "tier_2_applied": fix_result.tier_2_applied,
        "tier_2_skipped": fix_result.tier_2_skipped,
        "tier_3_escalated": fix_result.tier_3_escalated,
        "scenes_regenerated": fix_result.scenes_regenerated,
        "regeneration_cost_usd": fix_result.regeneration_cost_usd,
        "regeneration_time_sec": fix_result.regeneration_time_sec,
    }


# ── Main callable ──────────────────────────────────────────────────────

def run_fixer_loop(
    book_dir: Path,
    manuscript_path: Path,
    max_iterations: int = MAX_ITERATIONS,
    llm_client=None,
    briefs_dir: Path | None = None,
    briefs: BriefBundle | None = None,
) -> RunnerResult:
    """Run audit → fix → re-audit loop until convergence or iteration cap."""
    t_start = time.time()
    bookslug = _bookslug_from_book_dir(book_dir)

    workspace_dir = book_dir / "_fixer_workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    audit_runs_dir = workspace_dir / "audit_runs"
    audit_runs_dir.mkdir(exist_ok=True)

    if briefs is None:
        briefs = _load_briefs_from_paths(book_dir, briefs_dir)

    result = RunnerResult(
        book_dir=str(book_dir),
        manuscript_path=str(manuscript_path),
    )

    current_manuscript_path = manuscript_path
    prev_class_a = None

    for iter_num in range(1, max_iterations + 1):
        # ── Audit pass ──────────────────────────────────────────────
        iter_audit_dir = audit_runs_dir / f"iter_{iter_num:02d}"
        iter_audit_dir.mkdir(exist_ok=True)

        t_audit = time.time()
        try:
            ms = load_manuscript(str(current_manuscript_path))
            run_audit(
                manuscript=ms,
                briefs=briefs,
                output_dir=str(iter_audit_dir),
            )
        except Exception as e:
            audit_wall = time.time() - t_audit
            print(f"  Iteration {iter_num}: AUDIT ERROR after {audit_wall:.1f}s: {e}", file=sys.stderr)
            result.termination_reason = "error"
            result.iterations.append(IterationResult(
                iteration=iter_num,
                audit_report_path=str(iter_audit_dir / "manuscript_audit_REPORT.json"),
                class_a_count=-1, class_b_count=-1, class_c_count=-1, total_findings=-1,
                fixer_summary=None, audit_wall_sec=time.time() - t_audit, fixer_wall_sec=0.0,
            ))
            break
        audit_wall = time.time() - t_audit

        report_path = iter_audit_dir / "manuscript_audit_REPORT.json"
        try:
            class_a, class_b, class_c, total = _load_audit_summary(report_path)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"  Iteration {iter_num}: AUDIT REPORT MISSING/CORRUPT after {audit_wall:.1f}s: {e}", file=sys.stderr)
            result.termination_reason = "audit_failure"
            result.iterations.append(IterationResult(
                iteration=iter_num,
                audit_report_path=str(report_path),
                class_a_count=-1, class_b_count=-1, class_c_count=-1, total_findings=-1,
                fixer_summary=None, audit_wall_sec=audit_wall, fixer_wall_sec=0.0,
            ))
            break

        # ── Convergence check ───────────────────────────────────────
        if class_a == 0:
            result.iterations.append(IterationResult(
                iteration=iter_num, audit_report_path=str(report_path),
                class_a_count=0, class_b_count=class_b,
                class_c_count=class_c, total_findings=total,
                fixer_summary=None, audit_wall_sec=audit_wall, fixer_wall_sec=0.0,
            ))
            result.termination_reason = "converged"
            result.final_class_a = 0
            print(f"  Iteration {iter_num}: audited (0 CLASS_A in {audit_wall:.0f}s) → CONVERGED")
            break

        # ── No-progress check ───────────────────────────────────────
        if prev_class_a is not None and (prev_class_a - class_a) < PROGRESS_THRESHOLD:
            result.iterations.append(IterationResult(
                iteration=iter_num, audit_report_path=str(report_path),
                class_a_count=class_a, class_b_count=class_b,
                class_c_count=class_c, total_findings=total,
                fixer_summary=None, audit_wall_sec=audit_wall, fixer_wall_sec=0.0,
            ))
            result.termination_reason = "no_progress"
            result.final_class_a = class_a
            print(f"  Iteration {iter_num}: audited ({class_a} CLASS_A in {audit_wall:.0f}s) → NO PROGRESS (was {prev_class_a})")
            break

        # ── Fixer pass ──────────────────────────────────────────────
        findings = _extract_class_a_findings(report_path)
        t_fix = time.time()
        try:
            fixer = ManuscriptFixer(
                book_dir=book_dir,
                briefs=briefs,
                llm_client=llm_client,
                skip_workspace_setup=(iter_num > 1),
                iteration_number=iter_num,
                manuscript_src=current_manuscript_path,
            )
            fix_result = fixer.run(findings)
        except Exception as e:
            fix_wall = time.time() - t_fix
            print(f"  Iteration {iter_num}: FIXER ERROR after {fix_wall:.1f}s: {e}", file=sys.stderr)
            result.termination_reason = "error"
            result.iterations.append(IterationResult(
                iteration=iter_num, audit_report_path=str(report_path),
                class_a_count=class_a, class_b_count=class_b,
                class_c_count=class_c, total_findings=total,
                fixer_summary=None, audit_wall_sec=audit_wall, fixer_wall_sec=fix_wall,
            ))
            break
        fix_wall = time.time() - t_fix

        result.iterations.append(IterationResult(
            iteration=iter_num, audit_report_path=str(report_path),
            class_a_count=class_a, class_b_count=class_b,
            class_c_count=class_c, total_findings=total,
            fixer_summary=_fixer_summary(fix_result),
            audit_wall_sec=audit_wall, fixer_wall_sec=fix_wall,
        ))
        result.total_cost_usd += fix_result.regeneration_cost_usd

        print(f"  Iteration {iter_num}: audited ({class_a} CLASS_A in {audit_wall:.0f}s) → "
              f"fixed ({len(fix_result.scenes_regenerated)} scenes regen, "
              f"T1={fix_result.tier_1_applied}, in {fix_wall:.0f}s)")

        # Next iteration audits workspace, not source
        ws_manuscript = workspace_dir / "manuscript"
        current_manuscript_path = ws_manuscript
        prev_class_a = class_a

    else:
        # Max iterations exhausted
        result.termination_reason = "max_iterations"
        if result.iterations:
            result.final_class_a = result.iterations[-1].class_a_count
        print(f"  Max iterations ({max_iterations}) reached. Final CLASS_A: {result.final_class_a}")

    # ── Write consolidated log ─────────────────────────────────────
    result.total_wall_sec = time.time() - t_start
    if result.final_class_a < 0 and result.iterations:
        result.final_class_a = result.iterations[-1].class_a_count

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = workspace_dir / f"fixer_run_{bookslug}_{ts}.json"

    log_data = {
        "runner_meta": {
            "book_slug": bookslug,
            "book_dir": str(book_dir),
            "manuscript_path": str(manuscript_path),
            "started_at": datetime.fromtimestamp(t_start, tz=timezone.utc).isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "total_wall_sec": round(result.total_wall_sec, 1),
            "total_cost_usd": round(result.total_cost_usd, 4),
            "termination_reason": result.termination_reason,
            "max_iterations_setting": max_iterations,
            "iterations_run": len(result.iterations),
        },
        "iterations": [asdict(it) for it in result.iterations],
        "final_state": {
            "class_a": result.final_class_a,
            "publish_gate_clearable": result.final_class_a == 0,
            "workspace_manuscript_path": str(workspace_dir / "manuscript"),
            "next_step_hint": _next_step_hint(result),
        },
    }
    log_path.write_text(json.dumps(log_data, indent=2, default=str), encoding="utf-8")
    result.log_path = str(log_path)

    print(f"Fixer runner complete: {result.termination_reason}, "
          f"final CLASS_A = {result.final_class_a}, log = {log_path}")

    return result


def _next_step_hint(result: RunnerResult) -> str:
    if result.termination_reason == "converged":
        return ("Workspace manuscript is publish-gate-clearable. "
                "Run anpd_export.py against the workspace manuscript, "
                "OR invoke F6 to promote workspace to source.")
    if result.termination_reason == "no_progress":
        return ("Convergence halted — fixer is not reducing CLASS_A. "
                "Residual findings likely indicate system gaps.")
    if result.termination_reason == "max_iterations":
        return (f"Max iterations ({MAX_ITERATIONS}) reached with residual CLASS_A. "
                "Consider system improvements for persistent defect classes.")
    if result.termination_reason == "audit_failure":
        return ("Audit report missing or corrupt. Check audit output directory "
                "for manuscript_audit_REPORT.json.")
    if result.termination_reason == "error":
        return "Halted on error. See log for details."
    return ""


# ── CLI wrapper ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="fixer_runner",
        description="Run audit → fix → re-audit convergence loop.",
    )
    parser.add_argument("--book-dir", required=True,
                        help="Path to book directory (e.g. /anpd/v25/series/airmen/b01)")
    parser.add_argument("--manuscript", required=True,
                        help="Path to assembled manuscript (e.g. act1_full.md)")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS,
                        help=f"Maximum convergence iterations (default: {MAX_ITERATIONS})")
    parser.add_argument("--briefs-dir", default=None,
                        help="Path to series briefs dir; defaults to parent of book-dir")
    args = parser.parse_args()

    if os.geteuid() == 0:
        print("ERROR: fixer_runner must not be run as root. Run as 'anpd'.", file=sys.stderr)
        sys.exit(4)

    book_dir = Path(args.book_dir)
    manuscript_path = Path(args.manuscript)
    briefs_dir = Path(args.briefs_dir) if args.briefs_dir else None

    if not book_dir.is_dir():
        print(f"ERROR: book-dir not found: {book_dir}", file=sys.stderr)
        sys.exit(1)
    if not manuscript_path.exists():
        print(f"ERROR: manuscript not found: {manuscript_path}", file=sys.stderr)
        sys.exit(1)

    result = run_fixer_loop(
        book_dir=book_dir,
        manuscript_path=manuscript_path,
        max_iterations=args.max_iterations,
        briefs_dir=briefs_dir,
    )

    if result.termination_reason == "converged":
        sys.exit(0)
    if result.termination_reason == "max_iterations":
        sys.exit(2)
    if result.termination_reason == "no_progress":
        sys.exit(3)
    sys.exit(1)


if __name__ == "__main__":
    main()
