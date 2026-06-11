"""V26 shim — aliases this name to the canonical module."""
import importlib as _il
import sys as _sys
try:
    _canonical = _il.import_module("pipeline.pipeline_receipt_writer_v26_20260611")
except (ImportError, ModuleNotFoundError):
    _canonical = _il.import_module("pipeline_receipt_writer_v26_20260611")
_sys.modules[__name__] = _canonical

if __name__ == "__main__":
    if hasattr(_canonical, "main"):
        _canonical.main()
    else:
        print(f"ERROR: {_canonical.__name__} has no main() — cannot run as subprocess", file=_sys.stderr)
        _sys.exit(1)
