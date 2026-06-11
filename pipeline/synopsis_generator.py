"""V26 shim — aliases this name to the canonical module."""
import importlib as _il
import sys as _sys
try:
    _canonical = _il.import_module("pipeline.synopsis_generator_v26_20260611_T0600")
except (ImportError, ModuleNotFoundError):
    _canonical = _il.import_module("synopsis_generator_v26_20260611_T0600")
_sys.modules[__name__] = _canonical
