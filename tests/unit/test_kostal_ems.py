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
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Kostal": {
                    "enabled": True,
                    "host": "192.168.1.100"
                }
            }
        }
        
        with patch('TWCManager.EMS.Kostal.logger'):
            from TWCManager.EMS.Kostal import Kostal
            kostal = Kostal(master)
            
            master.releaseModule.assert_not_called()
            assert kostal.host == "192.168.1.100"
