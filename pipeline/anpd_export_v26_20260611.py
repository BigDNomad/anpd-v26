#!/usr/bin/env python3
"""
anpd_export — Copy files from /anpd/v25/series/ with V25 filename convention.

Reads series_config.json for slug information, applies the filename mapping
table from Data Standards §3, and copies the file to the destination with
its new name.

Manuscript exports are gated by publish_gate: a manuscript file cannot be
exported unless an audit report is provided (--audit-report) and the
clearance check passes, or the operator explicitly overrides
(--override-clearance).

Usage:
    python3 anpd_export.py <source_file> [--dest <dir>]
    python3 anpd_export.py <manuscript.md> --audit-report <report.json> [--dest <dir>]
    python3 anpd_export.py --self-test

Exit codes:
    0  success
    1  error (source not found, not under series/, slug missing, gate refusal)
    2  directory input (not yet supported)
    3  manuscript clearance refused
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from publish_gate import evaluate_clearance


# ── Constants ────────────────────────────────────────────────────────────────

SERIES_ROOT = "/anpd/v25/series"
DEFAULT_DEST = "/tmp/anpd_export"

# Series-level files: use series_slug
SERIES_LEVEL_MAP = {
    "series_bible.json":       "series_bible_{series_slug}.json",
    "series_config.json":      "series_config_{series_slug}.json",
    "character_profiles.json": "character_profiles_{series_slug}.json",
    "banned_phrases.json":     "banned_phrases_{series_slug}.json",
}

# Book-level files: use book_slug
BOOK_LEVEL_MAP = {
    "intake.json":      "intake_{book_slug}.json",
    "outline.md":       "outline_{book_slug}.md",
    "book_config.json": "book_config_{book_slug}.json",
    "synopsis.md":      "synopsis_{book_slug}.md",
    "manuscript.md":    "manuscript_{book_slug}.md",
    "act1_full.md":     "manuscript_{book_slug}.md",
    "synopsis_generation_state.json": "synopsis_generation_state_{book_slug}.json",
    "capsule_manifest.json": "capsule_manifest_{book_slug}.json",
}

# Regex patterns for dynamic filenames
SCENE_FILE_RE = re.compile(r"^(sc_\d{3})\.md$")
STATE_FILE_RE = re.compile(r"^(state_after_sc\d{2,3})\.json$")
TIMESTAMPED_SYNOPSIS_RE = re.compile(r"^synopsis_(\d{8}_\d{4})\.md$")
TIMESTAMPED_MANUSCRIPT_RE = re.compile(r"^manuscript_(\d{8}_\d{4})\.md$")
ACT_MANUSCRIPT_RE = re.compile(r"^act\d+_full\.md$")
BLOCKED_MANUSCRIPT_RE = re.compile(r"^manuscript_.*BLOCKED.*\.md$")
RECEIPT_RE = re.compile(r"^(.+)_receipt\.json$")
FINDINGS_RE = re.compile(r"^(.+)_findings\.json$")
AUDIT_REPORT_RE = re.compile(r"^(.+)_audit_report\.json$")


# ── Path resolution ──────────────────────────────────────────────────────────

def find_series_dir(source_path: str) -> str | None:
    """Walk up from source_path to find the series directory under SERIES_ROOT."""
    abs_path = os.path.abspath(source_path)
    abs_root = os.path.abspath(SERIES_ROOT)

    if not abs_path.startswith(abs_root + os.sep) and not abs_path.startswith(abs_root):
        return None

    # The series directory is the first component after SERIES_ROOT
    rel = os.path.relpath(abs_path, abs_root)
    parts = rel.split(os.sep)
    if not parts:
        return None
    return os.path.join(abs_root, parts[0])


def find_book_key(source_path: str) -> str | None:
    """Extract book key (e.g., 'b01') from path components."""
    abs_path = os.path.abspath(source_path)
    parts = abs_path.split(os.sep)
    for part in parts:
        if re.match(r"^b\d{2}$", part):
            return part
    return None


def load_series_config(series_dir: str) -> dict:
    """Load series_config.json from a series directory."""
    config_path = os.path.join(series_dir, "series_config.json")
    if not os.path.isfile(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Filename mapping ─────────────────────────────────────────────────────────

def compute_new_name(
    basename: str,
    series_slug: str,
    book_slug: str | None,
) -> tuple[str, bool]:
    """Compute the new filename for a given basename.

    Returns (new_name, was_mapped). If was_mapped is False, the basename
    was copied as-is (no mapping rule matched).
    """
    # Check series-level map first
    if basename in SERIES_LEVEL_MAP:
        template = SERIES_LEVEL_MAP[basename]
        return template.format(series_slug=series_slug), True

    # Check book-level map
    if basename in BOOK_LEVEL_MAP:
        if book_slug is None:
            return basename, False
        template = BOOK_LEVEL_MAP[basename]
        return template.format(book_slug=book_slug), True

    # Scene files: sc_NNN.md → sc_NNN_{book_slug}.md
    m = SCENE_FILE_RE.match(basename)
    if m and book_slug:
        return f"{m.group(1)}_{book_slug}.md", True

    # State files: state_after_scNN.json → state_after_sc{NN}_{book_slug}.json
    m = STATE_FILE_RE.match(basename)
    if m and book_slug:
        return f"{m.group(1)}_{book_slug}.json", True

    # Timestamped synopsis: synopsis_YYYYMMDD_HHMM.md → synopsis_{book_slug}_{ts}.md
    m = TIMESTAMPED_SYNOPSIS_RE.match(basename)
    if m and book_slug:
        ts = m.group(1)
        return f"synopsis_{book_slug}_{ts}.md", True

    # Timestamped manuscript: manuscript_YYYYMMDD_HHMM.md → manuscript_{book_slug}_{ts}.md
    m = TIMESTAMPED_MANUSCRIPT_RE.match(basename)
    if m and book_slug:
        ts = m.group(1)
        return f"manuscript_{book_slug}_{ts}.md", True

    # Act manuscripts (act2_full.md, act3_full.md, …) → manuscript_act{N}_{book_slug}.md
    # (act1_full.md is handled by BOOK_LEVEL_MAP above as the canonical manuscript)
    m = ACT_MANUSCRIPT_RE.match(basename)
    if m and book_slug:
        stem = basename.removesuffix(".md")  # e.g. "act2_full"
        return f"manuscript_{stem}_{book_slug}.md", True

    # BLOCKED manuscripts: manuscript_*BLOCKED*.md → manuscript_BLOCKED_{book_slug}.md
    m = BLOCKED_MANUSCRIPT_RE.match(basename)
    if m and book_slug:
        return f"manuscript_BLOCKED_{book_slug}.md", True

    # Receipt files: *_receipt.json → {prefix}_{book_slug}_{export_ts}.json
    m = RECEIPT_RE.match(basename)
    if m and book_slug:
        prefix = m.group(1)
        export_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        return f"{prefix}_{book_slug}_{export_ts}.json", True

    # Findings files: *_findings.json → {prefix}_{book_slug}_{export_ts}.json
    m = FINDINGS_RE.match(basename)
    if m and book_slug:
        prefix = m.group(1)
        export_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        return f"{prefix}_{book_slug}_{export_ts}.json", True

    # Audit report files: *_audit_report.json → {prefix}_{book_slug}_{export_ts}.json
    m = AUDIT_REPORT_RE.match(basename)
    if m and book_slug:
        prefix = m.group(1)
        export_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        return f"{prefix}_{book_slug}_{export_ts}.json", True

    # No mapping rule matched
    return basename, False


# ── Export logic ─────────────────────────────────────────────────────────────

def _write_gate_record(
    dest_dir: str,
    prefix: str,
    source: str,
    result: object,
    override_reason: str | None = None,
) -> str:
    """Write a refusal or override JSON record to dest_dir. Returns the path."""
    # Derive book_slug from source path using existing helpers
    book_key = find_book_key(source)
    book_slug = "unknown"
    if book_key:
        series_dir = find_series_dir(source)
        if series_dir:
            config = load_series_config(series_dir)
            book_slug = config.get("book_slugs", {}).get(book_key, "unknown")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"{prefix}_{book_slug}_{ts}.json"

    record: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "dest_dir": dest_dir,
        "clearance_result": {
            "status": result.status,
            "reason": result.reason,
            "detail": result.detail,
            "findings": result.findings,
        },
    }
    if override_reason is not None:
        record["override_reason"] = override_reason

    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, default=str)
    return path


def export_file(
    source: str,
    dest_dir: str,
    audit_report: str | None = None,
    override_clearance: str | None = None,
) -> int:
    """Export a single file with renamed convention.

    Returns exit code (0 success, 1 error, 2 directory input, 3 gate refusal).
    """
    source = os.path.abspath(source)

    # Check if source is a directory
    if os.path.isdir(source):
        print("directory export not yet implemented", file=sys.stderr)
        return 2

    # Check source exists
    if not os.path.isfile(source):
        print(f"ERROR: source file not found: {source}", file=sys.stderr)
        return 1

    # Check source is under SERIES_ROOT
    series_dir = find_series_dir(source)
    if series_dir is None:
        print(f"ERROR: source must be under {SERIES_ROOT}/", file=sys.stderr)
        return 1

    # Load series config
    config = load_series_config(series_dir)
    if not config:
        print(f"ERROR: series_config.json not found in {series_dir}", file=sys.stderr)
        return 1

    series_slug = config.get("series_slug")
    if not series_slug:
        config_path = os.path.join(series_dir, "series_config.json")
        print(f"ERROR: series_slug not registered in {config_path}", file=sys.stderr)
        return 1

    # Determine book context
    book_key = find_book_key(source)
    book_slug = None
    if book_key:
        book_slugs = config.get("book_slugs", {})
        book_slug = book_slugs.get(book_key)

    basename = os.path.basename(source)

    # Check if this is a book-level file that needs a book_slug
    if basename in BOOK_LEVEL_MAP and book_slug is None:
        print(f"ERROR: cannot determine book_slug from path {source}", file=sys.stderr)
        return 1

    # Compute new name
    new_name, was_mapped = compute_new_name(basename, series_slug, book_slug)

    if not was_mapped:
        print(f"WARNING: no rename rule for {basename}; copied as-is", file=sys.stderr)

    # ── Publish gate (manuscript sources only) ──────────────────────────────
    is_manuscript = (
        basename == "manuscript.md"
        or bool(TIMESTAMPED_MANUSCRIPT_RE.match(basename))
        or bool(ACT_MANUSCRIPT_RE.match(basename))
        or bool(BLOCKED_MANUSCRIPT_RE.match(basename))
    )

    if is_manuscript:
        if not audit_report and not override_clearance:
            print(
                "ERROR: manuscript export requires --audit-report PATH "
                "(pointing at manuscript_audit_REPORT.json) or "
                '--override-clearance "<reason>".',
                file=sys.stderr,
            )
            return 1

        manuscript_dir = Path(source).parent
        audit_report_path = Path(audit_report) if audit_report else Path("/nonexistent")
        result = evaluate_clearance(manuscript_dir, audit_report_path)

        if result.status == "CLEARED":
            pass  # fall through to existing copy
        elif override_clearance:
            _write_gate_record(dest_dir, "export_override", source, result, override_clearance)
            print(
                f"WARNING: clearance overridden ({result.reason}): {override_clearance}",
                file=sys.stderr,
            )
        else:
            _write_gate_record(dest_dir, "export_refused", source, result)
            print(
                f"REFUSED ({result.reason}): see export_refused_*.json in {dest_dir}",
                file=sys.stderr,
            )
            return 3

    # ── Copy (existing logic, unchanged) ────────────────────────────────────

    # Create destination directory
    os.makedirs(dest_dir, exist_ok=True)

    # Copy file
    dest_path = os.path.join(dest_dir, new_name)
    shutil.copy2(source, dest_path)
    print(f"exported: {dest_path}")

    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anpd_export.py",
        description="Export files from /anpd/v25/series/ with V25 filename convention",
    )
    parser.add_argument("source_file", nargs="?", default=None,
                        help="Path to file inside /anpd/v25/series/")
    parser.add_argument("--dest", default=DEFAULT_DEST,
                        help=f"Destination directory (default: {DEFAULT_DEST})")
    parser.add_argument("--audit-report", default=None,
                        help="Path to manuscript_audit_REPORT.json (required for manuscript exports)")
    parser.add_argument("--override-clearance", default=None,
                        help='Override a BLOCKED/UNAUDITED clearance with a reason string')
    parser.add_argument("--self-test", action="store_true",
                        help="Run inline test suite")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        import subprocess
        test_path = os.path.join(os.path.dirname(__file__), "tests", "test_anpd_export.py")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_path, "-v"],
            cwd=os.path.dirname(__file__),
        )
        return result.returncode

    if args.source_file is None:
        parser.print_help()
        return 1

    return export_file(
        args.source_file,
        args.dest,
        audit_report=args.audit_report,
        override_clearance=args.override_clearance,
    )


if __name__ == "__main__":
    sys.exit(main())
