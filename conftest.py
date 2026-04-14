"""
Root conftest.py for TWCManager tests.

Ensures lib/ is in sys.path so imports work correctly.
Initializes custom logging levels.
"""

import sys
from pathlib import Path

# Add lib directory to path so TWCManager package can be imported
lib_path = Path(__file__).parent / "lib"
if str(lib_path) not in sys.path:
    sys.path.insert(0, str(lib_path))

# Initialize custom logging levels
from TWCManager.LoggingLevels import initialize_logging_levels

initialize_logging_levels()
