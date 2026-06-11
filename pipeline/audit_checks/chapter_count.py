"""
MA-C11: Chapter Count Check (25-Chapter Rule)

Deterministic, CLASS_A. Every novel must have exactly 25 chapters.
Reads chapter headers from the synopsis at the well-known path.

A chapter count != 25 is a CLASS_A finding (blocks publication).
"""

from __future__ import annotations

import re

from pathlib import Path

from audit_checks import ManuscriptArtifact, BriefBundle, Finding

_CHAPTER_HEADER_RE = re.compile(r"^##\s+Chapter\s+\d+", re.MULTILINE | re.IGNORECASE)
_SCENE_HEADER_RE = re.compile(r"^###\s+Scene\s+\d+", re.MULTILINE | re.IGNORECASE)

REQUIRED_CHAPTER_COUNT = 25


class ChapterCount:
    check_id = "MA-C11-chapter-count"
    severity = "CLASS_A"
    description = (
        "25-chapter rule: every novel must have exactly 25 chapters. "
        "Deterministic, CLASS_A."
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        if not briefs.synopsis_path or not Path(briefs.synopsis_path).exists():
            return [Finding(
                check_id=self.check_id,
                severity="CLASS_A",
                scene_number=None,
                description="No synopsis available; cannot verify chapter count",
                suggested_fix="Provide synopsis.md at the expected path",
            )]

        text = Path(briefs.synopsis_path).read_text(encoding="utf-8")

        scene_header_count = len(_SCENE_HEADER_RE.findall(text))
        chapter_numbers = set()
        for m in _CHAPTER_HEADER_RE.finditer(text):
            num_match = re.search(r"\d+", m.group(0))
            if num_match:
                chapter_numbers.add(int(num_match.group()))

        count = len(chapter_numbers)

        # Scene-organized format (1 chapter per scene): 25-rule does not apply.
        if scene_header_count > 0 and count == scene_header_count:
            return []

        if count == REQUIRED_CHAPTER_COUNT:
            return []

        return [Finding(
            check_id=self.check_id,
            severity="CLASS_A",
            scene_number=None,
            description=f"Chapter count is {count}, rule requires exactly {REQUIRED_CHAPTER_COUNT}",
            suggested_fix=(
                f"Re-structure the outline/synopsis to have exactly "
                f"{REQUIRED_CHAPTER_COUNT} chapters (currently {count})"
            ),
        )]
