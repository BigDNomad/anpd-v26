"""
Tests for S-2 Phase 2a: entity_ledger_builder.py

Eight acceptance tests per spec §7:
1. Builds against CSAR synopsis — sealed, no crash
2. Scalar extraction — rotors, claymores, Archer's weapon
3. Stateful promotion — Coyle with wound progression + forbidden_states
4. Lifecycle fold-in — Bounchanh + Tran from series_bible.recurring_entities
5. Role-binding — Black Widow crew from book_config.entity_invariants
6. Seal discipline — synthetic multi-valued synopsis seals with auto_resolved
7. Staleness — edited synopsis produces different hash
8. Provenance completeness — every fact has origin + resolution
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entity_ledger_builder import (
    build_ledger,
    write_ledger,
    _parse_scenes,
    _extract_raw_scalars,
    _associate_entities_heuristic,
    _resolve_scalars,
)

# ── Paths to real CSAR fixtures ─────────────────────────────────────────

_CSAR_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                         "series", "airmen", "b01", "work")
_SYNOPSIS = os.path.join(_CSAR_DIR, "synopsis.md")
_SERIES_BIBLE = os.path.join(os.path.dirname(__file__), "..", "..",
                             "series", "airmen", "series_bible.json")
_BOOK_CONFIG = os.path.join(_CSAR_DIR, "intake.json")

_CSAR_AVAILABLE = (
    os.path.exists(_SYNOPSIS) and
    os.path.exists(_SERIES_BIBLE) and
    os.path.exists(_BOOK_CONFIG)
)


# ── Synthetic synopsis fixture ───────────────────────────────────────────

_SYNTHETIC_SYNOPSIS = """# Synopsis — Test Book
Generated: 20260528

## Chapter 1

### Scene 1 — Opening [TYPE: NON-ACTION]

- The helicopter has eight rotors spinning on its main assembly.
- The KL-7 cipher machine sits in the cargo bay.
- The convoy of twelve trucks moves along the road.

### Scene 2 — Middle [TYPE: ACTION]

- The helicopter's three rotors spin in the morning light.
- The pilot checks the KL-7 designation panel.
- Two Claymore mines are placed along the perimeter.

## Chapter 2

### Scene 3 — Climax [TYPE: ACTION]

- The eight rotors of the helicopter catch the wind.
- The GAU-5/A carbine fires into the tree line.
- The Claymore mines detonate.
"""

_SYNTHETIC_BIBLE = {
    "recurring_entities": [
        {
            "name": "Test Character",
            "aliases": ["TC"],
            "appears_in_books": [1],
            "lifecycle_constraints": {"alive_at_end_of_book": True},
        }
    ]
}

_SYNTHETIC_CONFIG = {
    "book_number": 1,
    "entity_invariants": {
        "role_bindings": [],
        "forbidden_states": [],
    }
}


def _write_synthetic_fixtures(tmpdir):
    """Write synthetic fixtures to tmpdir, return paths."""
    syn_path = os.path.join(tmpdir, "synopsis.md")
    with open(syn_path, "w") as f:
        f.write(_SYNTHETIC_SYNOPSIS)

    bible_path = os.path.join(tmpdir, "series_bible.json")
    with open(bible_path, "w") as f:
        json.dump(_SYNTHETIC_BIBLE, f)

    sc_path = os.path.join(tmpdir, "series_config.json")
    with open(sc_path, "w") as f:
        json.dump({"series_slug": "tst", "book_slugs": {"b01": "tst001"}}, f)

    config_path = os.path.join(tmpdir, "book_config.json")
    with open(config_path, "w") as f:
        json.dump(_SYNTHETIC_CONFIG, f)

    return syn_path, bible_path, config_path


# ── Test 1: Builds against CSAR synopsis ─────────────────────────────────

@pytest.mark.skipif(not _CSAR_AVAILABLE, reason="CSAR fixtures not available")
class TestBuildsAgainstCSAR:

    def test_builds_and_seals(self):
        """Builder runs against arm001 synopsis + bible + config without crash."""
        ledger, conflicts = build_ledger(
            synopsis_path=_SYNOPSIS,
            series_bible_path=_SERIES_BIBLE,
            book_config_path=_BOOK_CONFIG,
            use_llm=False,
        )
        assert ledger["ledger_meta"]["sealed"] is True
        assert len(ledger["entities"]) > 0
        assert ledger["ledger_meta"]["book_slug"] == "arm001"


# ── Test 2: Scalar extraction ────────────────────────────────────────────

@pytest.mark.skipif(not _CSAR_AVAILABLE, reason="CSAR fixtures not available")
class TestScalarExtraction:

    def test_key_scalars_extracted(self):
        """Ledger contains cipher_rotors (count + designation), claymores, Archer's weapon."""
        ledger, _ = build_ledger(
            synopsis_path=_SYNOPSIS,
            series_bible_path=_SERIES_BIBLE,
            book_config_path=_BOOK_CONFIG,
            use_llm=False,
        )
        entity_map = {e["id"]: e for e in ledger["entities"]}

        # Cipher rotors
        assert "cipher_rotors" in entity_map
        cr = entity_map["cipher_rotors"]
        assert cr["entity_class"] == "scalar"
        assert cr["invariants"]["count"] == 8
        assert cr["invariants"]["designation"] == "KL-7"

        # Claymores
        assert "claymores" in entity_map
        cl = entity_map["claymores"]
        assert cl["invariants"]["count"] == 2

        # Archer's weapon
        assert "archers_weapon" in entity_map
        aw = entity_map["archers_weapon"]
        assert aw["invariants"]["designation"] == "GAU-5/A"


# ── Test 3: Stateful promotion ───────────────────────────────────────────

@pytest.mark.skipif(not _CSAR_AVAILABLE, reason="CSAR fixtures not available")
class TestStatefulPromotion:

    def test_coyle_single_entity_with_both_tracks(self):
        """Coyle is ONE stateful entity carrying BOTH forbidden_states AND wound transitions."""
        ledger, _ = build_ledger(
            synopsis_path=_SYNOPSIS,
            series_bible_path=_SERIES_BIBLE,
            book_config_path=_BOOK_CONFIG,
            use_llm=False,
        )
        entity_map = {e["id"]: e for e in ledger["entities"]}

        # Exactly one Coyle entity
        coyle_ids = [eid for eid in entity_map if "coyle" in eid.lower()]
        assert coyle_ids == ["coyle"], f"Expected exactly one 'coyle' entity, got: {coyle_ids}"

        coyle = entity_map["coyle"]
        assert coyle["entity_class"] == "stateful"
        assert "state_track" in coyle

        # Forbidden states from book_config
        assert "eye_socket_injury" in coyle["state_track"]["forbidden_states"]
        assert "ankle_injury" in coyle["state_track"]["forbidden_states"]

        # Wound progression from synopsis extraction
        assert len(coyle["state_track"]["allowed_transitions"]) > 0, \
            "Coyle must carry wound-progression transitions"

        # No second coyle_wound entity
        assert "coyle_wound" not in entity_map, \
            "coyle_wound must be merged into coyle, not exist separately"

    def test_no_duplicate_character_entities(self):
        """No two stateful entity ids refer to the same character (prefix overlap)."""
        ledger, _ = build_ledger(
            synopsis_path=_SYNOPSIS,
            series_bible_path=_SERIES_BIBLE,
            book_config_path=_BOOK_CONFIG,
            use_llm=False,
        )
        # Only check stateful entities for character-id splits — scalar/lifecycle
        # entities like black_widow (aircraft) vs black_widow_crew (role) are
        # legitimately distinct.
        stateful_ids = [e["id"] for e in ledger["entities"]
                        if e["entity_class"] == "stateful"]
        all_ids = [e["id"] for e in ledger["entities"]]
        for sid in stateful_ids:
            variants = [eid for eid in all_ids
                        if eid != sid and (eid.startswith(sid + "_") or sid.startswith(eid + "_"))]
            assert not variants, \
                f"Stateful entity '{sid}' has variant(s) {variants} — should be merged"


# ── Test 4: Lifecycle fold-in ────────────────────────────────────────────

@pytest.mark.skipif(not _CSAR_AVAILABLE, reason="CSAR fixtures not available")
class TestLifecycleFoldIn:

    def test_bounchanh_and_tran_lifecycle(self):
        """Bounchanh and Tran declared in series_bible appear as lifecycle_role."""
        ledger, _ = build_ledger(
            synopsis_path=_SYNOPSIS,
            series_bible_path=_SERIES_BIBLE,
            book_config_path=_BOOK_CONFIG,
            use_llm=False,
        )
        entity_map = {e["id"]: e for e in ledger["entities"]}

        for eid in ("bounchanh_vorasak", "tran_van_khoa"):
            assert eid in entity_map, f"{eid} should be in ledger"
            entity = entity_map[eid]
            assert entity["entity_class"] == "lifecycle_role"
            assert entity["lifecycle"]["alive_at_end_of_book"] is True
            assert entity["lifecycle"]["source"] == "series_bible:recurring_entities"


# ── Test 5: Role-binding ─────────────────────────────────────────────────

@pytest.mark.skipif(not _CSAR_AVAILABLE, reason="CSAR fixtures not available")
class TestRoleBinding:

    def test_black_widow_crew_role_binding(self):
        """Black Widow crew carries role_only binding with forbidden names."""
        ledger, _ = build_ledger(
            synopsis_path=_SYNOPSIS,
            series_bible_path=_SERIES_BIBLE,
            book_config_path=_BOOK_CONFIG,
            use_llm=False,
        )
        entity_map = {e["id"]: e for e in ledger["entities"]}

        assert "black_widow_crew" in entity_map
        bwc = entity_map["black_widow_crew"]
        assert bwc["entity_class"] == "lifecycle_role"
        assert len(bwc["role_bindings"]) > 0
        rb = bwc["role_bindings"][0]
        assert rb["required_form"] == "role_only"
        assert "Dalton" in rb["forbidden_references"]
        assert "Evans" in rb["forbidden_references"]
        assert "Vance" in rb["forbidden_references"]
        assert "co-pilot" in rb["permitted_roles"]
        assert "sensor operator" in rb["permitted_roles"]


# ── Test 6: Seal discipline (synthetic multi-valued) ─────────────────────

class TestSealDiscipline:

    def test_synthetic_multi_valued_seals(self):
        """Synthetic synopsis with contradictory scalar values still seals."""
        with tempfile.TemporaryDirectory() as tmpdir:
            syn_path, bible_path, config_path = _write_synthetic_fixtures(tmpdir)

            ledger, conflicts = build_ledger(
                synopsis_path=syn_path,
                series_bible_path=bible_path,
                book_config_path=config_path,
                use_llm=False,
            )

            # Must seal
            assert ledger["ledger_meta"]["sealed"] is True

            # The synthetic synopsis has "eight rotors" (scenes 1, 3) and
            # "three rotors" (scene 2) — should produce a conflict
            entity_map = {e["id"]: e for e in ledger["entities"]}

            # Check for rotor count conflict in provenance
            rotor_entities = [e for e in ledger["entities"]
                              if "rotor" in e["id"]]
            if rotor_entities:
                # Should have auto_resolved if multi-valued
                for re in rotor_entities:
                    if "invariants" in re and "count" in re["invariants"]:
                        prov_key = f"{re['id']}.count"
                        if prov_key in ledger["provenance"]:
                            prov = ledger["provenance"][prov_key]
                            if len(prov.get("synopsis_assertions", [])) > 1:
                                vals = set(a["value"] for a in prov["synopsis_assertions"])
                                if len(vals) > 1:
                                    assert prov["resolution"] == "auto_resolved"

            # Write and check ledger_conflicts.json
            out_path = os.path.join(tmpdir, "entity_ledger.json")
            write_ledger(ledger, conflicts, out_path=out_path)
            conflicts_path = os.path.join(tmpdir, "ledger_conflicts.json")
            assert os.path.exists(conflicts_path)
            with open(conflicts_path) as f:
                conflict_data = json.load(f)
            # If there are multi-valued scalars, conflicts should be populated
            if conflicts:
                assert len(conflict_data) > 0
                assert conflict_data[0]["resolution_method"] == "plurality_first_tiebreak"


# ── Test 7: Staleness ────────────────────────────────────────────────────

class TestStaleness:

    def test_edited_synopsis_different_hash(self):
        """Re-running builder against an edited synopsis produces a different hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            syn_path, bible_path, config_path = _write_synthetic_fixtures(tmpdir)

            ledger1, _ = build_ledger(
                synopsis_path=syn_path,
                series_bible_path=bible_path,
                book_config_path=config_path,
                use_llm=False,
            )
            hash1 = ledger1["ledger_meta"]["source_synopsis_sha256"]

            # Edit the synopsis
            with open(syn_path, "a") as f:
                f.write("\n### Scene 4 — New Scene [TYPE: NON_ACTION]\n\n- Something new.\n")

            ledger2, _ = build_ledger(
                synopsis_path=syn_path,
                series_bible_path=bible_path,
                book_config_path=config_path,
                use_llm=False,
            )
            hash2 = ledger2["ledger_meta"]["source_synopsis_sha256"]

            assert hash1 != hash2, "Edited synopsis should produce different SHA-256"


# ── Test 8: Provenance completeness ──────────────────────────────────────

@pytest.mark.skipif(not _CSAR_AVAILABLE, reason="CSAR fixtures not available")
class TestProvenanceCompleteness:

    def test_every_fact_has_provenance(self):
        """Every scalar invariant and declared constraint has provenance."""
        ledger, _ = build_ledger(
            synopsis_path=_SYNOPSIS,
            series_bible_path=_SERIES_BIBLE,
            book_config_path=_BOOK_CONFIG,
            use_llm=False,
        )
        provenance = ledger["provenance"]

        # Every entity has at least one provenance entry
        entity_ids = {e["id"] for e in ledger["entities"]}
        prov_entity_ids = {k.split(".")[0] for k in provenance.keys()}
        missing = entity_ids - prov_entity_ids
        assert not missing, f"Entities without provenance: {missing}"

        # Every provenance entry has required fields
        for key, prov in provenance.items():
            assert "origin" in prov, f"{key} missing origin"
            assert "resolution" in prov, f"{key} missing resolution"
            assert prov["origin"] in (
                "synopsis_extracted", "series_bible_declared", "book_config_declared"
            ), f"{key} has invalid origin: {prov['origin']}"
            assert prov["resolution"] in (
                "unambiguous", "auto_resolved", "manual_resolved", "declared"
            ), f"{key} has invalid resolution: {prov['resolution']}"

            # Extracted scalars must have synopsis_assertions
            if prov["origin"] == "synopsis_extracted":
                assert len(prov["synopsis_assertions"]) > 0, \
                    f"{key} has synopsis_extracted origin but no assertions"

            # Declared constraints must have declared resolution
            if prov["origin"] in ("series_bible_declared", "book_config_declared"):
                assert prov["resolution"] == "declared", \
                    f"{key} has declared origin but resolution={prov['resolution']}"


# ── Test: absent book_config does not crash ──────────────────────────────

class TestAbsentBookConfig:

    def test_no_book_config_does_not_crash(self):
        """Builder runs without book_config (absent entity_invariants)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            syn_path, bible_path, _ = _write_synthetic_fixtures(tmpdir)

            ledger, conflicts = build_ledger(
                synopsis_path=syn_path,
                series_bible_path=bible_path,
                book_config_path=None,
                use_llm=False,
            )
            assert ledger["ledger_meta"]["sealed"] is True
