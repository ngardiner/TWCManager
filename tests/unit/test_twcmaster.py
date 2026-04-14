"""
Unit tests for TWCManager TWCMaster module.

Tests master control logic, amperage distribution, and slave management.
"""

import pytest
import time
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timedelta


class TestTWCMasterInitialization:
    """Test TWCMaster initialization."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    def test_master_initialization(self, mock_config):
        """Test TWCMaster initializes correctly."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.TWCID == "AB"
        assert master.config == mock_config
        assert master.version == "1.3.4"
    
    def test_master_default_settings(self, mock_config):
        """Test master has default settings."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeStopMode"] == "1"
        assert master.settings["kWhDelivered"] == 119
    
    def test_master_protocol_version(self, mock_config):
        """Test master protocol version defaults to 2."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.protocolVersion == 2
    
    def test_master_sign_values(self, mock_config):
        """Test master and slave sign values."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.masterSign == bytearray(b"\x77")
        assert master.slaveSign == bytearray(b"\x77")


class TestTWCMasterAmperage:
    """Test amperage distribution logic."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_set_max_amps_to_divide(self, master):
        """Test setting max amps to divide among slaves."""
        master.setMaxAmpsToDivideAmongSlaves(32)
        
        assert master.maxAmpsToDivideAmongSlaves == 32
    
    def test_set_limit_amps_to_divide(self, master):
        """Test setting limit amps to divide among slaves."""
        master.setLimitAmpsToDivideAmongSlaves(16)
        
        assert master.limitAmpsToDivideAmongSlaves == 16
    
    def test_set_allowed_flex(self, master):
        """Test setting allowed flex amperage."""
        master.setAllowedFlex(8)
        
        assert master.allowed_flex == 8
    
    def test_add_kwh_delivered(self, master):
        """Test adding kWh delivered."""
        initial = master.settings["kWhDelivered"]
        master.addkWhDelivered(5.5)
        
        assert master.settings["kWhDelivered"] == initial + 5.5
    
    def test_add_kwh_delivered_multiple_times(self, master):
        """Test adding kWh multiple times accumulates."""
        initial = master.settings["kWhDelivered"]
        master.addkWhDelivered(2.0)
        master.addkWhDelivered(3.0)
        
        assert master.settings["kWhDelivered"] == initial + 5.0


class TestTWCMasterSlaveManagement:
    """Test slave TWC management."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_add_slave_twc(self, master):
        """Test adding a slave TWC."""
        slave = Mock()
        slave.TWCID = "CD"
        
        master.addSlaveTWC(slave)
        
        assert slave in master.slaveTWCRoundRobin
    
    def test_get_slave_twcs(self, master):
        """Test getting slave TWCs."""
        slave1 = Mock()
        slave1.TWCID = "CD"
        slave2 = Mock()
        slave2.TWCID = "EF"
        
        master.addSlaveTWC(slave1)
        master.addSlaveTWC(slave2)
        
        slaves = master.getSlaveTWCs()
        
        assert len(slaves) == 2
        assert slave1 in slaves
        assert slave2 in slaves
    
    def test_get_slave_sign(self, master):
        """Test getting slave sign."""
        sign = master.getSlaveSign()
        
        assert sign == bytearray(b"\x77")
    
    def test_get_master_sign(self, master):
        """Test getting master sign."""
        sign = master.getMasterSign()
        
        assert sign == bytearray(b"\x77")


class TestTWCMasterSettings:
    """Test settings management."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_get_setting(self, master):
        """Test getting a setting."""
        value = master.getSetting("chargeNowAmps")
        
        assert value == 0
    
    def test_get_setting_default(self, master):
        """Test getting a non-existent setting returns default."""
        value = master.getSetting("nonexistent", 42)
        
        assert value == 42
    
    def test_set_setting(self, master):
        """Test setting a value."""
        master.setSetting("chargeNowAmps", 32)
        
        assert master.settings["chargeNowAmps"] == 32
    
    def test_charge_now_amps(self, master):
        """Test chargeNowAmps setting."""
        master.setChargeNowAmps(16)
        
        assert master.settings["chargeNowAmps"] == 16
    
    def test_reset_charge_now_amps(self, master):
        """Test resetting chargeNowAmps."""
        master.setChargeNowAmps(16)
        master.resetChargeNowAmps()
        
        assert master.settings["chargeNowAmps"] == 0


class TestTWCMasterConfiguration:
    """Test configuration handling."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_subtract_charger_load_config(self, mock_config):
        """Test subtractChargerLoad configuration."""
        mock_config["config"]["subtractChargerLoad"] = True
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        
        assert master.subtractChargerLoad is True
    
    def test_treat_generation_as_grid_delivery_config(self, mock_config):
        """Test treatGenerationAsGridDelivery configuration."""
        mock_config["config"]["treatGenerationAsGridDelivery"] = True
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        
        assert master.treatGenerationAsGridDelivery is True
    
    def test_debug_output_to_file_config(self, mock_config):
        """Test debugOutputToFile configuration."""
        mock_config["config"]["debugOutputToFile"] = True
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        
        assert master.debugOutputToFile is True


class TestTWCMasterModuleManagement:
    """Test module registration and management."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_register_module(self, master):
        """Test registering a module."""
        module_info = {
            "name": "TestModule",
            "ref": Mock(),
            "type": "Test"
        }
        
        master.registerModule(module_info)
        
        assert "TestModule" in master.modules
    
    def test_get_module_by_name(self, master):
        """Test getting module by name."""
        module_ref = Mock()
        module_info = {
            "name": "TestModule",
            "ref": module_ref,
            "type": "Test"
        }
        master.registerModule(module_info)
        
        result = master.getModuleByName("TestModule")
        
        assert result == module_ref
    
    def test_get_module_by_name_not_found(self, master):
        """Test getting non-existent module returns None."""
        result = master.getModuleByName("NonExistent")
        
        assert result is None


class TestTWCMasterHistory:
    """Test history snapshot management."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_advance_history_snap(self, master):
        """Test advancing history snapshot."""
        initial_snap = master.nextHistorySnap
        master.advanceHistorySnap()
        
        # Should have advanced to next 5-minute boundary
        assert master.nextHistorySnap > initial_snap
    
    def test_history_snap_is_future(self, master):
        """Test history snap is in the future."""
        master.advanceHistorySnap()
        
        assert master.nextHistorySnap > datetime.now().astimezone()


class TestSettingsInitialization:
    """Test suite for settings initialization and persistence."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "settingsPath": "/tmp/test_settings",
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance for testing."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_settings_has_default_values(self, master):
        """Test that settings contain all default values."""
        required_defaults = [
            "chargeNowAmps",
            "chargeStopMode",
            "chargeNowTimeEnd",
            "homeLat",
            "homeLon",
            "hourResumeTrackGreenEnergy",
            "kWhDelivered",
            "nonScheduledAmpsMax",
            "respondToSlaves",
            "scheduledAmpsDaysBitmap",
            "scheduledAmpsEndHour",
            "scheduledAmpsMax",
            "scheduledAmpsStartHour",
            "sendServerTime",
        ]
        
        for key in required_defaults:
            assert key in master.settings, f"Settings should contain {key}"
    
    def test_settings_default_values_correct(self, master):
        """Test that default settings have correct values."""
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeStopMode"] == "1"
        assert master.settings["chargeNowTimeEnd"] == 0
        assert master.settings["homeLat"] == 10000
        assert master.settings["homeLon"] == 10000
        assert master.settings["hourResumeTrackGreenEnergy"] == -1
        assert master.settings["kWhDelivered"] == 119
        assert master.settings["nonScheduledAmpsMax"] == 0
        assert master.settings["respondToSlaves"] == 1
        assert master.settings["scheduledAmpsDaysBitmap"] == 0x7F
        assert master.settings["scheduledAmpsEndHour"] == -1
        assert master.settings["scheduledAmpsMax"] == 0
        assert master.settings["scheduledAmpsStartHour"] == -1
        assert master.settings["sendServerTime"] == 0
    
    def test_load_settings_with_missing_file(self, master, tmp_path):
        """Test loadSettings when settings file doesn't exist."""
        # Update config to use temp directory
        master.config["config"]["settingsPath"] = str(tmp_path)
        
        # Call loadSettings
        master.loadSettings()
        
        # Should have default settings
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeNowTimeEnd"] == 0
        assert "chargeStopMode" in master.settings
    
    def test_load_settings_merges_with_defaults(self, master, tmp_path):
        """Test that loaded settings are merged with defaults."""
        import json
        import os
        
        # Create a settings file with partial data
        settings_dir = tmp_path / "settings"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"
        
        partial_settings = {
            "chargeNowAmps": 32,
            "customField": "custom_value"
        }
        
        with open(settings_file, "w") as f:
            json.dump(partial_settings, f)
        
        # Update config
        master.config["config"]["settingsPath"] = str(settings_dir)
        
        # Load settings
        master.loadSettings()
        
        # Should have loaded value
        assert master.settings["chargeNowAmps"] == 32
        # Should have custom field
        assert master.settings.get("customField") == "custom_value"
        # Should have defaults for missing keys
        assert master.settings["chargeNowTimeEnd"] == 0
        assert master.settings["chargeStopMode"] == "1"
    
    def test_get_status_with_charge_now_active(self, master):
        """Test getStatus when chargeNow is active."""
        master.settings["chargeNowAmps"] = 32
        master.settings["chargeNowTimeEnd"] = int(time.time()) + 3600
        
        # Mock required methods
        master.getScheduledAmpsBatterySize = Mock(return_value=100)
        master.getInterfaceModule = Mock(return_value=Mock(timeLastTx=time.time()))
        
        status = master.getStatus()
        
        assert status["chargeNowAmps"] == 32
        assert "chargeNowTimeEnd" in status
    
    def test_get_status_without_charge_now(self, master):
        """Test getStatus when chargeNow is not active."""
        master.settings["chargeNowAmps"] = 0
        master.settings["chargeNowTimeEnd"] = 0
        
        # Mock required methods
        master.getScheduledAmpsBatterySize = Mock(return_value=100)
        master.getInterfaceModule = Mock(return_value=Mock(timeLastTx=time.time()))
        
        status = master.getStatus()
        
        assert status.get("chargeNowAmps", 0) == 0
        assert "chargeNowTimeEnd" not in status or status.get("chargeNowTimeEnd", 0) == 0
    
    def test_charge_now_sets_correct_values(self, master):
        """Test that chargeNow sets correct settings values."""
        master.chargeNow(32, 3600)
        
        assert master.settings["chargeNowAmps"] == 32
        assert master.settings["chargeNowTimeEnd"] > time.time()
    
    def test_cancel_charge_now_clears_values(self, master):
        """Test that cancelChargeNow clears settings."""
        # First set charge now
        master.chargeNow(32, 3600)
        assert master.settings["chargeNowAmps"] == 32
        
        # Then cancel
        master.cancelChargeNow()
        
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeNowTimeEnd"] == 0


class TestSettingsEdgeCases:
    """Test edge cases in settings handling."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "settingsPath": "/tmp/test_settings",
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance for testing."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_charge_now_with_very_high_amps(self, master):
        """Test chargeNow with very high amperage."""
        master.chargeNow(200, 3600)
        assert master.settings["chargeNowAmps"] == 200
    
    def test_charge_now_with_very_long_duration(self, master):
        """Test chargeNow with very long duration."""
        master.chargeNow(32, 86400 * 7)  # 7 days
        assert master.settings["chargeNowTimeEnd"] > time.time() + 86400 * 6
    
    def test_charge_now_with_minimum_amps(self, master):
        """Test chargeNow with minimum amperage."""
        master.chargeNow(1, 3600)
        assert master.settings["chargeNowAmps"] == 1
    
    def test_charge_now_with_minimum_duration(self, master):
        """Test chargeNow with minimum duration."""
        master.chargeNow(32, 60)  # 1 minute
        assert master.settings["chargeNowTimeEnd"] > time.time()
    
    def test_multiple_charge_now_calls_override(self, master):
        """Test that multiple chargeNow calls override previous values."""
        master.chargeNow(32, 3600)
        first_end_time = master.settings["chargeNowTimeEnd"]
        
        time.sleep(0.1)
        
        master.chargeNow(24, 7200)
        second_end_time = master.settings["chargeNowTimeEnd"]
        
        assert master.settings["chargeNowAmps"] == 24
        assert second_end_time > first_end_time

