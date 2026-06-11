"""Test that V26 shims propagate subprocess exit codes from main().

The systemic shim defect (2026-06-12): shims called _canonical.main()
without sys.exit(), so non-zero exit codes were silently swallowed.
This caused Gate 3 to vacuously pass when the auditor returned 2.

This test creates a fixture canonical module whose main() returns 2,
writes a shim pointing to it, invokes the shim as a subprocess, and
asserts the exit code is 2.
"""

import os
import subprocess
import sys
import tempfile
import textwrap

import pytest


def test_shim_propagates_nonzero_exit_code():
    """A shim must propagate main()'s return value as the process exit code."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg_dir = os.path.join(tmpdir, "fixture_pkg")
        os.makedirs(pkg_dir)

        # Write __init__.py
        with open(os.path.join(pkg_dir, "__init__.py"), "w") as fh:
            fh.write("")

        # Write canonical module whose main() returns 2
        with open(os.path.join(pkg_dir, "canonical_exit2.py"), "w") as fh:
            fh.write(textwrap.dedent("""\
                def main(argv=None):
                    return 2
            """))

        # Write shim using the V26 fixed pattern
        with open(os.path.join(pkg_dir, "shim_under_test.py"), "w") as fh:
            fh.write(textwrap.dedent("""\
                \"\"\"V26 shim — aliases this name to the canonical module.\"\"\"
                import importlib as _il
                import sys as _sys
                try:
                    _canonical = _il.import_module("fixture_pkg.canonical_exit2")
                except (ImportError, ModuleNotFoundError):
                    _canonical = _il.import_module("canonical_exit2")
                _sys.modules[__name__] = _canonical

                if __name__ == "__main__":
                    _sys.exit(_canonical.main() if hasattr(_canonical, "main") else 2)
            """))

        # Invoke the shim as a subprocess
        result = subprocess.run(
            [sys.executable, "-m", "fixture_pkg.shim_under_test"],
            capture_output=True,
            text=True,
            cwd=tmpdir,
        )

        assert result.returncode == 2, (
            f"Expected exit code 2, got {result.returncode}. "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )


def test_shim_propagates_zero_exit_code():
    """A shim must propagate exit code 0 when main() returns 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg_dir = os.path.join(tmpdir, "fixture_pkg")
        os.makedirs(pkg_dir)

        with open(os.path.join(pkg_dir, "__init__.py"), "w") as fh:
            fh.write("")

        with open(os.path.join(pkg_dir, "canonical_exit0.py"), "w") as fh:
            fh.write(textwrap.dedent("""\
                def main(argv=None):
                    return 0
            """))

        with open(os.path.join(pkg_dir, "shim_under_test.py"), "w") as fh:
            fh.write(textwrap.dedent("""\
                \"\"\"V26 shim — aliases this name to the canonical module.\"\"\"
                import importlib as _il
                import sys as _sys
                try:
                    _canonical = _il.import_module("fixture_pkg.canonical_exit0")
                except (ImportError, ModuleNotFoundError):
                    _canonical = _il.import_module("canonical_exit0")
                _sys.modules[__name__] = _canonical

                if __name__ == "__main__":
                    _sys.exit(_canonical.main() if hasattr(_canonical, "main") else 2)
            """))

        result = subprocess.run(
            [sys.executable, "-m", "fixture_pkg.shim_under_test"],
            capture_output=True,
            text=True,
            cwd=tmpdir,
        )

        assert result.returncode == 0, (
            f"Expected exit code 0, got {result.returncode}"
        )


def test_shim_returns_2_when_no_main():
    """A shim must exit 2 when canonical has no main()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg_dir = os.path.join(tmpdir, "fixture_pkg")
        os.makedirs(pkg_dir)

        with open(os.path.join(pkg_dir, "__init__.py"), "w") as fh:
            fh.write("")

        with open(os.path.join(pkg_dir, "canonical_no_main.py"), "w") as fh:
            fh.write("# No main function\nVALUE = 42\n")

        with open(os.path.join(pkg_dir, "shim_under_test.py"), "w") as fh:
            fh.write(textwrap.dedent("""\
                \"\"\"V26 shim — aliases this name to the canonical module.\"\"\"
                import importlib as _il
                import sys as _sys
                try:
                    _canonical = _il.import_module("fixture_pkg.canonical_no_main")
                except (ImportError, ModuleNotFoundError):
                    _canonical = _il.import_module("canonical_no_main")
                _sys.modules[__name__] = _canonical

                if __name__ == "__main__":
                    _sys.exit(_canonical.main() if hasattr(_canonical, "main") else 2)
            """))

        result = subprocess.run(
            [sys.executable, "-m", "fixture_pkg.shim_under_test"],
            capture_output=True,
            text=True,
            cwd=tmpdir,
        )

        assert result.returncode == 2, (
            f"Expected exit code 2 (no main), got {result.returncode}"
        )
