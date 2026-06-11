"""
MA-009 word_count_discipline — verifies manuscript meets V25 length standards.

CLASS_A on hard floor/ceiling violations.
CLASS_B on soft target-deviation signals (within range but off target).

No LLM phase; pure arithmetic.
"""

from __future__ import annotations

import sys

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Configuration ────────────────────────────────────────────────────────────

MA009_DEFAULTS = {
    "word_count_target":    85_000,
    "word_count_floor":     65_000,
    "word_count_ceiling":   95_000,
    "scene_count_target":   100,
    "scene_count_floor":    80,
    "scene_count_ceiling":  120,
}

MA009_WORD_SOFT_DEVIATION = 0.10   # 10% off target -> CLASS_B
MA009_SCENE_SOFT_DEVIATION = 0.15  # 15% off target -> CLASS_B


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_overrides(briefs: BriefBundle) -> dict[str, int]:
    """Pull per-book overrides from book_config if present, else defaults."""
    overrides = MA009_DEFAULTS.copy()
    book_cfg = getattr(briefs, "book_config", None)
    if book_cfg and isinstance(book_cfg, dict):
        for key in MA009_DEFAULTS:
            if key in book_cfg:
                overrides[key] = int(book_cfg[key])
    return overrides


def _finding(severity: str, description: str, evidence: list[str] | None = None) -> Finding:
    return Finding(
        check_id="MA-009-word-count-discipline",
        severity=severity,
        scene_number=None,
        scene_numbers=[],
        description=description,
        evidence=evidence or [],
        suggested_fix="Adjust manuscript length toward target via scene-level revision or expansion.",
    )


# ── Check class ──────────────────────────────────────────────────────────────

class WordCountDiscipline:
    check_id = "MA-009-word-count-discipline"
    severity = "CLASS_A"
    description = "Word count discipline: manuscript length and scene count verification"

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        cfg = _load_overrides(briefs)
        findings: list[Finding] = []

        total_words = sum(len(s.text.split()) for s in manuscript.scenes)
        scene_count = len(manuscript.scenes)

        print(f"    Words: {total_words:,}, scenes: {scene_count}", file=sys.stderr)

        # ─── Word count ───
        if total_words < cfg["word_count_floor"]:
            findings.append(_finding(
                severity="CLASS_A",
                description=(
                    f"Word count {total_words:,} below floor {cfg['word_count_floor']:,} "
                    f"(target {cfg['word_count_target']:,})"
                ),
                evidence=[f"Total words: {total_words:,}"],
            ))
        elif total_words > cfg["word_count_ceiling"]:
            findings.append(_finding(
                severity="CLASS_A",
                description=(
                    f"Word count {total_words:,} above ceiling {cfg['word_count_ceiling']:,} "
                    f"(target {cfg['word_count_target']:,})"
                ),
                evidence=[f"Total words: {total_words:,}"],
            ))
        else:
            deviation = abs(total_words - cfg["word_count_target"]) / cfg["word_count_target"]
            if deviation > MA009_WORD_SOFT_DEVIATION:
                findings.append(_finding(
                    severity="CLASS_B",
                    description=(
                        f"Word count {total_words:,} deviates {deviation * 100:.1f}% from "
                        f"target {cfg['word_count_target']:,} (within range, signal only)"
                    ),
                    evidence=[f"Total words: {total_words:,}"],
                ))

        # ─── Scene count ───
        if scene_count < cfg["scene_count_floor"]:
            findings.append(_finding(
                severity="CLASS_A",
                description=(
                    f"Scene count {scene_count} below floor {cfg['scene_count_floor']} "
                    f"(target {cfg['scene_count_target']})"
                ),
                evidence=[f"Total scenes: {scene_count}"],
            ))
        elif scene_count > cfg["scene_count_ceiling"]:
            findings.append(_finding(
                severity="CLASS_A",
                description=(
                    f"Scene count {scene_count} above ceiling {cfg['scene_count_ceiling']} "
                    f"(target {cfg['scene_count_target']})"
                ),
                evidence=[f"Total scenes: {scene_count}"],
            ))
        else:
            deviation = abs(scene_count - cfg["scene_count_target"]) / cfg["scene_count_target"]
            if deviation > MA009_SCENE_SOFT_DEVIATION:
                findings.append(_finding(
                    severity="CLASS_B",
                    description=(
                        f"Scene count {scene_count} deviates {deviation * 100:.1f}% from "
                        f"target {cfg['scene_count_target']} (within range, signal only)"
                    ),
                    evidence=[f"Total scenes: {scene_count}"],
                ))

        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        class_b = sum(1 for f in findings if f.severity == "CLASS_B")
        print(f"    -> {len(findings)} findings ({class_a} A, {class_b} B)", file=sys.stderr)

        return findings
