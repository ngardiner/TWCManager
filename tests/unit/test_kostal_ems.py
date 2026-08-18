"""
Unit tests for Kostal EMS module.

Tests cover:
- Initialization and configuration
- Module enable/disable
"""

import pytest
from unittest.mock import Mock, patch


class TestKostalInitialization:
    """Test Kostal module initialization."""
    
    def test_init_disabled_module(self):
        """Test that module unloads when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Kostal": {
                    "enabled": False
                }
            }
        }
        
        with patch('TWCManager.EMS.Kostal.logger'):
            from TWCManager.EMS.Kostal import Kostal
            kostal = Kostal(master)
            
            master.releaseModule.assert_called_once()
    
    def test_init_missing_host(self):
        """Test that module unloads when host missing."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Kostal": {
                    "enabled": True,
                    "host": None
                }
            }
        }
        
        with patch('TWCManager.EMS.Kostal.logger'):
            from TWCManager.EMS.Kostal import Kostal
            kostal = Kostal(master)
            
            master.releaseModule.assert_called_once()
    
    def test_init_with_host(self):
        """Test initialization with valid host."""
        from unittest.mock import MagicMock
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Kostal": {
                    "enabled": True,
                    "serverIP": "192.168.1.100"
                }
            }
        }

        with patch('TWCManager.EMS.Kostal.logger'), \
             patch('TWCManager.EMS.Kostal.ModbusClient') as mock_modbus_cls:
            mock_client = MagicMock()
            mock_client.open.return_value = True
            mock_client.is_open.return_value = True
            # String reads return a list of zeroes; produces null-char string
            mock_client.read_holding_registers.return_value = [0] * 32
            mock_modbus_cls.return_value = mock_client

            from TWCManager.EMS.Kostal import Kostal
            kostal = Kostal(master)

            master.releaseModule.assert_not_called()
            assert kostal.host == "192.168.1.100"
