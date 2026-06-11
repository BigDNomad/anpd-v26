"""
Timeline Extractor — per-scene elapsed-time estimation.

Reads manuscript scene by scene, extracts time anchors (explicit dates,
durations, day/night cycles, temporal phrases), and produces an ordered
list of (scene_number, estimated_elapsed_days) tuples from book start.

Used by MA-001 to determine whether contradictions between scenes are
plausible given the elapsed time between them.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

from audit_checks import ManuscriptArtifact, SceneText


# ── Constants ────────────────────────────────────────────────────────────────

SONNET_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 10  # scenes per extraction call — timeline is lighter than detail extraction
MAX_RETRIES = 2

TIMELINE_SYSTEM = """You are a timeline analyst for a novel manuscript. Your job is to extract time anchors from each scene.

For each scene, identify:
1. EXPLICIT dates or times ("March 15", "2:00 AM", "midnight")
2. RELATIVE time references ("the next morning", "two days later", "that evening", "after midnight")
3. DAY/NIGHT indicators ("the sun was setting", "dawn", "darkness", "afternoon light")
4. DURATION references ("three hours", "forty-eight hours", "eighteen months ago")
5. CONTINUITY markers ("still", "continued", "resumed" — suggesting same timeframe as previous scene)

Output one JSON object per scene on its own line:
{"scene_number": N, "anchors": ["anchor text 1", "anchor text 2"], "time_of_day": "morning|afternoon|evening|night|unknown", "relation_to_previous": "same_moment|same_day|next_day|days_later|weeks_later|unknown", "estimated_hours_since_previous": N_or_null}

If a scene has no time indicators at all, still output the object with empty anchors and "unknown" values.
Output JSON objects only, nothing else."""

TIMELINE_PROMPT = """Extract time anchors from these manuscript scenes. Focus on temporal indicators that help establish when each scene occurs relative to the others.

SCENES:
{scenes_block}

Output one JSON object per line."""


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class SceneTimeline:
    """Timeline data for a single scene."""
    scene_number: int
    anchors: list[str] = field(default_factory=list)
    time_of_day: str = "unknown"
    relation_to_previous: str = "unknown"
    estimated_hours_since_previous: float | None = None
    estimated_elapsed_days: float = 0.0


# ── Deterministic pre-extraction ─────────────────────────────────────────────

# Patterns for common temporal phrases
_TIME_PATTERNS = [
    (re.compile(r'\b(?:the\s+)?next\s+morning\b', re.I), "next_day", 12.0),
    (re.compile(r'\b(?:the\s+)?following\s+(?:day|morning)\b', re.I), "next_day", 24.0),
    (re.compile(r'\b(?:the\s+)?next\s+day\b', re.I), "next_day", 24.0),
    (re.compile(r'\btwo\s+days?\s+later\b', re.I), "days_later", 48.0),
    (re.compile(r'\bthree\s+days?\s+later\b', re.I), "days_later", 72.0),
    (re.compile(r'\ba\s+week\s+later\b', re.I), "weeks_later", 168.0),
    (re.compile(r'\btwo\s+weeks?\s+later\b', re.I), "weeks_later", 336.0),
    (re.compile(r'\ba\s+month\s+later\b', re.I), "weeks_later", 720.0),
    (re.compile(r'\bthat\s+(?:same\s+)?(?:evening|night|afternoon|morning)\b', re.I), "same_day", 4.0),
    (re.compile(r'\blater\s+that\s+(?:evening|night|afternoon|day)\b', re.I), "same_day", 4.0),
    (re.compile(r'\bafter\s+midnight\b', re.I), "same_day", 2.0),
    (re.compile(r'\bmidnight\b', re.I), "same_day", 0.0),
    (re.compile(r'\b(?:at\s+)?dawn\b', re.I), "same_day", 0.0),
    (re.compile(r'\bforty-?eight\s+hours\b', re.I), "days_later", 48.0),
    (re.compile(r'\btwenty-?four\s+hours\b', re.I), "next_day", 24.0),
    (re.compile(r'\bseveral\s+(?:days|hours)\s+later\b', re.I), "days_later", 72.0),
]

_TOD_PATTERNS = [
    (re.compile(r'\b(?:morning|dawn|sunrise|breakfast)\b', re.I), "morning"),
    (re.compile(r'\b(?:afternoon|midday|noon|lunch)\b', re.I), "afternoon"),
    (re.compile(r'\b(?:evening|sunset|dusk|dinner)\b', re.I), "evening"),
    (re.compile(r'\b(?:night|midnight|dark(?:ness)?|after\s+midnight)\b', re.I), "night"),
]


def _deterministic_time_extract(scene: SceneText) -> dict:
    """Fast regex-based time extraction — supplements LLM."""
    text = scene.text[:2000]  # First 2000 chars usually contain time setting
    anchors = []
    relation = "unknown"
    hours = None
    tod = "unknown"

    for pattern, rel, hrs in _TIME_PATTERNS:
        m = pattern.search(text)
        if m:
            anchors.append(m.group(0))
            if relation == "unknown":
                relation = rel
                hours = hrs

    for pattern, time_of_day in _TOD_PATTERNS:
        if pattern.search(text):
            tod = time_of_day
            break

    return {
        "anchors": anchors,
        "time_of_day": tod,
        "relation_to_previous": relation,
        "estimated_hours_since_previous": hours,
    }


# ── LLM helper ──────────────────────────────────────────────────────────────

def _call_llm(system: str, user: str, model: str = SONNET_MODEL) -> str:
    """Call LLM via the pipeline's llm_client."""
    pipeline_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from llm_client import call_llm
    response = call_llm(
        provider="anthropic",
        model=model,
        system=system,
        user=user,
        max_tokens=4096,
        temperature=0.0,
    )
    return response.text


# ── LLM extraction ──────────────────────────────────────────────────────────

def _extract_timeline_batch(scenes_block: str) -> list[dict]:
    """Extract timeline data from a batch of scenes via LLM."""
    prompt = TIMELINE_PROMPT.format(scenes_block=scenes_block)
    response = _call_llm(TIMELINE_SYSTEM, prompt)

    results = []
    for line in response.strip().splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            results.append(obj)
        except json.JSONDecodeError:
            continue
    return results


# ── Main extraction pipeline ─────────────────────────────────────────────────

def extract_timeline(manuscript: ManuscriptArtifact, use_llm: bool = True) -> list[SceneTimeline]:
    """Extract timeline for all scenes in the manuscript.

    Returns an ordered list of SceneTimeline objects with estimated_elapsed_days
    from the book start.

    Args:
        manuscript: The manuscript to analyze.
        use_llm: If True, uses LLM for richer extraction. If False, uses only
                 deterministic regex patterns (faster, for testing).
    """
    scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)
    if not scenes:
        return []

    # Phase 1: Deterministic extraction for all scenes
    det_data: dict[int, dict] = {}
    for scene in scenes:
        det_data[scene.scene_number] = _deterministic_time_extract(scene)

    # Phase 2: LLM extraction (if enabled)
    llm_data: dict[int, dict] = {}
    if use_llm:
        for i in range(0, len(scenes), BATCH_SIZE):
            batch = scenes[i:i + BATCH_SIZE]
            scenes_block = "\n\n".join(
                f"--- SCENE {s.scene_number} ---\n{s.text[:2000]}"
                for s in batch
            )
            for attempt in range(MAX_RETRIES + 1):
                try:
                    results = _extract_timeline_batch(scenes_block)
                    for r in results:
                        sn = r.get("scene_number", 0)
                        if sn:
                            llm_data[sn] = r
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES:
                        time.sleep(5 * (attempt + 1))
                        continue
                    print(f"    WARN: timeline extraction failed for scenes "
                          f"{batch[0].scene_number}-{batch[-1].scene_number}: {e}",
                          file=sys.stderr)

    # Phase 3: Merge deterministic + LLM data, compute elapsed days
    timelines: list[SceneTimeline] = []
    cumulative_days = 0.0

    for idx, scene in enumerate(scenes):
        sn = scene.scene_number
        det = det_data.get(sn, {})
        llm = llm_data.get(sn, {})

        # Merge: prefer LLM data, fall back to deterministic
        anchors = llm.get("anchors", []) or det.get("anchors", [])
        tod = llm.get("time_of_day", "unknown")
        if tod == "unknown":
            tod = det.get("time_of_day", "unknown")
        relation = llm.get("relation_to_previous", "unknown")
        if relation == "unknown":
            relation = det.get("relation_to_previous", "unknown")
        hours = llm.get("estimated_hours_since_previous")
        if hours is None:
            hours = det.get("estimated_hours_since_previous")

        # Compute elapsed days from start
        if idx == 0:
            cumulative_days = 0.0
        else:
            if hours is not None:
                cumulative_days += hours / 24.0
            else:
                # Default: assume scenes without time info are same day
                # unless relation suggests otherwise
                if relation == "next_day":
                    cumulative_days += 1.0
                elif relation == "days_later":
                    cumulative_days += 3.0
                elif relation == "weeks_later":
                    cumulative_days += 14.0
                elif relation == "same_day" or relation == "same_moment":
                    cumulative_days += 0.0
                else:
                    # Unknown: assume minimal progression (half day)
                    cumulative_days += 0.5

        timelines.append(SceneTimeline(
            scene_number=sn,
            anchors=anchors,
            time_of_day=tod,
            relation_to_previous=relation,
            estimated_hours_since_previous=hours,
            estimated_elapsed_days=round(cumulative_days, 2),
        ))

    return timelines


def elapsed_days_between(
    timelines: list[SceneTimeline],
    scene_a: int,
    scene_b: int,
) -> float | None:
    """Return estimated elapsed days between two scenes.

    Returns None if either scene is not in the timeline.
    """
    timeline_map = {t.scene_number: t for t in timelines}
    ta = timeline_map.get(scene_a)
    tb = timeline_map.get(scene_b)
    if ta is None or tb is None:
        return None
    return abs(tb.estimated_elapsed_days - ta.estimated_elapsed_days)
