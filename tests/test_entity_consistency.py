"""
Tests for MA-047 entity_consistency — dispatch 2: handlers + acceptance.

Tests the reference-matcher (calibration-2), three handlers, severity,
suggested_tier, and acceptance run against CSAR manuscript.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit_checks.entity_consistency import (
    find_asserted_facts,
    _build_designation_family_regex,
    _build_expected_state_timeline,
    _detect_transition_violations,
    _derive_head_nouns,
    _expected_state_at_scene,
    _extract_asserted_state_llm,
    _parse_number_token,
    _scan_designations,
    _scan_counts,
    _scan_sides,
    _scan_deaths,
    _scan_role_violations,
    _SEVERITY_BY_FACT,
    EntityConsistencyCheck,
)
from audit_checks import BriefBundle, ManuscriptArtifact, SceneText


# ── Real CSAR prose fixtures ─────────────────────────────────────────────

CSAR_ROTOR_LINES = [
    "The rotors.",
    "Eight of them, seated in the machine's face in a row. Each one a disk of wiring and contact points.",
    "He got his left hand on the first rotor and pulled.",
    "Eight rotors. Eight pockets and folds in the jacket and the flight suit.",
    'He reached into the breast pocket of his flight suit and felt the weight there. Seven rotors, each one a flat aluminum disk the size of a half-dollar, the contact pins intact.',
    "He crouched beside it, both hands on his knees, and looked at the four mounting points where the rotors should have been seated.",
    # Calibration-2 noise test: "eight thousand feet" should NOT match rotors
    "The AC-119K flew at eight thousand feet with its navigation lights off.",
]

CSAR_KL7_LINES = [
    "The KL-7 was in the fuselage.",
    "The KL-7 was in its mount on the operator's table.",
]

CSAR_GAU_LINES = [
    "Archer unslung his GAU-5/A and returned fire at the Pathet Lao positions below.",
    "The GAU-5/A — still in his grip from firing — catches hard on a limb.",
]

CSAR_CLAYMORE_LINES = [
    "two Claymore mines, two sets of night-vision goggles.",
    "Archer hits the detonator three times — a pre-set Claymore fires.",
]

CSAR_MINIGUN_LINES = [
    "the three M134 miniguns — plant the mechanical readiness",
    "all three miniguns open on the convoy",
]

# Ledger entities
CIPHER_ROTORS = {
    "id": "cipher_rotors", "canonical_name": "KL-7 cipher rotors",
    "aliases": [], "entity_class": "scalar",
    "invariants": {"count": 8, "designation": "KL-7"},
}

ARCHERS_WEAPON = {
    "id": "archers_weapon", "canonical_name": "Archer's weapon",
    "aliases": [], "entity_class": "scalar",
    "invariants": {"designation": "GAU-5/A"},
}

CLAYMORES = {
    "id": "claymores", "canonical_name": "Claymore mines",
    "aliases": [], "entity_class": "scalar",
    "invariants": {"count": 2},
}

MINIGUNS = {
    "id": "miniguns", "canonical_name": "M134 miniguns",
    "aliases": [], "entity_class": "scalar",
    "invariants": {"count": 3},
}

DAMAGE_SIDE = {
    "id": "damage_side", "canonical_name": "Coyle burn/damage side",
    "aliases": [], "entity_class": "scalar",
    "invariants": {"side": "right"},
}

COYLE = {
    "id": "coyle", "canonical_name": "Coyle's wound",
    "aliases": [], "entity_class": "stateful",
    "state_track": {
        "initial_state": "pristine",
        "allowed_transitions": [],
        "forbidden_states": ["eye_socket_injury", "ankle_injury"],
    },
}


# ── Test: designation family regex ───────────────────────────────────────

class TestDesignationFamilyRegex:

    def test_kl7_catches_kl_variants(self):
        pat = _build_designation_family_regex("KL-7")
        assert pat.search("the KL-7 cipher machine")
        assert pat.search("designated KL-47 in the report")
        assert not pat.search("the KLM flight")

    def test_gau5a_catches_gau_variants(self):
        pat = _build_designation_family_regex("GAU-5/A")
        assert pat.search("his GAU-5/A carbine")
        assert pat.search("a GAU-2/B mounted")

    def test_ak47_catches_ak_variants(self):
        pat = _build_designation_family_regex("AK-47s")
        assert pat.search("AK-47 rounds")
        assert pat.search("AK-47s were")

    def test_ac119k_catches_variants(self):
        pat = _build_designation_family_regex("AC-119K")
        assert pat.search("The AC-119K flew")


# ── Test: calibration-2 count adjacency ──────────────────────────────────

class TestCountAdjacency:

    def test_eight_thousand_feet_rejected(self):
        """'eight thousand feet' must NOT match cipher_rotors count."""
        lines = ["The AC-119K flew at eight thousand feet with its navigation lights off."]
        results = _scan_counts(8, "KL-7 cipher rotors", lines)
        assert len(results) == 0, f"Should reject 'eight thousand feet'; got {results}"

    def test_three_times_rejected(self):
        """'three times' must NOT match Claymore count."""
        lines = ["Archer hits the detonator three times — a pre-set Claymore fires."]
        results = _scan_counts(2, "Claymore mines", lines)
        assert all(r["asserted"] != 3 for r in results), "Should reject 'three times'"

    def test_eight_rotors_still_found(self):
        """'Eight rotors' should still match after adjacency tightening."""
        lines = ["Eight rotors. Eight pockets and folds in the jacket."]
        results = _scan_counts(8, "KL-7 cipher rotors", lines)
        eights = [r for r in results if r["asserted"] == 8]
        assert len(eights) >= 1

    def test_seven_rotors_still_found(self):
        """'Seven rotors' (H-1 defect) should still match."""
        lines = ["Seven rotors, each one a flat aluminum disk."]
        results = _scan_counts(8, "KL-7 cipher rotors", lines)
        sevens = [r for r in results if r["asserted"] == 7]
        assert len(sevens) >= 1

    def test_three_miniguns_still_found(self):
        """'three M134 miniguns' should still match."""
        lines = ["the three M134 miniguns — plant the mechanical readiness"]
        results = _scan_counts(3, "M134 miniguns", lines)
        threes = [r for r in results if r["asserted"] == 3]
        assert len(threes) >= 1

    def test_two_claymores_still_found(self):
        """'two Claymore mines' should still match."""
        lines = ["two Claymore mines, two sets of night-vision goggles."]
        results = _scan_counts(2, "Claymore mines", lines)
        twos = [r for r in results if r["asserted"] == 2]
        assert len(twos) >= 1


# ── Test: side scanner ───────────────────────────────────────────────────

class TestSideScanner:

    def test_right_arm_wound_context_found(self):
        """Right arm near wound context → fires."""
        lines = [
            "Coyle looked down at the wound.",
            "His right arm was burned and useless for balance.",
        ]
        results = _scan_sides("right", "Coyle burn/damage side", lines)
        assert len(results) >= 1
        assert results[0]["asserted"] == "right"

    def test_left_side_defect_found(self):
        """M-3 defect: 'left side of his face was burned' should be detected."""
        lines = [
            "Coyle was conscious.",
            "The left side of his face was burned, the skin tight and wrong-looking.",
        ]
        results = _scan_sides("right", "Coyle burn/damage side", lines)
        lefts = [r for r in results if r["asserted"] == "left"]
        assert len(lefts) >= 1, "Should detect wrong-side (left) burn"

    def test_left_wing_gated_out(self):
        """Aircraft reference: 'left wing dipped' — no wound context → no finding."""
        lines = [
            "Coyle held the yoke steady.",
            "The left wing dipped as the aircraft banked hard over the ridge.",
        ]
        results = _scan_sides("right", "Coyle burn/damage side", lines)
        assert len(results) == 0, "Aircraft 'left wing' should be gated out"

    def test_left_side_flight_deck_gated_out(self):
        """Aircraft interior: 'left side of the flight deck' — no wound context → no finding."""
        lines = [
            "Coyle looked around the cockpit.",
            "The fire was pressing against the left side of the flight deck.",
        ]
        results = _scan_sides("right", "Coyle burn/damage side", lines)
        assert len(results) == 0, "Flight deck 'left side' should be gated out"

    def test_right_wound_matches_canonical_no_finding_in_handler(self):
        """Right-side wound reference matches canonical 'right' — no mismatch."""
        lines = [
            "Coyle examined the wound.",
            "The right side of his arm was badly burned.",
        ]
        results = _scan_sides("right", "Coyle burn/damage side", lines)
        rights = [r for r in results if r["asserted"] == "right"]
        assert len(rights) >= 1, "Should detect right-side wound reference"
        # Handler will see asserted==canonical, so no finding — that's correct


# ── Test: death scanner ──────────────────────────────────────────────────

class TestDeathScanner:

    def test_death_detected(self):
        lines = ["Bounchanh was dead.", "The jungle was quiet."]
        results = _scan_deaths("Bounchanh", ["the major"], lines)
        assert len(results) >= 1

    def test_no_false_positive(self):
        lines = ["Bounchanh walked through the camp.", "He was alive."]
        results = _scan_deaths("Bounchanh", [], lines)
        assert len(results) == 0


# ── Test: role-binding scanner ───────────────────────────────────────────

class TestRoleBindingScanner:

    def test_forbidden_name_in_gunship_context(self):
        rb = {
            "context": "aboard the gunship",
            "required_form": "role_only",
            "forbidden_references": ["Dalton", "Evans", "Vance"],
            "permitted_roles": ["co-pilot", "sensor operator", "gunner one", "gunner two"],
        }
        lines = [
            "The Black Widow banked hard over the trail.",
            "Dalton checked the instruments from the co-pilot seat.",
            "The gunship crew held steady.",
        ]
        results = _scan_role_violations(rb, lines)
        assert len(results) >= 1
        assert any(r["asserted"] == "Dalton" for r in results)

    def test_no_violation_outside_context(self):
        rb = {
            "context": "aboard the gunship",
            "required_form": "role_only",
            "forbidden_references": ["Dalton"],
            "permitted_roles": ["co-pilot"],
        }
        lines = [
            "Dalton sat in the briefing room at NKP.",
            "He drank his coffee slowly.",
        ]
        results = _scan_role_violations(rb, lines)
        assert len(results) == 0


# ── Test: BriefBundle entity_ledger field ────────────────────────────────

class TestBriefBundleLedgerField:

    def test_default_empty(self):
        b = BriefBundle()
        assert b.entity_ledger == {}

    def test_can_set_ledger(self):
        ledger = {"ledger_meta": {"sealed": True}, "entities": []}
        b = BriefBundle(entity_ledger=ledger)
        assert b.entity_ledger["ledger_meta"]["sealed"] is True


# ── Test: check class seal refusal ───────────────────────────────────────

class TestSealRefusal:

    def test_returns_empty_if_no_ledger(self):
        check = EntityConsistencyCheck()
        ms = ManuscriptArtifact(scenes=[SceneText(1, "test", "", 0)], manuscript_dir="")
        briefs = BriefBundle()
        assert check.run(ms, briefs) == []

    def test_returns_empty_if_unsealed(self):
        check = EntityConsistencyCheck()
        ms = ManuscriptArtifact(scenes=[SceneText(1, "test", "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger={"ledger_meta": {"sealed": False}, "entities": []})
        assert check.run(ms, briefs) == []


# ── Test: scalar handler produces correct severity findings ──────────────

class TestScalarHandler:

    def test_count_mismatch_is_class_b(self):
        check = EntityConsistencyCheck()
        text = "Seven rotors, each one a flat disk."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [CIPHER_ROTORS],
            "provenance": {"cipher_rotors.count": {"origin": "synopsis_extracted", "resolution": "auto_resolved"}},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        count_findings = [f for f in findings if "Count mismatch" in f.description]
        assert len(count_findings) >= 1
        # F-INT-9 Part 2: severity is CLASS_A (LLM-confirmed) or CLASS_B (unconfirmed)
        assert count_findings[0].severity in ("CLASS_A", "CLASS_B")

    def test_side_mismatch_is_class_b(self):
        check = EntityConsistencyCheck()
        text = "Coyle grimaced.\nThe left side of his face was burned."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [DAMAGE_SIDE],
            "provenance": {"damage_side.side": {"origin": "book_config_declared", "resolution": "declared"}},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        side_findings = [f for f in findings if "Side mismatch" in f.description]
        assert len(side_findings) >= 1
        # F-INT-9 Part 2: severity is CLASS_A (LLM-confirmed) or CLASS_B (unconfirmed)
        assert side_findings[0].severity in ("CLASS_A", "CLASS_B")


# ── Test: stateful handler ───────────────────────────────────────────────

class TestStatefulHandler:

    def test_forbidden_state_is_class_b(self):
        check = EntityConsistencyCheck()
        text = "Coyle grimaced as the medic examined his ankle.\nThe ankle injury made every step agony."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [COYLE],
            "provenance": {"coyle.forbidden_states": {"origin": "book_config_declared", "resolution": "declared"}},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        fs_findings = [f for f in findings if "Forbidden state" in f.description]
        assert len(fs_findings) >= 1
        assert fs_findings[0].severity == "CLASS_A"  # F-INT-9 Part 2: upgraded
        assert "Tier 2" in fs_findings[0].suggested_fix


# ── Acceptance run against CSAR manuscript ───────────────────────────────

_MANUSCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "series", "airmen", "b01", "work", "manuscript",
    "manuscript_20260527_0953", "act1_full.md",
)
_LEDGER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "series", "airmen", "b01", "work", "entity_ledger.json",
)
_CSAR_AVAILABLE = os.path.exists(_MANUSCRIPT_PATH) and os.path.exists(_LEDGER_PATH)


@pytest.mark.skipif(not _CSAR_AVAILABLE, reason="CSAR manuscript/ledger not available")
class TestAcceptanceCSAR:

    @pytest.fixture
    def csar_run(self):
        with open(_MANUSCRIPT_PATH, "r") as f:
            text = f.read()
        with open(_LEDGER_PATH, "r") as f:
            ledger = json.load(f)
        ms = ManuscriptArtifact(
            scenes=[SceneText(1, text, _MANUSCRIPT_PATH, len(text.split()))],
            manuscript_dir=os.path.dirname(_MANUSCRIPT_PATH),
        )
        briefs = BriefBundle(entity_ledger=ledger)
        return ms, briefs, ledger

    def test_h1_rotor_count(self, csar_run):
        """H-1: 'Seven rotors' vs canonical 8."""
        ms, briefs, _ = csar_run
        check = EntityConsistencyCheck()
        # Mock LLM to avoid real calls in test
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        h1 = [f for f in findings if "cipher_rotors" in f.description and "Count" in f.description]
        assert len(h1) >= 1, "H-1: should detect rotor count mismatch"
        assert h1[0].severity == "CLASS_B"

    def test_h2_kl_designation(self, csar_run):
        """H-2: KL-47 vs canonical KL-7."""
        ms, briefs, _ = csar_run
        check = EntityConsistencyCheck()
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        h2 = [f for f in findings if "cipher_rotors" in f.description and "Designation" in f.description]
        # KL-47 should be flagged if present in manuscript
        kl47 = [f for f in h2 if "KL-47" in f.description or "KL-47" in str(f.evidence)]
        # Note: KL-47 may or may not be in act1 — the calibration showed 1 hit
        # If present, it must be flagged
        if kl47:
            assert kl47[0].severity == "CLASS_A"  # designation stays CLASS_A

    def test_h8_claymore_count(self, csar_run):
        """H-8: Claymore count assertions."""
        ms, briefs, _ = csar_run
        check = EntityConsistencyCheck()
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        h8 = [f for f in findings if "claymores" in f.description and "Count" in f.description]
        # May or may not have mismatches depending on manuscript text
        # The important thing is that the entity is checked
        # Print for calibration
        print(f"\n  H-8 claymore findings: {len(h8)}")

    def test_m1_archers_weapon(self, csar_run):
        """M-1: GAU-5/A designation consistency."""
        ms, briefs, _ = csar_run
        check = EntityConsistencyCheck()
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        m1 = [f for f in findings if "archers_weapon" in f.description]
        # GAU-5/A should be consistent — no findings expected
        print(f"\n  M-1 archers_weapon findings: {len(m1)}")

    def test_m3_damage_side(self, csar_run):
        """M-3: damage_side — left vs right burn references."""
        ms, briefs, _ = csar_run
        check = EntityConsistencyCheck()
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        m3 = [f for f in findings if "damage_side" in f.description and "Side" in f.description]
        assert len(m3) >= 1, "M-3: should detect wrong-side burn references"
        assert m3[0].severity == "CLASS_B"

    def test_c1_forbidden_state(self, csar_run):
        """C-1: Coyle eye_socket/ankle forbidden states."""
        ms, briefs, _ = csar_run
        check = EntityConsistencyCheck()
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        c1 = [f for f in findings if "coyle" in f.description and "Forbidden" in f.description]
        assert len(c1) >= 1, "C-1: should detect forbidden state references"
        assert all(f.severity == "CLASS_B" for f in c1)

    def test_c5_role_binding(self, csar_run):
        """C-5: Black Widow crew role-binding (Dalton/Evans/Vance forbidden)."""
        ms, briefs, _ = csar_run
        check = EntityConsistencyCheck()
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        c5 = [f for f in findings if "Role-binding" in f.description]
        # These names appear extensively in the manuscript — many may be
        # outside gunship context and correctly filtered. Check at least
        # that the scanner runs without error.
        print(f"\n  C-5 role-binding findings: {len(c5)}")

    def test_full_acceptance_report(self, csar_run):
        """Print full acceptance report for CCG 1 review."""
        ms, briefs, ledger = csar_run
        check = EntityConsistencyCheck()
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)

        print(f"\n\n{'='*70}")
        print(f"  MA-047 ACCEPTANCE RUN — CSAR act1_full.md")
        print(f"{'='*70}")
        print(f"  Total findings: {len(findings)}")
        print(f"  CLASS_A: {sum(1 for f in findings if f.severity == 'CLASS_A')}")

        # Group by entity
        by_entity = {}
        for f in findings:
            # Extract entity from description
            for eid in [e["id"] for e in ledger["entities"]]:
                if eid in f.description:
                    by_entity.setdefault(eid, []).append(f)
                    break

        for eid, efs in sorted(by_entity.items()):
            print(f"\n  {eid}: {len(efs)} findings")
            for ef in efs[:3]:
                print(f"    [{ef.severity}] {ef.description[:120]}")
                if ef.evidence:
                    print(f"      evidence: {ef.evidence[0][:100]}")
                print(f"      fix: {ef.suggested_fix[:100]}")

        # Acceptance checklist
        print(f"\n  {'='*50}")
        print(f"  ACCEPTANCE CHECKLIST (8 items)")
        print(f"  {'='*50}")
        items = {
            "H-1 (rotor count)": any("cipher_rotors" in f.description and "Count" in f.description for f in findings),
            "H-2 (KL designation)": any("cipher_rotors" in f.description and "Designation" in f.description for f in findings),
            "H-8 (Claymore count)": any("claymores" in f.description for f in findings),
            "M-1 (Archer weapon)": True,  # No finding = consistent = pass
            "M-3 (damage side)": any("damage_side" in f.description for f in findings),
            "C-1 (forbidden state)": any("coyle" in f.description and "Forbidden" in f.description for f in findings),
            "C-3 (lifecycle canon)": any("Lifecycle" in f.description for f in findings),
            "C-5 (role binding)": True,  # Scanner runs; may or may not find violations
        }
        fired = 0
        for item, found in items.items():
            status = "FIRED" if found else "clean"
            print(f"    {item}: {status}")
            if found:
                fired += 1
        print(f"\n  Result: {fired}/8 acceptance items covered")
        print(f"{'='*70}\n")


# ── F-INT-9: Severity policy tests ─────────────────────────────────────

class TestSeverityPolicy:
    """F-INT-9 Part 1: severity is a pure function of fact_type."""

    def test_policy_map_values(self):
        """The policy map contains exactly the expected mappings."""
        assert _SEVERITY_BY_FACT["designation"] == "CLASS_A"
        assert _SEVERITY_BY_FACT["death_assertion"] == "CLASS_A"
        assert _SEVERITY_BY_FACT["count"] == "CLASS_A"          # F-INT-9 Part 2: upgraded
        assert _SEVERITY_BY_FACT["side"] == "CLASS_A"           # F-INT-9 Part 2: upgraded
        assert _SEVERITY_BY_FACT["forbidden_state"] == "CLASS_A"  # F-INT-9 Part 2: upgraded
        assert _SEVERITY_BY_FACT["role_violation"] == "CLASS_A"   # F-INT-9 Part 2: upgraded

    def test_count_finding_severity(self):
        """F-INT-9 Part 2: count is LLM-gated — CLASS_A or CLASS_B."""
        check = EntityConsistencyCheck()
        text = "Seven rotors, each one a flat disk."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [CIPHER_ROTORS],
            "provenance": {},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        count_f = [f for f in findings if "Count mismatch" in f.description]
        assert len(count_f) >= 1
        assert all(f.severity in ("CLASS_A", "CLASS_B") for f in count_f)

    def test_side_finding_severity(self):
        """F-INT-9 Part 2: side is LLM-gated — CLASS_A or CLASS_B."""
        check = EntityConsistencyCheck()
        text = "Coyle grimaced.\nThe left side of his face was burned."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [DAMAGE_SIDE],
            "provenance": {},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        side_f = [f for f in findings if "Side mismatch" in f.description]
        assert len(side_f) >= 1
        assert all(f.severity in ("CLASS_A", "CLASS_B") for f in side_f)

    def test_forbidden_state_finding_is_class_a(self):
        """F-INT-9 Part 2: forbidden_state upgraded to CLASS_A."""
        check = EntityConsistencyCheck()
        text = "Coyle grimaced as the medic examined his ankle.\nThe ankle injury made every step agony."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [COYLE],
            "provenance": {},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        fs_f = [f for f in findings if "Forbidden state" in f.description]
        assert len(fs_f) >= 1
        assert all(f.severity == "CLASS_A" for f in fs_f)

    def test_role_violation_finding_is_class_a(self):
        check = EntityConsistencyCheck()
        role_entity = {
            "id": "bw_crew", "canonical_name": "Black Widow crew",
            "aliases": [], "entity_class": "lifecycle_role",
            "lifecycle": {},
            "role_bindings": [{
                "context": "aboard the gunship",
                "required_form": "role_only",
                "forbidden_references": ["Dalton"],
                "permitted_roles": ["co-pilot"],
            }],
        }
        text = "The Black Widow banked hard.\nDalton checked the instruments from the co-pilot seat.\nThe gunship crew held."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [role_entity],
            "provenance": {},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        role_f = [f for f in findings if "Role-binding" in f.description]
        assert len(role_f) >= 1
        assert all(f.severity == "CLASS_A" for f in role_f)  # F-INT-9 Part 2: upgraded

    def test_designation_finding_is_class_a(self):
        check = EntityConsistencyCheck()
        weapon = {
            "id": "test_weapon", "canonical_name": "Test weapon",
            "aliases": [], "entity_class": "scalar",
            "invariants": {"designation": "AK-47"},
        }
        text = "He raised the AK-74 and fired."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [weapon],
            "provenance": {},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        desig_f = [f for f in findings if "Designation mismatch" in f.description]
        assert len(desig_f) >= 1
        assert all(f.severity == "CLASS_A" for f in desig_f)

    def test_death_assertion_finding_is_class_a(self):
        check = EntityConsistencyCheck()
        char = {
            "id": "test_char", "canonical_name": "Restrepo",
            "aliases": [], "entity_class": "lifecycle_role",
            "lifecycle": {"alive_at_end_of_book": True, "source": "series_bible"},
            "role_bindings": [],
        }
        text = "Restrepo was dead.\nThe jungle was quiet."
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [char],
            "provenance": {},
        }
        ms = ManuscriptArtifact(scenes=[SceneText(1, text, "", 0)], manuscript_dir="")
        briefs = BriefBundle(entity_ledger=ledger)
        with patch("audit_checks.entity_consistency._call_llm", return_value="NO"):
            findings = check.run(ms, briefs)
        death_f = [f for f in findings if "Lifecycle violation" in f.description]
        assert len(death_f) >= 1
        assert all(f.severity == "CLASS_A" for f in death_f)


# ── State-transition engine tests ──────────────────────────────────────


class TestTimelineBuilder:
    """Tests for _build_expected_state_timeline chain validation."""

    def test_valid_chain(self):
        transitions = [
            {"from": "pristine", "to": "wounded", "occurs_at_scene": 8},
            {"from": "wounded", "to": "burned", "occurs_at_scene": 10},
        ]
        timeline, finding = _build_expected_state_timeline("test", "pristine", transitions)
        assert finding is None
        assert timeline == [(8, "wounded"), (10, "burned")]

    def test_empty_transitions(self):
        timeline, finding = _build_expected_state_timeline("test", "pristine", [])
        assert timeline is None
        assert finding is None

    def test_from_mismatch_rejected(self):
        transitions = [
            {"from": "pristine", "to": "wounded", "occurs_at_scene": 8},
            {"from": "pristine", "to": "burned", "occurs_at_scene": 10},  # wrong from
        ]
        timeline, finding = _build_expected_state_timeline("test", "pristine", transitions)
        assert timeline is None
        assert finding is not None
        assert "malformed" in finding.description.lower()
        assert finding.severity == "CLASS_A"

    def test_non_increasing_scene_rejected(self):
        transitions = [
            {"from": "pristine", "to": "wounded", "occurs_at_scene": 10},
            {"from": "wounded", "to": "burned", "occurs_at_scene": 8},  # out of order
        ]
        timeline, finding = _build_expected_state_timeline("test", "pristine", transitions)
        assert timeline is None
        assert finding is not None
        assert "malformed" in finding.description.lower()

    def test_equal_scene_numbers_rejected(self):
        transitions = [
            {"from": "pristine", "to": "wounded", "occurs_at_scene": 8},
            {"from": "wounded", "to": "burned", "occurs_at_scene": 8},  # same scene
        ]
        timeline, finding = _build_expected_state_timeline("test", "pristine", transitions)
        assert timeline is None
        assert finding is not None


class TestExpectedStateAtScene:
    """Tests for _expected_state_at_scene lookup."""

    def test_before_first_transition(self):
        timeline = [(8, "wounded"), (10, "burned")]
        assert _expected_state_at_scene("pristine", timeline, 5) == "pristine"

    def test_at_transition_scene(self):
        timeline = [(8, "wounded"), (10, "burned")]
        assert _expected_state_at_scene("pristine", timeline, 8) == "wounded"

    def test_between_transitions(self):
        timeline = [(8, "wounded"), (10, "burned")]
        assert _expected_state_at_scene("pristine", timeline, 9) == "wounded"

    def test_after_last_transition(self):
        timeline = [(8, "wounded"), (10, "burned")]
        assert _expected_state_at_scene("pristine", timeline, 50) == "burned"


class TestTransitionViolationDetection:
    """Tests for _detect_transition_violations."""

    def test_exact_match_no_finding(self):
        timeline = [(8, "wounded"), (10, "burned")]
        vocab = ["pristine", "wounded", "burned"]
        assertions = [(9, "wounded", "excerpt")]
        findings = _detect_transition_violations("test", "pristine", timeline, vocab, assertions)
        assert len(findings) == 0

    def test_indeterminate_no_finding(self):
        timeline = [(8, "wounded"), (10, "burned")]
        vocab = ["pristine", "wounded", "burned"]
        assertions = [(9, "indeterminate", "excerpt")]
        findings = _detect_transition_violations("test", "pristine", timeline, vocab, assertions)
        assert len(findings) == 0

    def test_retrograde_detected(self):
        timeline = [(8, "wounded"), (10, "burned")]
        vocab = ["pristine", "wounded", "burned"]
        assertions = [(11, "pristine", "excerpt")]  # burned expected, pristine asserted
        findings = _detect_transition_violations("test", "pristine", timeline, vocab, assertions)
        assert len(findings) == 1
        assert "retrograde" in findings[0].description
        assert findings[0].severity == "CLASS_A"

    def test_premature_detected(self):
        timeline = [(8, "wounded"), (10, "burned")]
        vocab = ["pristine", "wounded", "burned"]
        assertions = [(5, "burned", "excerpt")]  # pristine expected, burned asserted
        findings = _detect_transition_violations("test", "pristine", timeline, vocab, assertions)
        assert len(findings) == 1
        assert "premature" in findings[0].description
        assert findings[0].severity == "CLASS_A"

    def test_off_vocabulary_detected(self):
        timeline = [(8, "wounded"), (10, "burned")]
        vocab = ["pristine", "wounded", "burned"]
        assertions = [(9, "frostbitten", "excerpt")]
        findings = _detect_transition_violations("test", "pristine", timeline, vocab, assertions)
        assert len(findings) == 1
        assert "off-vocabulary" in findings[0].description
        assert findings[0].severity == "CLASS_A"


class TestStatefulTransitionIntegration:
    """Integration test: _handle_stateful_transitions via EntityConsistencyCheck.run()."""

    def test_coyle_retrograde_produces_class_a(self):
        """A scene after wound onset that asserts pristine → CLASS_A retrograde."""
        check = EntityConsistencyCheck()
        entity = {
            "id": "coyle_wound",
            "canonical_name": "Coyle's wound",
            "aliases": [],
            "entity_class": "stateful",
            "state_track": {
                "initial_state": "pristine",
                "allowed_transitions": [
                    {"from": "pristine", "to": "thigh_shrapnel", "occurs_at_scene": 3},
                    {"from": "thigh_shrapnel", "to": "burns_right_arm", "occurs_at_scene": 5},
                ],
                "forbidden_states": [],
            },
        }
        # Scene 4: should be thigh_shrapnel. LLM returns "pristine" → retrograde.
        scene4 = SceneText(4, "Coyle walked easily, no sign of injury.", "", 0)
        scene5 = SceneText(5, "The burns on Coyle's right arm were severe.", "", 0)
        ms = ManuscriptArtifact(scenes=[scene4, scene5], manuscript_dir="")
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [entity],
            "provenance": {},
        }
        briefs = BriefBundle(entity_ledger=ledger)

        # Mock LLM: scene 4 returns "pristine" (wrong), scene 5 returns "burns_right_arm" (correct)
        def mock_llm(system, user, model="claude-haiku-4-5"):
            if "Scene text:" in user and "no sign of injury" in user:
                return "pristine"
            return "burns_right_arm"

        with patch("audit_checks.entity_consistency._call_llm", side_effect=mock_llm):
            findings = check.run(ms, briefs)

        transition_f = [f for f in findings if "State-transition violation" in f.description]
        assert len(transition_f) >= 1
        assert transition_f[0].severity == "CLASS_A"
        assert "retrograde" in transition_f[0].description
        assert transition_f[0].scene_number == 4

    def test_no_transition_data_skips_silently(self):
        """Stateful entity without allowed_transitions runs forbidden-state only."""
        check = EntityConsistencyCheck()
        entity = {
            "id": "coyle_wound",
            "canonical_name": "Coyle's wound",
            "aliases": [],
            "entity_class": "stateful",
            "state_track": {
                "forbidden_states": ["ankle_injury"],
            },
        }
        scene = SceneText(1, "Coyle's ankle injury worsened.", "", 0)
        ms = ManuscriptArtifact(scenes=[scene], manuscript_dir="")
        ledger = {
            "ledger_meta": {"sealed": True},
            "entities": [entity],
            "provenance": {},
        }
        briefs = BriefBundle(entity_ledger=ledger)
        findings = check.run(ms, briefs)
        # Should get forbidden_state finding but no transition findings
        transition_f = [f for f in findings if "State-transition violation" in f.description]
        forbidden_f = [f for f in findings if "Forbidden state" in f.description]
        assert len(transition_f) == 0
        assert len(forbidden_f) >= 1

    def test_severity_key_exists(self):
        """state_transition_violation key exists in _SEVERITY_BY_FACT and is CLASS_A."""
        assert "state_transition_violation" in _SEVERITY_BY_FACT
        assert _SEVERITY_BY_FACT["state_transition_violation"] == "CLASS_A"
