"""
Unit tests for TWCManager TWCSlave module.

Tests slave TWC state management and heartbeat handling.
"""

import pytest
import time
from unittest.mock import Mock, MagicMock, patch


class TestTWCSlaveInitialization:
    """Test TWCSlave initialization."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        return master
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
    
    def test_slave_initialization(self, mock_master, mock_config):
        """Test TWCSlave initializes correctly."""
        from TWCManager.TWCSlave import TWCSlave
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
        
        assert slave.TWCID == bytearray(b"AB")
        assert slave.maxAmps == 80
        assert slave.master == mock_master
    
    def test_slave_default_protocol_version(self, mock_master, mock_config):
        """Test slave defaults to protocol version 1."""
        from TWCManager.TWCSlave import TWCSlave
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
        
        assert slave.protocolVersion == 1
    
    def test_slave_min_amps_supported(self, mock_master, mock_config):
        """Test slave minimum amps supported."""
        from TWCManager.TWCSlave import TWCSlave
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
        
        assert slave.minAmpsTWCSupports == 6
    
    def test_slave_wiring_max_amps_from_config(self, mock_master, mock_config):
        """Test slave wiring max amps from configuration."""
        from TWCManager.TWCSlave import TWCSlave
        
        mock_config["config"]["wiringMaxAmpsPerTWC"] = 32
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
        
        assert slave.wiringMaxAmps == 32
    
    def test_slave_start_stop_delay_from_config(self, mock_master, mock_config):
        """Test slave start/stop delay from configuration."""
        from TWCManager.TWCSlave import TWCSlave
        
        mock_config["config"]["startStopDelay"] = 120
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
        
        assert slave.startStopDelay == 120


class TestTWCSlaveState:
    """Test TWCSlave state management."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        return master
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
    
    @pytest.fixture
    def slave(self, mock_master, mock_config):
        """Create a TWCSlave instance."""
        from TWCManager.TWCSlave import TWCSlave
        return TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
    
    def test_slave_reported_amps_max(self, slave):
        """Test slave reported max amps."""
        slave.reportedAmpsMax = 80
        
        assert slave.reportedAmpsMax == 80
    
    def test_slave_reported_amps_actual(self, slave):
        """Test slave reported actual amps."""
        slave.reportedAmpsActual = 32
        
        assert slave.reportedAmpsActual == 32
    
    def test_slave_reported_state(self, slave):
        """Test slave reported state."""
        slave.reportedState = 1
        
        assert slave.reportedState == 1
    
    def test_slave_is_charging(self, slave):
        """Test slave charging state."""
        slave.isCharging = 1
        
        assert slave.isCharging == 1
    
    def test_slave_lifetime_kwh(self, slave):
        """Test slave lifetime kWh."""
        slave.lifetimekWh = 1234.56
        
        assert slave.lifetimekWh == 1234.56
    
    def test_slave_voltage_phases(self, slave):
        """Test slave voltage readings."""
        slave.voltsPhaseA = 240
        slave.voltsPhaseB = 240
        slave.voltsPhaseC = 240
        
        assert slave.voltsPhaseA == 240
        assert slave.voltsPhaseB == 240
        assert slave.voltsPhaseC == 240


class TestTWCSlaveHeartbeat:
    """Test TWCSlave heartbeat handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        return master
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
    
    @pytest.fixture
    def slave(self, mock_master, mock_config):
        """Create a TWCSlave instance."""
        from TWCManager.TWCSlave import TWCSlave
        return TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
    
    def test_slave_heartbeat_data_default(self, slave):
        """Test slave default heartbeat data."""
        assert slave.masterHeartbeatData == bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    
    def test_slave_time_last_rx(self, slave):
        """Test slave last receive time."""
        initial_time = slave.timeLastRx
        
        assert initial_time > 0
        assert isinstance(initial_time, float)
    
    def test_slave_last_heartbeat_debug_output(self, slave):
        """Test slave last heartbeat debug output."""
        slave.lastHeartbeatDebugOutput = "Test output"
        
        assert slave.lastHeartbeatDebugOutput == "Test output"


class TestTWCSlaveVehicleModule:
    """Test TWCSlave vehicle module selection."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
    
    def test_get_vehicle_module_priority(self, mock_config):
        """Test vehicle module selection with VehiclePriority."""
        from TWCManager.TWCSlave import TWCSlave
        
        master = Mock()
        vehicle_priority = Mock()
        master.getModuleByName = Mock(side_effect=lambda name: 
            vehicle_priority if name == "VehiclePriority" else None)
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, master)
        
        assert slave.vehicleModule == vehicle_priority
    
    def test_get_vehicle_module_hass_fallback(self, mock_config):
        """Test vehicle module selection falls back to HomeAssistant."""
        from TWCManager.TWCSlave import TWCSlave
        
        master = Mock()
        hass = Mock()
        master.getModuleByName = Mock(side_effect=lambda name: 
            hass if name == "HomeAssistant" else None)
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, master)
        
        assert slave.vehicleModule == hass
    
    def test_get_vehicle_module_ble_fallback(self, mock_config):
        """Test vehicle module selection falls back to TeslaBLE."""
        from TWCManager.TWCSlave import TWCSlave
        
        master = Mock()
        ble = Mock()
        master.getModuleByName = Mock(side_effect=lambda name: 
            ble if name == "TeslaBLE" else None)
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, master)
        
        assert slave.vehicleModule == ble
    
    def test_get_vehicle_module_api_fallback(self, mock_config):
        """Test vehicle module selection falls back to TeslaAPI."""
        from TWCManager.TWCSlave import TWCSlave
        
        master = Mock()
        api = Mock()
        master.getModuleByName = Mock(side_effect=lambda name: 
            api if name == "TeslaAPI" else None)
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, master)
        
        assert slave.vehicleModule == api
    
    def test_get_vehicle_module_none(self, mock_config):
        """Test vehicle module returns None when no module available."""
        from TWCManager.TWCSlave import TWCSlave
        
        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        
        slave = TWCSlave(bytearray(b"AB"), 80, mock_config, master)
        
        assert slave.vehicleModule is None


class TestTWCSlaveVIN:
    """Test TWCSlave VIN handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        return master
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
    
    @pytest.fixture
    def slave(self, mock_master, mock_config):
        """Create a TWCSlave instance."""
        from TWCManager.TWCSlave import TWCSlave
        return TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
    
    def test_slave_vin_data_default(self, slave):
        """Test slave default VIN data."""
        assert slave.VINData == ["", "", ""]
    
    def test_slave_current_vin(self, slave):
        """Test slave current VIN."""
        slave.currentVIN = "5TDJKRFH4LS123456"
        
        assert slave.currentVIN == "5TDJKRFH4LS123456"
    
    def test_slave_last_vin(self, slave):
        """Test slave last VIN."""
        slave.lastVIN = "5TDJKRFH4LS654321"
        
        assert slave.lastVIN == "5TDJKRFH4LS654321"
    
    def test_slave_last_vin_query(self, slave):
        """Test slave last VIN query time."""
        slave.lastVINQuery = time.time()
        
        assert slave.lastVINQuery > 0
    
    def test_slave_vin_query_attempt(self, slave):
        """Test slave VIN query attempt counter."""
        slave.vinQueryAttempt = 3
        
        assert slave.vinQueryAttempt == 3


class TestTWCSlaveAmpsTracking:
    """Test TWCSlave amperage tracking."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        return master
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
    
    @pytest.fixture
    def slave(self, mock_master, mock_config):
        """Create a TWCSlave instance."""
        from TWCManager.TWCSlave import TWCSlave
        return TWCSlave(bytearray(b"AB"), 80, mock_config, mock_master)
    
    def test_slave_last_amps_offered(self, slave):
        """Test slave last amps offered."""
        slave.lastAmpsOffered = 32
        
        assert slave.lastAmpsOffered == 32
    
    def test_slave_last_amps_desired(self, slave):
        """Test slave last amps desired."""
        slave.lastAmpsDesired = 16
        
        assert slave.lastAmpsDesired == 16
    
    def test_slave_history_avg_amps(self, slave):
        """Test slave history average amps."""
        slave.historyAvgAmps = 25.5
        
        assert slave.historyAvgAmps == 25.5
    
    def test_slave_history_num_samples(self, slave):
        """Test slave history number of samples."""
        slave.historyNumSamples = 100
        
        assert slave.historyNumSamples == 100
    
    def test_slave_reported_amps_actual_significant_change_monitor(self, slave):
        """Test slave amps actual significant change monitor."""
        slave.reportedAmpsActualSignificantChangeMonitor = 30.5
        
        assert slave.reportedAmpsActualSignificantChangeMonitor == 30.5
    
    def test_slave_time_reported_amps_actual_changed_significantly(self, slave):
        """Test slave time amps changed significantly."""
        current_time = time.time()
        slave.timeReportedAmpsActualChangedSignificantly = current_time
        
        assert slave.timeReportedAmpsActualChangedSignificantly == current_time


class TestTWCSlaveConfiguration:
    """Test TWCSlave configuration handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        return master
    
    def test_slave_use_flex_amps_to_start_charge(self, mock_master):
        """Test useFlexAmpsToStartCharge configuration."""
        from TWCManager.TWCSlave import TWCSlave
        
        config = {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": True,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
        
        slave = TWCSlave(bytearray(b"AB"), 80, config, mock_master)
        
        assert slave.useFlexAmpsToStartCharge is True
    
    def test_slave_api_control_default(self, mock_master):
        """Test slave API control defaults to False."""
        from TWCManager.TWCSlave import TWCSlave
        
        config = {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
        
        slave = TWCSlave(bytearray(b"AB"), 80, config, mock_master)
        
        assert slave.APIcontrol is False
