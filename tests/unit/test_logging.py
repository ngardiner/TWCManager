"""
Unit tests for TWCManager Logging module.

Tests logger factory and logging configuration.
"""

import pytest
import logging
from unittest.mock import Mock, patch


class TestLoggerFactory:
    """Test LoggerFactory functionality."""
    
    def test_logger_factory_get_logger_with_icons(self):
        """Test getting logger with icons enabled."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        # Reset master to None to use default (icons)
        LoggerFactory.master = None
        
        logger = LoggerFactory.get_logger("TestModule", "EMS")
        
        assert logger is not None
        assert "⚡" in logger.name
        assert "TestModule" in logger.name
    
    def test_logger_factory_get_logger_without_icons(self):
        """Test getting logger with icons disabled."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        master = Mock()
        master.config = {"logging": {"use_icons": False}}
        LoggerFactory.set_master(master)
        
        logger = LoggerFactory.get_logger("TestModule", "EMS")
        
        assert logger is not None
        assert "[EMS]" in logger.name
        assert "TestModule" in logger.name
    
    def test_logger_factory_all_categories_with_icons(self):
        """Test all categories have icon mappings."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        categories = ["EMS", "Status", "Vehicle", "Control", "Interface", 
                     "Policy", "Master", "Manager", "Slave", "Protocol"]
        
        for category in categories:
            logger = LoggerFactory.get_logger("Test", category)
            assert logger is not None
            # Should have an icon or fallback category
            assert "[" in logger.name or any(icon in logger.name for icon in LoggerFactory.ICON_MAP.values())
    
    def test_logger_factory_all_categories_without_icons(self):
        """Test all categories have text mappings."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        master = Mock()
        master.config = {"logging": {"use_icons": False}}
        LoggerFactory.set_master(master)
        
        categories = ["EMS", "Status", "Vehicle", "Control", "Interface", 
                     "Policy", "Master", "Manager", "Slave", "Protocol"]
        
        for category in categories:
            logger = LoggerFactory.get_logger("Test", category)
            assert logger is not None
            assert f"[{category}]" in logger.name
    
    def test_logger_factory_unknown_category_with_icons(self):
        """Test unknown category falls back to text format."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        logger = LoggerFactory.get_logger("Test", "UnknownCategory")
        
        assert logger is not None
        assert "[UnknownCategory]" in logger.name
    
    def test_logger_factory_unknown_category_without_icons(self):
        """Test unknown category falls back to text format."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        master = Mock()
        master.config = {"logging": {"use_icons": False}}
        LoggerFactory.set_master(master)
        
        logger = LoggerFactory.get_logger("Test", "UnknownCategory")
        
        assert logger is not None
        assert "[UnknownCategory]" in logger.name
    
    def test_logger_factory_set_master(self):
        """Test setting master configuration."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        master = Mock()
        master.config = {"logging": {"use_icons": False}}
        
        LoggerFactory.set_master(master)
        
        assert LoggerFactory.master == master
    
    def test_logger_factory_returns_logging_logger(self):
        """Test that factory returns actual logging.Logger instances."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        logger = LoggerFactory.get_logger("Test", "EMS")
        
        assert isinstance(logger, logging.Logger)
    
    def test_logger_factory_same_logger_returned(self):
        """Test that same logger is returned for same name."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        logger1 = LoggerFactory.get_logger("Test", "EMS")
        logger2 = LoggerFactory.get_logger("Test", "EMS")
        
        assert logger1 is logger2
    
    def test_logger_factory_different_loggers_for_different_modules(self):
        """Test different loggers for different module names."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        logger1 = LoggerFactory.get_logger("Module1", "EMS")
        logger2 = LoggerFactory.get_logger("Module2", "EMS")
        
        assert logger1 is not logger2
    
    def test_logger_factory_different_loggers_for_different_categories(self):
        """Test different loggers for different categories."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        logger1 = LoggerFactory.get_logger("Test", "EMS")
        logger2 = LoggerFactory.get_logger("Test", "Status")
        
        assert logger1 is not logger2
    
    def test_logger_factory_icon_map_completeness(self):
        """Test that ICON_MAP has all expected categories."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        expected_categories = ["EMS", "Status", "Vehicle", "Control", "Interface", 
                              "Policy", "Master", "Manager", "Slave", "Protocol"]
        
        for category in expected_categories:
            assert category in LoggerFactory.ICON_MAP
            assert LoggerFactory.ICON_MAP[category] is not None
    
    def test_logger_factory_category_map_completeness(self):
        """Test that CATEGORY_MAP has all expected categories."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        expected_categories = ["EMS", "Status", "Vehicle", "Control", "Interface", 
                              "Policy", "Master", "Manager", "Slave", "Protocol"]
        
        for category in expected_categories:
            assert category in LoggerFactory.CATEGORY_MAP
            assert LoggerFactory.CATEGORY_MAP[category] is not None
    
    def test_logger_factory_config_missing_logging_section(self):
        """Test factory handles missing logging config section."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        master = Mock()
        master.config = {}  # No logging section
        LoggerFactory.set_master(master)
        
        logger = LoggerFactory.get_logger("Test", "EMS")
        
        # Should default to icons
        assert logger is not None
        assert "⚡" in logger.name
    
    def test_logger_factory_config_missing_use_icons_key(self):
        """Test factory handles missing use_icons config key."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        master = Mock()
        master.config = {"logging": {}}  # No use_icons key
        LoggerFactory.set_master(master)
        
        logger = LoggerFactory.get_logger("Test", "EMS")
        
        # Should default to icons
        assert logger is not None
        assert "⚡" in logger.name
    
    def test_logger_factory_module_name_with_spaces(self):
        """Test logger factory handles module names with spaces."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        logger = LoggerFactory.get_logger("Test Module Name", "EMS")
        
        assert logger is not None
        assert "Test Module Name" in logger.name
    
    def test_logger_factory_module_name_with_special_chars(self):
        """Test logger factory handles module names with special characters."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        
        logger = LoggerFactory.get_logger("Test-Module_123", "EMS")
        
        assert logger is not None
        assert "Test-Module_123" in logger.name


class TestLoggerIntegration:
    """Test logger integration with logging module."""
    
    def test_logger_can_log_messages(self):
        """Test that returned logger can log messages."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        logger = LoggerFactory.get_logger("Test", "EMS")
        
        # Should not raise any exceptions
        logger.info("Test message")
        logger.debug("Debug message")
        logger.warning("Warning message")
        logger.error("Error message")
    
    def test_logger_has_standard_methods(self):
        """Test that logger has standard logging methods."""
        from TWCManager.Logging.LoggerFactory import LoggerFactory
        
        LoggerFactory.master = None
        logger = LoggerFactory.get_logger("Test", "EMS")
        
        assert hasattr(logger, "info")
        assert hasattr(logger, "debug")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "error")
        assert hasattr(logger, "critical")
        assert callable(logger.info)
        assert callable(logger.debug)
        assert callable(logger.warning)
        assert callable(logger.error)
        assert callable(logger.critical)
