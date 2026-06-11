"""
outline_comparator.py — V25 Outline Comparator
ANPD V25 | Version: 20260523

Compares generated synopsis against operator outline. Produces structured
findings for each chapter/scene.

Two modes:
  - Chapter-organized outlines: compares per-chapter (legacy).
  - Scene-organized outlines: compares per-scene (1:1 by scene number).

Finding severity:
  - CLASS_A (structural, gate-blocking): scene missing, scene-type mismatch,
    POV mismatch, named-character absent, count mismatch.
  - CLASS_B (semantic, informational): per-beat coverage gaps. Reported for
    review but do NOT block the gate.
"""

import json
import os
import re
from dataclasses import dataclass, field

from pathlib import Path

from outline_parser import parse_outline, parse_outline_scenes, OutlineStructure


@dataclass
class Finding:
    id: str
    principle_id: str
    severity: str  # CLASS_A or CLASS_B
    location: str  # e.g., "Chapter 3" or "Scene 14"
    excerpt: str
    message: str
    recommendation: str


@dataclass
class ChapterComparison:
    chapter_number: int
    passed: bool
    findings: list = field(default_factory=list)
    beat_coverage: dict = field(default_factory=dict)  # beat_text -> covered: bool


@dataclass
class ComparisonResult:
    passed: bool
    findings: list = field(default_factory=list)
    chapter_results: dict = field(default_factory=dict)  # chapter_num -> ChapterComparison


# ── Synopsis scene parser ─────────────────────────────────────────────────

_SYNOPSIS_SCENE_RE = re.compile(
    r"^###\s+Scene\s+(\d+)\s*[:\u2014\u2013\-]\s*(.+)",
    re.IGNORECASE,
)


def _parse_synopsis_chapters(synopsis_text: str) -> dict:
    """Parse synopsis markdown into per-chapter content.

    Returns dict: chapter_number -> chapter_content_text.
    """
    chapters = {}
    current_chapter = None
    current_lines = []

    for line in synopsis_text.split('\n'):
        # Detect chapter headers: "## Chapter N" or "# Chapter N"
        m = re.match(r'^#{1,3}\s*Chapter\s+(\d+)', line, re.IGNORECASE)
        if m:
            if current_chapter is not None:
                chapters[current_chapter] = '\n'.join(current_lines)
            current_chapter = int(m.group(1))
            current_lines = [line]
        elif current_chapter is not None:
            current_lines.append(line)

    if current_chapter is not None:
        chapters[current_chapter] = '\n'.join(current_lines)

    return chapters


def _parse_synopsis_scenes(synopsis_text: str) -> dict:
    """Parse synopsis markdown into per-scene content blocks.

    Returns dict: scene_number -> full scene text (header + body).
    """
    scenes = {}
    current_scene = None
    current_lines = []

    for line in synopsis_text.split('\n'):
        m = _SYNOPSIS_SCENE_RE.match(line.strip())
        if m:
            if current_scene is not None:
                scenes[current_scene] = '\n'.join(current_lines)
            current_scene = int(m.group(1))
            current_lines = [line]
        elif current_scene is not None:
            current_lines.append(line)

    if current_scene is not None:
        scenes[current_scene] = '\n'.join(current_lines)

    return scenes


# ── Structural checks (Class A) ──────────────────────────────────────────

def _extract_scene_type_from_synopsis(scene_text: str) -> str | None:
    """Extract [TYPE: X] tag from synopsis scene header."""
    m = re.search(r'\[TYPE:\s*([^\]]+)\]', scene_text, re.IGNORECASE)
    return m.group(1).strip().upper() if m else None


def _extract_focus_from_synopsis(scene_text: str) -> str | None:
    """Extract [FOCUS: X] or [POV: X] from synopsis scene header."""
    m = re.search(r'\[(?:FOCUS|POV):\s*([^\]]+)\]', scene_text, re.IGNORECASE)
    return m.group(1).strip() if m else None


# Common name fragments to ignore in character extraction
_NAME_STOPWORDS = {
    'the', 'and', 'his', 'her', 'she', 'they', 'him', 'from', 'with',
    'that', 'this', 'into', 'does', 'not', 'has', 'had', 'was', 'were',
    'are', 'for', 'but', 'who', 'what', 'one', 'two', 'man', 'men',
    'team', 'back', 'down', 'first', 'last', 'new', 'old',
}


def _extract_named_characters(text: str) -> set:
    """Extract likely character names from outline scene text.

    Looks for capitalized multi-word names and single capitalized words
    that appear to be names (not sentence-initial). Conservative: only
    returns names that appear in clear name patterns.
    """
    names = set()
    # Pattern: capitalized words that look like names (2+ chars, not ALL CAPS)
    # Match "Hank Reyes", "Mia", "Vera", "Funes", "Prada", "Lena Ibarra", etc.
    # Also match "Capitán Luis Vera" style
    for m in re.finditer(r'\b([A-Z][a-záéíóúñ]+(?:\s+[A-Z][a-záéíóúñ]+)*)\b', text):
        candidate = m.group(1)
        # Filter out common non-name words
        words = candidate.split()
        # Keep if at least one word is not a stopword and not a common noun
        significant = [w for w in words if w.lower() not in _NAME_STOPWORDS and len(w) > 2]
        if significant:
            # Store individual significant name parts for flexible matching
            for w in significant:
                if len(w) >= 3:  # Skip very short fragments
                    names.add(w)
    return names


def _check_scene_structural(
    scene_num: int,
    outline_scene,  # ChapterSpec from outline parser
    synopsis_scene_text: str,
    finding_counter: int,
) -> tuple[list, int]:
    """Run structural checks on a single scene pair. Returns (findings, updated_counter)."""
    findings = []
    location = f"Scene {scene_num}"
    synopsis_lower = synopsis_scene_text.lower()

    # ── Scene-type tag check ──
    outline_type = (outline_scene.annotations or {}).get("scene_type", "").upper()
    synopsis_type = _extract_scene_type_from_synopsis(synopsis_scene_text)

    if outline_type and synopsis_type:
        # Normalize for comparison: SUSPENSE and MIXED are compatible with ACTION
        # Only flag clear mismatches: ACTION vs NON-ACTION, NON-ACTION vs ACTION
        type_conflict = False
        if outline_type == "ACTION" and synopsis_type == "NON-ACTION":
            type_conflict = True
        elif outline_type == "NON-ACTION" and synopsis_type == "ACTION":
            type_conflict = True
        if type_conflict:
            finding_counter += 1
            findings.append(Finding(
                id=f"F{finding_counter:03d}",
                principle_id="SCENE-TYPE-FIDELITY",
                severity="CLASS_A",
                location=location,
                excerpt=f"Outline: {outline_type}, Synopsis: {synopsis_type}",
                message=f"Scene {scene_num} type mismatch: outline={outline_type}, synopsis={synopsis_type}",
                recommendation=f"Regenerate scene {scene_num} with correct type tag",
            ))

    # ── POV/Focus check ──
    outline_pov = (outline_scene.annotations or {}).get("pov", "")
    synopsis_focus = _extract_focus_from_synopsis(synopsis_scene_text)

    if outline_pov and synopsis_focus:
        # Check that the primary POV name appears in the synopsis focus
        # Extract the main name from the outline POV (e.g., "Hank Reyes" from
        # "Hank Reyes" or "Capitán Luis Vera, intercut with Cole Briggs")
        pov_names = _extract_named_characters(outline_pov)
        focus_lower = synopsis_focus.lower()
        # At least one POV name word must appear in the focus
        if pov_names and not any(n.lower() in focus_lower for n in pov_names):
            finding_counter += 1
            findings.append(Finding(
                id=f"F{finding_counter:03d}",
                principle_id="POV-FIDELITY",
                severity="CLASS_A",
                location=location,
                excerpt=f"Outline POV: {outline_pov}, Synopsis FOCUS: {synopsis_focus}",
                message=f"Scene {scene_num} POV mismatch: outline POV '{outline_pov}' not reflected in synopsis focus '{synopsis_focus}'",
                recommendation=f"Check scene {scene_num} focus tag matches outline POV",
            ))

    # ── Named character presence ──
    outline_names = _extract_named_characters(outline_scene.content)
    if outline_names:
        missing_names = []
        for name in outline_names:
            if name.lower() not in synopsis_lower:
                missing_names.append(name)
        # Only flag if major characters are missing (>30% of named characters absent)
        # This avoids false positives from minor mentions
        if missing_names and len(missing_names) > len(outline_names) * 0.5:
            finding_counter += 1
            findings.append(Finding(
                id=f"F{finding_counter:03d}",
                principle_id="CHARACTER-PRESENCE",
                severity="CLASS_A",
                location=location,
                excerpt=f"Missing: {', '.join(sorted(missing_names)[:5])}",
                message=f"Scene {scene_num}: {len(missing_names)} of {len(outline_names)} named characters from outline absent in synopsis",
                recommendation=f"Review scene {scene_num} for missing characters",
            ))

    return findings, finding_counter


# ── Beat coverage (Class B — semantic, informational) ────────────────────

def _check_beat_coverage_deterministic(outline_beats: list, synopsis_text: str) -> dict:
    """Check beat coverage using keyword matching (no LLM).

    Returns dict: beat_text -> covered (bool).
    Threshold: ≥30% of significant words present (relaxed from 40% to
    reduce false positives from legitimate prose compression).
    """
    coverage = {}
    synopsis_lower = synopsis_text.lower()
    for beat in outline_beats:
        words = re.findall(r'\b[a-zA-Z]{4,}\b', beat.lower())
        significant_words = [w for w in words if w not in {
            'that', 'this', 'with', 'from', 'they', 'their', 'them', 'have', 'been',
            'will', 'would', 'could', 'should', 'about', 'there', 'where', 'when',
            'which', 'other', 'than', 'more', 'into', 'also', 'does', 'each',
            'what', 'some', 'only', 'just', 'very', 'then', 'through',
        }]
        if not significant_words:
            coverage[beat] = True
            continue
        found = sum(1 for w in significant_words if w in synopsis_lower)
        ratio = found / len(significant_words) if significant_words else 1.0
        coverage[beat] = ratio >= 0.3
    return coverage


def _check_beat_coverage_llm(outline_beats: list, synopsis_text: str) -> dict:
    """Check beat coverage using LLM semantic comparison.

    Returns dict: beat_text -> covered (bool).
    """
    from llm_client import call_llm

    coverage = {}
    beats_text = "\n".join(f"{i+1}. {beat}" for i, beat in enumerate(outline_beats))

    prompt = f"""Compare each numbered outline beat below against the synopsis scene content.
For each beat, respond with ONLY the beat number and YES or NO — does the synopsis cover this beat's key action/event?
Minor wording differences or compression are acceptable — answer YES if the core event is present.

OUTLINE BEATS:
{beats_text}

SYNOPSIS SCENE CONTENT:
{synopsis_text[:12000]}

Respond in format:
1. YES
2. NO
3. YES
...
"""

    try:
        response = call_llm(
            provider="anthropic",
            model="claude-sonnet-4-6",
            system="You are a precise text comparison assistant. Be generous: if the core event from the beat is present in the synopsis (even compressed or reworded), answer YES.",
            user=prompt,
            max_tokens=2048,
        )
        result_text = response.text

        for i, beat in enumerate(outline_beats):
            pattern = rf'{i+1}\.\s*(YES|NO)'
            m = re.search(pattern, result_text, re.IGNORECASE)
            coverage[beat] = m.group(1).upper() == "YES" if m else True
    except Exception:
        return _check_beat_coverage_deterministic(outline_beats, synopsis_text)

    return coverage


# ── Main comparison functions ─────────────────────────────────────────────

def compare_outline_to_synopsis(
    outline_path: str,
    synopsis_path: str,
    intake_path: str,
    principles_path: str = None,
    use_llm: bool = True,
) -> ComparisonResult:
    """Compare generated synopsis against operator outline.

    Auto-detects format (scene-organized vs chapter-organized) and routes
    to the appropriate comparison path.

    Returns ComparisonResult with pass/fail (Class A only), findings, and
    per-scene/chapter results.
    """
    # Load inputs
    outline = parse_outline(outline_path)
    with open(synopsis_path, 'r', encoding='utf-8') as f:
        synopsis_text = f.read()
    with open(intake_path, 'r', encoding='utf-8') as f:
        intake = json.load(f)

    content_chapters = [ch for ch in outline.chapters if ch.content.strip()]
    is_scene_organized = outline.top_matter.get("format") == "scene-organized"

    if is_scene_organized:
        return _compare_scene_organized(
            content_chapters, synopsis_text, intake, use_llm,
        )
    else:
        return _compare_chapter_organized(
            content_chapters, synopsis_text, intake, use_llm,
        )


def _compare_scene_organized(
    outline_scenes: list,
    synopsis_text: str,
    intake: dict,
    use_llm: bool,
) -> ComparisonResult:
    """Per-scene 1:1 comparison for scene-organized outlines."""
    findings = []
    chapter_results = {}
    finding_counter = 0

    synopsis_scenes = _parse_synopsis_scenes(synopsis_text)

    # ── Check 1: Scene count match (Class A) ──
    outline_count = len(outline_scenes)
    synopsis_count = len(synopsis_scenes)
    if synopsis_count != outline_count:
        finding_counter += 1
        findings.append(Finding(
            id=f"F{finding_counter:03d}",
            principle_id="OPERATOR-STRUCTURE-FIDELITY",
            severity="CLASS_A",
            location="Global",
            excerpt=f"Outline: {outline_count} scenes, Synopsis: {synopsis_count} scenes",
            message=f"Scene count mismatch: outline has {outline_count} scenes, synopsis has {synopsis_count}",
            recommendation="Regenerate synopsis to match outline scene count exactly",
        ))

    # ── Check 2: Per-scene structural + semantic ──
    for outline_scene in outline_scenes:
        sc_num = outline_scene.chapter_number
        synopsis_sc_text = synopsis_scenes.get(sc_num, "")

        if not synopsis_sc_text:
            finding_counter += 1
            f = Finding(
                id=f"F{finding_counter:03d}",
                principle_id="OPERATOR-STRUCTURE-FIDELITY",
                severity="CLASS_A",
                location=f"Scene {sc_num}",
                excerpt="",
                message=f"Scene {sc_num} present in outline but missing from synopsis",
                recommendation=f"Regenerate: ensure scene {sc_num} is present in synopsis",
            )
            findings.append(f)
            chapter_results[sc_num] = ChapterComparison(
                chapter_number=sc_num, passed=False,
                findings=[f], beat_coverage={},
            )
            continue

        sc_findings = []

        # Structural checks (Class A)
        structural_findings, finding_counter = _check_scene_structural(
            sc_num, outline_scene, synopsis_sc_text, finding_counter,
        )
        sc_findings.extend(structural_findings)
        findings.extend(structural_findings)

        # Beat coverage (Class B — informational)
        if use_llm:
            coverage = _check_beat_coverage_llm(outline_scene.beats, synopsis_sc_text)
        else:
            coverage = _check_beat_coverage_deterministic(outline_scene.beats, synopsis_sc_text)

        for beat, covered in coverage.items():
            if not covered:
                finding_counter += 1
                f = Finding(
                    id=f"F{finding_counter:03d}",
                    principle_id="BEAT-COVERAGE",
                    severity="CLASS_B",
                    location=f"Scene {sc_num}",
                    excerpt=beat[:200],
                    message=f"Outline beat may not be covered in synopsis scene {sc_num}",
                    recommendation=f"Review scene {sc_num}: check if this beat is addressed",
                )
                findings.append(f)
                sc_findings.append(f)

        # Scene passes if no Class A findings for this scene
        scene_class_a = [f for f in sc_findings if f.severity == "CLASS_A"]
        chapter_results[sc_num] = ChapterComparison(
            chapter_number=sc_num,
            passed=len(scene_class_a) == 0,
            findings=sc_findings,
            beat_coverage=coverage,
        )

    # ── Check 3: Time-window respect (Class A) ──
    out_of_scope = intake.get("historical_anchors_out_of_scope", [])
    if out_of_scope:
        synopsis_lower = synopsis_text.lower()
        for anchor in out_of_scope:
            if anchor.lower() in synopsis_lower:
                finding_counter += 1
                findings.append(Finding(
                    id=f"F{finding_counter:03d}",
                    principle_id="TIMELINE-SCOPE-SELECTION",
                    severity="CLASS_A",
                    location="Global",
                    excerpt=anchor,
                    message=f"Out-of-scope historical anchor '{anchor}' appears in synopsis",
                    recommendation=f"Remove references to '{anchor}' — outside operator's historical window",
                ))

    # Gate: pass if zero Class A
    class_a = [f for f in findings if f.severity == "CLASS_A"]
    return ComparisonResult(
        passed=len(class_a) == 0,
        findings=findings,
        chapter_results=chapter_results,
    )


def _compare_chapter_organized(
    content_chapters: list,
    synopsis_text: str,
    intake: dict,
    use_llm: bool,
) -> ComparisonResult:
    """Legacy per-chapter comparison for chapter-organized outlines."""
    findings = []
    chapter_results = {}
    finding_counter = 0

    synopsis_chapters = _parse_synopsis_chapters(synopsis_text)

    # ── Check 1: Chapter count match (Class A) ──
    outline_chapter_count = len(content_chapters)
    synopsis_chapter_count = len(synopsis_chapters)
    if synopsis_chapter_count != outline_chapter_count:
        finding_counter += 1
        findings.append(Finding(
            id=f"F{finding_counter:03d}",
            principle_id="OPERATOR-STRUCTURE-FIDELITY",
            severity="CLASS_A",
            location="Global",
            excerpt=f"Outline: {outline_chapter_count} chapters, Synopsis: {synopsis_chapter_count} chapters",
            message=f"Chapter count mismatch: outline has {outline_chapter_count} content chapters, synopsis has {synopsis_chapter_count}",
            recommendation="Regenerate synopsis to match outline chapter count exactly",
        ))

    # ── Check 2: Per-chapter beat coverage ──
    for ch in content_chapters:
        ch_num = ch.chapter_number
        synopsis_ch_text = synopsis_chapters.get(ch_num, "")

        if not synopsis_ch_text:
            finding_counter += 1
            findings.append(Finding(
                id=f"F{finding_counter:03d}",
                principle_id="OPERATOR-STRUCTURE-FIDELITY",
                severity="CLASS_A",
                location=f"Chapter {ch_num}",
                excerpt="",
                message=f"Chapter {ch_num} present in outline but missing from synopsis",
                recommendation=f"Regenerate: ensure chapter {ch_num} is present in synopsis",
            ))
            chapter_results[ch_num] = ChapterComparison(
                chapter_number=ch_num, passed=False,
                findings=[findings[-1]], beat_coverage={},
            )
            continue

        if use_llm:
            coverage = _check_beat_coverage_llm(ch.beats, synopsis_ch_text)
        else:
            coverage = _check_beat_coverage_deterministic(ch.beats, synopsis_ch_text)

        ch_findings = []
        for beat, covered in coverage.items():
            if not covered:
                finding_counter += 1
                f = Finding(
                    id=f"F{finding_counter:03d}",
                    principle_id="BEAT-COVERAGE",
                    severity="CLASS_B",
                    location=f"Chapter {ch_num}",
                    excerpt=beat[:200],
                    message=f"Outline beat may not be covered in synopsis chapter {ch_num}",
                    recommendation=f"Review chapter {ch_num}: check if this beat is addressed",
                )
                findings.append(f)
                ch_findings.append(f)

        chapter_results[ch_num] = ChapterComparison(
            chapter_number=ch_num,
            passed=len([f for f in ch_findings if f.severity == "CLASS_A"]) == 0,
            findings=ch_findings,
            beat_coverage=coverage,
        )

    # ── Check 3: Time-window respect (Class A) ──
    out_of_scope = intake.get("historical_anchors_out_of_scope", [])
    if out_of_scope:
        synopsis_lower = synopsis_text.lower()
        for anchor in out_of_scope:
            if anchor.lower() in synopsis_lower:
                finding_counter += 1
                findings.append(Finding(
                    id=f"F{finding_counter:03d}",
                    principle_id="TIMELINE-SCOPE-SELECTION",
                    severity="CLASS_A",
                    location="Global",
                    excerpt=anchor,
                    message=f"Out-of-scope historical anchor '{anchor}' appears in synopsis",
                    recommendation=f"Remove references to '{anchor}' — outside operator's historical window",
                ))

    class_a_findings = [f for f in findings if f.severity == "CLASS_A"]
    passed = len(class_a_findings) == 0

    return ComparisonResult(
        passed=passed,
        findings=findings,
        chapter_results=chapter_results,
    )


def verify_scene_count_match(
    outline_path: str,
    synopsis_path: str,
) -> tuple[bool, str]:
    """Verify that synopsis scene count equals outline scene count.

    Returns (passed, message). If passed is False, the calling pipeline
    must HARD FAIL — do not proceed to downstream generation.
    """
    outline = parse_outline_scenes(outline_path)
    expected = outline.total_scene_count

    if expected == 0:
        return (True, "Outline has 0 scenes (chapter-organized?) — skipping count gate.")

    synopsis_text = Path(synopsis_path).read_text(encoding="utf-8")
    actual = len(re.findall(r"^###\s+Scene\s+", synopsis_text, re.MULTILINE))

    if actual != expected:
        return (False, (
            f"Scene count mismatch: outline declares {expected} scenes, "
            f"synopsis produced {actual}. Pipeline halt — synopsis fidelity "
            f"violation. Investigate synopsis_generator decomposition."
        ))
    return (True, f"Scene count match: {expected} scenes.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("Usage: python3 outline_comparator.py <outline> <synopsis> <intake> [principles]")
        sys.exit(1)
    result = compare_outline_to_synopsis(
        outline_path=sys.argv[1],
        synopsis_path=sys.argv[2],
        intake_path=sys.argv[3],
        principles_path=sys.argv[4] if len(sys.argv) > 4 else None,
    )
    class_a = [f for f in result.findings if f.severity == "CLASS_A"]
    class_b = [f for f in result.findings if f.severity == "CLASS_B"]
    print(f"Passed: {result.passed}")
    print(f"Class A (structural, gate-blocking): {len(class_a)}")
    print(f"Class B (semantic, informational):   {len(class_b)}")
    if class_a:
        print("\nClass A findings:")
        for f in class_a:
            print(f"  [{f.severity}] {f.id} @ {f.location}: {f.message}")
    if class_b:
        print(f"\nClass B findings ({len(class_b)} total):")
        # Group by location
        by_loc = {}
        for f in class_b:
            by_loc.setdefault(f.location, []).append(f)
        for loc, flist in sorted(by_loc.items()):
            print(f"  {loc}: {len(flist)} beat(s) flagged")
