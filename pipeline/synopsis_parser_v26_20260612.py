# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
synopsis_parser.py — V26 Synopsis Parser
ANPD V26 | Version: 20260612

Parses approved synopsis markdown into structured scene objects
for scene_writer consumption.

Imports the canonical scene-header regex from synopsis_parsing.py
(shared with synopsis_auditor).  There is ONE parser for the
scene-header format.
"""

import re
import os
from dataclasses import dataclass, field

try:
    from pipeline.synopsis_parsing import (
        parse_scene_headers,
        CHAPTER_HEADER_RE,
        ParsedScene,
    )
except ImportError:
    from synopsis_parsing import (
        parse_scene_headers,
        CHAPTER_HEADER_RE,
        ParsedScene,
    )


@dataclass
class SceneEntry:
    chapter_number: int
    scene_number: int
    title: str
    scene_type: str  # ACTION, MIXED, NON_ACTION, SUSPENSE
    pov: str
    body: str
    position_in_chapter: int  # 1-indexed
    pillar: str = ""


@dataclass
class ChapterEntry:
    chapter_number: int
    title: str = ""
    scenes: list = field(default_factory=list)


@dataclass
class SynopsisStructure:
    chapters: list = field(default_factory=list)

    @property
    def all_scenes(self):
        """Flat list of all scenes across all chapters, in order."""
        scenes = []
        for ch in self.chapters:
            scenes.extend(ch.scenes)
        return scenes

    @property
    def scene_count(self):
        return sum(len(ch.scenes) for ch in self.chapters)


def parse_synopsis(synopsis_path: str) -> SynopsisStructure:
    """Parse approved synopsis file into structured scene objects.

    Uses the shared canonical parser from synopsis_parsing.py for
    header matching, then organises scenes into chapter groups via
    the chapter-header regex.
    """
    if not os.path.exists(synopsis_path):
        raise FileNotFoundError(f"Synopsis file not found: {synopsis_path}")

    with open(synopsis_path, 'r', encoding='utf-8') as f:
        text = f.read()

    # Step 1: Parse all scene headers via the shared parser
    parsed_scenes = parse_scene_headers(text)

    # Step 2: Build chapter structure by scanning chapter headers
    lines = text.split('\n')

    # First pass: find chapter boundaries (line index → ChapterEntry)
    chapter_starts: list[tuple[int, ChapterEntry]] = []
    for line_idx, line in enumerate(lines):
        ch_match = CHAPTER_HEADER_RE.match(line.strip())
        if ch_match:
            ch_num = int(ch_match.group(1))
            ch_title = ch_match.group(2).strip() if ch_match.group(2) else ""
            chapter_starts.append((line_idx, ChapterEntry(
                chapter_number=ch_num, title=ch_title,
            )))

    if not chapter_starts:
        # No chapter headers — put all scenes in chapter 0
        ch = ChapterEntry(chapter_number=0, title="")
        for pos, ps in enumerate(parsed_scenes, 1):
            ch.scenes.append(SceneEntry(
                chapter_number=0,
                scene_number=ps.number,
                title=ps.title,
                scene_type=ps.scene_type,
                pov=ps.pov,
                body=ps.body,
                position_in_chapter=pos,
                pillar=ps.pillar,
            ))
        return SynopsisStructure(chapters=[ch] if ch.scenes else [])

    # Step 3: Assign each parsed scene to its chapter.
    # Determine which chapter a scene belongs to by finding which
    # chapter header appears before the scene's header in the text.
    # We find the character offset of each scene header in the text.
    scene_offsets: dict[int, int] = {}
    for ps in parsed_scenes:
        # Find the line of this scene header
        pattern = re.compile(
            rf"^###\s+Scene\s+{ps.number}\s*[—–\-]",
            re.MULTILINE | re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            scene_offsets[ps.number] = m.start()

    # Map chapters to their character offsets
    chapter_char_offsets: list[tuple[int, ChapterEntry]] = []
    char_offset = 0
    for line_idx, ch_entry in chapter_starts:
        # Calculate character offset from line index
        offset = sum(len(lines[i]) + 1 for i in range(line_idx))
        chapter_char_offsets.append((offset, ch_entry))

    # Assign scenes to chapters
    chapters = [ch for _, ch in chapter_char_offsets]
    for ps in parsed_scenes:
        scene_char_offset = scene_offsets.get(ps.number, 0)
        # Find the last chapter that starts before this scene
        assigned_chapter = chapters[0]
        for ch_offset, ch_entry in chapter_char_offsets:
            if ch_offset <= scene_char_offset:
                assigned_chapter = ch_entry
            else:
                break

        pos = len(assigned_chapter.scenes) + 1
        assigned_chapter.scenes.append(SceneEntry(
            chapter_number=assigned_chapter.chapter_number,
            scene_number=ps.number,
            title=ps.title,
            scene_type=ps.scene_type,
            pov=ps.pov,
            body=ps.body,
            position_in_chapter=pos,
            pillar=ps.pillar,
        ))

    return SynopsisStructure(chapters=chapters)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 synopsis_parser.py <synopsis.md>")
        sys.exit(1)
    result = parse_synopsis(sys.argv[1])
    print(f"Chapters: {len(result.chapters)}")
    for ch in result.chapters:
        print(f"  Chapter {ch.chapter_number} — {ch.title or '(no title)'}: {len(ch.scenes)} scenes")
        for sc in ch.scenes:
            print(f"    Scene {sc.scene_number} — {sc.title} [{sc.scene_type}]"
                  f"{f' [PILLAR: {sc.pillar}]' if sc.pillar else ''}"
                  f" [POV: {sc.pov}] ({len(sc.body.split())} words)")
