"""
Root conftest.py for TWCManager tests.

Ensures lib/ is in sys.path so imports work correctly.
Initializes custom logging levels.
Provides verbose test diagnostics.
"""

import sys
from pathlib import Path
import logging

# Add lib directory to path so TWCManager package can be imported
lib_path = Path(__file__).parent / "lib"
if str(lib_path) not in sys.path:
    sys.path.insert(0, str(lib_path))
    print(f"[conftest] Added {lib_path} to sys.path")

# Initialize custom logging levels
from TWCManager.LoggingLevels import initialize_logging_levels

print("[conftest] Initializing custom logging levels...")
initialize_logging_levels()
print("[conftest] Custom logging levels initialized")

# Configure verbose logging for tests
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

print("[conftest] Test environment initialized successfully")
print(f"[conftest] Python path: {sys.path[:3]}")
print(f"[conftest] Logging level: DEBUG")
