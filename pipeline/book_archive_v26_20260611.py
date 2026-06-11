#!/usr/bin/env python3
"""
book_archive.py — V25 book-completion archiver.

Collects a completed book's inputs + outputs into
  series/<series>/b<NN>/out/_book_archive_<book_id>/
with two zones:
  full_snapshot/  — complete copy of out/ (and work/ if present). "When unsure,
                    keep everything; sort later." Always populated, deterministic.
  canonical/      — the identified final files. Confident items detected
                    deterministically; the ambiguous print-docx and final-md are
                    chosen by NAMING CONVENTION, and if the convention is not met
                    the script FLAGS candidates and exits non-zero rather than guess.

  canonical/marketing/ — populated by --backfill after KDP publication (blurbs,
                    covers, keywords, metadata) which lag the manuscript.

Canonical detection tiers (per Dave 2026-05-25): convention-first, flag-and-confirm
fallback. Receipts do not name final files, so there is no receipt tier.

Convention (future books should emit finals matching these):
  print docx : *_KDP.docx
  final md   : *_FINAL_*.md   (or *_FINAL.md)

Usage:
  book_archive.py --book-dir series/black_tide/b01 [--book-id btd001] [--title ...]
  book_archive.py --book-dir series/black_tide/b01 --backfill \
      --blurbs PATH --ebook-cover PATH [--print-cover PATH] [...]

Exit codes:
  0  archive complete, canonical fully resolved
  3  archive built, BUT canonical docx/md ambiguous — ARCHIVE_FLAGS.json written,
     operator must confirm (re-run with --print-docx / --final-md to resolve)
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import shutil
import sys


# ── helpers ──────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_T%H%M")


def _log(msg: str) -> None:
    print(f"  book_archive: {msg}", file=sys.stderr)


def _newest_by_mtime(paths: list[str]) -> str | None:
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return None
    return max(paths, key=lambda p: os.path.getmtime(p))


def _newest_timestamped(pattern_dir: str, prefix: str) -> str | None:
    """Newest file matching <prefix>*.md by embedded timestamp if present, else mtime."""
    cands = glob.glob(os.path.join(pattern_dir, f"{prefix}*"))
    cands = [c for c in cands if os.path.isfile(c)]
    if not cands:
        return None
    def keyf(p):
        m = re.search(r'(\d{8})[_T]?(\d{4})', os.path.basename(p))
        return (m.group(1) + m.group(2)) if m else "00000000000"
    ts_sorted = sorted(cands, key=keyf)
    # prefer embedded-timestamp ordering; fall back to mtime if all unstamped
    if any(re.search(r'\d{8}', os.path.basename(c)) for c in cands):
        return ts_sorted[-1]
    return _newest_by_mtime(cands)


def _cp(src: str, dst: str) -> None:
    """Copy file or dir, dereferencing symlinks (cp -rL semantics)."""
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=True)
    else:
        shutil.copy2(os.path.realpath(src), dst)


# ── full snapshot ──────────────────────────────────────────────────────────────

def build_full_snapshot(book_dir: str, archive_dir: str) -> None:
    """Copy all of out/ (excluding the archive dir itself) and work/ if present."""
    snap = os.path.join(archive_dir, "full_snapshot")
    os.makedirs(snap, exist_ok=True)
    out_dir = os.path.join(book_dir, "out")
    archive_basename = os.path.basename(archive_dir)
    for name in sorted(os.listdir(out_dir)):
        if name == archive_basename or name.startswith("_book_archive"):
            continue
        src = os.path.join(out_dir, name)
        dst = os.path.join(snap, name)
        try:
            _cp(src, dst)
        except Exception as e:
            _log(f"WARN snapshot copy failed for {name}: {e}")
    work_dir = os.path.join(book_dir, "work")
    if os.path.isdir(work_dir):
        try:
            _cp(work_dir, os.path.join(snap, "work"))
        except Exception as e:
            _log(f"WARN snapshot copy failed for work/: {e}")
    _log("full_snapshot built")


# ── canonical detection ────────────────────────────────────────────────────────

def detect_canonical(book_dir: str, overrides: dict) -> tuple[dict, dict]:
    """Return (resolved, flags).
    resolved: file-class -> source path (confident + convention-resolved + overrides)
    flags:    file-class -> list of candidate paths (ambiguous, need operator)
    """
    out_dir = os.path.join(book_dir, "out")
    work_dir = os.path.join(book_dir, "work")
    series_dir = os.path.dirname(os.path.dirname(book_dir)) if False else _series_dir(book_dir)
    resolved: dict = {}
    flags: dict = {}

    # --- deterministic: series-level SNAPSHOTs ---
    for cls, fname in [
        ("character_profiles_SNAPSHOT", "character_profiles.json"),
        ("series_bible_SNAPSHOT", "series_bible.json"),
        ("banned_phrases_SNAPSHOT", "banned_phrases.json"),
        ("series_config_SNAPSHOT", "series_config.json"),
    ]:
        p = os.path.join(series_dir, fname)
        if os.path.exists(p):
            resolved[cls] = p

    # --- deterministic: intake ---
    for base in (work_dir, out_dir, book_dir):
        p = os.path.join(base, "intake.json")
        if os.path.exists(p):
            resolved["intake"] = p
            break

    # --- deterministic: newest-timestamped outline + synopsis (in work/) ---
    search = work_dir if os.path.isdir(work_dir) else out_dir
    o = _newest_timestamped(search, "outline_v") or _newest_timestamped(search, "outline")
    if o:
        resolved["outline"] = o
    # synopsis: prefer a timestamped synopsis_<date>[_<time>].md; never the
    # report/findings/state/receipt/regen variants; fall back to plain synopsis.md.
    syn_cands = [
        p for p in glob.glob(os.path.join(search, "synopsis*.md"))
        if os.path.isfile(p)
        and not re.search(r'(audit|findings|state|receipt|regen|generation)', os.path.basename(p))
    ]
    syn_ts = [p for p in syn_cands
              if re.search(r'synopsis_\d{8}(_\d{4})?\.md$', os.path.basename(p))]
    if syn_ts:
        resolved["synopsis"] = _newest_timestamped(search, "synopsis_2") or syn_ts[-1]
    elif syn_cands:
        resolved["synopsis"] = _newest_by_mtime(syn_cands)

    # --- deterministic dirs ---
    for cls, name in [("scene_prose", "scene_prose"), ("chapters_for_format", "chapters_for_format")]:
        # scene_prose may be under a manuscript_* dir; prefer top-level out/ copy if present
        p = os.path.join(out_dir, name)
        if os.path.isdir(p):
            resolved[cls] = p
        else:
            hits = glob.glob(os.path.join(out_dir, "manuscript_*", name))
            nd = _newest_by_mtime([h for h in hits if os.path.isdir(h)])
            if nd:
                resolved[cls] = nd
    # final_audit = newest reports_reaudit* dir, else reports/
    reaudits = [d for d in glob.glob(os.path.join(out_dir, "reports_reaudit*")) if os.path.isdir(d)]
    fa = _newest_by_mtime(reaudits) or (os.path.join(out_dir, "reports")
                                        if os.path.isdir(os.path.join(out_dir, "reports")) else None)
    if fa:
        resolved["final_audit"] = fa

    # --- convention / flag: print docx ---
    if overrides.get("print_docx"):
        resolved["published_print_docx"] = overrides["print_docx"]
    else:
        kdp = glob.glob(os.path.join(out_dir, "*_KDP.docx"))
        if len(kdp) == 1:
            resolved["published_print_docx"] = kdp[0]
        elif len(kdp) > 1:
            flags["published_print_docx"] = sorted(kdp)
        else:
            all_docx = sorted(glob.glob(os.path.join(out_dir, "*.docx")))
            if all_docx:
                flags["published_print_docx"] = all_docx

    # --- convention / flag: final md ---
    if overrides.get("final_md"):
        resolved["final_manuscript_text"] = overrides["final_md"]
    else:
        fin = glob.glob(os.path.join(out_dir, "*_FINAL_*.md")) + glob.glob(os.path.join(out_dir, "*_FINAL.md"))
        if len(fin) == 1:
            resolved["final_manuscript_text"] = fin[0]
        elif len(fin) > 1:
            flags["final_manuscript_text"] = sorted(fin)
        else:
            all_md = sorted(p for p in glob.glob(os.path.join(out_dir, "*.md")))
            if all_md:
                flags["final_manuscript_text"] = all_md

    return resolved, flags


def _series_dir(book_dir: str) -> str:
    # book_dir = series/<series>/b<NN>  -> series dir is its parent
    return os.path.dirname(os.path.abspath(book_dir))


# ── metadata from receipts ──────────────────────────────────────────────────────

def read_metadata(book_dir: str, book_id: str | None, title: str | None) -> dict:
    meta = {"book_id": book_id, "title": title, "series": None,
            "author_pen_name": None, "word_count": None}
    # try intake
    for base in (os.path.join(book_dir, "work"), os.path.join(book_dir, "out"), book_dir):
        ip = os.path.join(base, "intake.json")
        if os.path.exists(ip):
            try:
                d = json.load(open(ip))
                meta["book_id"] = meta["book_id"] or d.get("book_id")
                meta["title"] = meta["title"] or d.get("title")
                meta["series"] = d.get("series") or d.get("series_name")
            except Exception:
                pass
            break
    # try pipeline receipt for pen_name + word count
    for rp in glob.glob(os.path.join(book_dir, "out", "**", "PIPELINE_RECEIPT.json"), recursive=True):
        try:
            d = json.load(open(rp))
            meta["title"] = meta["title"] or d.get("title")
            cfg = d.get("effective_config_snapshot", {})
            meta["author_pen_name"] = meta["author_pen_name"] or cfg.get("pen_name")
        except Exception:
            pass
        break
    for mp in glob.glob(os.path.join(book_dir, "out", "**", "manuscript_receipt.json"), recursive=True):
        try:
            d = json.load(open(mp))
            if d.get("total_words"):
                meta["word_count"] = d["total_words"]
        except Exception:
            pass
        break
    return meta


# ── archive assembly ─────────────────────────────────────────────────────────

def assemble_canonical(archive_dir: str, resolved: dict) -> dict:
    canon = os.path.join(archive_dir, "canonical")
    os.makedirs(canon, exist_ok=True)
    placed: dict = {}
    for cls, src in resolved.items():
        try:
            dstname = os.path.basename(src.rstrip("/"))
            if cls.endswith("_SNAPSHOT"):
                root, ext = os.path.splitext(dstname)
                dstname = f"{root}_SNAPSHOT{ext}"
            elif cls == "final_audit":
                dstname = "final_audit"
            dst = os.path.join(canon, dstname)
            _cp(src, dst)
            placed[cls] = f"canonical/{dstname}"
        except Exception as e:
            _log(f"WARN canonical copy failed for {cls} ({src}): {e}")
    return placed


def write_manifest(archive_dir: str, meta: dict, placed: dict, flags: dict, ts: str) -> str:
    manifest = {
        "book_id": meta.get("book_id"),
        "title": meta.get("title"),
        "series": meta.get("series"),
        "author_pen_name": meta.get("author_pen_name"),
        "word_count": meta.get("word_count"),
        "archived_date": datetime.date.today().isoformat(),
        "archive_run": ts,
        "status": "archived_canonical_resolved" if not flags else "archived_canonical_FLAGGED",
        "canonical_files": placed,
        "full_snapshot": "full_snapshot/ — complete copy of out/ (+work/) at archive time. "
                         "Per operator rule: when unsure, keep everything; sort later.",
        "marketing": "canonical/marketing/ — populated post-publication via --backfill "
                     "(blurbs, covers, keywords, KDP metadata).",
    }
    if flags:
        manifest["UNRESOLVED_canonical"] = {
            k: f"AMBIGUOUS — operator must confirm; candidates: {v}" for k, v in flags.items()
        }
    fname = f"ARCHIVE_MANIFEST_{ts}.json"
    path = os.path.join(archive_dir, fname)
    json.dump(manifest, open(path, "w"), indent=2)
    # also refresh a stable pointer to the latest manifest
    latest = os.path.join(archive_dir, "ARCHIVE_MANIFEST_LATEST.json")
    try:
        if os.path.islink(latest) or os.path.exists(latest):
            os.remove(latest)
        os.symlink(fname, latest)
    except Exception:
        shutil.copy2(path, latest)
    return path


def write_flags(archive_dir: str, flags: dict, ts: str) -> str:
    path = os.path.join(archive_dir, f"ARCHIVE_FLAGS_{ts}.json")
    payload = {
        "archive_run": ts,
        "message": "Canonical file(s) could not be resolved by naming convention. "
                   "Re-run with the explicit flag(s) below to finalize the canonical set.",
        "unresolved": {
            k: {"candidates": v,
                "resolve_with": ("--print-docx PATH" if k == "published_print_docx"
                                 else "--final-md PATH")}
            for k, v in flags.items()
        },
    }
    json.dump(payload, open(path, "w"), indent=2)
    return path


# ── backfill (post-publication marketing) ──────────────────────────────────────

def backfill_marketing(archive_dir: str, args) -> str:
    mk = os.path.join(archive_dir, "canonical", "marketing")
    os.makedirs(mk, exist_ok=True)
    present, missing = {}, {}
    for key, src, desc in [
        ("blurbs", args.blurbs, "blurb candidate set / published selection"),
        ("ebook_cover", args.ebook_cover, "ebook front cover"),
        ("print_cover", args.print_cover, "full print wrap (front+spine+back)"),
    ]:
        if src and os.path.exists(src):
            dn = os.path.basename(src)
            _cp(src, os.path.join(mk, dn))
            present[key] = dn
        else:
            missing[key] = desc
    if args.keywords:
        present["keywords"] = args.keywords
    else:
        missing["keywords"] = "7 KDP keyword slots"
    if args.kdp_metadata:
        present["kdp_metadata"] = args.kdp_metadata
    else:
        missing["kdp_metadata"] = "categories, ASIN(s), Amazon links, price, pub date"
    ts = _ts()
    man = {"book_id": os.path.basename(archive_dir).replace("_book_archive_", ""),
           "backfill_run": ts, "present": present, "missing_TODO": missing}
    path = os.path.join(mk, f"MARKETING_MANIFEST_{ts}.json")
    json.dump(man, open(path, "w"), indent=2)
    _log(f"marketing backfill written: {path}")
    return path


# ── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="V25 book-completion archiver")
    ap.add_argument("--book-dir", required=True, help="series/<series>/b<NN>")
    ap.add_argument("--book-id", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--print-docx", default=None, help="explicit canonical print .docx (resolves flag)")
    ap.add_argument("--final-md", default=None, help="explicit canonical final .md (resolves flag)")
    ap.add_argument("--backfill", action="store_true", help="marketing backfill mode")
    ap.add_argument("--blurbs", default=None)
    ap.add_argument("--ebook-cover", default=None)
    ap.add_argument("--print-cover", default=None)
    ap.add_argument("--keywords", default=None)
    ap.add_argument("--kdp-metadata", default=None)
    args = ap.parse_args()

    book_dir = args.book_dir.rstrip("/")
    if not os.path.isdir(os.path.join(book_dir, "out")):
        _log(f"ERROR: {book_dir}/out not found")
        sys.exit(2)

    meta = read_metadata(book_dir, args.book_id, args.title)
    book_id = meta.get("book_id") or args.book_id
    if not book_id:
        _log("ERROR: book_id not determinable; pass --book-id")
        sys.exit(2)

    archive_dir = os.path.join(book_dir, "out", f"_book_archive_{book_id}")
    os.makedirs(archive_dir, exist_ok=True)
    ts = _ts()

    if args.backfill:
        backfill_marketing(archive_dir, args)
        sys.exit(0)

    build_full_snapshot(book_dir, archive_dir)
    resolved, flags = detect_canonical(book_dir, {"print_docx": args.print_docx,
                                                  "final_md": args.final_md})
    placed = assemble_canonical(archive_dir, resolved)
    manifest_path = write_manifest(archive_dir, meta, placed, flags, ts)
    _log(f"manifest: {manifest_path}")
    _log(f"canonical resolved: {sorted(placed.keys())}")

    if flags:
        fp = write_flags(archive_dir, flags, ts)
        _log(f"AMBIGUOUS canonical — flags written: {fp}")
        for k, v in flags.items():
            _log(f"  UNRESOLVED {k}: {len(v)} candidates")
        sys.exit(3)
    _log("archive complete, canonical fully resolved")
    sys.exit(0)


if __name__ == "__main__":
    main()
