"""
MA-008 pillar_position_verification — verifies the three structural pillars
hit their expected manuscript positions.

Recalibrated 2026-06-13 (operator-derived bands):

Pillar 1 (action opening):
  CLASS_A unless >= 1 ACTION or MIXED scene within scenes 1-5.
  Advisory CLASS_B if first 3 scenes are all NON-ACTION (briefing-opening
  anti-pattern), even when the overall Pillar 1 passes.

Pillar 3 (final battle):
  CLASS_A unless the LAST ACTION or MIXED scene sits at >= 85% position
  (scene_num / total_scenes).

Pillar 2 (three-twist position) requires LLM semantic analysis; deferred.

All structural findings CLASS_A; advisory findings CLASS_B.
"""

from __future__ import annotations

import sys

from audit_checks import ManuscriptArtifact, BriefBundle, Finding
from audit_checks._lib.synopsis_scene_types import load_scene_type_map


# -- Configuration -----------------------------------------------------------

MA008_OPENING_WINDOW = 5          # First N scenes inspected for action opening
MA008_BRIEFING_WINDOW = 3         # First N scenes for NON-ACTION advisory
MA008_FINAL_POSITION_PCT = 0.85   # Last ACTION/MIXED must be >= this position


# -- Finding builder ---------------------------------------------------------

def _finding(severity: str, scene_number: int, description: str,
             evidence: list[str] | None = None) -> Finding:
    return Finding(
        check_id="MA-008-pillar-position-verification",
        severity=severity,
        scene_number=scene_number,
        scene_numbers=[scene_number],
        description=description,
        evidence=evidence or [],
        suggested_fix=(
            "Restructure scenes to satisfy pillar requirements "
            "(action opening in first 5 scenes / final battle at >= 85% position)."
        ),
    )


# -- Check class -------------------------------------------------------------

class PillarPositionVerification:
    check_id = "MA-008-pillar-position-verification"
    severity = "CLASS_A"
    description = (
        "Pillar position verification: action opening (scenes 1-5) and "
        "final battle at >= 85% position (pillar 2 twist detection deferred)"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        scene_type_map = load_scene_type_map(briefs.synopsis_path)

        if not scene_type_map:
            print("    WARN: no synopsis for pillar check; skipping", file=sys.stderr)
            return []

        findings: list[Finding] = []
        N = len(manuscript.scenes)
        if N == 0:
            return []

        print(f"    Scene type map: {len(scene_type_map)} entries, manuscript: {N} scenes",
              file=sys.stderr)

        # --- Sub-check A: Action opening (Pillar 1) ---
        # CLASS_A unless >= 1 ACTION or MIXED in scenes 1..OPENING_WINDOW
        opening_types = {
            i: scene_type_map.get(i, "UNKNOWN")
            for i in range(1, min(MA008_OPENING_WINDOW, N) + 1)
        }
        has_action_opening = any(
            t in ("ACTION", "MIXED") for t in opening_types.values()
        )

        if not has_action_opening:
            findings.append(_finding(
                severity="CLASS_A",
                scene_number=1,
                description=(
                    f"Pillar 1 violation: no ACTION or MIXED scene in first "
                    f"{MA008_OPENING_WINDOW} scenes. Book must open with "
                    f"action density in the opening window."
                ),
                evidence=[
                    f"Scenes 1-{min(MA008_OPENING_WINDOW, N)} TYPEs: "
                    + ", ".join(f"sc{i}={t}" for i, t in opening_types.items())
                ],
            ))

        # Advisory: first 3 scenes all NON-ACTION (briefing-opening anti-pattern)
        briefing_types = [
            scene_type_map.get(i, "UNKNOWN")
            for i in range(1, min(MA008_BRIEFING_WINDOW, N) + 1)
        ]
        if all(t == "NON-ACTION" for t in briefing_types):
            findings.append(_finding(
                severity="CLASS_B",
                scene_number=1,
                description=(
                    f"Pillar 1 advisory: first {MA008_BRIEFING_WINDOW} scenes "
                    f"all NON-ACTION (briefing-opening anti-pattern)."
                ),
                evidence=[
                    f"Scenes 1-{MA008_BRIEFING_WINDOW}: "
                    + ", ".join(briefing_types)
                ],
            ))

        # --- Sub-check B: Final battle position (Pillar 3) ---
        # Find the LAST ACTION or MIXED scene
        last_action_scene = None
        for i in range(N, 0, -1):
            t = scene_type_map.get(i, "UNKNOWN")
            if t in ("ACTION", "MIXED"):
                last_action_scene = i
                break

        if last_action_scene is None:
            findings.append(_finding(
                severity="CLASS_A",
                scene_number=N,
                description=(
                    "Pillar 3 violation: no ACTION or MIXED scene found in "
                    "entire manuscript. Final battle missing entirely."
                ),
                evidence=["No ACTION or MIXED scenes in type map"],
            ))
        else:
            position = last_action_scene / N
            if position < MA008_FINAL_POSITION_PCT:
                findings.append(_finding(
                    severity="CLASS_A",
                    scene_number=last_action_scene,
                    description=(
                        f"Pillar 3 violation: last ACTION/MIXED scene is "
                        f"sc{last_action_scene} at {position:.1%} position, "
                        f"below the {MA008_FINAL_POSITION_PCT:.0%} threshold. "
                        f"Final battle too early in manuscript."
                    ),
                    evidence=[
                        f"Last ACTION/MIXED: scene {last_action_scene}/{N} "
                        f"= {position:.1%} (threshold: {MA008_FINAL_POSITION_PCT:.0%})"
                    ],
                ))

        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        class_b = sum(1 for f in findings if f.severity == "CLASS_B")
        print(f"    -> {len(findings)} findings ({class_a} CLASS_A, {class_b} CLASS_B)",
              file=sys.stderr)

        return findings
