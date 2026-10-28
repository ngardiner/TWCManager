"""
Unit tests for TWCManager core module.

Tests application initialization, module loading, and core orchestration.

Note: lib/TWCManager/TWCManager.py is an executable entry point which starts
the application at import time, so it cannot be imported here. These tests
read what they need from the source directly instead.
"""

import ast
import logging
import os
import pytest
from unittest.mock import Mock, MagicMock, patch

_LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))


def _load_modules_available():
    """Read the modules_available list from the launcher source without
    importing it."""
    with open(os.path.join(_LIB_DIR, "TWCManager", "TWCManager.py")) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == "modules_available":
                    return [elt.value for elt in node.value.elts]
    return []


class TestLoggingLevels:
    """Test custom logging levels."""

    @pytest.fixture(autouse=True)
    def initialized_levels(self):
        from TWCManager.LoggingLevels import initialize_logging_levels

        initialize_logging_levels()

    def test_custom_logging_levels_defined(self):
        """Test that custom logging levels are defined."""
        assert logging.INFO2 == 19
        assert logging.INFO3 == 18
        assert logging.INFO4 == 17
        assert logging.INFO5 == 16
        assert logging.INFO6 == 15
        assert logging.INFO7 == 14
        assert logging.INFO8 == 13
        assert logging.INFO9 == 12
        assert logging.DEBUG2 == 9

    def test_custom_logging_level_names(self):
        """Test that custom logging level names are registered."""
        assert logging.getLevelName(19) == "INFO2"
        assert logging.getLevelName(18) == "INFO3"
        assert logging.getLevelName(17) == "INFO4"
        assert logging.getLevelName(16) == "INFO5"
        assert logging.getLevelName(15) == "INFO6"
        assert logging.getLevelName(14) == "INFO7"
        assert logging.getLevelName(13) == "INFO8"
        assert logging.getLevelName(12) == "INFO9"
        assert logging.getLevelName(9) == "DEBUG2"


class TestModuleAvailability:
    """Test module availability definitions."""

    def test_modules_available_list_exists(self):
        """Test that modules_available list is defined."""
        modules_available = _load_modules_available()

        assert isinstance(modules_available, list)
        assert len(modules_available) > 0

    def test_logging_modules_first(self):
        """Test that logging modules are listed first."""
        modules_available = _load_modules_available()

        # First modules should be logging modules
        assert "Logging.ConsoleLogging" in modules_available
        assert modules_available.index("Logging.ConsoleLogging") < 5

    def test_protocol_module_included(self):
        """Test that protocol module is included."""
        modules_available = _load_modules_available()

        assert "Protocol.TWCProtocol" in modules_available

    def test_interface_modules_included(self):
        """Test that interface modules are included."""
        modules_available = _load_modules_available()

        assert "Interface.Dummy" in modules_available
        assert "Interface.RS485" in modules_available
        assert "Interface.TCP" in modules_available

    def test_policy_module_included(self):
        """Test that policy module is included."""
        modules_available = _load_modules_available()

        assert "Policy.Policy" in modules_available

    def test_vehicle_modules_included(self):
        """Test that vehicle modules are included."""
        modules_available = _load_modules_available()

        assert "Vehicle.TeslaAPI" in modules_available
        assert "Vehicle.TeslaBLE" in modules_available

    def test_control_modules_included(self):
        """Test that control modules are included."""
        modules_available = _load_modules_available()

        assert "Control.HTTPControl" in modules_available
        assert "Control.MQTTControl" in modules_available

    def test_ems_modules_included(self):
        """Test that EMS modules are included."""
        modules_available = _load_modules_available()

        assert "EMS.HASS" in modules_available
        assert "EMS.Fronius" in modules_available


class TestLoggerFactory:
    """Test logger factory initialization."""

    def test_logger_created(self):
        """Test that logger is created."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory

        logger = LoggerFactory.get_logger("TWCManager", "Manager")

        assert logger is not None
        assert isinstance(logger, logging.Logger)

    def test_logger_name_contains_manager(self):
        """Test that logger name contains Manager."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory

        logger = LoggerFactory.get_logger("TWCManager", "Manager")

        assert "TWCManager" in logger.name


def _launcher_source():
    with open(os.path.join(_LIB_DIR, "TWCManager", "TWCManager.py")) as f:
        return f.read()


def _launcher_function(name):
    # TWCManager.py runs at import: one function is compiled from its source
    path = os.path.join(_LIB_DIR, "TWCManager", "TWCManager.py")
    fn = next(
        n
        for n in ast.parse(_launcher_source()).body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[name]


def _loop_source():
    # The serial loop: from the outer "while True:" to the shutdown
    source = _launcher_source()
    start = source.index("\nwhile True:")
    return source[start : source.index("shutdownRequested", start)]


class TestTokenRefreshTask:
    """Tesla tokens refresh on the background thread, never the serial loop (#690)."""

    def _run(self, carapi):
        master = Mock()
        master.getModuleByName.return_value = carapi
        refreshTokens = _launcher_function("refreshTokens")
        try:
            refreshTokens(master)
        except RuntimeError:
            pass
        return master

    @pytest.mark.parametrize("pending", [True, False])
    def test_refresh_tokens_task_requeues_itself(self, pending):
        carapi = Mock()
        carapi.listPending.return_value = pending
        master = self._run(carapi)
        carapi.refreshTokenIfNeeded.assert_called_once_with()
        assert carapi.loadList.call_count == (1 if pending else 0)
        master.queue_background_task.assert_called_once_with(
            {"cmd": "refreshTokens"}, delay=300
        )

    def test_a_raising_refresh_still_requeues(self):  # F10
        carapi = Mock()
        carapi.refreshTokenIfNeeded.side_effect = RuntimeError("boom")
        master = self._run(carapi)
        master.queue_background_task.assert_called_once_with(
            {"cmd": "refreshTokens"}, delay=300
        )

    def test_without_teslaapi_it_still_requeues(self):
        master = self._run(None)
        master.queue_background_task.assert_called_once_with(
            {"cmd": "refreshTokens"}, delay=300
        )

    def test_it_is_dispatched_and_queued_at_startup(self):
        source = _launcher_source()
        assert 'task["cmd"] == "refreshTokens"' in source
        assert 'master.queue_background_task({"cmd": "refreshTokens"}, delay=300)' in source

    def test_the_serial_loop_never_refreshes_tokens(self):
        src = _loop_source()
        assert "while True:" in src and "flushRS485" in src
        assert "refreshTokenIfNeeded" not in src
        assert "lastTeslaTokenRefreshCheck" not in src
