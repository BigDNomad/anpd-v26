"""
Tests for MA-004 object_state_continuity — object state contradiction check.

Covers:
  - Terminal state regex detection
  - Device-dead disambiguation (device vs character)
  - Replacement marker detection
  - Named object extraction
  - Generic object skipping
  - Terminal → functional lifecycle violation
  - Replacement suppression
  - Lena laptop conservative bias (must NOT flag)
  - Object class mismatch
  - Module auto-discovery
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks import (
    ManuscriptArtifact,
    BriefBundle,
    SceneText,
    REGISTRY,
    discover_and_register,
)
from audit_checks.object_state_continuity import (
    ObjectStateContinuity,
    ObjectStateClaim,
    extract_object_claims,
    replacement_marker_between,
    _normalize_object_token,
    _object_class,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_manuscript(*scenes):
    return ManuscriptArtifact(
        scenes=[SceneText(scene_number=n, text=t, file_path=f"/fake/sc_{n:03d}.md")
                for n, t in scenes],
        manuscript_dir="/fake",
    )


# ─── Terminal State Detection ─────────────────────────────────────────────────

class TestTerminalStateRegex:

    def test_destroyed(self):
        ms = _make_manuscript(
            (1, "Hank's rifle was hit by shrapnel. The weapon was destroyed beyond repair."),
        )
        # Need to test with named object "Hank's rifle"
        ms2 = _make_manuscript(
            (1, "The blast caught the barrel. Hank's rifle was destroyed."),
        )
        claims = extract_object_claims(ms2)
        terminal = [c for c in claims if c.state == "terminal"]
        assert len(terminal) >= 1
        assert "destroyed" in terminal[0].state_label

    def test_bricked(self):
        ms = _make_manuscript(
            (50, "Lena rode in the second vehicle with the bricked laptop in her bag."),
        )
        claims = extract_object_claims(ms)
        terminal = [c for c in claims if c.state == "terminal"]
        assert len(terminal) >= 1
        assert "bricked" in terminal[0].state_label

    def test_device_dead_catches_device(self):
        """'phone was dead' → terminal; 'the man was dead' → not terminal."""
        ms_device = _make_manuscript(
            (1, "Cole checked. Cole's phone was dead. No signal, no power."),
        )
        claims_device = extract_object_claims(ms_device)
        device_terminal = [c for c in claims_device if c.state == "terminal" and "dead" in c.state_label]
        assert len(device_terminal) >= 1

    def test_man_dead_not_terminal(self):
        """'the man was dead' should not produce an object terminal claim."""
        ms_person = _make_manuscript(
            (1, "They found the man was dead. He had been shot twice."),
        )
        claims_person = extract_object_claims(ms_person)
        device_terminal = [c for c in claims_person if c.state == "terminal" and "dead" in c.state_label]
        assert len(device_terminal) == 0


# ─── Replacement Markers ─────────────────────────────────────────────────────

class TestReplacementMarkers:

    def test_burner_replacement(self):
        ms = _make_manuscript(
            (1, "Cole's phone was dead."),
            (2, "He picked up a burner from the kit bag."),
            (3, "Cole checked his phone. The signal was strong."),
        )
        assert replacement_marker_between(ms, 1, 3, "computing") is True

    def test_no_replacement(self):
        ms = _make_manuscript(
            (1, "Cole's phone was dead."),
            (2, "They drove in silence through the dark."),
            (3, "Cole checked his phone. The signal was strong."),
        )
        assert replacement_marker_between(ms, 1, 3, "computing") is False


# ─── Named Object Extraction ─────────────────────────────────────────────────

class TestNamedObjectExtraction:

    def test_possessive_named_object(self):
        """'Hank's envelope' → extracted as named object."""
        ms = _make_manuscript(
            (5, "Hank pulled Hank's envelope from the drawer and opened it."),
        )
        claims = extract_object_claims(ms)
        envelope_claims = [c for c in claims if "envelope" in c.object_token]
        assert len(envelope_claims) >= 1

    def test_generic_object_skipped(self):
        """'the phone' without possessor → no claim."""
        ms = _make_manuscript(
            (1, "She picked up the phone and dialed. The phone rang twice."),
        )
        claims = extract_object_claims(ms)
        # Generic "the phone" should not produce claims
        assert len(claims) == 0


# ─── Lifecycle Violation Detection ────────────────────────────────────────────

class TestLifecycleViolation:

    @patch("audit_checks.object_state_continuity._call_llm")
    def test_terminal_then_functional_flags(self, mock_llm):
        """Terminal → functional with no replacement → CLASS_A."""
        mock_llm.return_value = "CONTRADICTION_CONFIRMED\nSame rifle, no replacement mentioned."

        ms = _make_manuscript(
            (1, "The shot went wide. Cole's rifle jammed on the third round."),
            (3, "Cole's rifle cracked once and the target dropped. Cole fired the rifle cleanly."),
        )
        briefs = BriefBundle()
        check = ObjectStateContinuity()
        findings = check.run(ms, briefs)

        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) >= 1
        assert "cole" in class_a[0].description.lower() or "rifle" in class_a[0].description.lower()

    @patch("audit_checks.object_state_continuity._call_llm")
    def test_terminal_then_functional_with_replacement_suppresses(self, mock_llm):
        """Terminal → replacement → functional → no finding."""
        mock_llm.return_value = "REPLACEMENT_PRESENT\nBackup sidearm drawn."

        ms = _make_manuscript(
            (1, "Cole's rifle jammed on the third round. He cursed."),
            (2, "Cole drew his spare rifle from the rack."),
            (3, "Cole fired three rounds. Cole's rifle held steady."),
        )
        briefs = BriefBundle()
        check = ObjectStateContinuity()
        findings = check.run(ms, briefs)

        # Replacement marker should suppress
        class_a = [f for f in findings if f.severity == "CLASS_A"]
        assert len(class_a) == 0


# ─── Lena Laptop Conservative Bias ───────────────────────────────────────────

class TestLenaLaptopConservativeBias:

    def test_lena_laptop_not_flagged(self):
        """Lena leaves 'the laptop' (generic) in sc 5, uses 'her laptop' (generic)
        later. Since neither reference is uniquely identified (no possessive name,
        no unique modifier), no claim should be extracted."""
        ms = _make_manuscript(
            (5, "She left the laptop. It was registered and traceable and she would not need it where she was going."),
            (14, "Lena sat with the laptop open and the encrypted channel running."),
            (22, "Lena was in the cab with the laptop open."),
        )
        claims = extract_object_claims(ms)
        # "the laptop" / "the laptop open" are generic — no unique identifier
        laptop_claims = [c for c in claims if "laptop" in c.object_token]
        # Should either be 0 claims or no terminal→functional pair
        terminal = [c for c in laptop_claims if c.state == "terminal"]
        assert len(terminal) == 0, \
            "Lena's generic laptop should not produce terminal claims (conservative bias)"


# ─── Object Class Mismatch ────────────────────────────────────────────────────

class TestObjectClassMismatch:

    def test_different_object_classes_no_pair(self):
        """Terminal 'phone' + functional 'rifle' → no candidate pair."""
        ms = _make_manuscript(
            (1, "Cole's phone was dead. No signal at all."),
            (3, "Cole's rifle cracked once. Cole fired the rifle cleanly."),
        )
        claims = extract_object_claims(ms)
        phone_tokens = {c.object_token for c in claims if "phone" in c.object_token}
        rifle_tokens = {c.object_token for c in claims if "rifle" in c.object_token}
        # Different tokens → they won't be paired
        assert phone_tokens.isdisjoint(rifle_tokens)


# ─── Module Interface ────────────────────────────────────────────────────────

class TestModuleInterface:

    def test_has_required_interface(self):
        check = ObjectStateContinuity()
        assert check.check_id == "MA-004-object-state-continuity"
        assert check.severity == "CLASS_A"
        assert hasattr(check, "run")
        assert hasattr(check, "description")

    def test_module_auto_discovered(self):
        REGISTRY.clear()
        discover_and_register()
        check_ids = [c.check_id for c in REGISTRY]
        assert "MA-004-object-state-continuity" in check_ids
        REGISTRY.clear()
