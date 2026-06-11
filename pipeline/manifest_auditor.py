"""V26 shim — aliases this name to the canonical module."""
import importlib as _il
import sys as _sys
try:
    _canonical = _il.import_module("pipeline.manifest_auditor_v26_20260611_T2330")
except (ImportError, ModuleNotFoundError):
    _canonical = _il.import_module("manifest_auditor_v26_20260611_T2330")
_sys.modules[__name__] = _canonical
