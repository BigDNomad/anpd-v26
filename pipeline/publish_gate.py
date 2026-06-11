"""V26 shim — aliases this name to the canonical module."""
import sys as _sys
from . import publish_gate_v26_20260611 as _canonical
_sys.modules[__name__] = _canonical
