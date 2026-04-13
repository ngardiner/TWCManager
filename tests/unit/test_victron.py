import pytest
from unittest.mock import Mock, MagicMock, patch, call
import logging
from TWCManager.EMS.Victron import Victron


class TestVictronInit:
    """Test Victron module initialization"""

    def test_init_disabled(self):
        """Module should unload when disabled"""
        master = Mock()
        master.config = {"sources": {"Victron": {"enabled": False}}}

        victron = Victron(master)

        master.releaseModule.assert_called_once_with("lib.TWCManager.EMS", "Victron")
        assert victron is None

    def test_init_no_config(self):
        """Module should unload when not configured"""
        master = Mock()
        master.config = {"sources": {}}

        victron = Victron(master)

        master.releaseModule.assert_called_once_with("lib.TWCManager.EMS", "Victron")
        assert victron is None

    def test_init_no_server_ip(self):
        """Module should unload when serverIP is missing"""
        master = Mock()
        master.config = {"sources": {"Victron": {"enabled": True, "serverIP": None}}}

        victron = Victron(master)

        master.releaseModule.assert_called_once_with("lib.TWCManager.EMS", "Victron")
        assert victron is None

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_init_connection_success(self, mock_modbus_class):
        """Module should initialize successfully with valid config"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": 502,
                    "unitID": 100,
                }
            }
        }

        victron = Victron(master)

        assert victron is not None
        assert victron.enabled is True
        assert victron.serverIP == "192.168.1.100"
        assert victron.serverPort == 502
        assert victron.unitID == 100
        mock_client.open.assert_called_once()

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_init_connection_failure(self, mock_modbus_class):
        """Module should unload on connection failure"""
        mock_client = MagicMock()
        mock_client.open.return_value = False
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)

        master.releaseModule.assert_called_once_with("lib.TWCManager.EMS", "Victron")
        assert victron is None

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_init_custom_registers(self, mock_modbus_class):
        """Module should accept custom register addresses"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_modbus_class.return_value = mock_client

        custom_consumption = [[820, 100], [821, 100]]
        custom_generation = [[823, 100], [824, 100]]

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "consumptionRegisters": custom_consumption,
                    "generationRegisters": custom_generation,
                }
            }
        }

        victron = Victron(master)

        assert victron.consumptionRegisters == custom_consumption
        assert victron.generationRegisters == custom_generation


class TestVictronGetters:
    """Test getConsumption and getGeneration methods"""

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_get_consumption_disabled(self, mock_modbus_class):
        """getConsumption should return 0 when disabled"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        victron.enabled = False

        result = victron.getConsumption()

        assert result == 0

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_get_generation_disabled(self, mock_modbus_class):
        """getGeneration should return 0 when disabled"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        victron.enabled = False

        result = victron.getGeneration()

        assert result == 0

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_get_consumption_returns_negative(self, mock_modbus_class):
        """getConsumption should return negative value"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.read_input_registers.return_value = [1000]
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        victron.consumedW = 1000

        result = victron.getConsumption()

        assert result == -1000.0

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_get_generation_returns_positive(self, mock_modbus_class):
        """getGeneration should return positive value"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.read_input_registers.return_value = [2000]
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        victron.generatedW = 2000

        result = victron.getGeneration()

        assert result == 2000.0


class TestVictronReadRegisters:
    """Test register reading functionality"""

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_read_registers_success(self, mock_modbus_class):
        """Should successfully read and sum multiple registers"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.is_open = True
        mock_client.read_input_registers.side_effect = [[1000], [1500], [2000]]
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        registers = [(817, 100), (818, 100), (819, 100)]

        result = victron._Victron__readRegisters(registers)

        assert result == 4500
        assert mock_client.read_input_registers.call_count == 3

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_read_registers_partial_failure(self, mock_modbus_class):
        """Should continue reading on partial failures"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.is_open = True
        mock_client.read_input_registers.side_effect = [[1000], None, [2000]]
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        registers = [(817, 100), (818, 100), (819, 100)]

        result = victron._Victron__readRegisters(registers)

        # Should sum only successful reads
        assert result == 3000

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_read_registers_connection_closed(self, mock_modbus_class):
        """Should reopen connection if closed"""
        mock_client = MagicMock()
        mock_client.is_open = False
        mock_client.open.return_value = True
        mock_client.read_input_registers.return_value = [1000]
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        registers = [(817, 100)]

        result = victron._Victron__readRegisters(registers)

        mock_client.open.assert_called()
        assert result == 1000

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_read_registers_unit_id_switching(self, mock_modbus_class):
        """Should switch unit IDs for each register"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.is_open = True
        mock_client.unit_id = 100
        mock_client.read_input_registers.return_value = [1000]
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        registers = [(817, 100), (818, 101), (819, 102)]

        result = victron._Victron__readRegisters(registers)

        # Verify unit_id was set for each register
        unit_id_calls = [call.unit_id for call in mock_client.method_calls if "unit_id" in str(call)]
        assert len(unit_id_calls) >= 3


class TestVictronCaching:
    """Test caching behavior"""

    @patch("TWCManager.EMS.Victron.ModbusClient")
    @patch("TWCManager.EMS.Victron.time")
    def test_cache_expiration(self, mock_time, mock_modbus_class):
        """Should update when cache expires"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.is_open = True
        mock_client.read_input_registers.return_value = [1000]
        mock_modbus_class.return_value = mock_client

        # Mock time progression
        mock_time.time.side_effect = [0, 0, 15]  # Initial, then 15 seconds later

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        victron.lastUpdate = 0

        # First call should update
        victron._Victron__update()
        first_call_count = mock_client.read_input_registers.call_count

        # Second call within cache window should not update
        victron._Victron__update()
        assert mock_client.read_input_registers.call_count == first_call_count

        # Third call after cache expiration should update
        victron._Victron__update()
        assert mock_client.read_input_registers.call_count > first_call_count


class TestVictronCleanup:
    """Test module cleanup"""

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_destructor_closes_connection(self, mock_modbus_class):
        """Destructor should close Modbus connection"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.is_open = True
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        victron.__del__()

        mock_client.close.assert_called_once()

    @patch("TWCManager.EMS.Victron.ModbusClient")
    def test_destructor_handles_closed_connection(self, mock_modbus_class):
        """Destructor should handle already-closed connection"""
        mock_client = MagicMock()
        mock_client.open.return_value = True
        mock_client.is_open = False
        mock_modbus_class.return_value = mock_client

        master = Mock()
        master.config = {
            "sources": {
                "Victron": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                }
            }
        }

        victron = Victron(master)
        victron.__del__()

        # Should not call close if already closed
        mock_client.close.assert_not_called()
