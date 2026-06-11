# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V24 Scene Formatter — assemble chapter files from per-scene files

Phase 4 component, invoked by master_controller's chapter_assembly handler
(phase_handlers.handle_chapter_assembly). Takes:

- Per-scene markdown files from {book_dir}/out/scenes/ (sc{NN}_{slug}.md)
- scene_map.md from {book_dir} (canonical or latest timestamped variant)
- target_chapter_count (default 25 per Data Standards §4.3)

Produces:

- ch{NN}_{slug}.md files in {book_dir}/out/chapters/

Two scene-to-chapter assignment modes:

1. Explicit assignment via scene_map "Chapter N:" headings.
   The scene_map carries `## Chapter K: Title` headings interspersed with
   `## Scene N — Title` entries. Each scene rolls up to the most recent
   preceding chapter heading.

2. Computed assignment via D009-validated scenes-per-chapter ratio.
   When scene_map carries no chapter headings, scenes are divided evenly
   into target_chapter_count chapters. Per preflight D009 the ratio is
   constrained to 3, 4, or 5.

Mode 1 takes precedence when chapter headings are present in the scene_map.

CHAPTER FILE NAMING CONVENTION

Chapter filenames standardize as `ch{NN}_{slug}.md`, mirroring the scene
§2.5 pattern. The slug derives from the chapter heading (Mode 1) or from
the first scene's slug (Mode 2). Examples: ch01_opening.md, ch02_arrival.md.

Data Standards §1.2 references the `chapters/` directory but does not
currently specify the chapter file naming convention. Data Standards
revision queued to document this; scene_formatter implements the
convention here to unblock Phase 6 in the pipeline.

CHAPTER BODY ASSEMBLY

Each chapter file consists of:
    # Chapter N — Title
    <blank>
    <scene 1 file contents>
    <blank>
    <scene 2 file contents>
    ...

Scene files are concatenated verbatim, in scene-number order. Any
internal headings (## Scene N — Title) the scene_writer wrote are
preserved. The component does not add per-scene headings.

Silent-failure-prohibited per White Paper §2.1: any missing scene file
referenced by the assignment is a Class A finding with STOP_REPORT.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone


# ─── Constants ────────────────────────────────────────────────────────────────

DEFAULT_TARGET_CHAPTER_COUNT = 25
SCENE_FILENAME_PATTERN = re.compile(r"^sc(\d{2,3})_(.+)\.md$")

SCENE_HEADING_RE = re.compile(
    r"^#{2,4}\s+Scene\s+(\d+)\s*[—:\-]\s*(.+?)$",
    re.MULTILINE | re.IGNORECASE,
)
CHAPTER_HEADING_RE = re.compile(
    r"^#{1,3}\s+Chapter\s+(\d+)\s*[—:\-]\s*(.+?)$",
    re.MULTILINE | re.IGNORECASE,
)


# ─── STOP_REPORT helper ───────────────────────────────────────────────────────

def write_stop_report(
    book_dir: str,
    error_message: str,
    suggested_fix: str,
    file_path: str | None = None,
) -> str:
    """Write Class A STOP_REPORT.json per Data Standards §4.6."""
    reports_dir = os.path.join(book_dir, "out", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, "STOP_REPORT.json")
    payload = {
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "component":     "scene_formatter",
        "phase":         6,
        "scene_number":  None,
        "error_type":    "Class A",
        "error_message": error_message,
        "file_path":     file_path,
        "suggested_fix": suggested_fix,
        "pipeline_state": "halted at chapter assembly",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


# ─── Scene file inventory ─────────────────────────────────────────────────────

def list_scene_files(scenes_dir: str) -> list[tuple[int, str, str]]:
    """Inventory scene files in canonical scenes_dir.

    Returns list of (scene_number, slug, full_path) sorted by scene_number.
    Skips _original backup files per Data Standards §2.5.
    """
    if not os.path.isdir(scenes_dir):
        return []

    found: list[tuple[int, str, str]] = []
    for path in glob.glob(os.path.join(scenes_dir, "sc*_*.md")):
        basename = os.path.basename(path)
        if basename.endswith("_original.md"):
            continue
        m = SCENE_FILENAME_PATTERN.match(basename)
        if not m:
            continue
        found.append((int(m.group(1)), m.group(2), path))

    found.sort(key=lambda t: t[0])
    return found


# ─── Scene-map parsing ────────────────────────────────────────────────────────

def parse_scene_map(scene_map_path: str) -> tuple[list[dict], list[dict]]:
    """Parse scene_map.md into (scenes, chapters).

    Returns:
        scenes:   list of {scene_number, title, position}
        chapters: list of {chapter_number, title, position}

    `position` is the character offset in the source text. Used by
    assign_scenes_to_chapters_explicit() to map scenes to most-recent-
    preceding chapter.
    """
    if not os.path.isfile(scene_map_path):
        raise FileNotFoundError(f"scene_map not found at {scene_map_path}")

    with open(scene_map_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    scenes: list[dict] = [
        {
            "scene_number": int(m.group(1)),
            "title":        m.group(2).strip(),
            "position":     m.start(),
        }
        for m in SCENE_HEADING_RE.finditer(text)
    ]
    chapters: list[dict] = [
        {
            "chapter_number": int(m.group(1)),
            "title":          m.group(2).strip(),
            "position":       m.start(),
        }
        for m in CHAPTER_HEADING_RE.finditer(text)
    ]

    scenes.sort(key=lambda s: s["scene_number"])
    chapters.sort(key=lambda c: c["chapter_number"])
    return scenes, chapters


# ─── Chapter assignment ───────────────────────────────────────────────────────

def assign_scenes_to_chapters_explicit(
    scenes: list[dict],
    chapters: list[dict],
) -> dict[int, list[int]]:
    """Mode 1: each scene rolls up to most-recent-preceding chapter heading.

    Returns {chapter_number: [scene_number, ...]}. Scenes that precede the
    first chapter heading are not assigned (caller decides whether that's
    a Class A failure).
    """
    assignment: dict[int, list[int]] = {c["chapter_number"]: [] for c in chapters}

    for scene in scenes:
        active_chapter = None
        for ch in chapters:
            if ch["position"] < scene["position"]:
                active_chapter = ch["chapter_number"]
            else:
                break
        if active_chapter is not None:
            assignment[active_chapter].append(scene["scene_number"])

    # Sort scene lists so chapters render in order.
    for ch_num in assignment:
        assignment[ch_num].sort()

    return assignment


def assign_scenes_to_chapters_computed(
    scene_count: int,
    target_chapter_count: int,
) -> dict[int, list[int]]:
    """Mode 2: split scenes evenly into target_chapter_count chapters.

    Per preflight D009 the scenes-per-chapter ratio is 3, 4, or 5.
    If the division isn't even, the remainder distributes one extra to
    the early chapters until exhausted.
    """
    if target_chapter_count <= 0:
        raise ValueError(
            f"target_chapter_count must be positive, got {target_chapter_count}"
        )
    if scene_count <= 0:
        return {}

    base, remainder = divmod(scene_count, target_chapter_count)
    assignment: dict[int, list[int]] = {}
    next_scene = 1
    for ch_num in range(1, target_chapter_count + 1):
        size = base + (1 if ch_num <= remainder else 0)
        if size <= 0:
            assignment[ch_num] = []
            continue
        assignment[ch_num] = list(range(next_scene, next_scene + size))
        next_scene += size

    return assignment


# ─── Chapter title resolution ─────────────────────────────────────────────────

def resolve_chapter_titles(
    assignment: dict[int, list[int]],
    chapters_from_map: list[dict],
    scenes_from_map: list[dict],
) -> dict[int, str]:
    """Determine each chapter's title.

    - If chapters_from_map has an entry for chapter N, use its title.
    - Else use the title of the first scene assigned to chapter N.
    - Else fall back to f"Chapter N".
    """
    titles_from_map = {c["chapter_number"]: c["title"] for c in chapters_from_map}
    scene_title_by_number = {
        s["scene_number"]: s["title"] for s in scenes_from_map
    }

    out: dict[int, str] = {}
    for ch_num, scene_numbers in assignment.items():
        if ch_num in titles_from_map:
            out[ch_num] = titles_from_map[ch_num]
        elif scene_numbers and scene_title_by_number.get(scene_numbers[0]):
            out[ch_num] = scene_title_by_number[scene_numbers[0]]
        else:
            out[ch_num] = f"Chapter {ch_num}"
    return out


# ─── Slug computation ─────────────────────────────────────────────────────────

def slug_from_title(title: str) -> str:
    """File-safe slug from chapter title — first-meaningful-word lowercased.

    Mirrors phase_handlers._slug_from_title to keep convention uniform.
    """
    if not title:
        return "untitled"
    head = title.split(":")[0].strip()
    words = re.findall(r"[A-Za-z0-9]+", head.lower())
    return words[0] if words else "untitled"


# ─── Chapter file writing ────────────────────────────────────────────────────

def assemble_chapter_text(
    chapter_number: int,
    chapter_title: str,
    scene_numbers: list[int],
    scenes_dir: str,
) -> tuple[str, list[str]]:
    """Build the chapter file body by concatenating scene files in order.

    Returns (chapter_text, missing_scene_files). Caller treats missing
    scene files as Class A.
    """
    lines: list[str] = [f"# Chapter {chapter_number} — {chapter_title}", ""]
    missing: list[str] = []

    for sn in scene_numbers:
        scene_pattern = os.path.join(scenes_dir, f"sc{sn:02d}_*.md")
        matches = [
            p for p in glob.glob(scene_pattern)
            if not p.endswith("_original.md")
        ]
        if not matches:
            missing.append(f"sc{sn:02d}_*.md")
            continue
        scene_path = max(matches, key=os.path.getmtime)
        try:
            with open(scene_path, "r", encoding="utf-8") as fh:
                scene_text = fh.read().rstrip()
        except OSError as exc:
            missing.append(f"{os.path.basename(scene_path)} (read error: {exc})")
            continue

        lines.append(scene_text)
        lines.append("")

    chapter_text = "\n".join(lines).rstrip() + "\n"
    return chapter_text, missing


def write_chapter_file(
    chapters_dir: str,
    chapter_number: int,
    chapter_title: str,
    chapter_text: str,
) -> str:
    """Write chapter file and return its path."""
    os.makedirs(chapters_dir, exist_ok=True)
    slug = slug_from_title(chapter_title)
    filename = f"ch{chapter_number:02d}_{slug}.md"
    path = os.path.join(chapters_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(chapter_text)
    return path


# ─── Orchestration ────────────────────────────────────────────────────────────

def run_scene_formatter(
    book_dir: str,
    scenes_dir: str,
    chapters_dir: str,
    scene_map_path: str,
    target_chapter_count: int,
) -> tuple[int, dict]:
    """Main orchestration. Returns (exit_code, summary_dict).

    exit_code is 0 on full success, 1 on any Class A finding.
    summary_dict carries chapters_written, missing_scenes, and mode.
    """
    print(f"=== scene_formatter ===")
    print(f"  scenes_dir:           {scenes_dir}")
    print(f"  chapters_dir:         {chapters_dir}")
    print(f"  scene_map:            {scene_map_path}")
    print(f"  target_chapter_count: {target_chapter_count}")

    # Inventory scenes on disk
    scene_inventory = list_scene_files(scenes_dir)
    if not scene_inventory:
        msg = f"no sc{{NN}}_*.md files in {scenes_dir}"
        write_stop_report(
            book_dir,
            error_message=msg,
            suggested_fix="run scene generation (Phase 5) before chapter assembly",
        )
        print(f"  FAIL: {msg}")
        return (1, {"chapters_written": 0, "missing_scenes": [], "mode": "n/a"})

    print(f"  scenes on disk: {len(scene_inventory)}")

    # Parse scene_map
    try:
        map_scenes, map_chapters = parse_scene_map(scene_map_path)
    except (OSError, FileNotFoundError) as exc:
        msg = f"scene_map parse failed: {exc}"
        write_stop_report(
            book_dir,
            error_message=msg,
            suggested_fix="ensure scene_map.md exists and is readable",
            file_path=scene_map_path,
        )
        print(f"  FAIL: {msg}")
        return (1, {"chapters_written": 0, "missing_scenes": [], "mode": "n/a"})

    # Pick mode based on whether chapter headings are present
    if map_chapters:
        mode = "explicit"
        assignment = assign_scenes_to_chapters_explicit(map_scenes, map_chapters)
        # Verify every scene-on-disk is assigned to some chapter; otherwise Class A
        assigned_scene_nums = {sn for sns in assignment.values() for sn in sns}
        on_disk_nums = {sn for sn, _, _ in scene_inventory}
        unassigned = on_disk_nums - assigned_scene_nums
        if unassigned:
            msg = (
                f"explicit mode: {len(unassigned)} scenes on disk are not assigned "
                f"to any chapter heading in scene_map (unassigned: "
                f"{sorted(unassigned)[:10]}{'...' if len(unassigned) > 10 else ''})"
            )
            write_stop_report(
                book_dir,
                error_message=msg,
                suggested_fix=(
                    "either add Chapter N: headings to scene_map covering all scenes, "
                    "or remove the headings to use computed assignment mode"
                ),
                file_path=scene_map_path,
            )
            print(f"  FAIL: {msg}")
            return (1, {"chapters_written": 0, "missing_scenes": [], "mode": mode})
    else:
        mode = "computed"
        scene_count = len(scene_inventory)
        try:
            assignment = assign_scenes_to_chapters_computed(
                scene_count, target_chapter_count,
            )
        except ValueError as exc:
            msg = f"computed mode: {exc}"
            write_stop_report(
                book_dir,
                error_message=msg,
                suggested_fix="set target_chapter_count to a positive integer",
            )
            print(f"  FAIL: {msg}")
            return (1, {"chapters_written": 0, "missing_scenes": [], "mode": mode})

    print(f"  assignment mode: {mode}")
    print(f"  chapters: {len(assignment)}")

    # Resolve titles
    titles = resolve_chapter_titles(assignment, map_chapters, map_scenes)

    # Write chapters
    chapters_written: list[str] = []
    all_missing: list[tuple[int, list[str]]] = []

    for ch_num in sorted(assignment.keys()):
        scene_numbers = assignment[ch_num]
        if not scene_numbers:
            print(f"  chapter {ch_num:02d}: SKIPPED (no scenes assigned)")
            continue
        chapter_text, missing = assemble_chapter_text(
            ch_num, titles[ch_num], scene_numbers, scenes_dir,
        )
        if missing:
            all_missing.append((ch_num, missing))
            continue  # don't write incomplete chapters
        path = write_chapter_file(chapters_dir, ch_num, titles[ch_num], chapter_text)
        chapters_written.append(path)
        print(f"  chapter {ch_num:02d}: wrote {os.path.basename(path)} ({len(scene_numbers)} scenes)")

    # If anything missing, halt as Class A
    if all_missing:
        details = ", ".join(
            f"ch{n:02d}: {len(m)} missing"
            for n, m in all_missing
        )
        msg = f"missing scene files: {details}"
        suggested_fix = (
            "ensure all referenced scene files exist in out/scenes/ "
            "before invoking chapter assembly; use master_controller's "
            "--from-phase scenes to regenerate missing scenes"
        )
        write_stop_report(
            book_dir,
            error_message=msg,
            suggested_fix=suggested_fix,
        )
        print(f"  FAIL: {msg}")
        return (1, {
            "chapters_written": len(chapters_written),
            "missing_scenes": all_missing,
            "mode": mode,
        })

    print(f"  SUCCESS: {len(chapters_written)} chapter files written")
    return (0, {
        "chapters_written": len(chapters_written),
        "missing_scenes": [],
        "mode": mode,
    })


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scene_formatter.py",
        description="ANPD V24 scene_formatter — split scenes into chapters",
    )
    parser.add_argument("--scenes-dir", required=True,
                        help="Path to {book_dir}/out/scenes/")
    parser.add_argument("--chapters-dir", required=True,
                        help="Path to {book_dir}/out/chapters/")
    parser.add_argument("--scene-map", required=True,
                        help="Path to scene_map.md")
    parser.add_argument("--target-chapter-count",
                        type=int, default=DEFAULT_TARGET_CHAPTER_COUNT,
                        help="Target chapter count (default: 25)")
    parser.add_argument("--book-dir",
                        help="Path to book directory (for STOP_REPORT location); "
                             "if absent, derived from --scenes-dir parent's parent")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Derive book_dir if not provided: scenes_dir is {book_dir}/out/scenes/
    if args.book_dir is None:
        args.book_dir = os.path.dirname(os.path.dirname(args.scenes_dir.rstrip("/")))
    exit_code, _ = run_scene_formatter(
        book_dir=args.book_dir,
        scenes_dir=args.scenes_dir,
        chapters_dir=args.chapters_dir,
        scene_map_path=args.scene_map,
        target_chapter_count=args.target_chapter_count,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
