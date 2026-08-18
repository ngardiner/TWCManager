"""
Unit test configuration.

The project uses namespace packages (find_namespace_packages in setup.py), so
lib/TWCManager/ has no __init__.py.  When running tests from the project root
without an installed package, Python finds TWCManager.py (the entry-point
launcher) before the lib/TWCManager/ namespace package, causing the launcher
to execute and fail (it tries to drop privileges to a 'twcmanager' OS user).

Fix: pre-register TWCManager in sys.modules as a package pointing at
lib/TWCManager/ so that subsequent `from TWCManager.*` imports resolve
correctly without ever executing the launcher script.
"""

import os
import sys
import types

_lib_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "lib")
)

# Insert lib/ so sub-package imports are resolvable.
if _lib_path not in sys.path:
    sys.path.insert(0, _lib_path)

# Pre-register the top-level namespace so Python never falls back to
# executing TWCManager.py (the root launcher script).
if "TWCManager" not in sys.modules:
    _pkg = types.ModuleType("TWCManager")
    _pkg.__path__ = [os.path.join(_lib_path, "TWCManager")]
    _pkg.__package__ = "TWCManager"
    sys.modules["TWCManager"] = _pkg
