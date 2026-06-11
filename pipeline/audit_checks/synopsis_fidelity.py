"""
MA-035: Synopsis Fidelity Check (Review Surface)

Compares each manuscript scene against its synopsis beats and surfaces beats
that look missing or contradicted, plus major events invented in prose.

This is a REVIEW SURFACE, not a publication gate.  LLM-graded — severity
CLASS_B (non-blocking).  A scene that cannot be verified is always flagged,
never silently passed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from audit_checks import ManuscriptArtifact, BriefBundle, Finding
from audit_checks._lib.synopsis_scene_types import load_scene_specs

# ── Constants ────────────────────────────────────────────────────────────────

HAIKU_MODEL = "claude-haiku-4-5"
MAX_RETRIES = 2

FIDELITY_SYSTEM = """You are a synopsis-fidelity auditor for a novel manuscript.

You receive:
1. A numbered list of SYNOPSIS BEATS for a single scene.
2. The full PROSE of the manuscript scene.

TASK A — Beat verdicts
For each beat (by index), classify it as EXACTLY ONE of:
  PRESENT — the beat is rendered in the prose (any elaboration is fine)
  EXPANDED — the beat is present and the prose adds significant detail
  COMPRESSED — the beat is present but rendered briefly
  MISSING — the beat does not appear in the prose at all
  CONTRADICTED — the prose renders the beat in a way that changes plot or fact

TASK B — Invented major events
List any MAJOR plot event in the prose that is NOT in the beats.
Major means: death, capture/release, betrayal revealed, location change,
mission-outcome flip.  Do NOT flag: reordered beats, added sensory detail,
compression, scene_writer-authored dialogue, minor props, voice color.
Do NOT flag invented named characters (another check handles that).

Respond with ONLY a JSON object (no markdown fences, no commentary):
{"verdicts":[{"beat_index":0,"verdict":"PRESENT","note":""},...],
 "invented_major":[{"event":"...","evidence":"<=15 word prose quote"}]}

If there are no invented major events, return an empty list for invented_major."""

FIDELITY_USER = """SYNOPSIS BEATS for Scene {scene_number} — "{title}":
{beats_block}

MANUSCRIPT PROSE (Scene {scene_number}):
{prose}"""


# ── LLM helper — calls call_llm DIRECTLY (not MA-001 wrapper) ───────────────

def _get_call_llm():
    """Import call_llm from the pipeline's llm_client."""
    pipeline_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from llm_client import call_llm
    return call_llm


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences from LLM JSON output."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _compare_scene(spec, prose: str) -> tuple[list[dict], list[dict], bool]:
    """Call LLM to compare a single scene's beats against its prose.

    Returns (verdicts, invented_major, ok).
    ok=False means the response was truncated or unparseable after retries.
    """
    call_llm = _get_call_llm()

    beats_block = "\n".join(
        f"  [{i}] {beat}" for i, beat in enumerate(spec.beats)
    )
    user_prompt = FIDELITY_USER.format(
        scene_number=spec.number,
        title=spec.title,
        beats_block=beats_block,
        prose=prose,
    )

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = call_llm(
                provider="anthropic",
                model=HAIKU_MODEL,
                system=FIDELITY_SYSTEM,
                user=user_prompt,
                max_tokens=4096,
                temperature=0.0,
            )
            # Truncation guard
            if response.stop_reason == "max_tokens":
                last_error = "truncated"
                if attempt < MAX_RETRIES:
                    time.sleep(2)
                continue

            raw = _strip_json_fences(response.text)
            data = json.loads(raw)
            verdicts = data.get("verdicts", [])
            invented = data.get("invented_major", [])
            return verdicts, invented, True

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(2)
            continue
        except Exception as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(2)
            continue

    # All retries exhausted — unverifiable
    return [], [], False


# ── Check module class ───────────────────────────────────────────────────────

class SynopsisFidelity:
    check_id = "MA-035-synopsis-fidelity"
    severity = "CLASS_B"
    description = ("Synopsis fidelity (review surface): flags synopsis beats that "
                   "appear missing or contradicted in the manuscript scene, and "
                   "major events invented in prose. Non-blocking; LLM-graded.")

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        specs = load_scene_specs(briefs.synopsis_path)
        if not specs:
            return [Finding(
                check_id=self.check_id,
                severity="CLASS_B",
                scene_number=None,
                description="No synopsis available; fidelity unverifiable",
                suggested_fix="Provide synopsis.md at the expected path",
            )]

        findings: list[Finding] = []
        for n, spec in sorted(specs.items()):
            scene = manuscript.scene_by_number(n)
            if scene is None:
                findings.append(Finding(
                    check_id=self.check_id,
                    severity="CLASS_B",
                    scene_number=n,
                    description=f"Synopsis specifies scene {n} but manuscript has no sc_{n:03d}",
                    suggested_fix="Generate the missing scene or reconcile numbering",
                ))
                continue

            if not spec.beats:
                continue

            print(f"    MA-035: checking scene {n} ({len(spec.beats)} beats)", file=sys.stderr)
            verdicts, invented, ok = _compare_scene(spec, scene.text)

            if not ok:
                findings.append(Finding(
                    check_id=self.check_id,
                    severity="CLASS_B",
                    scene_number=n,
                    description=f"Scene {n}: fidelity unverifiable (LLM truncation/parse failure after retries)",
                    suggested_fix="Re-run; if persistent, review scene manually",
                ))
                continue

            for v in verdicts:
                if v.get("verdict") in ("MISSING", "CONTRADICTED"):
                    beat_idx = v.get("beat_index", 0)
                    beat_text = spec.beats[beat_idx][:80] if beat_idx < len(spec.beats) else "(unknown beat)"
                    findings.append(Finding(
                        check_id=self.check_id,
                        severity="CLASS_B",
                        scene_number=n,
                        description=f"Scene {n}: beat {v['verdict'].lower()} — {beat_text}",
                        evidence=[v.get("note", "")[:200]],
                        suggested_fix="Review; if real, regenerate scene to render the beat",
                    ))

            for ev in invented:
                findings.append(Finding(
                    check_id=self.check_id,
                    severity="CLASS_B",
                    scene_number=n,
                    description=f"Scene {n}: possible invented major event — {ev.get('event', '')[:80]}",
                    evidence=[ev.get("evidence", "")[:200]],
                    suggested_fix="Review; remove if unintended or add to synopsis",
                ))

        return findings
