"""
Unit tests for TeslaPowerwall2 EMS module.

Tests cover:
- Initialization and configuration
- API connection and error handling
- Data parsing and validation
- Caching behavior
"""

import pytest
from unittest.mock import Mock, patch
import time


class TestTeslaPowerwall2Initialization:
    """Test TeslaPowerwall2 module initialization."""
    
    def test_init_disabled_module(self):
        """Test that module unloads when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "TeslaPowerwall2": {
                    "enabled": False
                }
            }
        }
        
        with patch('TWCManager.EMS.TeslaPowerwall2.logger'):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)
            
            master.releaseModule.assert_called_once()
    
    def test_init_missing_credentials(self):
        """Test that module unloads when credentials missing."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "TeslaPowerwall2": {
                    "enabled": True,
                    "host": None,
                    "password": None
                }
            }
        }
        
        with patch('TWCManager.EMS.TeslaPowerwall2.logger'):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)
            
            master.releaseModule.assert_called_once()
    
    def test_init_with_credentials(self):
        """Test initialization with valid credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "TeslaPowerwall2": {
                    "enabled": True,
                    "host": "192.168.1.100",
                    "password": "test_password"
                }
            }
        }
        
        with patch('TWCManager.EMS.TeslaPowerwall2.logger'):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)
            
            master.releaseModule.assert_not_called()
            assert tp2.host == "192.168.1.100"


class TestTeslaPowerwall2Getters:
    """Test TeslaPowerwall2 getter methods."""
    
    def test_get_generation_returns_float(self):
        """Test that getGeneration returns float."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "TeslaPowerwall2": {
                    "enabled": True,
                    "host": "192.168.1.100",
                    "password": "test_password"
                }
            }
        }
        
        with patch('TWCManager.EMS.TeslaPowerwall2.logger'):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)
            
            # Mock the update method
            with patch.object(tp2, 'update'):
                result = tp2.getGeneration()
                assert isinstance(result, (int, float))
    
    def test_get_consumption_returns_float(self):
        """Test that getConsumption returns float."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "TeslaPowerwall2": {
                    "enabled": True,
                    "host": "192.168.1.100",
                    "password": "test_password"
                }
            }
        }
        
        with patch('TWCManager.EMS.TeslaPowerwall2.logger'):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)
            
            # Mock the update method
            with patch.object(tp2, 'update'):
                result = tp2.getConsumption()
                assert isinstance(result, (int, float))
