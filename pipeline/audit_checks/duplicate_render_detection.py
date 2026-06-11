"""
MA-011 duplicate_render_detection — flags adjacent/near scenes that re-render the
same beat (same characters + location + core action), especially when a concrete
object's state contradicts between the two renders.

Origin: Mandate b01 Scene 52->53. The scene_writer split one synopsis beat across
two files and re-narrated the connection at the seam, producing a duplicate render
with an object-state contradiction (laptop open / case closed vs. re-connect "I'm in").

Violation classes:
  CLASS_A duplicate-with-contradiction — two near scenes render the same beat AND a
          concrete object's state disagrees between them (the dangerous case: the
          contradiction reaches the reader as a continuity error).
  CLASS_B duplicate-clean — two near scenes render the same beat with no contradiction
          (redundant re-narration; review, may be intentional).

NOT flagged: same location revisited with a DIFFERENT core action (intentional
return to a setting). The same-core-action requirement suppresses this false positive.

LLM extraction (Sonnet) builds a per-scene beat signature; comparison is deterministic.
"""

from __future__ import annotations

import json
import os
import sys
import time

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


SONNET_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 5
MAX_RETRIES = 2
WINDOW = 2  # compare each scene against the next WINDOW scenes (adjacent + near)

SIG_SYSTEM = """You are a scene-beat extractor for a novel manuscript. For each scene, produce a structured signature of what physically happens — not theme or mood, only the concrete beat.

For each scene output exactly one JSON object on its own line:
{"scene_number": N, "characters": ["names physically present and acting"], "location": "where it physically takes place, short noun phrase", "core_action": "the single main physical thing that happens, <=12 words", "objects": [{"name": "concrete object central to the action", "state": "its state/position in this scene, <=8 words"}]}

Rules:
- characters: only those physically present and acting in the scene (not mentioned/remembered).
- location: the physical place (e.g. "the study", "service entrance corridor"). Short.
- core_action: the main physical event, stated plainly (e.g. "connects device to laptop and starts data transfer").
- objects: 0-4 concrete objects that matter to the action, each with its observable state in THIS scene (e.g. {"name":"laptop","state":"open, screen locked"}; {"name":"case","state":"closed on desk"}). Record states that could be checked against another scene.

Output one JSON object per scene, nothing else."""

SIG_PROMPT = """Extract one beat-signature JSON per scene from the following scenes.

SCENES:
{scenes_block}

Output one JSON object per scene."""


def _call_llm(system: str, user: str) -> str:
    pipeline_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    from llm_client import call_llm
    resp = call_llm(provider="anthropic", model=SONNET_MODEL,
                    system=system, user=user, max_tokens=4096, temperature=0.0)
    return resp.text


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _tokens(s: str) -> set[str]:
    stop = {"the", "a", "an", "and", "to", "of", "in", "on", "at", "with", "from",
            "her", "his", "their", "into", "for", "it", "as", "by"}
    return {w for w in _norm(s).replace(",", " ").split() if w and w not in stop}


def _same_beat(a: dict, b: dict) -> bool:
    """Same characters (overlap) AND same location AND same core action."""
    ca = {_norm(x) for x in a.get("characters", [])}
    cb = {_norm(x) for x in b.get("characters", [])}
    if not (ca and cb) or not (ca & cb):
        return False
    # location: token overlap (loose — "the study" vs "study")
    la, lb = _tokens(a.get("location", "")), _tokens(b.get("location", ""))
    if not (la and lb) or not (la & lb):
        return False
    # core action: meaningful token overlap (>=2 shared content tokens)
    aa, ab = _tokens(a.get("core_action", "")), _tokens(b.get("core_action", ""))
    if len(aa & ab) < 2:
        return False
    return True


def _object_contradiction(a: dict, b: dict) -> tuple[str, str, str] | None:
    """Same-named object with differing state across the two scenes."""
    by_name_b = {_norm(o.get("name", "")): o.get("state", "") for o in b.get("objects", [])}
    for o in a.get("objects", []):
        nm = _norm(o.get("name", ""))
        if nm and nm in by_name_b:
            sa = _norm(o.get("state", ""))
            sb = _norm(by_name_b[nm])
            if sa and sb and not (_tokens(sa) & _tokens(sb)):
                return (o.get("name", ""), o.get("state", ""), by_name_b[nm])
    return None


def _build_findings(sigs: dict[int, dict]) -> list[Finding]:
    findings: list[Finding] = []
    nums = sorted(sigs.keys())
    seen_pairs: set[tuple[int, int]] = set()
    for i, n in enumerate(nums):
        for m in nums[i + 1:]:
            if m - n > WINDOW:
                break
            if (n, m) in seen_pairs:
                continue
            a, b = sigs[n], sigs[m]
            if not _same_beat(a, b):
                continue
            seen_pairs.add((n, m))
            contra = _object_contradiction(a, b)
            if contra:
                obj, sa, sb = contra
                findings.append(Finding(
                    check_id="MA-011-duplicate-render-detection",
                    severity="CLASS_A",
                    scene_number=None,
                    scene_numbers=[n, m],
                    description=(f"Scenes {n} and {m} re-render the same beat "
                                 f"(\"{a.get('core_action','')}\") with a contradicting "
                                 f"object state: '{obj}' is \"{sa}\" in {n} but \"{sb}\" in {m}"),
                    evidence=[f"Scene {n}: location \"{a.get('location','')}\", "
                              f"{obj}=\"{sa}\"",
                              f"Scene {m}: location \"{b.get('location','')}\", "
                              f"{obj}=\"{sb}\""],
                    suggested_fix=(f"Collapse the duplicate render: scene {m} should continue "
                                   f"from scene {n} rather than re-narrate the beat. Remove the "
                                   f"re-render and resolve the '{obj}' contradiction."),
                ))
            else:
                findings.append(Finding(
                    check_id="MA-011-duplicate-render-detection",
                    severity="CLASS_B",
                    scene_number=None,
                    scene_numbers=[n, m],
                    description=(f"Scenes {n} and {m} appear to re-render the same beat "
                                 f"(same characters, location \"{a.get('location','')}\", "
                                 f"action \"{a.get('core_action','')}\") — possible redundant "
                                 f"re-narration (review; may be intentional)"),
                    evidence=[f"Scene {n}: \"{a.get('core_action','')}\"",
                              f"Scene {m}: \"{b.get('core_action','')}\""],
                    suggested_fix=(f"Verify scenes {n} and {m} are not redundant. If {m} "
                                   f"re-narrates {n}, collapse so {m} continues the beat."),
                ))
    return findings


def _extract(manuscript: ManuscriptArtifact) -> dict[int, dict]:
    sigs: dict[int, dict] = {}
    scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)
    for i in range(0, len(scenes), BATCH_SIZE):
        batch = scenes[i:i + BATCH_SIZE]
        block = "\n\n".join(f"--- SCENE {s.scene_number} ---\n{s.text[:3500]}" for s in batch)
        prompt = SIG_PROMPT.format(scenes_block=block)
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = _call_llm(SIG_SYSTEM, prompt)
                for line in resp.strip().splitlines():
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        o = json.loads(line)
                        sn = int(o.get("scene_number", 0) or 0)
                        if sn:
                            sigs[sn] = o
                    except (json.JSONDecodeError, ValueError):
                        continue
                break
            except Exception as e:
                if attempt < MAX_RETRIES:
                    time.sleep(5 * (attempt + 1))
                    continue
                print(f"    WARN: beat extraction failed scenes "
                      f"{batch[0].scene_number}-{batch[-1].scene_number}: {e}", file=sys.stderr)
    return sigs


class DuplicateRenderDetection:
    check_id = "MA-011-duplicate-render-detection"
    severity = "CLASS_A"
    description = ("Duplicate render: adjacent/near scenes re-rendering the same beat; "
                   "Class A when an object-state contradiction is present.")

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        sigs = _extract(manuscript)
        print(f"    MA-011: {len(sigs)} scene signatures extracted", file=sys.stderr)
        findings = _build_findings(sigs)
        print(f"    MA-011: {len(findings)} findings", file=sys.stderr)
        return findings
