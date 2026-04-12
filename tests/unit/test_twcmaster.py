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
        assert master.version == "1.3.2"
    
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
