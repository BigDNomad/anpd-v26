"""
audit_existing_manuscript.py — V25 LLM Beat-Coverage Audit
ANPD V25 | Version: 20260511

Runs LLM-based beat-coverage audit on existing manuscript prose files.
Read-only — does NOT regenerate any scenes.

Usage:
    python3 audit_existing_manuscript.py \
      --synopsis <path> \
      --manuscript-dir <path> \
      --series-bible <path> \
      --principles <path>
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synopsis_parser import parse_synopsis
from scene_auditor import audit_scene
from principles_loader import load_principles


def run_audit(
    synopsis_path: str,
    manuscript_dir: str,
    series_bible_path: str,
    principles_path: str,
):
    """Run LLM beat-coverage audit on existing manuscript."""
    print(f"\n{'='*70}")
    print(f"  V25 — LLM BEAT-COVERAGE AUDIT (read-only)")
    print(f"{'='*70}")

    # Load inputs
    synopsis = parse_synopsis(synopsis_path)
    with open(series_bible_path, 'r', encoding='utf-8') as f:
        series_bible = json.load(f)
    craft_principles = load_principles(principles_path)

    scene_prose_dir = os.path.join(manuscript_dir, "scene_prose")
    if not os.path.isdir(scene_prose_dir):
        raise FileNotFoundError(f"scene_prose directory not found: {scene_prose_dir}")

    # Output directory
    audit_dir = os.path.join(manuscript_dir, "audit_report")
    os.makedirs(audit_dir, exist_ok=True)
    scene_audit_dir = os.path.join(audit_dir, "scene_audits")
    os.makedirs(scene_audit_dir, exist_ok=True)

    all_scenes = synopsis.all_scenes
    print(f"\n  Auditing {len(all_scenes)} scenes with LLM beat-coverage...\n")

    total_tokens = {"input": 0, "output": 0}
    per_scene_results = {}
    total_beats = 0
    total_covered = 0
    total_class_a = 0
    total_class_b = 0
    all_uncovered = []

    t_start = time.time()

    for idx, scene in enumerate(all_scenes):
        ch = scene.chapter_number
        sc = scene.scene_number
        label = f"ch{ch:02d}_sc{sc:02d}"

        # Load prose
        prose_path = os.path.join(scene_prose_dir, f"{label}.md")
        if not os.path.exists(prose_path):
            print(f"    [{idx+1}/{len(all_scenes)}] {label} — MISSING FILE")
            per_scene_results[label] = {"error": "prose file not found"}
            continue

        with open(prose_path, 'r', encoding='utf-8') as f:
            prose = f.read()

        print(f"    [{idx+1}/{len(all_scenes)}] {label} — {scene.title}...", end='', flush=True)
        t0 = time.time()

        # Run full audit with LLM
        result = audit_scene(
            prose=prose,
            scene=scene,
            craft_principles=craft_principles,
            series_bible=series_bible,
            use_llm=True,
        )
        elapsed = time.time() - t0

        # Count beats
        beats = [p.strip() for p in scene.body.split('\n\n') if p.strip() and len(p.strip()) > 30]
        beat_count = len(beats) if beats else 1
        beat_misses = [f for f in result.findings if f.check == "beat_coverage"]
        covered = beat_count - len(beat_misses)
        coverage_pct = (covered / beat_count * 100) if beat_count > 0 else 100.0

        total_beats += beat_count
        total_covered += covered

        class_a = [f for f in result.findings if f.severity == "CLASS_A"]
        class_b = [f for f in result.findings if f.severity == "CLASS_B"]
        total_class_a += len(class_a)
        total_class_b += len(class_b)

        status = "PASS" if result.passed else f"FAIL({len(class_a)}A)"
        print(f" {elapsed:.0f}s, {coverage_pct:.0f}% coverage, {status}")

        uncovered_beats = [f.excerpt for f in beat_misses]
        if uncovered_beats:
            for ub in uncovered_beats:
                all_uncovered.append({"scene": label, "title": scene.title, "beat": ub})

        # Store results
        per_scene_results[label] = {
            "title": scene.title,
            "word_count": len(prose.split()),
            "beat_coverage_pct": round(coverage_pct, 1),
            "beats_total": beat_count,
            "beats_covered": covered,
            "uncovered_beats": uncovered_beats,
            "class_a_count": len(class_a),
            "class_b_count": len(class_b),
            "all_findings": [
                {"id": f.id, "check": f.check, "severity": f.severity,
                 "message": f.message, "excerpt": f.excerpt}
                for f in result.findings
            ],
        }

        # Save per-scene audit
        audit_path = os.path.join(scene_audit_dir, f"{label}_audit.json")
        with open(audit_path, 'w', encoding='utf-8') as f:
            json.dump(per_scene_results[label], f, indent=2)

    total_elapsed = time.time() - t_start
    overall_coverage = (total_covered / total_beats * 100) if total_beats > 0 else 100.0

    # ── Write per_scene_findings.json ──
    findings_path = os.path.join(audit_dir, "per_scene_findings.json")
    with open(findings_path, 'w', encoding='utf-8') as f:
        json.dump(per_scene_results, f, indent=2)

    # ── Write summary.md ──
    summary_lines = [
        "# V25 LLM Beat-Coverage Audit Report",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Synopsis: {synopsis_path}",
        f"Manuscript: {manuscript_dir}",
        "",
        "## Summary",
        "",
        f"- Total scenes audited: {len(all_scenes)}",
        f"- Total beats checked: {total_beats}",
        f"- Beats covered: {total_covered} ({overall_coverage:.1f}%)",
        f"- Beats uncovered: {total_beats - total_covered}",
        f"- Class A findings: {total_class_a}",
        f"- Class B findings: {total_class_b}",
        f"- Wall time: {total_elapsed:.0f}s",
        "",
    ]

    if all_uncovered:
        summary_lines.append("## Uncovered Beats")
        summary_lines.append("")
        for item in all_uncovered:
            summary_lines.append(f"- **{item['scene']}** ({item['title']}): {item['beat'][:150]}")
        summary_lines.append("")

    summary_lines.append("## Per-Scene Results")
    summary_lines.append("")
    summary_lines.append("| Scene | Title | Beats | Covered | Class A | Class B |")
    summary_lines.append("|---|---|---|---|---|---|")
    for label, data in per_scene_results.items():
        if "error" in data:
            summary_lines.append(f"| {label} | ERROR | — | — | — | — |")
        else:
            summary_lines.append(
                f"| {label} | {data['title']} | {data['beats_total']} | "
                f"{data['beats_covered']}/{data['beats_total']} ({data['beat_coverage_pct']}%) | "
                f"{data['class_a_count']} | {data['class_b_count']} |"
            )

    summary_path = os.path.join(audit_dir, "summary.md")
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(summary_lines))

    # ── Print summary ──
    print(f"\n{'='*70}")
    print(f"  AUDIT COMPLETE")
    print(f"  Scenes: {len(all_scenes)}")
    print(f"  Beat coverage: {total_covered}/{total_beats} ({overall_coverage:.1f}%)")
    print(f"  Class A findings: {total_class_a}")
    print(f"  Class B findings: {total_class_b}")
    print(f"  Wall time: {total_elapsed:.0f}s")
    print(f"  Report: {audit_dir}")
    print(f"{'='*70}\n")

    return {
        "scenes_audited": len(all_scenes),
        "total_beats": total_beats,
        "beats_covered": total_covered,
        "coverage_pct": overall_coverage,
        "class_a": total_class_a,
        "class_b": total_class_b,
        "wall_time": total_elapsed,
        "uncovered": all_uncovered,
    }


def main():
    parser = argparse.ArgumentParser(description='V25 LLM Beat-Coverage Audit')
    parser.add_argument('--synopsis', required=True)
    parser.add_argument('--manuscript-dir', required=True)
    parser.add_argument('--series-bible', required=True)
    parser.add_argument('--principles', required=True)
    args = parser.parse_args()

    run_audit(
        synopsis_path=args.synopsis,
        manuscript_dir=args.manuscript_dir,
        series_bible_path=args.series_bible,
        principles_path=args.principles,
    )


if __name__ == "__main__":
    main()
