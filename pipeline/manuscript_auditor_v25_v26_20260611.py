# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify other pipeline components to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V25 Manuscript Auditor — Check-Module Architecture

Extends V24's 4-pass auditor with a modular check system. Check modules
live in /anpd/v25/pipeline/audit_checks/ and are auto-discovered at runtime.

INPUT FORMATS:
  - Scene-per-file:  /path/to/manuscript/sc_NNN.md  (V25 Mandate format)
  - Assembled chapters: /path/to/chapters/ch{NN}_{slug}.md  (V24 format)

CLI:
    python3 manuscript_auditor_v25.py \\
      --manuscript-dir /path/to/manuscript/ \\
      --series-bible /path/to/series_bible.json \\
      --character-profiles /path/to/character_profiles.json \\
      --output-dir /path/to/audit/

OUTPUT:
    manuscript_audit_REPORT.json   — programmatic consumption
    manuscript_audit_REPORT.md     — operator review

EXIT CODE:
    0 = no CLASS_A findings
    1 = CLASS_A findings present (blocks publication)
    2 = orchestrator-level failure
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    Finding,
    REGISTRY,
    discover_and_register,
)


# ── Scene-per-file loader ──────────────────────────────────────────────────

SCENE_FILE_RE = re.compile(r"^sc_?(\d{2,3})(?:_.+)?\.md$")


def load_manuscript_scenes(manuscript_dir: str) -> ManuscriptArtifact:
    """Load scene-per-file manuscript (sc_NNN.md)."""
    scenes: list[SceneText] = []
    for path in sorted(glob.glob(os.path.join(manuscript_dir, "sc*.md")), key=lambda p: int(SCENE_FILE_RE.match(os.path.basename(p)).group(1)) if SCENE_FILE_RE.match(os.path.basename(p)) else 0):
        basename = os.path.basename(path)
        m = SCENE_FILE_RE.match(basename)
        if not m:
            continue
        scene_number = int(m.group(1))
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        scenes.append(SceneText(
            scene_number=scene_number,
            text=text,
            file_path=path,
        ))
    return ManuscriptArtifact(scenes=scenes, manuscript_dir=manuscript_dir)


# ── Assembled-chapter loader (V24 compat) ─────────────────────────────────

CHAPTER_FILE_RE = re.compile(r"^ch(\d{2,3})_(.+)\.md$")
SCENE_HEADING_RE = re.compile(r"^#{2,4}\s+Scene\s+(\d+)", re.MULTILINE | re.IGNORECASE)


def load_manuscript_chapters(chapters_dir: str) -> ManuscriptArtifact:
    """Load assembled-chapter manuscript (ch{NN}_{slug}.md).

    Splits each chapter into scenes based on ## Scene N headings.
    Falls back to treating the whole chapter as a single scene.
    """
    scenes: list[SceneText] = []
    for path in sorted(glob.glob(os.path.join(chapters_dir, "ch*_*.md"))):
        basename = os.path.basename(path)
        m = CHAPTER_FILE_RE.match(basename)
        if not m:
            continue
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()

        headings = list(SCENE_HEADING_RE.finditer(text))
        if headings:
            for i, heading in enumerate(headings):
                sn = int(heading.group(1))
                start = heading.start()
                end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
                scene_text = text[start:end].strip()
                scenes.append(SceneText(
                    scene_number=sn,
                    text=scene_text,
                    file_path=path,
                ))
        else:
            ch_num = int(m.group(1))
            scenes.append(SceneText(
                scene_number=ch_num,
                text=text,
                file_path=path,
            ))
    return ManuscriptArtifact(scenes=scenes, manuscript_dir=chapters_dir)


ASSEMBLED_BREAK_RE = re.compile(
    r"^(?:\*{3}|#\s+(?:Scene|Chapter)\s+\d+)",
    re.MULTILINE | re.IGNORECASE,
)


def load_manuscript_assembled(file_path: str) -> ManuscriptArtifact:
    """Load a single assembled manuscript file, splitting on scene-break markers.

    Scene-break markers: '***' on its own line, '# Scene N', '# Chapter N'.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    lines = text.split("\n")
    scenes: list[SceneText] = []
    current_start = 0
    scene_num = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "***" or re.match(r"^#\s+(?:Scene|Chapter)\s+\d+", stripped, re.IGNORECASE):
            if i > current_start:
                scene_text = "\n".join(lines[current_start:i])
                if scene_text.strip():
                    scene_num += 1
                    scenes.append(SceneText(
                        scene_number=scene_num,
                        text=scene_text,
                        file_path=file_path,
                    ))
            current_start = i + 1

    # Last segment
    if current_start < len(lines):
        scene_text = "\n".join(lines[current_start:])
        if scene_text.strip():
            scene_num += 1
            scenes.append(SceneText(
                scene_number=scene_num,
                text=scene_text,
                file_path=file_path,
            ))

    manuscript_dir = os.path.dirname(file_path)
    return ManuscriptArtifact(scenes=scenes, manuscript_dir=manuscript_dir)


def load_manuscript(path: str) -> ManuscriptArtifact:
    """Auto-detect format and load manuscript.

    Accepts a directory (scene-per-file or chapter-per-file) or a single
    assembled .md file.
    """
    # Single file path
    if os.path.isfile(path):
        return load_manuscript_assembled(path)

    scene_files = glob.glob(os.path.join(path, "sc*.md"))
    chapter_files = glob.glob(os.path.join(path, "ch*_*.md"))

    if scene_files:
        return load_manuscript_scenes(path)
    elif chapter_files:
        return load_manuscript_chapters(path)
    else:
        # Fallback: look for .md files and try assembled loading
        md_files = sorted(glob.glob(os.path.join(path, "*.md")))
        if md_files:
            return load_manuscript_assembled(md_files[0])
        return ManuscriptArtifact(scenes=[], manuscript_dir=path)


# ── Brief loader ───────────────────────────────────────────────────────────

def load_briefs(
    series_bible_path: str | None = None,
    character_profiles_path: str | None = None,
    book_config_path: str | None = None,
    entity_ledger_path: str | None = None,
    synopsis_path: str | None = None,
) -> BriefBundle:
    """Load all reference material into a BriefBundle."""
    bundle = BriefBundle()

    for attr, path in [
        ("series_bible", series_bible_path),
        ("character_profiles", character_profiles_path),
        ("book_config", book_config_path),
        ("entity_ledger", entity_ledger_path),
    ]:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                setattr(bundle, attr, json.load(f))

    if synopsis_path:
        if not os.path.isfile(synopsis_path):
            raise FileNotFoundError(f"synopsis not found: {synopsis_path}")
        _text = Path(synopsis_path).read_text(encoding="utf-8")
        bundle.synopsis_text = _text
        bundle.synopsis_path = str(synopsis_path)
        bundle.synopsis_sha256 = hashlib.sha256(_text.encode("utf-8")).hexdigest()

    return bundle


# ── Report generation ──────────────────────────────────────────────────────

def generate_json_report(
    findings: list[Finding],
    manuscript: ManuscriptArtifact,
    briefs: BriefBundle,
    checks_run: list[str],
    wall_seconds: float,
) -> dict:
    """Build the JSON report structure."""
    class_a = [f for f in findings if f.severity == "CLASS_A"]
    class_b = [f for f in findings if f.severity == "CLASS_B"]
    class_c = [f for f in findings if f.severity == "CLASS_C"]

    by_check: dict[str, list[dict]] = {}
    for f in findings:
        by_check.setdefault(f.check_id, []).append(f.to_dict())

    return {
        "header": {
            "manuscript_dir": manuscript.manuscript_dir,
            "total_scenes": len(manuscript.scenes),
            "total_words": manuscript.total_words(),
            "audit_timestamp": datetime.now(timezone.utc).isoformat(),
            "checks_run": checks_run,
            "wall_seconds": round(wall_seconds, 1),
            "synopsis_path": briefs.synopsis_path,
            "synopsis_sha256": briefs.synopsis_sha256,
        },
        "summary": {
            "total_findings": len(findings),
            "class_a": len(class_a),
            "class_b": len(class_b),
            "class_c": len(class_c),
            "blocks_publication": len(class_a) > 0,
        },
        "findings_by_check": by_check,
        "all_findings": [f.to_dict() for f in findings],
    }


def generate_markdown_report(report: dict) -> str:
    """Build the markdown report from JSON report structure."""
    lines = []
    h = report["header"]
    s = report["summary"]

    lines.append("# Manuscript Audit Report")
    lines.append("")
    lines.append(f"**Manuscript:** `{h['manuscript_dir']}`")
    lines.append(f"**Scenes:** {h['total_scenes']}  |  **Words:** {h['total_words']:,}")
    lines.append(f"**Timestamp:** {h['audit_timestamp']}")
    lines.append(f"**Wall time:** {h['wall_seconds']}s")
    lines.append(f"**Checks run:** {', '.join(h['checks_run'])}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Class | Count |")
    lines.append(f"|-------|-------|")
    lines.append(f"| CLASS_A (blocks publication) | **{s['class_a']}** |")
    lines.append(f"| CLASS_B (flags for review) | {s['class_b']} |")
    lines.append(f"| CLASS_C (auto-fixable) | {s['class_c']} |")
    lines.append(f"| **Total** | **{s['total_findings']}** |")
    lines.append("")

    if s["blocks_publication"]:
        lines.append("> **PUBLICATION BLOCKED** — CLASS_A findings present.")
        lines.append("")

    for check_id, check_findings in report["findings_by_check"].items():
        lines.append(f"## {check_id}")
        lines.append("")
        for i, f in enumerate(check_findings, 1):
            sn = f.get("scene_number", "")
            sns = f.get("scene_numbers", [])
            scene_ref = f"Scene {sn}" if sn else (f"Scenes {sns}" if sns else "Global")
            lines.append(f"### Finding {i} [{f['severity']}] — {scene_ref}")
            lines.append("")
            lines.append(f"**{f['description']}**")
            lines.append("")
            if f.get("evidence"):
                lines.append("Evidence:")
                for ev in f["evidence"]:
                    lines.append(f"  - {ev}")
                lines.append("")
            if f.get("suggested_fix"):
                lines.append(f"*Fix:* {f['suggested_fix']}")
                lines.append("")

    return "\n".join(lines)


# ── Orchestration ──────────────────────────────────────────────────────────

def run_audit(
    manuscript: ManuscriptArtifact,
    briefs: BriefBundle,
    output_dir: str | None = None,
    checks: list | None = None,
) -> tuple[int, list[Finding]]:
    """Run all registered check modules against the manuscript.

    Returns (exit_code, findings).
    exit_code: 0 = clean, 1 = CLASS_A present, 2 = orchestrator failure.

    If *checks* is provided, use exactly those check instances and skip
    auto-discovery.  When checks is None (default), auto-discover from
    audit_checks/ on disk (production path).
    """
    if not manuscript.scenes:
        print("ERROR: No scenes loaded.", file=sys.stderr)
        return (2, [])

    if checks is not None:
        active_checks = checks
    else:
        # Discover and register all check modules
        discover_and_register()
        active_checks = list(REGISTRY)

    if not active_checks:
        print("WARNING: No check modules registered.", file=sys.stderr)
        return (0, [])

    all_findings: list[Finding] = []
    checks_run: list[str] = []
    start = time.time()

    print(f"=== V25 manuscript_auditor ===", file=sys.stderr)
    print(f"  Scenes: {len(manuscript.scenes)}", file=sys.stderr)
    print(f"  Words:  {manuscript.total_words():,}", file=sys.stderr)
    print(f"  Checks: {len(active_checks)}", file=sys.stderr)

    for check in active_checks:
        check_name = check.check_id
        checks_run.append(check_name)
        print(f"\n  Running: {check_name}", file=sys.stderr)
        try:
            findings = check.run(manuscript, briefs)
            all_findings.extend(findings)
            n_a = sum(1 for f in findings if f.severity == "CLASS_A")
            n_b = sum(1 for f in findings if f.severity == "CLASS_B")
            n_c = sum(1 for f in findings if f.severity == "CLASS_C")
            print(f"    -> {len(findings)} findings (A:{n_a} B:{n_b} C:{n_c})", file=sys.stderr)
        except Exception as exc:
            print(f"    -> ERROR: {exc}", file=sys.stderr)
            all_findings.append(Finding(
                check_id=check_name,
                severity="CLASS_B",
                scene_number=None,
                description=f"Check module {check_name} failed: {exc}",
                suggested_fix="Investigate check module failure and retry",
            ))

    elapsed = time.time() - start

    # Generate reports
    report = generate_json_report(all_findings, manuscript, briefs, checks_run, elapsed)

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        json_path = os.path.join(output_dir, "manuscript_audit_REPORT.json")
        md_path = os.path.join(output_dir, "manuscript_audit_REPORT.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(generate_markdown_report(report))

        print(f"\n  Reports written:", file=sys.stderr)
        print(f"    JSON: {json_path}", file=sys.stderr)
        print(f"    MD:   {md_path}", file=sys.stderr)

    # Also emit JSON to stdout for pipeline consumption
    print(json.dumps(report))

    has_class_a = any(f.severity == "CLASS_A" for f in all_findings)
    print(f"\n  Total: {len(all_findings)} findings, "
          f"{'BLOCKED' if has_class_a else 'CLEAN'}, "
          f"{elapsed:.1f}s", file=sys.stderr)

    return (1 if has_class_a else 0, all_findings)


# ── Synopsis resolver ─────────────────────────────────────────────────────

def _resolve_synopsis(manuscript_path: str) -> str | None:
    """Derive <book>/work/synopsis.md by finding the 'work' ancestor of the
    manuscript path. Robust to the manuscript-run-dir nesting (finds 'work'
    by name, not by parent count). Returns None if no synopsis.md found."""
    p = Path(manuscript_path).resolve()
    for anc in [p, *p.parents]:
        if anc.name == "work":
            cand = anc / "synopsis.md"
            return str(cand) if cand.exists() else None
    return None


# ── CLI ────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manuscript_auditor_v25.py",
        description="ANPD V25 Manuscript Auditor — check-module architecture",
    )
    parser.add_argument("--manuscript-dir", default=None,
                        help="Directory containing sc_NNN.md or ch{NN}_{slug}.md files")
    parser.add_argument("--manuscript", default=None,
                        help="Path to a single assembled manuscript .md file")
    parser.add_argument("--series-bible", default=None,
                        help="Path to series_bible.json")
    parser.add_argument("--character-profiles", default=None,
                        help="Path to character_profiles.json")
    parser.add_argument("--book-config", default=None,
                        help="Path to book_config.json")
    parser.add_argument("--entity-ledger", default=None,
                        help="Path to entity_ledger.json (S-2 Phase 2a)")
    parser.add_argument("--synopsis", default=None,
                        help="Path to the book's synopsis.md. If omitted, "
                             "auto-derived from the manuscript's work/ dir. "
                             "Audit aborts if it cannot be resolved.")
    parser.add_argument("--output-dir", default=None,
                        help="Directory for JSON + markdown reports")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    manuscript_path = args.manuscript or args.manuscript_dir
    if not manuscript_path:
        parser.error("one of --manuscript-dir or --manuscript is required")
    manuscript = load_manuscript(manuscript_path)

    synopsis_path = args.synopsis or _resolve_synopsis(manuscript_path)
    if not synopsis_path or not os.path.isfile(synopsis_path):
        print(f"ERROR: synopsis could not be resolved (got: {synopsis_path}). "
              f"Pass --synopsis explicitly.", file=sys.stderr)
        return 2

    briefs = load_briefs(
        series_bible_path=args.series_bible,
        character_profiles_path=args.character_profiles,
        book_config_path=args.book_config,
        entity_ledger_path=args.entity_ledger,
        synopsis_path=synopsis_path,
    )

    exit_code, _ = run_audit(manuscript, briefs, output_dir=args.output_dir)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
