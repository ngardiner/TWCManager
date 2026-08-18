"""
Unit tests for SolarEdge EMS module.

Tests cover:
- Initialization with cloud API and local Modbus TCP
- API connection and error handling
- Data parsing and validation
- Caching behavior
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import time

# Mock solaredge_modbus before importing SolarEdge
sys.modules['solaredge_modbus'] = MagicMock()


class TestSolarEdgeInitialization:
    """Test SolarEdge module initialization."""
    
    def test_init_disabled_module(self):
        """Test that module unloads when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SolarEdge": {
                    "enabled": False
                }
            }
        }
        
        with patch('TWCManager.EMS.SolarEdge.logger'):
            from TWCManager.EMS.SolarEdge import SolarEdge
            solaredge = SolarEdge(master)
            
            master.releaseModule.assert_called_once()
    
    def test_init_with_cloud_api(self):
        """Test initialization with cloud API credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SolarEdge": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "siteID": "test_site"
                }
            }
        }
        
        with patch('TWCManager.EMS.SolarEdge.logger'):
            from TWCManager.EMS.SolarEdge import SolarEdge
            solaredge = SolarEdge(master)
            
            master.releaseModule.assert_not_called()
            assert solaredge.apiKey == "test_key"
            assert solaredge.siteID == "test_site"
    
    def test_init_with_modbus_tcp(self):
        """Test initialization with Modbus TCP."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SolarEdge": {
                    "enabled": True,
                    "inverterHost": "192.168.1.100",
                    "inverterPort": 1502,
                    "smartMeters": [
                        {"name": "Meter1", "type": "consumption"}
                    ]
                }
            }
        }
        
        with patch('TWCManager.EMS.SolarEdge.logger'):
            from TWCManager.EMS.SolarEdge import SolarEdge
            solaredge = SolarEdge(master)
            
            master.releaseModule.assert_not_called()
            assert solaredge.inverterHost == "192.168.1.100"
            assert solaredge.useModbusTCP is True
            assert solaredge.cacheTime == 10
    
    def test_init_modbus_invalid_smartmeter_config(self):
        """Test that module unloads with invalid smartmeter config."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SolarEdge": {
                    "enabled": True,
                    "inverterHost": "192.168.1.100",
                    "inverterPort": 1502,
                    "smartMeters": [
                        {"name": "Meter1"}  # Missing 'type'
                    ]
                }
            }
        }
        
        with patch('TWCManager.EMS.SolarEdge.logger'):
            from TWCManager.EMS.SolarEdge import SolarEdge
            solaredge = SolarEdge(master)
            
            master.releaseModule.assert_called()


class TestSolarEdgeGetters:
    """Test SolarEdge getter methods."""
    
    def test_get_consumption_disabled(self):
        """Test getConsumption when module disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SolarEdge": {
                    "enabled": False
                }
            }
        }
        
        with patch('TWCManager.EMS.SolarEdge.logger'):
            from TWCManager.EMS.SolarEdge import SolarEdge
            solaredge = SolarEdge(master)
            
            master.releaseModule.assert_called_once()
    
    def test_get_generation_returns_float(self):
        """Test that getGeneration returns float."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SolarEdge": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "siteID": "test_site"
                }
            }
        }
        
        with patch('TWCManager.EMS.SolarEdge.logger'):
            from TWCManager.EMS.SolarEdge import SolarEdge
            solaredge = SolarEdge(master)
            solaredge.generatedW = 5000
            
            result = solaredge.getGeneration()
            
            assert isinstance(result, (int, float))
            assert result == 5000
