"""V26 shim — aliases this name to the canonical module."""
import importlib as _il
import sys as _sys
try:
    _canonical = _il.import_module("pipeline.state_tracker_v26_20260611")
except (ImportError, ModuleNotFoundError):
    _canonical = _il.import_module("state_tracker_v26_20260611")
_sys.modules[__name__] = _canonical
