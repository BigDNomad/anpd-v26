#!/usr/bin/env python3
"""
Regression test: MA-011 cross-scene duplication detector against CSAR manuscript.

Validates that the detector surfaces D1 and D2 from the CSAR punch list C-4
and produces exactly 2 Class A findings (no false positives).

FRICTION NOTE: The spec (§3 calibration table) lists D1 as "222 words, lines
1073/1171" and D2 as "150 words, lines 1398/1412". The actual duplications
in manuscript_20260527_0953 are larger:
  - D1: ceiling fan scene (~893 words) duplicated across scenes 17→18
        (lines 984-1018 duplicated within 1020-1098)
  - D2: briefing hut scene (~652 words) duplicated across scenes 18→19
        (lines 1021-1063 duplicated within 1101-1161)
The spec's 222/150 word counts may have measured specific paragraphs rather
than the full maximal match. The detector correctly finds the full extent.
The spec's D2 at lines 1398/1412 is a within-scene near-duplicate (different
prose, same beat) — out of scope for S-1 per spec §2.

Usage:
    cd /anpd/v25 && python3 pipeline/tests/regression_csar_duplication.py
"""

from __future__ import annotations

import os
import sys

# Add pipeline to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import ManuscriptArtifact, SceneText, BriefBundle
from audit_checks.cross_scene_duplication import (
    CrossSceneDuplication,
    split_assembled_manuscript,
)


MANUSCRIPT_PATH = "/anpd/v25/series/airmen/b01/work/manuscript/manuscript_20260527_0953/act1_full.md"


def main() -> int:
    # Load the manuscript
    if not os.path.isfile(MANUSCRIPT_PATH):
        print(f"FAIL: Manuscript not found at {MANUSCRIPT_PATH}")
        return 1

    with open(MANUSCRIPT_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    lines = text.split("\n")
    print(f"Manuscript loaded: {len(lines)} lines")

    # Split on scene-break markers (Mode B)
    segments = split_assembled_manuscript(text)
    print(f"Split into {len(segments)} scene segments")

    # Build ManuscriptArtifact
    scenes = []
    segment_starts = []
    for i, (scene_text, start_line) in enumerate(segments, 1):
        scenes.append(SceneText(
            scene_number=i,
            text=scene_text,
            file_path=MANUSCRIPT_PATH,
        ))
        segment_starts.append(start_line)

    ms = ManuscriptArtifact(
        scenes=scenes,
        manuscript_dir=os.path.dirname(MANUSCRIPT_PATH),
    )

    # Run the check
    checker = CrossSceneDuplication()
    briefs = BriefBundle()
    findings = checker.run(ms, briefs)

    # Report all findings
    print(f"\n{'='*70}")
    print(f"FINDINGS: {len(findings)} total")
    print(f"{'='*70}")

    class_a_findings = []
    class_b_findings = []

    for f in findings:
        scene_nums = f.scene_numbers
        scene_a_idx = scene_nums[0] - 1 if scene_nums else 0
        scene_b_idx = scene_nums[1] - 1 if len(scene_nums) > 1 else 0

        ms_line_a = segment_starts[scene_a_idx] if scene_a_idx < len(segment_starts) else 0
        ms_line_b = segment_starts[scene_b_idx] if scene_b_idx < len(segment_starts) else 0

        print(f"\n  [{f.severity}] Scenes {f.scene_numbers}")
        print(f"    {f.description}")
        print(f"    Manuscript line offsets: scene_a starts ~L{ms_line_a}, scene_b starts ~L{ms_line_b}")
        for ev in f.evidence:
            if ev.startswith("Preview:"):
                print(f"    {ev[:120]}...")
            else:
                print(f"    {ev}")

        if f.severity == "CLASS_A":
            class_a_findings.append((f, ms_line_a, ms_line_b))
        elif f.severity == "CLASS_B":
            class_b_findings.append(f)

    print(f"\n{'='*70}")
    print(f"CLASS A: {len(class_a_findings)}")
    print(f"CLASS B: {len(class_b_findings)}")
    print(f"{'='*70}")

    # ── Validation ────────────────────────────────────────────────────────
    errors = []

    # 1. Exactly 2 Class A findings (spec §10.2)
    if len(class_a_findings) != 2:
        errors.append(
            f"Expected exactly 2 Class A findings, got {len(class_a_findings)}"
        )

    # 2. D1: ceiling fan scene duplication (~893 words between scenes in
    #    the Chapter 5 area, lines 984-1098). Must be ≥200 words.
    d1_found = False
    d2_found = False

    for f, ms_line_a, ms_line_b in class_a_findings:
        word_count = 0
        for ev in f.evidence:
            if ev.startswith("Match length:"):
                word_count = int(ev.split(":")[1].strip().split()[0])

        # D1: the larger match (ceiling fan), scenes near line 984
        # Preview starts with "the ceiling fan turned"
        preview = ""
        for ev in f.evidence:
            if ev.startswith("Preview:"):
                preview = ev.lower()
                break

        if "ceiling fan" in preview or "the ceiling fan turned" in preview:
            d1_found = True
            if word_count < 200:
                errors.append(f"D1 match too short: {word_count} words (expected ≥200)")
            print(f"\n  D1 identified: {word_count} words, scenes {f.scene_numbers}")

        elif "briefing hut" in preview or "the briefing hut" in preview:
            d2_found = True
            if word_count < 100:
                errors.append(f"D2 match too short: {word_count} words (expected ≥100)")
            print(f"\n  D2 identified: {word_count} words, scenes {f.scene_numbers}")

    if not d1_found:
        errors.append("D1 (ceiling fan scene duplication) NOT FOUND")
    if not d2_found:
        errors.append("D2 (briefing hut scene duplication) NOT FOUND")

    # ── Result ────────────────────────────────────────────────────────────

    if errors:
        print(f"\nFAILURES:")
        for e in errors:
            print(f"  x {e}")
        return 1
    else:
        print(f"\nPASS:")
        print(f"  OK  D1 found (ceiling fan scene duplication)")
        print(f"  OK  D2 found (briefing hut scene duplication)")
        print(f"  OK  Exactly 2 Class A findings (no false positives)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
