"""
Tests for MA-046 cascade fix.

1. The removed `went down` pattern no longer triggers a death event.
2. Cascade cap: a single death + N subsequent active appearances produces
   exactly one finding, not N.
3. Real deaths (other TIER1 patterns) still trigger correctly.
4. Single-violation uses singular description (not "N subsequent scenes").
"""

from audit_checks import ManuscriptArtifact, SceneText, BriefBundle
from audit_checks.character_death_ledger import CharacterDeathLedger


def _make_briefs() -> BriefBundle:
    return BriefBundle(
        series_bible={},
        character_profiles={
            "characters": [
                {"name": "Archer"},
                {"name": "Coyle"},
                {"name": "Bounchanh"},
            ]
        },
        book_config={},
        scene_map={},
        entity_ledger={},
    )


def _make_ms(scenes_data: list[tuple[int, str]]) -> ManuscriptArtifact:
    scenes = [
        SceneText(
            scene_number=n,
            text=t,
            file_path=f"sc_{n:03d}.md",
            word_count=len(t.split()),
        )
        for n, t in scenes_data
    ]
    return ManuscriptArtifact(scenes=scenes, manuscript_dir="/tmp/test")


def test_went_down_no_longer_triggers_death():
    """The removed 'went down' pattern must not produce any finding when
    a character descends/kneels/stumbles."""
    scenes_data = [
        (1, "Archer went down on a hoist recovery. He kept the line steady."),
        (2, "Archer looked at the map. He spoke into the radio."),
        (3, "Archer raised the rifle and fired three rounds."),
    ]
    ms = _make_ms(scenes_data)
    findings = CharacterDeathLedger().run(ms, _make_briefs())
    assert findings == [], (
        f"Expected zero findings (went down is movement), got {len(findings)}: "
        f"{[f.description for f in findings]}"
    )


def test_cascade_cap_consolidates_into_one_finding():
    """A real death + N subsequent active scenes must produce exactly ONE
    finding, not N."""
    scenes_data = [
        (1, "Archer raised his weapon."),
        (2, "Archer fell dead at the door."),  # TIER1 hit
        (3, "Archer moved through the trees."),
        (4, "Archer raised his rifle."),
        (5, "Archer fired three rounds."),
        (6, "Archer turned to Coyle."),
        (7, "Archer spoke."),
    ]
    ms = _make_ms(scenes_data)
    findings = CharacterDeathLedger().run(ms, _make_briefs())
    death_then_alive = [
        f for f in findings if "Death-then-alive" in f.description
    ]
    assert len(death_then_alive) == 1, (
        f"Expected exactly 1 Death-then-alive finding (cap), got "
        f"{len(death_then_alive)}: {[f.description for f in death_then_alive]}"
    )
    desc = death_then_alive[0].description
    assert "5 subsequent scenes" in desc, (
        f"Expected description to summarize 5 subsequent scenes, got: {desc}"
    )
    assert "First violation: scene 3" in desc, (
        f"Expected first-violation note, got: {desc}"
    )


def test_real_death_pattern_still_fires():
    """Removing 'went down' must not regress detection of other TIER1
    patterns. 'fell dead' + later active appearance should still fire
    exactly one Death-then-alive finding."""
    scenes_data = [
        (1, "Archer raised his weapon."),
        (2, "Archer fell dead at the door."),
        (3, "Archer raised his rifle and fired."),
    ]
    ms = _make_ms(scenes_data)
    findings = CharacterDeathLedger().run(ms, _make_briefs())
    death_then_alive = [
        f for f in findings if "Death-then-alive" in f.description
    ]
    assert len(death_then_alive) == 1
    assert "scene 2" in death_then_alive[0].description
    assert "scene 3" in death_then_alive[0].description


def test_single_violation_uses_singular_description():
    """When there is exactly one violating scene, description should NOT
    use the 'N subsequent scenes' phrasing (singular vs plural code path)."""
    scenes_data = [
        (1, "Archer slumped against the wall."),  # TIER1 hit
        (2, "Archer raised the rifle."),
    ]
    ms = _make_ms(scenes_data)
    findings = CharacterDeathLedger().run(ms, _make_briefs())
    death_then_alive = [
        f for f in findings if "Death-then-alive" in f.description
    ]
    assert len(death_then_alive) == 1
    desc = death_then_alive[0].description
    assert "subsequent scenes" not in desc, (
        f"Singular case should not use plural phrasing, got: {desc}"
    )
    assert "scene 1" in desc and "scene 2" in desc
