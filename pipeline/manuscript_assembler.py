"""V26 shim — do not put logic here."""
from . import manuscript_assembler_v26_20260611 as _mod  # canonical
from .manuscript_assembler_v26_20260611 import *
import sys as _sys
_self = _sys.modules[__name__]
for _name in dir(_mod):
    if not hasattr(_self, _name):
        setattr(_self, _name, getattr(_mod, _name))
del _self, _name
