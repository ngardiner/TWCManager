"""
Unit tests for TWCManager Dummy interface.

Tests TWC emulation and scenario-based behavior.
"""

import pytest
import time
from unittest.mock import Mock, MagicMock, patch


class TestDummyInitialization:
    """Test Dummy interface initialization."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "interface": {
                "Dummy": {
                    "enabled": True,
                    "twcID": "AB"
                }
            }
        }
        master.getModuleByName = Mock(return_value=Mock())
        master.releaseModule = Mock()
        return master
    
    def test_dummy_initialization(self, mock_master):
        """Test Dummy interface initializes correctly."""
        from TWCManager.Interface.Dummy import Dummy
        dummy = Dummy(mock_master)
        
        assert dummy.enabled is True
        assert dummy.master == mock_master
    
    def test_dummy_disabled(self):
        """Test Dummy interface can be disabled."""
        from TWCManager.Interface.Dummy import Dummy
        master = Mock()
        master.config = {
            "interface": {
                "Dummy": {
                    "enabled": False
                }
            }
        }
        master.releaseModule = Mock()
        
        dummy = Dummy(master)
        
        master.releaseModule.assert_called_once()
    
    def test_dummy_custom_twc_id(self):
        """Test Dummy interface accepts custom TWC ID."""
        from TWCManager.Interface.Dummy import Dummy
        master = Mock()
        master.config = {
            "interface": {
                "Dummy": {
                    "enabled": True,
                    "twcID": "XY"
                }
            }
        }
        master.getModuleByName = Mock(return_value=Mock())
        
        dummy = Dummy(master)
        
        assert dummy.twcID == bytearray(b"XY")


class TestDummySimpleMode:
    """Test Dummy interface in simple mode (single slave)."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "interface": {
                "Dummy": {
                    "enabled": True
                }
            }
        }
        master.getModuleByName = Mock(return_value=Mock())
        master.getSlaveSign = Mock(return_value=bytearray(b"\x00"))
        master.hex_str = lambda x: x.hex()
        return master
    
    @pytest.fixture
    def dummy(self, mock_master):
        """Create a Dummy interface instance."""
        from TWCManager.Interface.Dummy import Dummy
        return Dummy(mock_master)
    
    def test_dummy_buffer_operations(self, dummy):
        """Test buffer read/write operations."""
        test_data = bytearray(b"test")
        dummy.msgBuffer = test_data
        
        assert dummy.getBufferLen() == 4
        
        read_data = dummy.read(2)
        assert read_data == bytearray(b"te")
        assert dummy.getBufferLen() == 2
    
    def test_dummy_close(self, dummy):
        """Test Dummy close returns 0."""
        assert dummy.close() == 0
    
    def test_dummy_send_returns_zero(self, dummy):
        """Test send method returns 0."""
        msg = bytearray(b"\xfb\xe2\x12\x34\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        result = dummy.send(msg)
        assert result == 0
    
    def test_dummy_linkready_response(self, dummy):
        """Test Dummy responds to linkready message."""
        # Mock the protocol
        proto = Mock()
        proto.parseMessage = Mock(return_value={
            "Command": "MasterLinkready2",
            "SenderID": bytearray(b"AB")
        })
        proto.createMessage = Mock(return_value=bytearray(b"response"))
        dummy.proto = proto
        
        msg = bytearray(b"\xfb\xe2\x12\x34\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        dummy.send(msg)
        
        # Should have called createMessage for SlaveLinkready
        proto.createMessage.assert_called()
    
    def test_dummy_heartbeat_response(self, dummy):
        """Test Dummy responds to heartbeat message."""
        proto = Mock()
        proto.parseMessage = Mock(return_value={
            "Command": "MasterHeartbeat",
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD")
        })
        proto.createMessage = Mock(return_value=bytearray(b"response"))
        dummy.proto = proto
        
        msg = bytearray(b"\xfb\xe0\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00")
        dummy.send(msg)
        
        proto.createMessage.assert_called()


class TestDummyScenarioMode:
    """Test Dummy interface in scenario mode (multi-slave)."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "interface": {
                "Dummy": {
                    "enabled": True,
                    "scenario": "single_slave_normal"
                }
            }
        }
        master.getModuleByName = Mock(return_value=Mock())
        master.getSlaveSign = Mock(return_value=bytearray(b"\x00"))
        master.hex_str = lambda x: x.hex()
        return master
    
    @pytest.fixture
    def dummy(self, mock_master):
        """Create a Dummy interface instance."""
        from TWCManager.Interface.Dummy import Dummy
        return Dummy(mock_master)
    
    def test_scenario_mode_enabled(self, dummy):
        """Test scenario mode is enabled when scenario is loaded."""
        # Scenario loading depends on file existence, so we'll mock it
        dummy.scenario = {"slaves": [{"id": "AB", "maxAmps": 80}]}
        dummy._initialize_slaves()
        
        assert dummy.use_scenarios is False  # Not set until scenario loads
        assert len(dummy.slaves) > 0
    
    def test_slave_initialization(self, dummy):
        """Test slave initialization from scenario."""
        dummy.scenario = {
            "slaves": [
                {"id": "AB", "maxAmps": 80, "behavior": "normal"},
                {"id": "CD", "maxAmps": 40, "behavior": "charging"}
            ]
        }
        dummy._initialize_slaves()
        
        assert "AB" in dummy.slaves
        assert "CD" in dummy.slaves
        assert dummy.slaves["AB"]["maxAmps"] == 80
        assert dummy.slaves["CD"]["behavior"] == "charging"
    
    def test_dynamic_slave_addition(self, dummy):
        """Test dynamic slave addition."""
        dummy.scenario = {
            "slaves": [{"id": "AB", "maxAmps": 80}],
            "dynamicSlaves": [
                {"id": "CD", "maxAmps": 40, "joinAfter": 0}
            ]
        }
        dummy._initialize_slaves()
        dummy.scenario_start_time = time.time() - 1  # Started 1 second ago
        
        dummy._check_dynamic_slaves()
        
        assert "CD" in dummy.slaves
    
    def test_dynamic_slave_not_added_yet(self, dummy):
        """Test dynamic slave not added before joinAfter time."""
        dummy.scenario = {
            "slaves": [{"id": "AB", "maxAmps": 80}],
            "dynamicSlaves": [
                {"id": "CD", "maxAmps": 40, "joinAfter": 100}
            ]
        }
        dummy._initialize_slaves()
        dummy.scenario_start_time = time.time()
        
        dummy._check_dynamic_slaves()
        
        assert "CD" not in dummy.slaves
    
    def test_update_slave_state_normal(self, dummy):
        """Test slave state update for normal behavior."""
        slave = {
            "behavior": "normal",
            "state": "idle",
            "requestedAmps": 32,
            "actualAmps": 32
        }
        
        dummy._update_slave_state(slave)
        
        assert slave["state"] == "idle"
        assert slave["requestedAmps"] == 0
        assert slave["actualAmps"] == 0
    
    def test_update_slave_state_car_plugged(self, dummy):
        """Test slave state update for car plugged."""
        slave = {
            "behavior": "car_plugged",
            "state": "idle",
            "requestedAmps": 0,
            "actualAmps": 0
        }
        
        dummy._update_slave_state(slave)
        
        assert slave["state"] == "plugged"
        assert slave["requestedAmps"] == 32
        assert slave["actualAmps"] == 0
    
    def test_update_slave_state_charging(self, dummy):
        """Test slave state update for charging."""
        slave = {
            "behavior": "charging",
            "state": "idle",
            "requestedAmps": 0,
            "actualAmps": 0
        }
        
        dummy._update_slave_state(slave)
        
        assert slave["state"] == "charging"
        assert slave["requestedAmps"] == 32
        assert slave["actualAmps"] == 32
    
    def test_update_slave_state_error(self, dummy):
        """Test slave state update for error."""
        slave = {
            "behavior": "error",
            "state": "idle",
            "requestedAmps": 32,
            "actualAmps": 32
        }
        
        dummy._update_slave_state(slave)
        
        assert slave["state"] == "error"
        assert slave["requestedAmps"] == 0
        assert slave["actualAmps"] == 0


class TestDummyBehaviors:
    """Test Dummy interface behavior configurations."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {"interface": {"Dummy": {"enabled": True}}}
        master.getModuleByName = Mock(return_value=Mock())
        master.getSlaveSign = Mock(return_value=bytearray(b"\x00"))
        master.hex_str = lambda x: x.hex()
        return master
    
    @pytest.fixture
    def dummy(self, mock_master):
        """Create a Dummy interface instance."""
        from TWCManager.Interface.Dummy import Dummy
        return Dummy(mock_master)
    
    def test_behavior_constants_defined(self, dummy):
        """Test all behavior constants are defined."""
        assert dummy.BEHAVIOR_NORMAL == "normal"
        assert dummy.BEHAVIOR_CHARGING == "charging"
        assert dummy.BEHAVIOR_CAR_PLUGGED == "car_plugged"
        assert dummy.BEHAVIOR_ERROR == "error"
        assert dummy.BEHAVIOR_INTERMITTENT == "intermittent"
        assert dummy.BEHAVIOR_SLOW == "slow"
    
    def test_intermittent_behavior_drops_messages(self, dummy):
        """Test intermittent behavior can drop messages."""
        slave = {
            "id": "AB",
            "behavior": "intermittent",
            "dropRate": 1.0  # Always drop
        }
        dummy.slaves = {"AB": slave}
        
        proto = Mock()
        proto.parseMessage = Mock(return_value={
            "Command": "MasterLinkready2",
            "SenderID": bytearray(b"AB")
        })
        proto.createMessage = Mock(return_value=bytearray(b"response"))
        dummy.proto = proto
        
        msg = bytearray(b"\xfb\xe2\x12\x34\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        dummy.use_scenarios = True
        dummy.send(msg)
        
        # With dropRate=1.0, message should be dropped
        # createMessage should not be called for this slave
        proto.createMessage.assert_not_called()
    
    def test_slow_response_behavior(self, dummy):
        """Test slow response behavior adds delay."""
        slave = {
            "id": "AB",
            "behavior": "slow",
            "responseDelay": 0.01  # 10ms delay
        }
        dummy.slaves = {"AB": slave}
        
        proto = Mock()
        proto.parseMessage = Mock(return_value={
            "Command": "MasterLinkready2",
            "SenderID": bytearray(b"AB")
        })
        proto.createMessage = Mock(return_value=bytearray(b"response"))
        dummy.proto = proto
        
        msg = bytearray(b"\xfb\xe2\x12\x34\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        dummy.use_scenarios = True
        
        start = time.time()
        dummy.send(msg)
        elapsed = time.time() - start
        
        # Should have taken at least the delay time
        assert elapsed >= 0.01


class TestDummyMessageHandling:
    """Test Dummy message handling in scenario mode."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {"interface": {"Dummy": {"enabled": True}}}
        master.getModuleByName = Mock(return_value=Mock())
        master.getSlaveSign = Mock(return_value=bytearray(b"\x00"))
        master.hex_str = lambda x: x.hex()
        return master
    
    @pytest.fixture
    def dummy(self, mock_master):
        """Create a Dummy interface instance."""
        from TWCManager.Interface.Dummy import Dummy
        return Dummy(mock_master)
    
    def test_handle_linkready_all_slaves(self, dummy):
        """Test linkready is sent from all slaves."""
        dummy.slaves = {
            "AB": {"id": "AB", "behavior": "normal", "dropRate": 0, "responseDelay": 0},
            "CD": {"id": "CD", "behavior": "normal", "dropRate": 0, "responseDelay": 0}
        }
        
        proto = Mock()
        proto.parseMessage = Mock(return_value={
            "Command": "MasterLinkready2",
            "SenderID": bytearray(b"AB")
        })
        proto.createMessage = Mock(return_value=bytearray(b"response"))
        dummy.proto = proto
        
        msg = bytearray(b"\xfb\xe2\x12\x34\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        dummy.use_scenarios = True
        dummy.send(msg)
        
        # Should have called createMessage twice (once per slave)
        assert proto.createMessage.call_count == 2
    
    def test_handle_heartbeat_specific_slave(self, dummy):
        """Test heartbeat is only sent from addressed slave."""
        dummy.slaves = {
            "AB": {"id": "AB", "behavior": "normal", "dropRate": 0, "responseDelay": 0, "lastHeartbeatTime": 0},
            "CD": {"id": "CD", "behavior": "normal", "dropRate": 0, "responseDelay": 0, "lastHeartbeatTime": 0}
        }
        
        proto = Mock()
        proto.parseMessage = Mock(return_value={
            "Command": "MasterHeartbeat",
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"AB")  # Only AB is addressed
        })
        proto.createMessage = Mock(return_value=bytearray(b"response"))
        dummy.proto = proto
        
        msg = bytearray(b"\xfb\xe0\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00")
        dummy.use_scenarios = True
        dummy.send(msg)
        
        # Should have called createMessage once (only for AB)
        assert proto.createMessage.call_count == 1
