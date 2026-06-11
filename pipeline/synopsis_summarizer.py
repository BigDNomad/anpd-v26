"""V26 shim — aliases this name to the canonical module."""
import sys as _sys
from . import synopsis_summarizer_v26_20260611 as _canonical
_sys.modules[__name__] = _canonical
