"""
Unit tests for Growatt EMS module.

Tests cover:
- Initialization and configuration
- Module enable/disable
"""

import pytest
from unittest.mock import Mock, patch


class TestGrowattInitialization:
    """Test Growatt module initialization."""
    
    def test_init_disabled_module(self):
        """Test that module unloads when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Growatt": {
                    "enabled": False
                }
            }
        }
        
        with patch('TWCManager.EMS.Growatt.logger'):
            from TWCManager.EMS.Growatt import Growatt
            growatt = Growatt(master)
            
            master.releaseModule.assert_called_once()
    
    def test_init_missing_credentials(self):
        """Test that module unloads when credentials missing."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Growatt": {
                    "enabled": True,
                    "username": None,
                    "password": None
                }
            }
        }
        
        with patch('TWCManager.EMS.Growatt.logger'):
            from TWCManager.EMS.Growatt import Growatt
            growatt = Growatt(master)
            
            master.releaseModule.assert_called_once()
    
    def test_init_with_credentials(self):
        """Test initialization with valid credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Growatt": {
                    "enabled": True,
                    "username": "test_user",
                    "password": "test_pass",
                    "deviceID": "test_device"
                }
            }
        }
        
        with patch('TWCManager.EMS.Growatt.logger'):
            from TWCManager.EMS.Growatt import Growatt
            growatt = Growatt(master)
            
            master.releaseModule.assert_not_called()
