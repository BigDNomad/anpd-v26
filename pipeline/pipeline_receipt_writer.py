"""V26 shim — aliases this name to the canonical module."""
import sys as _sys
from . import pipeline_receipt_writer_v26_20260611 as _canonical
_sys.modules[__name__] = _canonical
