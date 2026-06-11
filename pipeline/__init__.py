"""ANPD V26 pipeline package."""
import os as _os
import sys as _sys

# Add the pipeline directory to sys.path so that bare-name imports
# (e.g. ``from config_resolver import ...``) in canonical modules
# resolve to the shims in this directory.
_pipeline_dir = _os.path.dirname(_os.path.abspath(__file__))
if _pipeline_dir not in _sys.path:
    _sys.path.insert(0, _pipeline_dir)
