"""
MA-046 character_death_ledger — detects characters who die and then appear alive
in a later scene, or who are killed twice in separate scenes.

CLASS_A on any death continuity violation (double death or death-then-alive).

No LLM phase; deterministic syntactic pattern matching with reference disambiguation.
"""

from __future__ import annotations

import re
import sys

from audit_checks import ManuscriptArtifact, BriefBundle, Finding


# ── Configuration ────────────────────────────────────────────────────────────

# TIER 1: Death is SHOWN in real-time narrative (active killing action).
# These are always accepted, even if a prior death exists.
# NOTE: r"{NAME}\s+went down" was removed 2026-05-30 after the CSAR full
# audit showed every match on the pattern was a movement idiom (hoist
# descent, took a knee, descended a slope, stumbled on bad leg) and none
# was a death event. The idiom "went down" is non-death in military/
# aviation fiction. Do not re-add without a death-confirming co-marker
# (e.g., "went down hard" + corpse/medical language within window).
TIER1_DEATH_SUBJECT = [
    r"{NAME}\s+fell dead",
    r"{NAME}\s+slumped",
    r"{NAME}\s+collapsed",
    r"{NAME}\s+bled out",
    r"{NAME}\s+breathed (?:his|her) last",
    r"{NAME}'s head opened",
    r"{NAME}'s body (?:settled|fell|slumped|dropped|lay\b|collapsed|folded|crumpled)",
    r"{NAME}'s body .{{0,40}}did not move",
    r"{NAME}'s body .{{0,40}}didn't move",
]

TIER1_DEATH_OBJECT = [
    r"killed\s+{NAME}\b",
    r"shot\s+{NAME}\b",
    r"executed\s+{NAME}\b",
]

# TIER 2: Death is STATED (narration says character is dead, possibly after the fact).
# Later Tier 2 matches for the same character are filtered as references.
TIER2_DEATH = [
    r"{NAME}\s+was dead\b",
    r"{NAME}\s+died\b",
    r"{NAME}\s+was lifeless",
    r"{NAME}\s+was killed",
]

# Patterns indicating passive/possessive mentions (NOT alive-and-acting)
PASSIVE_MARKERS = [
    r"{NAME}'s (?:body|corpse|remains)",
    r"what remained of {NAME}",
    r"(?:had |he |she )?lost {NAME}",
    r"{NAME}'s (?:operation|organization|network|men|people|man\b|team|element|deputy)",
    r"{NAME},?\s+now dead",
    r"{NAME},?\s+already dead",
    r"{NAME}\s+had trained",
    r"trained .{{0,20}} people",
]

# Active subject patterns: character DOING something (alive)
ACTIVE_SUBJECT_PATTERNS = [
    r"{NAME}\s+(?:was behind|stood|raised|drew|fired|turned|walked|ran|moved|"
    r"sat\b|spoke|said|watched|looked|pulled|reached|pushed|stepped|came|went|"
    r"held|took|made\s+a|gave|kept|called|started|stopped|opened|closed|picked|"
    r"set\b|put\b|nodded|shook|gestured|studied|paused|removed|cut\b|"
    r"closed\b|laid\b|placed\b)",
    r"{NAME}'s (?:arm|hand|pistol|weapon|rifle|gun|eyes|grip|position|fingers|voice)\b",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _finding(description: str, evidence: list[str],
             scene_numbers: list[int], suggested_fix: str) -> Finding:
    return Finding(
        check_id="MA-046-character-death-ledger",
        severity="CLASS_A",
        scene_number=None,
        scene_numbers=scene_numbers,
        description=description,
        evidence=evidence,
        suggested_fix=suggested_fix,
    )


def _extract_snippet(text: str, pos: int, radius: int = 60) -> str:
    """Extract a short snippet around a position, max ~15 words."""
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    snippet = text[start:end].strip()
    words = snippet.split()
    if len(words) > 15:
        words = words[:15]
    return " ".join(words)


def _build_roster(manuscript: ManuscriptArtifact, briefs: BriefBundle) -> tuple[set[str], str]:
    """Build character roster from profiles + targeted name scan."""
    names: set[str] = set()
    source = ""

    # Source 1: character_profiles
    profiles = getattr(briefs, "character_profiles", {})
    if profiles:
        chars = profiles.get("characters", profiles)
        if isinstance(chars, list):
            for entry in chars:
                if isinstance(entry, dict):
                    full_name = entry.get("name", entry.get("Name", ""))
                    if full_name:
                        titles = {"capitán", "captain", "dr", "mr", "mrs", "ms",
                                  "the", "of", "el", "la", "de", "del", "chief"}
                        for part in full_name.split():
                            clean = part.strip(",.")
                            if (clean and clean[0].isupper() and len(clean) > 2
                                    and clean.lower() not in titles):
                                names.add(clean)
                elif isinstance(entry, str):
                    names.add(entry)
        elif isinstance(chars, dict):
            for key in chars:
                names.add(key)
        source = f"character_profiles ({len(names)} names)"

    # Source 2: scan for additional proper names (e.g., minor characters like Restrepo)
    full_text = manuscript.full_text()
    name_pattern = re.compile(r'(?<=[a-z,.;:!?\s] )([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})\b')
    name_counts: dict[str, int] = {}
    for m in name_pattern.finditer(full_text):
        candidate = m.group(1)
        name_counts[candidate] = name_counts.get(candidate, 0) + 1

    # Extensive stopword list
    stopwords = {
        "The", "This", "That", "These", "Those", "They", "Their", "There", "Then",
        "What", "When", "Where", "Which", "While", "Who", "Why", "How",
        "She", "Her", "His", "Him", "Its", "You", "Your",
        "And", "But", "Not", "For", "From", "Into", "With", "Over", "About",
        "After", "Before", "Between", "Through", "During", "Without", "Against",
        "Was", "Were", "Had", "Has", "Have", "Did", "Does", "Been", "Being",
        "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
        "Each", "Every", "Some", "Any", "All", "Most", "Both", "Neither", "Either",
        "Could", "Would", "Should", "Will", "Shall", "May", "Might", "Must",
        "Just", "Even", "Still", "Already", "Also", "Never", "Always", "Here",
        "Something", "Nothing", "Everything", "Anything", "Someone", "Anyone",
        "First", "Second", "Third", "Last", "Next", "Other", "Another",
        "Much", "Many", "More", "Less", "Few", "Enough",
        "Very", "Only", "Well", "Now", "Yes", "Left", "Right",
        "Suburban", "Marine", "Marines", "Colonel", "General", "Admiral",
        "Avenida", "Calle", "Hotel", "Avenue", "Street", "Building",
        "Cuban", "American", "Venezuelan", "Mexican", "Russian", "Americans",
        "Hellfire", "Mojave", "Indianapolis", "Mercedes", "Madrid",
        "Forty", "Fifty", "Twenty", "Thirty", "Sixty", "Hundred",
        "Whether", "Perhaps", "Probably", "Certainly", "Apparently",
        "Ground", "Branch", "Station", "Chief", "Interrogator",
        "Inside", "Outside", "Behind", "Beside", "Along",
        "Operation", "Protocol", "Pattern", "Signal", "Target",
        "Ministry", "Government", "Transition", "Network",
        "Somewhere", "Otherwise", "However", "Therefore", "Meanwhile",
        "Morning", "Evening", "Night", "Afternoon",
        "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Everything", "Anything", "Something", "Nothing",
        "Because", "Although", "Unless", "Until",
        "Kilo", "Papa", "Lima", "Bravo", "Charlie", "Delta", "Alpha",
        "Las", "San", "Santa", "Puerto", "Nueva",
        "Rómulo", "Gallegos", "Urdaneta", "Francisco", "Miranda",
        "Fort", "Tiuna", "Caracas", "Petare", "Baruta",
        "Stockdale", "Hilux",
    }

    scan_count = 0
    for name, count in name_counts.items():
        if count >= 5 and name not in stopwords and name not in names and len(name) > 2:
            names.add(name)
            scan_count += 1

    if scan_count > 0:
        source += f" + name scan ({scan_count} additional)"
    elif not source:
        source = f"name scan only ({len(names)} names)"

    return names, source


def _match_patterns(text: str, name: str,
                    patterns: list[str]) -> list[tuple[int, str]]:
    """Match a list of patterns with {NAME} replaced. Returns (position, snippet)."""
    results: list[tuple[int, str]] = []
    name_esc = re.escape(name)
    for pat_str in patterns:
        pat = re.compile(pat_str.replace("{NAME}", name_esc), re.IGNORECASE)
        for m in pat.finditer(text):
            snippet = _extract_snippet(text, m.start(), radius=80)
            results.append((m.start(), snippet))
    return results


def _detect_death_events(text: str, scene_number: int, name: str,
                         has_prior_death: bool) -> list[tuple[int, str, int]]:
    """Detect death events for a character in a scene.

    Returns list of (scene_number, snippet, tier) where tier is 1 or 2.
    Tier 2 events are filtered if has_prior_death is True.
    """
    results: list[tuple[int, str, int]] = []

    # Tier 1: active killing shown
    tier1_hits = (_match_patterns(text, name, TIER1_DEATH_SUBJECT) +
                  _match_patterns(text, name, TIER1_DEATH_OBJECT))
    if tier1_hits:
        _, snippet = tier1_hits[0]
        results.append((scene_number, snippet, 1))
        return results  # One death per scene

    # Tier 2: death stated (only accepted if no prior death)
    if not has_prior_death:
        tier2_hits = _match_patterns(text, name, TIER2_DEATH)
        if tier2_hits:
            _, snippet = tier2_hits[0]
            results.append((scene_number, snippet, 2))
            return results

    return results


def _detect_active_appearances(text: str, name: str) -> list[tuple[int, str]]:
    """Detect places where a character appears as an active, living subject."""
    hits: list[tuple[int, str]] = []
    name_esc = re.escape(name)

    for pat_str in ACTIVE_SUBJECT_PATTERNS:
        pat = re.compile(pat_str.replace("{NAME}", name_esc), re.IGNORECASE)
        for m in pat.finditer(text):
            pos = m.start()
            matched_text = m.group(0)

            # Check this isn't a passive/dead mention
            is_passive = False
            for pp_str in PASSIVE_MARKERS:
                pp = re.compile(pp_str.replace("{NAME}", name_esc), re.IGNORECASE)
                window_start = max(0, pos - 40)
                window_end = min(len(text), pos + len(matched_text) + 60)
                window = text[window_start:window_end]
                if pp.search(window):
                    is_passive = True
                    break

            if not is_passive:
                snippet = _extract_snippet(text, pos, radius=60)
                hits.append((pos, snippet))

    return hits


# ── Check class ──────────────────────────────────────────────────────────────

class CharacterDeathLedger:
    check_id = "MA-046-character-death-ledger"
    severity = "CLASS_A"
    description = "Character death ledger: detects characters who die then appear alive, or die twice"

    def run(self, manuscript: ManuscriptArtifact, briefs: BriefBundle) -> list[Finding]:
        findings: list[Finding] = []
        scenes = sorted(manuscript.scenes, key=lambda s: s.scene_number)

        print(f"    Scenes: {len(scenes)}", file=sys.stderr)

        # Phase 1: Build roster
        roster, roster_source = _build_roster(manuscript, briefs)
        print(f"    Roster: {len(roster)} names ({roster_source})", file=sys.stderr)

        # Phase 2: Per-scene death extraction
        # death_events[character] = list of (scene_number, snippet, tier)
        death_events: dict[str, list[tuple[int, str, int]]] = {}

        for scene in scenes:
            for name in roster:
                if name not in scene.text:
                    continue

                has_prior = name in death_events and len(death_events[name]) > 0
                new_events = _detect_death_events(
                    scene.text, scene.scene_number, name, has_prior
                )
                if new_events:
                    if name not in death_events:
                        death_events[name] = []
                    existing_scenes = {s for s, _, _ in death_events[name]}
                    for sn, snip, tier in new_events:
                        if sn not in existing_scenes:
                            death_events[name].append((sn, snip, tier))

        print(f"    Death events found: {sum(len(v) for v in death_events.values())} "
              f"across {len(death_events)} characters", file=sys.stderr)
        for name, events in sorted(death_events.items()):
            scenes_list = [(s, f"T{t}") for s, _, t in events]
            print(f"      {name}: {scenes_list}", file=sys.stderr)

        # Phase 3: Cross-scene verdicts
        for name, events in death_events.items():
            events_sorted = sorted(events, key=lambda x: x[0])

            # Check A: Double death
            if len(events_sorted) >= 2:
                for i in range(len(events_sorted) - 1):
                    death1_scene, death1_snip, _ = events_sorted[i]
                    death2_scene, death2_snip, _ = events_sorted[i + 1]
                    findings.append(_finding(
                        description=(
                            f"Double death: {name} has active death events in both "
                            f"scene {death1_scene} and scene {death2_scene}."
                        ),
                        evidence=[
                            f"Scene {death1_scene}: ...{death1_snip}...",
                            f"Scene {death2_scene}: ...{death2_snip}...",
                        ],
                        scene_numbers=[death1_scene, death2_scene],
                        suggested_fix=(
                            f"Resolve duplicate death of {name}: scenes {death1_scene} and "
                            f"{death2_scene} both render an active killing. Determine canonical "
                            f"death scene; convert the other to reference or remove."
                        ),
                    ))

            # Check B: Death-then-alive (capped: one finding per character per
            # death scene; cascade across many later scenes consolidates into
            # the finding's description rather than producing N findings).
            # A single false-death detection in Phase 2 must not produce N
            # false findings here.
            if events_sorted:
                first_death_scene = events_sorted[0][0]
                first_death_snip = events_sorted[0][1]
                subsequent_death_scenes = {s for s, _, _ in events_sorted[1:]}

                violating_scenes: list[tuple[int, str]] = []
                for scene in scenes:
                    if scene.scene_number <= first_death_scene:
                        continue
                    if scene.scene_number in subsequent_death_scenes:
                        continue
                    active_hits = _detect_active_appearances(scene.text, name)
                    if active_hits:
                        _, active_snip = active_hits[0]
                        violating_scenes.append((scene.scene_number, active_snip))

                if violating_scenes:
                    first_violating_scene, first_violating_snip = violating_scenes[0]
                    violating_scene_numbers = [v[0] for v in violating_scenes]
                    n_violating = len(violating_scenes)

                    if n_violating == 1:
                        description = (
                            f"Death-then-alive: {name} dies in scene "
                            f"{first_death_scene} but acts as a living character "
                            f"in scene {first_violating_scene}."
                        )
                    else:
                        preview = violating_scene_numbers[:5]
                        more = "" if n_violating <= 5 else f" (+{n_violating - 5} more)"
                        description = (
                            f"Death-then-alive: {name} dies in scene "
                            f"{first_death_scene} but acts as a living character "
                            f"in {n_violating} subsequent scenes: "
                            f"{preview}{more}. First violation: scene "
                            f"{first_violating_scene}."
                        )

                    findings.append(_finding(
                        description=description,
                        evidence=[
                            f"Scene {first_death_scene} (death): ...{first_death_snip}...",
                            f"Scene {first_violating_scene} (alive): ...{first_violating_snip}...",
                        ],
                        scene_numbers=[first_death_scene] + violating_scene_numbers,
                        suggested_fix=(
                            f"{name} dies in scene {first_death_scene} but acts in "
                            f"{n_violating} subsequent scene(s). Either move the death "
                            f"later, remove the post-death action, or revise the "
                            f"scene-{first_death_scene} death event if it is not actually "
                            f"a death."
                        ),
                    ))

        # Tally
        class_a = sum(1 for f in findings if f.severity == "CLASS_A")
        class_b = sum(1 for f in findings if f.severity == "CLASS_B")
        class_c = sum(1 for f in findings if f.severity == "CLASS_C")
        print(f"    -> {len(findings)} findings ({class_a} A, {class_b} B, {class_c} C)",
              file=sys.stderr)

        return findings
