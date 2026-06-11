"""V26 test configuration — add pipeline directory to sys.path."""
import sys
import os

# Add repo root so 'import pipeline.X' works, and add pipeline/ so bare
# 'import X' (as V25 tests use) resolves to pipeline/X.py.  The bare-name
# imports must go through the package to honour relative-import shims, so we
# register pipeline/ as a namespace *and* individually import via the package.
_repo = os.path.join(os.path.dirname(__file__), '..')
_pipe = os.path.join(_repo, 'pipeline')
sys.path.insert(0, _repo)

# Patch: when a test does "from X import ...", Python must find X inside the
# pipeline package (so relative imports in shims work).  We achieve this by
# making 'pipeline' importable as a package and then aliasing each shim module
# at the top-level of sys.modules.
import importlib as _il
import pipeline as _pkg

def _ensure(name):
    """Make 'import <name>' resolve to pipeline.<name>."""
    full = f'pipeline.{name}'
    try:
        if full not in sys.modules:
            _il.import_module(full)
        sys.modules.setdefault(name, sys.modules[full])
    except (ImportError, ModuleNotFoundError):
        pass  # module not yet transferred — skip

# Register every shim that exists in pipeline/ so bare imports work.
# Multi-pass: some modules import others at module level via bare names,
# so we retry until no new modules register (handles dependency ordering).
_shims = [_f[:-3] for _f in os.listdir(_pipe)
          if _f.endswith('.py') and not _f.startswith('_') and '_v26_' not in _f]
_prev = -1
while len(sys.modules) != _prev:
    _prev = len(sys.modules)
    for _n in _shims:
        _ensure(_n)
