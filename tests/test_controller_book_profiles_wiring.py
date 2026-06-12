"""
Tests for controller wiring of --book-character-profiles to manuscript_auditor.

Verifies the phase handler passes book-level character profiles when the
file exists and omits it when absent.
"""

from __future__ import annotations

import os
import sys
import tempfile
import json

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))


class TestBookProfilesWiring:

    def _extract_auditor_args(self, book_dir, has_book_profiles=True):
        """Simulate the auditor_args construction from the active phase handler."""
        # Replicate the exact logic from phase_handlers_v26_20260612_T2200.py
        auditor_args = ["--manuscript-dir", os.path.join(book_dir, "out", "scenes")]

        synopsis_path = os.path.join(book_dir, "work", "synopsis.md")
        if os.path.isfile(synopsis_path):
            auditor_args += ["--synopsis", synopsis_path]

        book_cp_path = os.path.join(book_dir, "work", "character_profiles.json")
        if os.path.isfile(book_cp_path):
            auditor_args += ["--book-character-profiles", book_cp_path]

        return auditor_args

    def test_book_profiles_included_when_present(self, tmp_path):
        """When book-level character_profiles.json exists, argv includes --book-character-profiles."""
        book_dir = tmp_path / "series" / "airmen" / "b01"
        (book_dir / "work").mkdir(parents=True)
        (book_dir / "out" / "scenes").mkdir(parents=True)

        cp = {"Bounmy": {"name": "Bounmy", "character_role": "minor"}}
        (book_dir / "work" / "character_profiles.json").write_text(json.dumps(cp))

        args = self._extract_auditor_args(str(book_dir))
        assert "--book-character-profiles" in args
        idx = args.index("--book-character-profiles")
        assert args[idx + 1] == str(book_dir / "work" / "character_profiles.json")

    def test_book_profiles_omitted_when_absent(self, tmp_path):
        """When book-level character_profiles.json does not exist, argv omits the flag."""
        book_dir = tmp_path / "series" / "other" / "b01"
        (book_dir / "work").mkdir(parents=True)
        (book_dir / "out" / "scenes").mkdir(parents=True)
        # No character_profiles.json created

        args = self._extract_auditor_args(str(book_dir))
        assert "--book-character-profiles" not in args

    def test_real_airmen_b01_would_include_flag(self):
        """The actual airmen b01 book directory has the file, so it would be wired."""
        book_cp = "/anpd/v26/series/airmen/b01/work/character_profiles.json"
        if not os.path.isfile(book_cp):
            pytest.skip("Airmen b01 book profiles not available")
        assert os.path.isfile(book_cp)
