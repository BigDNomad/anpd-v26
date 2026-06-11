"""V26 shim — aliases this name to the canonical module."""
import importlib as _il
import sys as _sys
try:
    _canonical = _il.import_module("pipeline.synopsis_parser_v26_20260612")
except (ImportError, ModuleNotFoundError):
    _canonical = _il.import_module("synopsis_parser_v26_20260612")
_sys.modules[__name__] = _canonical

if __name__ == "__main__":
    _sys.exit(_canonical.main() if hasattr(_canonical, "main") else 2)
