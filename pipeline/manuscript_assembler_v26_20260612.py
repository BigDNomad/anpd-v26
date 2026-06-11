# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
manuscript_assembler.py — V26 Manuscript Assembler
ANPD V26 | Version: 20260612

Concatenates per-chapter files into a single manuscript.md.

V26 update: adds CLI main() for subprocess invocation by
master_controller / phase_handlers.  The library function
assemble_manuscript() is preserved for direct callers.

Phase order: scenes → chapters (scene_formatter) → ASSEMBLER → Gate 3.
"""

import argparse
import glob
import os
import sys


def assemble_manuscript(
    scene_results: dict,
    output_dir: str,
    synopsis,
    class_a_failures: int = 0,
) -> dict:
    """Assemble per-scene prose into chapter files and full manuscript.

    Args:
        scene_results: dict mapping (chapter_num, scene_num) -> prose string
        output_dir: directory to write output files
        synopsis: SynopsisStructure from synopsis_parser
        class_a_failures: number of Class A failures remaining. When > 0,
            the full manuscript is written to manuscript_BLOCKED.md instead
            of the canonical act1_full.md.

    Returns dict of paths: {"chapters": [...], "full": str, "scene_files": [...],
                            "blocked": bool}
    """
    os.makedirs(output_dir, exist_ok=True)
    scene_dir = os.path.join(output_dir, "scene_prose")
    os.makedirs(scene_dir, exist_ok=True)

    chapter_paths = []
    scene_paths = []
    full_text = ""

    for ch in synopsis.chapters:
        ch_num = ch.chapter_number
        if ch.title:
            ch_header = f"# Chapter {ch_num} — {ch.title}\n\n"
        else:
            ch_header = f"# Chapter {ch_num}\n\n"

        ch_text = ch_header
        scene_texts = []

        for sc in ch.scenes:
            key = (ch_num, sc.scene_number)
            prose = scene_results.get(key, "")

            # Save per-scene file
            sc_filename = f"sc_{sc.scene_number:03d}.md"
            sc_path = os.path.join(scene_dir, sc_filename)
            with open(sc_path, 'w', encoding='utf-8') as f:
                f.write(prose)
            scene_paths.append(sc_path)

            scene_texts.append(prose)

        # Join scenes with scene break markers
        ch_text += "\n\n***\n\n".join(scene_texts)
        ch_text += "\n"

        # Save per-chapter file
        ch_filename = f"ch{ch_num:02d}.md"
        ch_path = os.path.join(output_dir, ch_filename)
        with open(ch_path, 'w', encoding='utf-8') as f:
            f.write(ch_text)
        chapter_paths.append(ch_path)

        full_text += ch_text + "\n\n"

    # Save full manuscript — filename conditional on Class A failures
    full_filename = "manuscript_BLOCKED.md" if class_a_failures > 0 else "act1_full.md"
    full_path = os.path.join(output_dir, full_filename)
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(full_text)

    return {
        "chapters": chapter_paths,
        "full": full_path,
        "scene_files": scene_paths,
        "blocked": class_a_failures > 0,
    }


# ─── CLI interface (V26 addition) ──────────────────────────────────────────

def assemble_from_chapter_files(chapters_dir: str, output_dir: str) -> str:
    """Concatenate ch*.md files into manuscript.md.

    This is the V26 subprocess entry point.  scene_formatter has
    already produced per-chapter files; this function simply
    concatenates them in order into a single manuscript.md.

    Returns the path to manuscript.md.
    """
    pattern = os.path.join(chapters_dir, "ch[0-9][0-9]_*.md")
    chapter_files = sorted(glob.glob(pattern))
    if not chapter_files:
        # Try alternate pattern (ch01.md without slug)
        pattern = os.path.join(chapters_dir, "ch[0-9][0-9].md")
        chapter_files = sorted(glob.glob(pattern))

    if not chapter_files:
        print(f"ERROR: no chapter files found in {chapters_dir}", file=sys.stderr)
        sys.exit(1)

    parts = []
    for ch_path in chapter_files:
        with open(ch_path, "r", encoding="utf-8") as fh:
            parts.append(fh.read())

    manuscript_text = "\n\n".join(parts)

    os.makedirs(output_dir, exist_ok=True)
    manuscript_path = os.path.join(output_dir, "manuscript.md")
    with open(manuscript_path, "w", encoding="utf-8") as fh:
        fh.write(manuscript_text)

    word_count = len(manuscript_text.split())
    print(f"manuscript.md written: {manuscript_path} ({word_count} words, "
          f"{len(chapter_files)} chapters)")
    return manuscript_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manuscript_assembler",
        description="Concatenate chapter files into manuscript.md",
    )
    parser.add_argument("--chapters-dir", required=True,
                        help="Path to directory containing ch*.md files")
    parser.add_argument("--output-dir", required=True,
                        help="Path to output directory for manuscript.md")
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    assemble_from_chapter_files(args.chapters_dir, args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
