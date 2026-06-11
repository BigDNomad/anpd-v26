"""
manuscript_assembler.py — V25 Manuscript Assembler
ANPD V25 | Version: 20260511

Concatenates per-scene prose into per-chapter files and full manuscript.
"""

import os


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
