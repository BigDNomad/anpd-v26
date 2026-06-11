"""
MA-011: Cross-Scene Duplication Detector

Detects verbatim contiguous text duplication across scene boundaries.
Deterministic, mechanical, no LLM calls. Pure Python stdlib.

Catches:
  - Class A: contiguous matches >= 40 words (blocks publication)
  - Class B: contiguous matches 25-39 words (warning, non-blocking)

Does NOT catch beat-level retells (different prose, same events) — that
requires LLM judgment and is scoped to S-1b.

Algorithm: Rabin-Karp rolling hash at k=25, extend matches to maximal,
deduplicate overlaps, severity by word count.

See: ANPD_V25_Check_Module_Spec_S1_cross_scene_duplication_20260528_T1400.md
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Configuration defaults ────────────────────────────────────────────────

DEFAULT_CLASS_A_THRESHOLD = 40
DEFAULT_CLASS_B_THRESHOLD = 25
DEFAULT_PAIR_WINDOW = 2

# Rabin-Karp parameters
RK_BASE = 257
RK_MOD = (1 << 61) - 1  # Mersenne prime — collision probability ~10^-18 per pair


# ── Normalization (spec §4.1) ─────────────────────────────────────────────

# Matches markdown structural marks to strip
_MD_STRUCTURAL = re.compile(
    r"^(?:#{1,6}\s.*|>\s.*|\*{3}|\*{2,}.*?\*{2,})$",
    re.MULTILINE,
)
# Non-word, non-whitespace characters
_NON_WORD_SPACE = re.compile(r"[^\w\s]", re.UNICODE)
# Whitespace runs
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text(text: str) -> tuple[list[str], list[int]]:
    """Normalize text per spec §4.1.

    Returns:
        words: list of normalized words
        word_to_line: for each word index, the 1-based line number in the
                      original text where that word appears
    """
    lines = text.split("\n")
    words: list[str] = []
    word_to_line: list[int] = []

    for line_idx, line in enumerate(lines):
        # Step 1: skip markdown structural lines
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith(">") or stripped == "***" or stripped == "**":
            continue
        # Also skip lines that are purely bold markers
        if re.match(r"^\*{2,}[^*]+\*{2,}$", stripped):
            continue

        # Step 2: lowercase
        processed = line.lower()
        # Step 3: replace non-word non-whitespace with spaces
        processed = _NON_WORD_SPACE.sub(" ", processed)
        # Step 4: collapse whitespace
        processed = _WHITESPACE_RUN.sub(" ", processed).strip()
        # Step 5: tokenize
        if processed:
            line_words = processed.split()
            for w in line_words:
                words.append(w)
                word_to_line.append(line_idx + 1)  # 1-based

    return words, word_to_line


# ── Rabin-Karp rolling hash (spec §4.2) ──────────────────────────────────

def _rk_hash(words: list[str], start: int, k: int) -> int:
    """Compute Rabin-Karp hash for words[start:start+k]."""
    h = 0
    for i in range(start, start + k):
        h = (h * RK_BASE + hash(words[i])) % RK_MOD
    return h


def _rk_pow_k(k: int) -> int:
    """Precompute RK_BASE^(k-1) mod RK_MOD for rolling."""
    return pow(RK_BASE, k - 1, RK_MOD)


def build_kgram_index(words: list[str], k: int) -> dict[int, list[int]]:
    """Build hash -> [start_positions] index of all k-grams in words."""
    if len(words) < k:
        return {}

    index: dict[int, list[int]] = {}
    h = _rk_hash(words, 0, k)
    index.setdefault(h, []).append(0)

    pow_k = _rk_pow_k(k)
    for i in range(1, len(words) - k + 1):
        # Roll: remove words[i-1], add words[i+k-1]
        h = (h * RK_BASE - hash(words[i - 1]) * pow_k * RK_BASE + hash(words[i + k - 1])) % RK_MOD
        index.setdefault(h, []).append(i)

    return index


# ── Match detection and extension (spec §4.2) ────────────────────────────

@dataclass
class Match:
    """A maximal contiguous match between two scenes."""
    scene_a_start: int   # word index in scene A
    scene_b_start: int   # word index in scene B
    length: int          # match length in words

    @property
    def scene_a_end(self) -> int:
        return self.scene_a_start + self.length

    @property
    def scene_b_end(self) -> int:
        return self.scene_b_start + self.length


def find_maximal_matches(
    words_a: list[str],
    words_b: list[str],
    k: int,
) -> list[Match]:
    """Find all maximal contiguous matches of length >= k between two word lists.

    Uses Rabin-Karp to find k-gram seed matches, extends to maximal,
    then deduplicates to keep only maximal matches.
    """
    if len(words_a) < k or len(words_b) < k:
        return []

    # Build index on scene A
    index_a = build_kgram_index(words_a, k)

    # Walk scene B, find seed matches
    raw_matches: list[Match] = []
    seen_seeds: set[tuple[int, int]] = set()  # (a_start, b_start) seeds already processed

    h_b = _rk_hash(words_b, 0, k)
    pow_k = _rk_pow_k(k)

    for b_pos in range(len(words_b) - k + 1):
        if b_pos > 0:
            h_b = (h_b * RK_BASE - hash(words_b[b_pos - 1]) * pow_k * RK_BASE + hash(words_b[b_pos + k - 1])) % RK_MOD

        if h_b not in index_a:
            continue

        for a_pos in index_a[h_b]:
            # Literal comparison to confirm (spec §4.2 — defensive against collisions)
            if words_a[a_pos:a_pos + k] != words_b[b_pos:b_pos + k]:
                continue

            # Skip if this seed is subsumed by an already-found match
            seed_key = (a_pos, b_pos)
            if seed_key in seen_seeds:
                continue
            seen_seeds.add(seed_key)

            # Extend match in both directions for maximality
            start_a, start_b = a_pos, b_pos
            end_a, end_b = a_pos + k, b_pos + k

            # Extend backward
            while start_a > 0 and start_b > 0 and words_a[start_a - 1] == words_b[start_b - 1]:
                start_a -= 1
                start_b -= 1

            # Extend forward
            while end_a < len(words_a) and end_b < len(words_b) and words_a[end_a] == words_b[end_b]:
                end_a += 1
                end_b += 1

            length = end_a - start_a
            raw_matches.append(Match(
                scene_a_start=start_a,
                scene_b_start=start_b,
                length=length,
            ))

    # Deduplicate: keep only maximal matches (spec §4.2 step 5)
    # A match M1 is subsumed by M2 if M1's range in both A and B is
    # contained within M2's range.
    if not raw_matches:
        return []

    # Sort by length descending for efficient subsumption check
    raw_matches.sort(key=lambda m: m.length, reverse=True)
    maximal: list[Match] = []

    for candidate in raw_matches:
        subsumed = False
        for keeper in maximal:
            if (candidate.scene_a_start >= keeper.scene_a_start and
                candidate.scene_a_end <= keeper.scene_a_end and
                candidate.scene_b_start >= keeper.scene_b_start and
                candidate.scene_b_end <= keeper.scene_b_end):
                subsumed = True
                break
        if not subsumed:
            maximal.append(candidate)

    return maximal


# ── Scene splitting for Mode B (spec §6.2) ────────────────────────────────

_SCENE_BREAK_RE = re.compile(
    r"^(?:\*{3}|#\s+(?:Scene|Chapter)\s+\d+)",
    re.MULTILINE | re.IGNORECASE,
)


def split_assembled_manuscript(text: str) -> list[tuple[str, int]]:
    """Split an assembled manuscript into scenes on break markers.

    Returns list of (scene_text, start_line_1based).
    """
    lines = text.split("\n")
    scenes: list[tuple[str, int]] = []
    current_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "***" or re.match(r"^#\s+(?:Scene|Chapter)\s+\d+", stripped, re.IGNORECASE):
            if i > current_start:
                scene_text = "\n".join(lines[current_start:i])
                if scene_text.strip():
                    scenes.append((scene_text, current_start + 1))
            current_start = i + 1

    # Last segment
    if current_start < len(lines):
        scene_text = "\n".join(lines[current_start:])
        if scene_text.strip():
            scenes.append((scene_text, current_start + 1))

    return scenes


# ── Configuration loading (spec §9) ──────────────────────────────────────

def _load_config(briefs: BriefBundle) -> dict:
    """Load cross_scene_duplication config from book_config, with defaults."""
    defaults = {
        "class_a_threshold_words": DEFAULT_CLASS_A_THRESHOLD,
        "class_b_threshold_words": DEFAULT_CLASS_B_THRESHOLD,
        "pair_window": DEFAULT_PAIR_WINDOW,
        "enabled": True,
    }
    book_cfg = getattr(briefs, "book_config", None)
    if book_cfg and isinstance(book_cfg, dict):
        overrides = book_cfg.get("cross_scene_duplication", {})
        if isinstance(overrides, dict):
            for key in defaults:
                if key in overrides:
                    defaults[key] = overrides[key]
    return defaults


# ── Workspace output (spec §7) ───────────────────────────────────────────

def _ensure_workspace(manuscript_dir: str) -> str:
    """Create and return workspace directory for match files."""
    ws = os.path.join(manuscript_dir, "_auditor_workspace", "duplication_matches")
    os.makedirs(ws, exist_ok=True)
    return ws


def _write_match_file(ws_dir: str, scene_a_id: str, scene_b_id: str,
                       matched_text: str) -> str:
    """Write full matched text to workspace file. Returns the path."""
    filename = f"match_{scene_a_id}_{scene_b_id}.txt"
    path = os.path.join(ws_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(matched_text)
    return path


def _write_run_report(manuscript_dir: str, report_data: dict) -> str:
    """Write the run report JSON."""
    ws = os.path.join(manuscript_dir, "_auditor_workspace")
    os.makedirs(ws, exist_ok=True)
    path = os.path.join(ws, "cross_scene_duplication_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    return path


# ── Check module class ────────────────────────────────────────────────────

class CrossSceneDuplication:
    check_id = "MA-011-cross-scene-duplication"
    severity = "CLASS_A"
    description = (
        "Cross-scene duplication detector: verbatim contiguous text "
        "duplications across adjacent and near-adjacent scenes"
    )

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        cfg = _load_config(briefs)

        if not cfg["enabled"]:
            print("    MA-011: disabled by config", file=sys.stderr)
            return []

        class_a_thresh = cfg["class_a_threshold_words"]
        class_b_thresh = cfg["class_b_threshold_words"]
        pair_window = cfg["pair_window"]

        start_time = time.time()
        findings: list[Finding] = []

        # Get scenes in order
        scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)
        if len(scenes) < 2:
            print("    MA-011: <2 scenes, nothing to compare", file=sys.stderr)
            return []

        # Normalize all scenes
        normalized: list[tuple[list[str], list[int]]] = []
        for scene in scenes:
            words, word_to_line = normalize_text(scene.text)
            normalized.append((words, word_to_line))

        # Generate pairs per spec §5
        pairs_compared = 0
        total_class_a = 0
        total_class_b = 0
        longest_match = 0

        ws_dir = _ensure_workspace(manuscript.manuscript_dir)

        for i in range(len(scenes)):
            for offset in range(1, pair_window + 1):
                j = i + offset
                if j >= len(scenes):
                    continue

                pairs_compared += 1
                words_a, wtl_a = normalized[i]
                words_b, wtl_b = normalized[j]

                matches = find_maximal_matches(words_a, words_b, class_b_thresh)

                for match in matches:
                    if match.length < class_b_thresh:
                        continue

                    if match.length >= class_a_thresh:
                        severity = "CLASS_A"
                        total_class_a += 1
                    else:
                        severity = "CLASS_B"
                        total_class_b += 1

                    if match.length > longest_match:
                        longest_match = match.length

                    scene_a = scenes[i]
                    scene_b = scenes[j]
                    scene_a_id = f"sc_{scene_a.scene_number:03d}"
                    scene_b_id = f"sc_{scene_b.scene_number:03d}"

                    # Reconstruct matched text from scene A's words
                    matched_words = words_a[match.scene_a_start:match.scene_a_end]
                    matched_text = " ".join(matched_words)
                    preview = matched_text[:200]

                    # Line ranges in the scene text
                    line_a_start = wtl_a[match.scene_a_start] if match.scene_a_start < len(wtl_a) else 0
                    line_a_end = wtl_a[match.scene_a_end - 1] if match.scene_a_end - 1 < len(wtl_a) else 0
                    line_b_start = wtl_b[match.scene_b_start] if match.scene_b_start < len(wtl_b) else 0
                    line_b_end = wtl_b[match.scene_b_end - 1] if match.scene_b_end - 1 < len(wtl_b) else 0

                    # Write full match text to workspace
                    match_path = _write_match_file(
                        ws_dir, scene_a_id, scene_b_id, matched_text
                    )

                    findings.append(Finding(
                        check_id=self.check_id,
                        severity=severity,
                        scene_number=None,
                        scene_numbers=[scene_a.scene_number, scene_b.scene_number],
                        description=(
                            f"Verbatim duplication: {match.length} words between "
                            f"{scene_a_id} (lines {line_a_start}-{line_a_end}) and "
                            f"{scene_b_id} (lines {line_b_start}-{line_b_end})"
                        ),
                        evidence=[
                            f"Match length: {match.length} words",
                            f"Preview: {preview}",
                            f"Full match: {match_path}",
                        ],
                        suggested_fix=(
                            "Review both scenes. Delete the weaker version of the "
                            "duplicated passage. Per punch list convention, the later "
                            "occurrence is usually the stronger version."
                        ),
                    ))

        elapsed = time.time() - start_time

        # Write run report (spec §7.3)
        _write_run_report(manuscript.manuscript_dir, {
            "check_id": "MA-011",
            "scene_count": len(scenes),
            "pairs_compared": pairs_compared,
            "class_a_findings": total_class_a,
            "class_b_findings": total_class_b,
            "longest_match_words": longest_match,
            "wall_time_seconds": round(elapsed, 2),
        })

        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        class_b = sum(1 for f in findings if f.severity == "CLASS_B")
        print(f"    MA-011: {pairs_compared} pairs, {len(findings)} findings "
              f"(A:{class_a} B:{class_b}), longest {longest_match}w, "
              f"{elapsed:.1f}s", file=sys.stderr)

        return findings
