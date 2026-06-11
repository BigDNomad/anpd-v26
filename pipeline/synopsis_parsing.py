# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
synopsis_parsing.py — SHARED canonical scene-header parser

ANPD V26 | Created: 20260612

THE authoritative scene-header regex and header-extraction logic for
the ANPD pipeline.  Both synopsis_parser (scene loop) and
synopsis_auditor MUST import from this module.  Two parsers for one
format caused the pillar-scene dropout (2026-06-12); there will
never be two again.

Supports:
  V25:  ### Scene N — Title [TYPE: X] [POV: Y]
  V26:  ### Scene N — Title [TYPE: X] [PILLAR: Y] [POV: Z]
  V24 fallback: ## SCENE N: Title
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ─── Dataclass ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ParsedScene:
    """Minimal scene record produced by the shared parser."""
    number: int
    title: str
    scene_type: str = "UNKNOWN"   # ACTION / MIXED / NON_ACTION / SUSPENSE / UNKNOWN
    pillar: str = ""              # TWIST1 / TWIST2 / TWIST3 / LOWEST_POINT / FINAL_BATTLE / ""
    pov: str = ""                 # POV character name or ""
    body: str = ""                # Body text (everything between this header and the next)


# ─── Canonical regexes ──────────────────────────────────────────────────────────

# V25/V26 scene header:
#   ### Scene N — Title [TYPE: X] [PILLAR: Y] [POV: Z]
# All bracket tags are optional; order is TYPE → PILLAR → POV.
SCENE_HEADER_RE = re.compile(
    r"^###\s+Scene\s+(\d+)\s*[—–\-]\s*"           # ### Scene N —
    r"(.+?)"                                        # title (non-greedy)
    r"(?:\s+\[TYPE:[^\]]*\])?"                      # optional [TYPE: ...]
    r"(?:\s+\[PILLAR:[^\]]*\])?"                    # optional [PILLAR: ...]
    r"(?:\s+\[POV:[^\]]*\])?"                       # optional [POV: ...]
    r"(?:\s+\[FOCUS:[^\]]*\])?"                     # optional [FOCUS: ...] (alias)
    r"(?:\s+\[MODE:[^\]]*\])?"                      # optional [MODE: ...] (legacy)
    r"\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# V24 legacy fallback: ## SCENE N: Title
SCENE_HEADER_V24_RE = re.compile(
    r"^##\s+SCENE\s+(\d+):\s*(.*?)$",
    re.MULTILINE,
)

# Chapter header: ## Chapter N — Title
CHAPTER_HEADER_RE = re.compile(
    r"^##\s+Chapter\s+(\d+)(?:\s*[—–\-]\s*(.+))?\s*$",
    re.IGNORECASE,
)

# Metadata extractors (applied to the full header line)
TYPE_RE = re.compile(r"\[TYPE:\s*([A-Z_\-]+)\s*\]", re.IGNORECASE)
PILLAR_RE = re.compile(r"\[PILLAR:\s*([A-Z_0-9]+)\s*\]", re.IGNORECASE)
POV_RE = re.compile(r"\[(?:POV|FOCUS):\s*([^\]]+)\s*\]", re.IGNORECASE)


# ─── Parse function ────────────────────────────────────────────────────────────

def parse_scene_headers(text: str) -> list[ParsedScene]:
    """Parse synopsis text into a flat list of ParsedScene records.

    Tries V25/V26 format first; falls back to V24 if zero V25 matches.
    V24 fallback exists for legacy synopses and will be removed when V24
    retires.

    Returns [] on empty input or no matching headers.
    """
    if not text or not text.strip():
        return []

    headers = list(SCENE_HEADER_RE.finditer(text))
    is_v25 = bool(headers)

    if not headers:
        headers = list(SCENE_HEADER_V24_RE.finditer(text))

    if not headers:
        return []

    scenes: list[ParsedScene] = []
    for i, match in enumerate(headers):
        number = int(match.group(1))
        raw_title = match.group(2).strip()

        # Clean title of any bracket metadata that leaked into group 2
        title = re.sub(r"\s*\[TYPE:[^\]]*\]", "", raw_title)
        title = re.sub(r"\s*\[PILLAR:[^\]]*\]", "", title)
        title = re.sub(r"\s*\[POV:[^\]]*\]", "", title)
        title = re.sub(r"\s*\[FOCUS:[^\]]*\]", "", title)
        title = re.sub(r"\s*\[MODE:[^\]]*\]", "", title)
        title = title.strip()

        # Body: text between this header and the next
        body_start = match.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[body_start:body_end].strip()

        # Extract metadata from the full matched line
        full_line = match.group(0)
        scene_type = "UNKNOWN"
        pillar = ""
        pov = ""

        if is_v25:
            type_m = TYPE_RE.search(full_line)
            if type_m:
                scene_type = type_m.group(1).upper().replace("-", "_")
            pillar_m = PILLAR_RE.search(full_line)
            if pillar_m:
                pillar = pillar_m.group(1).upper()
            pov_m = POV_RE.search(full_line)
            if pov_m:
                pov = pov_m.group(1).strip()

        scenes.append(ParsedScene(
            number=number,
            title=title,
            scene_type=scene_type,
            pillar=pillar,
            pov=pov,
            body=body,
        ))

    return scenes
