"""
Unit tests for TWCManager TWCSlave module.

Tests slave TWC state management and heartbeat handling.
"""

import pytest
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


class TestAmpsHeartbeat:
    """Test the set-amps master heartbeat payload."""

    def make_slave(self, protocol, state, ampsMax):
        from TWCManager.TWCSlave import TWCSlave

        master = Mock()
        master.getModuleByName = Mock(return_value=None)
        config = {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        }
        slave = TWCSlave(bytearray(b"AB"), 80, config, master)
        slave.protocolVersion = protocol
        slave.reportedState = state
        slave.reportedAmpsMax = ampsMax
        return slave

    def test_state_08_resends_even_when_max_matches(self):
        slave = self.make_slave(2, 0x08, 47)
        assert slave._amps_heartbeat(47) == bytearray([0x09, 0x12, 0x5C, 0, 0, 0, 0, 0, 0])

    def test_charging_state_matching_max_sends_nothing(self):
        slave = self.make_slave(2, 0x01, 47)
        assert slave._amps_heartbeat(47) == bytearray(9)

    def test_mismatch_sends_offer(self):
        slave = self.make_slave(2, 0x03, 47)
        assert slave._amps_heartbeat(32) == bytearray([0x09, 0x0C, 0x80, 0, 0, 0, 0, 0, 0])

    def test_protocol_1_state_08_uses_0x05(self):
        slave = self.make_slave(1, 0x08, 32)
        assert slave._amps_heartbeat(32) == bytearray([0x05, 0x0C, 0x80, 0, 0, 0, 0, 0, 0])

    def test_zero_amps_always_sent(self):
        slave = self.make_slave(2, 0x01, 0)
        assert slave._amps_heartbeat(0) == bytearray([0x09, 0, 0, 0, 0, 0, 0, 0, 0])


class TestVehicleChargeRateDesired:
    """The car's rate is desired state, never a command from the serial thread."""
    def test_heartbeat_code_never_calls_set_charge_rate_inline(self):
        import inspect
        from TWCManager.TWCSlave import TWCSlave

        source = inspect.getsource(TWCSlave)
        assert "setChargeRate" not in source and "queue_vehicle_command" not in source


class TestPolicyNotOnMainLoop:
    """The serial main loop must not evaluate charging policy inline."""

    def test_twcslave_never_calls_set_charging_per_policy(self):
        import inspect
        from TWCManager.TWCSlave import TWCSlave

        assert "setChargingPerPolicy" not in inspect.getsource(TWCSlave)
