"""V26 shim — aliases this name to the canonical module."""
import importlib as _il
import sys as _sys
_canonical = _il.import_module("pipeline.formatter_v26_20260611")
_sys.modules[__name__] = _canonical
