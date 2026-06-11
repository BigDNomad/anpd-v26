"""
Tests for MA-046 death-event contemplation suppression (2026-06-13).

Contemplation/hypothetical/feared death must not register as a death event.
Require an actual death assertion.

1. sc32-style contemplation text ("if Coyle died") -> no death event.
2. Explicit death passage ("Coyle fell dead") -> death event fires.
3. Mixed: hypothetical in same scene as active death — active still fires.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import ManuscriptArtifact, SceneText, BriefBundle
from audit_checks.character_death_ledger import (
    CharacterDeathLedger,
    _detect_death_events,
    _is_hypothetical,
)


def _make_briefs() -> BriefBundle:
    return BriefBundle(
        series_bible={},
        character_profiles={
            "characters": [
                {"name": "Archer"},
                {"name": "Coyle"},
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


class TestContemplationSuppression:

    def test_if_died_not_registered(self):
        """'if Coyle died before extraction' must NOT register as death."""
        text = (
            "He thought about what it would mean if Coyle died before extraction "
            "and someone found the body. What it would mean for the operation."
        )
        events = _detect_death_events(text, 32, "Coyle", has_prior_death=False)
        assert events == [], (
            f"Expected no death events for contemplation text, got {events}"
        )

    def test_thought_about_dying_not_registered(self):
        """'thought about Archer dying' must NOT register as death."""
        text = (
            "Coyle thought about Archer dying out there in the jungle. "
            "What it would look like. What it would mean."
        )
        events = _detect_death_events(text, 10, "Archer", has_prior_death=False)
        assert events == []

    def test_imagined_dead_not_registered(self):
        """'imagined Coyle dead' must NOT register as death."""
        text = "He imagined Coyle dead on the trail and felt something cold."
        events = _detect_death_events(text, 15, "Coyle", has_prior_death=False)
        assert events == []

    def test_feared_death_not_registered(self):
        """'feared Coyle died' must NOT register as death."""
        text = "Archer feared Coyle died somewhere in the canopy darkness."
        events = _detect_death_events(text, 20, "Coyle", has_prior_death=False)
        assert events == []


class TestExplicitDeathStillFires:

    def test_fell_dead_fires(self):
        """'Coyle fell dead' (explicit TIER1) must still register."""
        text = "Coyle fell dead at the door. His body did not move."
        events = _detect_death_events(text, 5, "Coyle", has_prior_death=False)
        assert len(events) == 1
        assert events[0][2] == 1  # tier 1

    def test_was_dead_fires(self):
        """'Coyle was dead' (explicit TIER2) must still register."""
        text = "Coyle was dead. Archer knelt beside him and checked for a pulse."
        events = _detect_death_events(text, 8, "Coyle", has_prior_death=False)
        assert len(events) == 1
        assert events[0][2] == 2  # tier 2

    def test_killed_name_fires(self):
        """'killed Coyle' (explicit TIER1 object) must still register."""
        text = "The round killed Coyle instantly."
        events = _detect_death_events(text, 12, "Coyle", has_prior_death=False)
        assert len(events) == 1
        assert events[0][2] == 1  # tier 1


class TestEndToEndContemplation:

    def test_contemplation_then_alive_no_finding(self):
        """Full end-to-end: contemplation in sc32 + Coyle alive in sc33 = no finding."""
        scenes_data = [
            (31, "Coyle sat against the rock. Coyle said something about the rotors."),
            (32, "He thought about what it would mean if Coyle died before extraction. "
                 "Coyle was propped against the rock, eyes open."),
            (33, "Coyle said, 'They get it?' He reached for the radio."),
            (34, "Coyle moved through the jungle with Archer supporting his weight."),
        ]
        ms = _make_ms(scenes_data)
        findings = CharacterDeathLedger().run(ms, _make_briefs())
        death_findings = [f for f in findings if "Coyle" in f.description]
        assert death_findings == [], (
            f"Expected no Coyle findings (contemplation, not death), "
            f"got: {[f.description for f in death_findings]}"
        )

    def test_real_death_then_alive_still_flags(self):
        """Explicit death + later alive must still produce a finding."""
        scenes_data = [
            (1, "Coyle fell dead at the door."),
            (2, "Coyle raised his rifle and fired."),
        ]
        ms = _make_ms(scenes_data)
        findings = CharacterDeathLedger().run(ms, _make_briefs())
        death_findings = [f for f in findings if "Coyle" in f.description]
        assert len(death_findings) == 1
        assert "Death-then-alive" in death_findings[0].description


class TestIsHypothetical:

    def test_if_clause_detected(self):
        text = "He thought about what it would mean if Coyle died."
        pos = text.index("Coyle died")
        assert _is_hypothetical(text, pos, "Coyle") is True

    def test_plain_death_not_hypothetical(self):
        text = "Coyle fell dead at the foot of the wall."
        pos = text.index("Coyle fell")
        assert _is_hypothetical(text, pos, "Coyle") is False
