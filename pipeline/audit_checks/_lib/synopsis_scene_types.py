"""Shared synopsis scene-TYPE loader for audit check modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SCENE_HEADER_RE = re.compile(
    r"###\s+Scene\s+\d+.*?\[TYPE:\s*([A-Z\-]+)\]", re.IGNORECASE
)

_SCENE_SPEC_RE = re.compile(
    r"###\s+Scene\s+\d+\s*—\s*(.+?)\s*\[TYPE:\s*([A-Z\-]+)\](?:\s*\[FOCUS:\s*([^\]]*)\])?",
    re.IGNORECASE,
)


@dataclass
class SceneSpec:
    """A single scene's synopsis specification: title, type, focus, and beats."""
    number: int
    title: str
    type: str
    focus: str
    beats: list[str] = field(default_factory=list)

def load_scene_type_map(synopsis_path: str | Path | None) -> dict[int, str]:
    """Read synopsis.md and return {flat_sequential_scene_number: TYPE}.

    The synopsis uses per-chapter scene numbering (resets each chapter).
    We extract TYPE tags in document order and assign flat 1-based indices
    to match the calibration baseline's sc_001, sc_002, ... numbering.
    """
    if not synopsis_path:
        return {}
    path = Path(synopsis_path)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    result: dict[int, str] = {}
    flat_index = 0
    for m in _SCENE_HEADER_RE.finditer(text):
        flat_index += 1
        result[flat_index] = m.group(1).upper()
    return result


def load_scene_specs(synopsis_path: str | Path | None) -> dict[int, SceneSpec]:
    """Read synopsis.md and return {flat_sequential_scene_number: SceneSpec}.

    Parses scene headers for title, TYPE, FOCUS, and collects bullet-point
    beats between consecutive scene headers.  Flat numbering matches
    load_scene_type_map (document order, 1-based).
    """
    if not synopsis_path:
        return {}
    path = Path(synopsis_path)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # First pass: locate every scene header line and extract metadata
    headers: list[tuple[int, str, str, str]] = []  # (line_idx, title, type, focus)
    for idx, line in enumerate(lines):
        m = _SCENE_SPEC_RE.search(line)
        if m:
            headers.append((idx, m.group(1).strip(), m.group(2).upper(), (m.group(3) or "").strip()))

    result: dict[int, SceneSpec] = {}
    for i, (line_idx, title, scene_type, focus) in enumerate(headers):
        # Beats are bullet lines between this header and the next header (or EOF)
        end_idx = headers[i + 1][0] if i + 1 < len(headers) else len(lines)
        beats: list[str] = []
        for j in range(line_idx + 1, end_idx):
            stripped = lines[j].strip()
            if stripped.startswith("- "):
                beats.append(stripped[2:].strip())
        flat_number = i + 1
        result[flat_number] = SceneSpec(
            number=flat_number,
            title=title,
            type=scene_type,
            focus=focus,
            beats=beats,
        )
    return result
