"""
MA-008 pillar_position_verification — verifies the three structural pillars
hit their expected manuscript positions.

D37 scope: pillars 1 and 3 (mechanical from synopsis TYPE tags).
Pillar 2 (three-twist position) requires LLM semantic analysis; deferred.

Sub-checks:
  A) Action opening — book opens with high-action density
  B) Final battle — climax ACTION present in final ~10% of scenes

All findings CLASS_A (structural, not stylistic).
"""

from __future__ import annotations

import sys

from audit_checks import ManuscriptArtifact, BriefBundle, Finding
from audit_checks._lib.synopsis_scene_types import load_scene_type_map


# ── Configuration ────────────────────────────────────────────────────────────

MA008_BRIEFING_OPENING_WINDOW = 3     # First N scenes inspected for opening
MA008_FINAL_BATTLE_WINDOW_PCT = 0.10  # Last 10% of scenes for climax
MA008_RUSHED_ENDING_WINDOW = 5        # Last N scenes for any action/mixed


# ── Finding builder ──────────────────────────────────────────────────────────

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
            "(action opening / final battle in last 10%)."
        ),
    )


# ── Check class ──────────────────────────────────────────────────────────────

class PillarPositionVerification:
    check_id = "MA-008-pillar-position-verification"
    severity = "CLASS_A"
    description = (
        "Pillar position verification: action opening and final battle "
        "at expected manuscript positions (pillar 2 twist detection deferred)"
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

        # ─── Sub-check A: Action opening ───
        scene_1_type = scene_type_map.get(1, "UNKNOWN")
        if scene_1_type != "ACTION":
            findings.append(_finding(
                severity="CLASS_A",
                scene_number=1,
                description=(
                    f"Pillar 1 violation: scene 1 TYPE is {scene_1_type}, "
                    f"expected ACTION. Book must open with high-action density."
                ),
                evidence=[f"Scene 1 TYPE: {scene_1_type}"],
            ))

        # Briefing-opening anti-pattern: first 3 scenes all NON-ACTION
        opening_types = [
            scene_type_map.get(i, "UNKNOWN")
            for i in range(1, MA008_BRIEFING_OPENING_WINDOW + 1)
        ]
        if all(t == "NON-ACTION" for t in opening_types):
            findings.append(_finding(
                severity="CLASS_A",
                scene_number=1,
                description=(
                    f"Pillar 1 violation: first {MA008_BRIEFING_OPENING_WINDOW} scenes "
                    f"all NON-ACTION (briefing-opening anti-pattern)."
                ),
                evidence=[
                    f"Scenes 1-{MA008_BRIEFING_OPENING_WINDOW}: "
                    + ", ".join(opening_types)
                ],
            ))

        # ─── Sub-check B: Final battle present ───
        final_window_start = max(1, round((1 - MA008_FINAL_BATTLE_WINDOW_PCT) * N) + 1)
        final_types = {
            i: scene_type_map.get(i, "UNKNOWN")
            for i in range(final_window_start, N + 1)
        }
        has_action_in_final = any(t == "ACTION" for t in final_types.values())

        if not has_action_in_final:
            findings.append(_finding(
                severity="CLASS_A",
                scene_number=final_window_start,
                description=(
                    f"Pillar 3 violation: no ACTION scene in final 10% "
                    f"(scenes {final_window_start}-{N}). "
                    f"Final battle missing or insufficient."
                ),
                evidence=[
                    "Final-window TYPEs: "
                    + ", ".join(f"sc{i}={t}" for i, t in final_types.items())
                ],
            ))

        # Rushed-ending anti-pattern: last 5 scenes no ACTION or MIXED
        last_5_start = max(1, N - MA008_RUSHED_ENDING_WINDOW + 1)
        last_5_types = [
            scene_type_map.get(i, "UNKNOWN")
            for i in range(last_5_start, N + 1)
        ]
        if not any(t in ("ACTION", "MIXED") for t in last_5_types):
            findings.append(_finding(
                severity="CLASS_A",
                scene_number=last_5_start,
                description=(
                    f"Pillar 3 violation: last {MA008_RUSHED_ENDING_WINDOW} scenes "
                    f"contain no ACTION or MIXED (rushed/quiet ending)."
                ),
                evidence=[
                    f"Last-{MA008_RUSHED_ENDING_WINDOW} TYPEs: "
                    + ", ".join(last_5_types)
                ],
            ))

        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        print(f"    -> {len(findings)} findings ({class_a} CLASS_A)", file=sys.stderr)

        return findings
