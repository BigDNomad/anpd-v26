"""V26 shim — aliases this name to the canonical module."""
import sys as _sys
from . import audit_existing_manuscript_v26_20260611 as _canonical
_sys.modules[__name__] = _canonical
