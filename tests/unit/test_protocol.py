"""
Unit tests for TWCManager Protocol module.

Tests TWC protocol message parsing and creation.
"""

import pytest
from unittest.mock import Mock


class TestProtocolMessageCreation:
    """Test TWC protocol message creation."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.masterTWCID = "AB"
        master.protocolVersion = 2
        slave = Mock()
        slave.TWCID = "CD"
        master.getSlaveTWCs = Mock(return_value=[slave])
        return master
    
    @pytest.fixture
    def protocol(self, mock_master):
        """Create a TWCProtocol instance with mock master."""
        from TWCManager.Protocol.TWCProtocol import TWCProtocol
        return TWCProtocol(mock_master)
    
    def test_create_slave_linkready_message(self, protocol):
        """Test creating a SlaveLinkready message."""
        packet = {
            "Command": "SlaveLinkready",
            "SenderID": bytearray(b"AB"),
            "Sign": bytearray(b"\x00"),
            "Amps": bytearray(b"\x1f\x40"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        assert msg.startswith(bytearray(b"\xfd\xe2"))
        assert b"AB" in msg
    
    def test_create_slave_heartbeat_message(self, protocol):
        """Test creating a SlaveHeartbeat message."""
        packet = {
            "Command": "SlaveHeartbeat",
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        assert msg.startswith(bytearray(b"\xfd\xe0"))
        assert b"AB" in msg
        assert b"CD" in msg
    
    def test_create_get_firmware_version_message(self, protocol):
        """Test creating a GetFirmwareVersion message."""
        packet = {
            "Command": "GetFirmwareVersion",
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        assert msg.startswith(bytearray(b"\xfb\x1b"))
    
    def test_create_message_auto_fills_sender_id(self, protocol):
        """Test that createMessage auto-fills SenderID if not provided."""
        packet = {
            "Command": "SlaveHeartbeat",
            "RecieverID": bytearray(b"CD"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        # Should have auto-filled with masterTWCID
        assert b"AB" in msg
    
    def test_create_message_auto_fills_receiver_id(self, protocol):
        """Test that createMessage auto-fills RecieverID if not provided."""
        packet = {
            "Command": "SlaveHeartbeat",
            "SenderID": bytearray(b"AB"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        # Should have auto-filled with first slave TWCID
        assert b"CD" in msg
    
    def test_create_custom_command_blocked_fc19(self, protocol):
        """Test that dangerous custom command fc19 is blocked."""
        packet = {
            "Command": "Custom",
            "CustomCommand": bytearray(b"fc19"),
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        result = protocol.createMessage(packet)
        
        assert result is None
        assert b"permanently disabled" in protocol.master.lastTWCResponseMsg
    
    def test_create_custom_command_blocked_fc1a(self, protocol):
        """Test that dangerous custom command fc1a is blocked."""
        packet = {
            "Command": "Custom",
            "CustomCommand": bytearray(b"fc1a"),
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        result = protocol.createMessage(packet)
        
        assert result is None
        assert b"permanently disabled" in protocol.master.lastTWCResponseMsg
    
    def test_create_custom_command_blocked_fbe8(self, protocol):
        """Test that dangerous custom command fbe8 is blocked."""
        packet = {
            "Command": "Custom",
            "CustomCommand": bytearray(b"fbe8"),
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        result = protocol.createMessage(packet)
        
        assert result is None
        assert b"crash" in protocol.master.lastTWCResponseMsg
    
    def test_create_custom_command_allowed(self, protocol):
        """Test that safe custom commands are allowed."""
        packet = {
            "Command": "Custom",
            "CustomCommand": bytearray(b"abcd"),
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        assert b"abcd" in msg
    
    def test_create_message_protocol_v1(self, protocol):
        """Test message creation for protocol version 1."""
        protocol.master.protocolVersion = 1
        packet = {
            "Command": "SlaveHeartbeat",
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        # Protocol v1 should not have the extra 2 bytes at the end
        assert not msg.endswith(bytearray(b"\x00\x00"))
    
    def test_create_message_protocol_v2(self, protocol):
        """Test message creation for protocol version 2."""
        protocol.master.protocolVersion = 2
        packet = {
            "Command": "SlaveHeartbeat",
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        
        msg = protocol.createMessage(packet)
        
        assert msg is not None
        # Protocol v2 should have extra 2 bytes at the end
        assert msg.endswith(bytearray(b"\x00\x00"))


class TestProtocolMessageParsing:
    """Test TWC protocol message parsing."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.hex_str = lambda x: x.hex()
        return master
    
    @pytest.fixture
    def protocol(self, mock_master):
        """Create a TWCProtocol instance with mock master."""
        from TWCManager.Protocol.TWCProtocol import TWCProtocol
        return TWCProtocol(mock_master)
    
    def test_parse_master_linkready1(self, protocol):
        """Test parsing MasterLinkready1 message."""
        # MasterLinkready1 format: \xfc\xe1 + 2 bytes ID + 1 byte sign + padding
        msg = bytearray(b"\xfc\xe1\x12\x34\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        
        packet = protocol.parseMessage(msg)
        
        assert packet["Command"] == "MasterLinkready1"
        assert packet["SenderID"] == bytearray(b"\x12\x34")
        assert packet["Match"] is True
    
    def test_parse_master_linkready2(self, protocol):
        """Test parsing MasterLinkready2 message."""
        # MasterLinkready2 format: \xfb\xe2 + 2 bytes ID + 1 byte sign + padding
        msg = bytearray(b"\xfb\xe2\x12\x34\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        
        packet = protocol.parseMessage(msg)
        
        assert packet["Command"] == "MasterLinkready2"
        assert packet["SenderID"] == bytearray(b"\x12\x34")
        assert packet["Match"] is True
    
    def test_parse_master_heartbeat(self, protocol):
        """Test parsing MasterHeartbeat message."""
        # MasterHeartbeat format: \xfb\xe0 + 2 bytes sender + 2 bytes receiver + data
        msg = bytearray(b"\xfb\xe0\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x00")
        
        packet = protocol.parseMessage(msg)
        
        assert packet["Command"] == "MasterHeartbeat"
        assert packet["SenderID"] == bytearray(b"\x12\x34")
        assert packet["ReceiverID"] == bytearray(b"\x56\x78")
        assert packet["Match"] is True
    
    def test_parse_unknown_message(self, protocol):
        """Test parsing unknown message returns no match."""
        msg = bytearray(b"\x00\x00\x00\x00\x00\x00")
        
        packet = protocol.parseMessage(msg)
        
        assert packet["Command"] is None
        assert packet["Match"] is False
    
    def test_parse_message_initializes_packet(self, protocol):
        """Test that parseMessage initializes packet structure."""
        msg = bytearray(b"\x00\x00\x00\x00")
        
        packet = protocol.parseMessage(msg)
        
        assert "Command" in packet
        assert "Errors" in packet
        assert "SenderID" in packet
        assert "Match" in packet
        assert isinstance(packet["Errors"], list)


class TestProtocolInitialization:
    """Test protocol initialization."""
    
    def test_protocol_initialization(self):
        """Test protocol initializes with master."""
        from TWCManager.Protocol.TWCProtocol import TWCProtocol
        master = Mock()
        
        protocol = TWCProtocol(master)
        
        assert protocol.master == master
        assert protocol.operationMode == 0
    
    def test_protocol_operation_mode_default(self):
        """Test protocol operation mode defaults to 0."""
        from TWCManager.Protocol.TWCProtocol import TWCProtocol
        master = Mock()
        
        protocol = TWCProtocol(master)
        
        assert protocol.operationMode == 0


class TestProtocolMessageRoundtrip:
    """Test creating and parsing messages (roundtrip)."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.masterTWCID = "AB"
        master.protocolVersion = 2
        master.hex_str = lambda x: x.hex()
        slave = Mock()
        slave.TWCID = "CD"
        master.getSlaveTWCs = Mock(return_value=[slave])
        return master
    
    @pytest.fixture
    def protocol(self, mock_master):
        """Create a TWCProtocol instance with mock master."""
        from TWCManager.Protocol.TWCProtocol import TWCProtocol
        return TWCProtocol(mock_master)
    
    def test_slave_linkready_roundtrip(self, protocol):
        """Test creating and parsing SlaveLinkready message."""
        # Create message
        create_packet = {
            "Command": "SlaveLinkready",
            "SenderID": bytearray(b"AB"),
            "Sign": bytearray(b"\x00"),
            "Amps": bytearray(b"\x1f\x40"),
        }
        msg = protocol.createMessage(create_packet)
        
        # Note: We can't easily parse SlaveLinkready since the parser
        # is designed for master->slave messages, not slave->master
        assert msg is not None
        assert msg.startswith(bytearray(b"\xfd\xe2"))
    
    def test_slave_heartbeat_roundtrip(self, protocol):
        """Test creating SlaveHeartbeat message."""
        create_packet = {
            "Command": "SlaveHeartbeat",
            "SenderID": bytearray(b"AB"),
            "RecieverID": bytearray(b"CD"),
        }
        msg = protocol.createMessage(create_packet)
        
        assert msg is not None
        assert msg.startswith(bytearray(b"\xfd\xe0"))
