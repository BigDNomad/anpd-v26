"""
outline_parser.py — V26 Outline Parser
ANPD V26 | Version: 20260612

Parses operator-authored outlines (PDF, markdown, docx) into structured
ChapterSpec objects for downstream consumption by synopsis_generator.

SG-1: Added ParsedScene/ParsedOutline for scene-organized outlines with
type-tag recognition, act assignment, and beat extraction.

SG-4: Accept minimal "Scene N - [TAG]" format (title optional, heading-line
bracket tag, scene-organized decoupled from type-tag presence).

V26: Extract pillar markers from HTML comments (<!-- TWIST 1 -->, etc.)
and populate annotations["pillar"] for downstream emission.
"""

import re
import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ChapterSpec:
    chapter_number: int
    title: str = ""
    content: str = ""
    annotations: dict = field(default_factory=dict)
    beats: list = field(default_factory=list)
    passthrough: bool = False
    passthrough_source: str = None


@dataclass
class ParsedScene:
    number: int
    title: str
    type: str            # canonical: ACTION/NON-ACTION/SUSPENSE/MIXED/UNKNOWN
    type_raw: str        # original captured type string
    act: Optional[str]   # "ACT ONE", "ACT TWO", "ACT THREE", "RESOLUTION", or None
    beats: str           # joined paragraph text
    pillar: Optional[str] = None  # V26: TWIST1/TWIST2/TWIST3/LOWEST_POINT/FINAL_BATTLE


# ── Passthrough directive patterns ──────────────────────────────────────────

PASSTHROUGH_PATTERN = re.compile(
    r'\((?:use\s+existing|preserve\s+from|preserve)\b[^)]*\)',
    re.IGNORECASE,
)

def _detect_passthrough(content: str) -> tuple:
    """Detect passthrough directives in chapter/scene content.
    Returns (is_passthrough: bool, source_hint: str or None).
    """
    m = PASSTHROUGH_PATTERN.search(content)
    if m:
        directive = m.group(0)
        # Try to extract a source filename
        source_match = re.search(r'(?:from|source:\s*|:)\s*(\S+\.md)', directive, re.IGNORECASE)
        source = source_match.group(1) if source_match else None
        return True, source
    return False, None


@dataclass
class OutlineStructure:
    chapters: list = field(default_factory=list)
    top_matter: dict = field(default_factory=dict)


@dataclass
class ParsedOutline:
    scenes: List[ParsedScene] = field(default_factory=list)
    total_scene_count: int = 0
    parse_warnings: List[str] = field(default_factory=list)


# ── Chapter heading patterns ────────────────────────────────────────────────

CHAPTER_PATTERNS = [
    # "Chapter N (Title)" or "Chapter N (anything)"
    re.compile(r'^Chapter\s+(\d+)\s*\(([^)]+)\)', re.IGNORECASE),
    # "Chapter N: Title" or "Chapter N — Title"
    re.compile(r'^Chapter\s+(\d+)\s*[:\u2014\-]\s*(.+)', re.IGNORECASE),
    # "## Chapter N" or "# Chapter N"
    re.compile(r'^#{1,3}\s*Chapter\s+(\d+)\s*[:\u2014\-]?\s*(.*)', re.IGNORECASE),
    # "Chapter N" alone
    re.compile(r'^Chapter\s+(\d+)\s*$', re.IGNORECASE),
]

# ── Scene heading patterns (scene-organized outlines) ─────────────────────
# SG-4: title is now optional (.*) in all patterns to support minimal format.

SCENE_PATTERNS = [
    # **Scene N — Title** or **Scene N – Title** or **Scene N - Title** or **Scene N: Title**
    # Also matches **Scene N -** (no title) and **Scene N - [ACTION]**
    re.compile(r'^\*\*Scene\s+(\d+)\s*[:\u2014\u2013\-]\s*(.*?)\*\*', re.IGNORECASE),
    # ## Scene N — Title or ### Scene N — Title (title optional)
    re.compile(r'^#{2,3}\s*Scene\s+(\d+)\s*[:\u2014\u2013\-]\s*(.*)', re.IGNORECASE),
    # Scene N — Title (plain line-start, title optional)
    re.compile(r'^Scene\s+(\d+)\s*[:\u2014\u2013\-]\s*(.*)', re.IGNORECASE),
    # Scene N [TAG] or Scene N (bare, no separator) — must be last
    re.compile(r'^Scene\s+(\d+)\s*(.*)', re.IGNORECASE),
]


# ── Heading-line bracket tag pattern ─────────────────────────────────────────
# Matches [ACTION], [NON-ACTION], [MIXED], [SUSPENSE] on the heading line.
_HEADING_TAG_RE = re.compile(
    r'\[(ACTION|NON-ACTION|NON_ACTION|MIXED|SUSPENSE)\]',
    re.IGNORECASE,
)


# ── Type-tag patterns (italic-wrapped type on line after scene marker) ──────

_TYPE_TAG_RE = re.compile(
    r"^\*([^*\n]+)\*\s*$",
    re.MULTILINE,
)

_ACT_HEADER_RE = re.compile(
    r"^###\s+(.+)",
    re.MULTILINE,
)


# ── Pillar marker patterns (HTML comments in outlines) ──────────────────────
# Maps outline comment labels → canonical PILLAR tag names.

_PILLAR_COMMENT_RE = re.compile(
    r'<!--\s*(TWIST\s*1|TWIST\s*2|TWIST\s*3|LOWEST\s*POINT|FINAL\s*BATTLE)\s*-->',
    re.IGNORECASE,
)

_PILLAR_LABEL_MAP = {
    "TWIST 1":      "TWIST1",
    "TWIST1":       "TWIST1",
    "TWIST 2":      "TWIST2",
    "TWIST2":       "TWIST2",
    "TWIST 3":      "TWIST3",
    "TWIST3":       "TWIST3",
    "LOWEST POINT": "LOWEST_POINT",
    "FINAL BATTLE": "FINAL_BATTLE",
}


def _extract_pillar_marker(lines: list, scene_line_idx: int, prev_scene_line_idx: int) -> str | None:
    """Scan lines between prev_scene_line_idx and scene_line_idx for a pillar comment.

    Returns canonical pillar label (e.g. 'TWIST1') or None.
    """
    search_start = max(prev_scene_line_idx, 0)
    for i in range(search_start, scene_line_idx):
        m = _PILLAR_COMMENT_RE.search(lines[i])
        if m:
            raw = m.group(1).strip().upper()
            # Normalize whitespace variants
            raw_normalized = re.sub(r'\s+', ' ', raw)
            return _PILLAR_LABEL_MAP.get(raw_normalized, raw_normalized)
    return None


def _normalize_scene_type(raw: str) -> str:
    """Map a raw type-tag string to canonical: ACTION/NON-ACTION/SUSPENSE/MIXED/UNKNOWN."""
    low = raw.strip().lower()
    # Take first word for compound types like "Suspense (transitioning to Action)"
    first_word = low.split()[0] if low else ""
    if first_word == "action":
        return "ACTION"
    if first_word.startswith("non"):
        return "NON-ACTION"
    if first_word == "suspense":
        return "SUSPENSE"
    if first_word == "mixed":
        return "MIXED"
    return "UNKNOWN"


# ── Annotation patterns ─────────────────────────────────────────────────────

ANNOTATION_PATTERNS = {
    "scene_type": re.compile(r'\[(ACTION|MIXED|NON-ACTION|NON_ACTION)\]', re.IGNORECASE),
    "pov": re.compile(r'\[POV:\s*([^\]]+)\]', re.IGNORECASE),
    "antagonist_phase": re.compile(r'\[ANTAGONIST:\s*([^\]]+)\]', re.IGNORECASE),
    "twist_marker": re.compile(r'\[(ACT\s*\d+\s*TWIST|MIDPOINT\s*TWIST)\]', re.IGNORECASE),
}


def _extract_text_from_pdf(path: str) -> str:
    """Extract text from PDF using pypdf."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    return "\n".join(pages)


def _extract_text_from_docx(path: str) -> str:
    """Extract text from docx."""
    try:
        from docx import Document
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs)
    except ImportError:
        raise ImportError("python-docx required for .docx parsing: pip install python-docx")


def _extract_text_from_markdown(path: str) -> str:
    """Read markdown file as text."""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _extract_text(path: str) -> str:
    """Extract text from supported file formats."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.pdf':
        return _extract_text_from_pdf(path)
    elif ext == '.docx':
        return _extract_text_from_docx(path)
    elif ext in ('.md', '.txt', '.markdown'):
        return _extract_text_from_markdown(path)
    else:
        raise ValueError(f"Unsupported outline format: {ext}. Supported: .pdf, .docx, .md")


def _extract_annotations(text: str) -> dict:
    """Extract inline annotation tags from text."""
    annotations = {}
    for key, pattern in ANNOTATION_PATTERNS.items():
        match = pattern.search(text)
        if match:
            annotations[key] = match.group(1).strip()
    return annotations


def _extract_beats(content: str) -> list:
    """Split chapter content into discrete beats.

    Each paragraph (double-newline separated) or sentence ending with a period
    that describes a discrete story event = one beat.
    """
    # Split by double newlines (paragraphs) first
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', content) if p.strip()]

    # If only one paragraph, try splitting by single newlines (common in PDF extraction)
    if len(paragraphs) <= 1 and content.strip():
        lines = [l.strip() for l in content.split('\n') if l.strip()]
        if len(lines) > 1:
            paragraphs = lines

    beats = []
    for para in paragraphs:
        # Split long paragraphs into sentences at period boundaries
        sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', para)
        if len(sentences) > 2:
            for s in sentences:
                s = s.strip()
                if s and len(s) > 20:
                    beats.append(s)
        else:
            if para and len(para) > 20:
                beats.append(para)

    return beats


def _extract_heading_tag(title_text: str) -> tuple[str, str]:
    """Extract a bracket tag [ACTION] etc. from the heading-line title text.

    Returns (scene_type, cleaned_title) where scene_type is canonical or ""
    and cleaned_title has the bracket tag stripped out.
    """
    m = _HEADING_TAG_RE.search(title_text)
    if m:
        raw_tag = m.group(1).strip()
        scene_type = _normalize_scene_type(raw_tag)
        # Remove the bracket tag from the title
        cleaned = _HEADING_TAG_RE.sub('', title_text).strip()
        return scene_type, cleaned
    return "", title_text


def _count_scene_headings(lines: list) -> list:
    """Scan lines for scene headings. Returns list of (line_index, scene_number, title)."""
    scene_starts = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in SCENE_PATTERNS:
            m = pattern.match(stripped)
            if m:
                scene_num = int(m.group(1))
                raw_title = m.group(2).strip().rstrip('*') if m.lastindex >= 2 and m.group(2) else ""
                # SG-4: strip heading-line bracket tags from the title
                # (they're extracted separately during scene parsing)
                _, cleaned_title = _extract_heading_tag(raw_title)
                scene_starts.append((i, scene_num, cleaned_title))
                break
    return scene_starts


def _count_chapter_headings(lines: list) -> list:
    """Scan lines for chapter headings. Returns list of (line_index, chapter_number, title)."""
    chapter_starts = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in CHAPTER_PATTERNS:
            m = pattern.match(stripped)
            if m:
                chapter_num = int(m.group(1))
                title = m.group(2).strip() if m.lastindex >= 2 and m.group(2) else ""
                chapter_starts.append((i, chapter_num, title))
                break
    return chapter_starts


def _parse_chapter_organized(lines: list, chapter_starts: list) -> OutlineStructure:
    """Parse a chapter-organized outline (existing V25 behavior)."""
    top_matter_lines = []
    first_chapter_line = chapter_starts[0][0] if chapter_starts else len(lines)
    for i, line in enumerate(lines):
        if i >= first_chapter_line:
            break
        stripped = line.strip()
        if stripped:
            top_matter_lines.append(stripped)

    chapters = []
    for idx, (line_idx, chapter_num, title) in enumerate(chapter_starts):
        start = line_idx + 1
        if idx + 1 < len(chapter_starts):
            end = chapter_starts[idx + 1][0]
        else:
            end = len(lines)

        content_lines = [l for l in lines[start:end] if l.strip()]
        content = "\n".join(content_lines).strip()

        annotations = _extract_annotations(content)
        beats = _extract_beats(content)
        is_passthrough, passthrough_src = _detect_passthrough(content)

        chapters.append(ChapterSpec(
            chapter_number=chapter_num,
            title=title,
            content=content,
            annotations=annotations,
            beats=beats,
            passthrough=is_passthrough,
            passthrough_source=passthrough_src,
        ))

    top_matter = {}
    if top_matter_lines:
        top_matter["raw"] = "\n".join(top_matter_lines)
    top_matter["format"] = "chapter-organized"

    return OutlineStructure(chapters=chapters, top_matter=top_matter)


def _scan_act_headers(lines: list) -> list:
    """Scan lines for act-level ### headers.

    Returns list of (line_index, act_label) where act_label is normalized
    to "ACT ONE", "ACT TWO", "ACT THREE", "RESOLUTION", etc.
    """
    act_headers = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = _ACT_HEADER_RE.match(stripped)
        if m:
            raw_label = m.group(1).strip()
            # Normalize: extract the ACT portion
            label = raw_label.upper()
            if "RESOLUTION" in label:
                act_headers.append((i, "RESOLUTION"))
            elif "ACT THREE" in label or "ACT 3" in label:
                act_headers.append((i, "ACT THREE"))
            elif "ACT TWO" in label or "ACT 2" in label:
                act_headers.append((i, "ACT TWO"))
            elif "ACT ONE" in label or "ACT 1" in label or "PROLOGUE" in label:
                act_headers.append((i, "ACT ONE"))
            else:
                # Unknown act-level header — record as-is
                act_headers.append((i, raw_label))
    return act_headers


def _act_for_line(line_idx: int, act_headers: list) -> str | None:
    """Given a line index, return the act label from the most recent act header above it."""
    current_act = None
    for header_line, label in act_headers:
        if header_line <= line_idx:
            current_act = label
        else:
            break
    return current_act


def _extract_type_tag(lines: list, start_line: int) -> tuple[str, str]:
    """Check line at start_line for an italic type tag (*Action*, *Non-action*, etc.).

    Returns (canonical_type, raw_type). If no type tag found, returns ("UNKNOWN", "").
    """
    if start_line >= len(lines):
        return ("UNKNOWN", "")
    stripped = lines[start_line].strip()
    m = _TYPE_TAG_RE.match(stripped)
    if m:
        raw = m.group(1).strip()
        return (_normalize_scene_type(raw), raw)
    return ("UNKNOWN", "")


def _parse_scene_organized(lines: list, scene_starts: list) -> OutlineStructure:
    """Parse a scene-organized outline. Each scene becomes a ChapterSpec
    (Approach B) so downstream components iterate identically.

    Enhanced in SG-1 to extract *Type* tags and populate annotations.
    Enhanced in SG-4 to extract heading-line bracket tags [ACTION] etc.
    """
    act_headers = _scan_act_headers(lines)

    top_matter_lines = []
    first_scene_line = scene_starts[0][0] if scene_starts else len(lines)
    for i, line in enumerate(lines):
        if i >= first_scene_line:
            break
        stripped = line.strip()
        if stripped:
            top_matter_lines.append(stripped)

    chapters = []
    for idx, (line_idx, scene_num, title) in enumerate(scene_starts):
        start = line_idx + 1
        if idx + 1 < len(scene_starts):
            end = scene_starts[idx + 1][0]
        else:
            end = len(lines)

        # SG-4: extract heading-line bracket tag from the RAW heading text
        # (before title cleaning — re-read the original line)
        raw_heading = lines[line_idx].strip()
        heading_type, _ = _extract_heading_tag(raw_heading)

        # Extract italic type tag from the line immediately after the scene marker
        scene_type, type_raw = _extract_type_tag(lines, start)
        type_tag_consumed = scene_type != "UNKNOWN"

        # Content lines: skip the type tag line if it was consumed
        content_start = start + 1 if type_tag_consumed else start
        content_lines = []
        for li in range(content_start, end):
            stripped = lines[li].strip()
            # Stop at --- separator or next act header
            if stripped == "---":
                break
            if stripped:
                content_lines.append(stripped)

        content = "\n".join(content_lines).strip()

        annotations = _extract_annotations(content)

        # Inject scene_type: heading-line bracket tag wins, then italic tag, then content bracket
        if heading_type and heading_type != "UNKNOWN":
            annotations["scene_type"] = heading_type
        elif scene_type != "UNKNOWN" and "scene_type" not in annotations:
            annotations["scene_type"] = scene_type

        if type_raw:
            annotations["type_raw"] = type_raw
            # Extract POV/FOCUS from the italic type tag.
            if "pov" not in annotations:
                pov_match = re.search(r'POV:\s*(.+?)\s*$', type_raw, re.IGNORECASE)
                if pov_match:
                    annotations["pov"] = pov_match.group(1).strip()

        # Inject act
        act = _act_for_line(line_idx, act_headers)
        if act:
            annotations["act"] = act

        # V26: Extract pillar marker from HTML comments above scene heading
        prev_end = scene_starts[idx - 1][0] + 1 if idx > 0 else 0
        pillar = _extract_pillar_marker(lines, line_idx, prev_end)
        if pillar:
            annotations["pillar"] = pillar

        beats = _extract_beats(content)
        is_passthrough, passthrough_src = _detect_passthrough(content)

        chapters.append(ChapterSpec(
            chapter_number=scene_num,
            title=title,
            content=content,
            annotations=annotations,
            beats=beats,
            passthrough=is_passthrough,
            passthrough_source=passthrough_src,
        ))

    top_matter = {"format": "scene-organized", "scene_count": len(chapters)}
    if top_matter_lines:
        top_matter["raw"] = "\n".join(top_matter_lines)

    return OutlineStructure(chapters=chapters, top_matter=top_matter)


def parse_outline_scenes(outline_path: str) -> ParsedOutline:
    """Parse scene-organized outline into ParsedOutline with type tags and act assignment.

    Returns ParsedOutline with scenes, total_scene_count, and parse_warnings.
    This is the rich-format parser for scene-organized outlines (like Mandate).
    For chapter-organized outlines, falls back to empty ParsedOutline.
    """
    if not os.path.exists(outline_path):
        raise FileNotFoundError(f"Outline file not found: {outline_path}")

    text = _extract_text(outline_path)
    lines = text.split('\n')

    scene_starts = _count_scene_headings(lines)
    if not scene_starts:
        return ParsedOutline(parse_warnings=["No scene headings found"])

    act_headers = _scan_act_headers(lines)
    warnings: list[str] = []
    scenes: list[ParsedScene] = []

    for idx, (line_idx, scene_num, title) in enumerate(scene_starts):
        start = line_idx + 1
        if idx + 1 < len(scene_starts):
            end = scene_starts[idx + 1][0]
        else:
            end = len(lines)

        # SG-4: check heading-line bracket tag first
        raw_heading = lines[line_idx].strip()
        heading_type, _ = _extract_heading_tag(raw_heading)

        scene_type, type_raw = _extract_type_tag(lines, start)
        type_tag_consumed = scene_type != "UNKNOWN"

        # Heading-line bracket tag wins over italic tag
        if heading_type and heading_type != "UNKNOWN":
            scene_type = heading_type
            type_raw = heading_type  # No italic raw — use the canonical

        if scene_type == "UNKNOWN":
            warnings.append(f"Scene {scene_num}: no type tag found")

        content_start = start + 1 if type_tag_consumed else start
        content_lines = []
        for li in range(content_start, end):
            stripped = lines[li].strip()
            if stripped == "---":
                break
            if stripped:
                content_lines.append(stripped)

        beats_text = "\n".join(content_lines).strip()
        act = _act_for_line(line_idx, act_headers)

        # V26: Extract pillar marker from HTML comments above scene heading
        prev_end = scene_starts[idx - 1][0] + 1 if idx > 0 else 0
        pillar = _extract_pillar_marker(lines, line_idx, prev_end)

        scenes.append(ParsedScene(
            number=scene_num,
            title=title,
            type=scene_type,
            type_raw=type_raw,
            act=act,
            beats=beats_text,
            pillar=pillar,
        ))

    # Check for numbering gaps
    numbers = [s.number for s in scenes]
    expected = set(range(min(numbers), max(numbers) + 1))
    gaps = sorted(expected - set(numbers))
    for g in gaps:
        warnings.append(f"Scene {g}: missing from outline (gap in numbering)")

    return ParsedOutline(
        scenes=scenes,
        total_scene_count=len(scenes),
        parse_warnings=warnings,
    )


def parse_outline(outline_path: str) -> OutlineStructure:
    """Parse an operator outline into structured form.

    Auto-detects format:
      - Chapter headings present, no scene headings → chapter-organized
      - Scene headings present, no chapter headings → scene-organized
      - Both present → chapter-organized (scenes are sub-units within chapters)
      - Neither → empty structure with parse warning

    Returns OutlineStructure with chapters and optional top_matter.
    """
    if not os.path.exists(outline_path):
        raise FileNotFoundError(f"Outline file not found: {outline_path}")

    text = _extract_text(outline_path)

    # Pre-process: insert newlines before "Chapter N" patterns that appear mid-line
    text = re.sub(r'(?<=[.!?\s])(?=Chapter\s+\d+)', '\n', text)
    text = re.sub(
        r'^(Chapter\s+\d+(?:\s*\([^)]*\))?)\s{2,}',
        r'\1\n',
        text,
        flags=re.MULTILINE,
    )

    lines = text.split('\n')

    # Detect format by scanning for both heading types
    chapter_starts = _count_chapter_headings(lines)
    scene_starts = _count_scene_headings(lines)

    if chapter_starts and not scene_starts:
        # Chapter-organized (existing path)
        return _parse_chapter_organized(lines, chapter_starts)
    elif scene_starts and not chapter_starts:
        # Scene-organized (new path)
        return _parse_scene_organized(lines, scene_starts)
    elif chapter_starts and scene_starts:
        # Both present — chapter-organized dominates (scenes are sub-units)
        return _parse_chapter_organized(lines, chapter_starts)
    else:
        # Neither — unrecognized format
        return OutlineStructure(
            chapters=[],
            top_matter={"format": "unrecognized",
                        "warning": "No chapter or scene headings detected"},
        )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 outline_parser.py <outline_file>")
        sys.exit(1)
    result = parse_outline(sys.argv[1])
    print(f"Chapters parsed: {len(result.chapters)}")
    for ch in result.chapters:
        print(f"  Chapter {ch.chapter_number}: {ch.title or '(no title)'} — {len(ch.beats)} beats, {len(ch.content)} chars")
        if ch.annotations:
            print(f"    Annotations: {ch.annotations}")
    if result.top_matter:
        print(f"  Top matter: {len(result.top_matter.get('raw', ''))} chars")
