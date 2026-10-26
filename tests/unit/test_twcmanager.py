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


class TestModuleLoading:
    """Test module loading and instantiation."""

    def test_importlib_available(self):
        """Test that importlib is available for module loading."""
        import importlib

        assert importlib is not None
        assert hasattr(importlib, "import_module")

    def test_module_path_construction(self):
        """Test that module paths can be constructed."""
        module_name = "Logging.ConsoleLogging"
        parts = module_name.split(".")

        assert len(parts) == 2
        assert parts[0] == "Logging"
        assert parts[1] == "ConsoleLogging"


class TestConfigurationHandling:
    """Test configuration file handling."""

    def test_yaml_import_available(self):
        """Test that YAML is available for config parsing."""
        import yaml

        assert yaml is not None
        assert hasattr(yaml, "safe_load")

    def test_config_file_path_handling(self):
        """Test that config file paths can be handled."""
        import os.path

        test_path = "/etc/twcmanager/config.json"
        assert os.path.isabs(test_path)


class TestEnumDefinitions:
    """Test enum definitions."""

    def test_enum_import_available(self):
        """Test that Enum is available."""
        from enum import Enum

        assert Enum is not None

    def test_can_create_enum(self):
        """Test that enums can be created."""
        from enum import Enum

        class TestEnum(Enum):
            VALUE1 = 1
            VALUE2 = 2

        assert TestEnum.VALUE1.value == 1
        assert TestEnum.VALUE2.value == 2


class TestThreading:
    """Test threading support."""

    def test_threading_available(self):
        """Test that threading is available."""
        import threading

        assert threading is not None
        assert hasattr(threading, "Thread")

    def test_can_create_thread(self):
        """Test that threads can be created."""
        import threading

        def dummy_func():
            pass

        thread = threading.Thread(target=dummy_func)
        assert thread is not None
        assert isinstance(thread, threading.Thread)


class TestDatetimeHandling:
    """Test datetime handling."""

    def test_datetime_available(self):
        """Test that datetime is available."""
        import datetime

        assert datetime is not None
        assert hasattr(datetime, "datetime")

    def test_can_get_current_time(self):
        """Test that current time can be obtained."""
        import datetime

        now = datetime.datetime.now()
        assert now is not None
        assert isinstance(now, datetime.datetime)


class TestRequestsLibrary:
    """Test requests library availability."""

    def test_requests_available(self):
        """Test that requests library is available."""
        import requests

        assert requests is not None
        assert hasattr(requests, "get")
        assert hasattr(requests, "post")


class TestMathOperations:
    """Test math operations."""

    def test_math_available(self):
        """Test that math module is available."""
        import math

        assert math is not None
        assert hasattr(math, "ceil")
        assert hasattr(math, "floor")

    def test_math_operations(self):
        """Test basic math operations."""
        import math

        assert math.ceil(3.2) == 4
        assert math.floor(3.8) == 3


class TestRegexSupport:
    """Test regex support."""

    def test_regex_available(self):
        """Test that regex is available."""
        import re

        assert re is not None
        assert hasattr(re, "match")
        assert hasattr(re, "search")

    def test_regex_operations(self):
        """Test basic regex operations."""
        import re

        pattern = r"test_\d+"
        assert re.match(pattern, "test_123") is not None
        assert re.match(pattern, "test_abc") is None


class TestSystemPaths:
    """Test system path handling."""

    def test_sys_available(self):
        """Test that sys module is available."""
        import sys

        assert sys is not None
        assert hasattr(sys, "path")

    def test_os_path_available(self):
        """Test that os.path is available."""
        import os.path

        assert os.path is not None
        assert hasattr(os.path, "exists")
        assert hasattr(os.path, "join")


class TestTimeOperations:
    """Test time operations."""

    def test_time_available(self):
        """Test that time module is available."""
        import time

        assert time is not None
        assert hasattr(time, "time")
        assert hasattr(time, "sleep")

    def test_time_operations(self):
        """Test basic time operations."""
        import time

        current_time = time.time()
        assert current_time > 0
        assert isinstance(current_time, float)


class TestTraceback:
    """Test traceback support."""

    def test_traceback_available(self):
        """Test that traceback is available."""
        import traceback

        assert traceback is not None
        assert hasattr(traceback, "format_exc")

    def test_traceback_formatting(self):
        """Test traceback formatting."""
        import traceback

        try:
            raise ValueError("Test error")
        except ValueError:
            tb_str = traceback.format_exc()
            assert "ValueError" in tb_str
            assert "Test error" in tb_str


class TestTWCMasterImport:
    """Test TWCMaster import."""

    def test_twcmaster_importable(self):
        """Test that TWCMaster can be imported."""
        from TWCManager.TWCMaster import TWCMaster

        assert TWCMaster is not None

    def test_twcmaster_is_class(self):
        """Test that TWCMaster is a class."""
        from TWCManager.TWCMaster import TWCMaster

        assert isinstance(TWCMaster, type)


class TestLoggerFactoryIntegration:
    """Test LoggerFactory integration."""

    def test_logger_factory_importable(self):
        """Test that LoggerFactory can be imported."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory

        assert LoggerFactory is not None

    def test_logger_factory_has_get_logger(self):
        """Test that LoggerFactory has get_logger method."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory

        assert hasattr(LoggerFactory, "get_logger")
        assert callable(LoggerFactory.get_logger)


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
