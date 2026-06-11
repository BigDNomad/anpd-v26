"""
V26 runtime bootstrap — bare-name sys.modules aliasing.

Importing this module registers every pipeline shim so that bare-name
imports (e.g. ``import master_controller``) resolve to the canonical
``pipeline.<name>_v26_20260611`` module, exactly as the test conftest does.

Usage (future entry-point wiring):
    from pipeline import _bootstrap  # noqa: F401  — side-effect import

This module is NOT required under pytest (conftest handles it).
"""

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure(name: str) -> None:
    """Make ``import <name>`` resolve to ``pipeline.<name>``."""
    full = f"pipeline.{name}"
    try:
        if full not in sys.modules:
            importlib.import_module(full)
        sys.modules.setdefault(name, sys.modules[full])
    except (ImportError, ModuleNotFoundError):
        pass


# Discover shims: every .py in pipeline/ that isn't underscore-prefixed
# and isn't a _v26_ canonical file.
_shims = sorted(
    f[:-3]
    for f in os.listdir(_HERE)
    if f.endswith(".py") and not f.startswith("_") and "_v26_" not in f
)

# Multi-pass: some modules import others at module level via bare names,
# so retry until no new modules register (handles dependency ordering).
_prev = -1
while len(sys.modules) != _prev:
    _prev = len(sys.modules)
    for _n in _shims:
        _ensure(_n)
