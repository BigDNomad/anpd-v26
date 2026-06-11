"""
synopsis_parser.py — V25 Synopsis Parser
ANPD V25 | Version: 20260511

Parses approved synopsis markdown into structured scene objects
for scene_writer consumption.
"""

import re
import os
from dataclasses import dataclass, field


@dataclass
class SceneEntry:
    chapter_number: int
    scene_number: int
    title: str
    scene_type: str  # ACTION, MIXED, NON-ACTION
    pov: str
    body: str
    position_in_chapter: int  # 1-indexed


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


# Scene header pattern: ### Scene N — Title [TYPE: X] [MODE: ...] [POV/FOCUS: Y]
# MODE is optional and skipped; POV/FOCUS is optional (captured as group 4).
SCENE_HEADER = re.compile(
    r'^###\s+Scene\s+(\d+)\s*[—\-]\s*(.+?)\s*'
    r'\[TYPE:\s*(ACTION|MIXED|NON[_-]ACTION|SUSPENSE)\]\s*'
    r'(?:\[MODE:\s*[^\]]+\]\s*)?'
    r'(?:\[(?:POV|FOCUS):\s*([^\]]+)\])?\s*$',
    re.IGNORECASE,
)

# Chapter header pattern: ## Chapter N or ## Chapter N — Title
CHAPTER_HEADER = re.compile(
    r'^##\s+Chapter\s+(\d+)(?:\s*[—\-]\s*(.+))?$',
    re.IGNORECASE,
)


def parse_synopsis(synopsis_path: str) -> SynopsisStructure:
    """Parse approved synopsis file into structured scene objects."""
    if not os.path.exists(synopsis_path):
        raise FileNotFoundError(f"Synopsis file not found: {synopsis_path}")

    with open(synopsis_path, 'r', encoding='utf-8') as f:
        text = f.read()

    lines = text.split('\n')
    chapters = []
    current_chapter = None
    current_scene = None
    current_body_lines = []
    scene_position = 0

    for line in lines:
        # Check for chapter header
        ch_match = CHAPTER_HEADER.match(line.strip())
        if ch_match:
            # Save any pending scene
            if current_scene is not None:
                current_scene.body = '\n'.join(current_body_lines).strip()
                if current_chapter:
                    current_chapter.scenes.append(current_scene)
                current_scene = None
                current_body_lines = []

            ch_num = int(ch_match.group(1))
            ch_title = ch_match.group(2).strip() if ch_match.group(2) else ""
            current_chapter = ChapterEntry(chapter_number=ch_num, title=ch_title)
            chapters.append(current_chapter)
            scene_position = 0
            continue

        # Check for scene header
        sc_match = SCENE_HEADER.match(line.strip())
        if sc_match:
            # Save any pending scene
            if current_scene is not None:
                current_scene.body = '\n'.join(current_body_lines).strip()
                if current_chapter:
                    current_chapter.scenes.append(current_scene)
                current_body_lines = []

            scene_position += 1
            sc_num = int(sc_match.group(1))
            sc_title = sc_match.group(2).strip()
            sc_type = sc_match.group(3).upper().replace('-', '_')
            sc_pov = (sc_match.group(4) or "").strip()

            current_scene = SceneEntry(
                chapter_number=current_chapter.chapter_number if current_chapter else 0,
                scene_number=sc_num,
                title=sc_title,
                scene_type=sc_type,
                pov=sc_pov,
                body="",
                position_in_chapter=scene_position,
            )
            continue

        # Accumulate body lines (skip separator lines)
        if current_scene is not None:
            if line.strip() == '---':
                continue
            current_body_lines.append(line)

    # Save last pending scene
    if current_scene is not None:
        current_scene.body = '\n'.join(current_body_lines).strip()
        if current_chapter:
            current_chapter.scenes.append(current_scene)

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
            print(f"    Scene {sc.scene_number} — {sc.title} [{sc.scene_type}] [POV: {sc.pov}] ({len(sc.body.split())} words)")
