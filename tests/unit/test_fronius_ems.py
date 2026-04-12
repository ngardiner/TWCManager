"""
Unit tests for TWCManager Fronius EMS module.

Tests Fronius solar inverter integration and energy data retrieval.
"""

import pytest
import json
from unittest.mock import Mock, MagicMock, patch


class TestFroniusInitialization:
    """Test Fronius module initialization."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_fronius_initialization(self, mock_master):
        """Test Fronius module initializes correctly."""
        from TWCManager.EMS.Fronius import Fronius
        
        fronius = Fronius(mock_master)
        
        assert fronius.master == mock_master
        assert fronius.status is True
        assert fronius.serverIP == ["192.168.1.100"]
    
    def test_fronius_disabled(self):
        """Test Fronius module can be disabled."""
        from TWCManager.EMS.Fronius import Fronius
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": False,
                    "serverIP": "192.168.1.100",
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        
        fronius = Fronius(master)
        
        master.releaseModule.assert_called_once()
    
    def test_fronius_missing_server_ip(self):
        """Test Fronius module unloads without server IP."""
        from TWCManager.EMS.Fronius import Fronius
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": None,
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        
        fronius = Fronius(master)
        
        master.releaseModule.assert_called_once()
    
    def test_fronius_invalid_port(self):
        """Test Fronius module unloads with invalid port."""
        from TWCManager.EMS.Fronius import Fronius
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "0"
                }
            }
        }
        master.releaseModule = Mock()
        
        fronius = Fronius(master)
        
        master.releaseModule.assert_called_once()
    
    def test_fronius_server_ip_list_conversion(self, mock_master):
        """Test single server IP is converted to list."""
        from TWCManager.EMS.Fronius import Fronius
        
        fronius = Fronius(mock_master)
        
        assert isinstance(fronius.serverIP, list)
        assert len(fronius.serverIP) == 1
    
    def test_fronius_server_ip_already_list(self):
        """Test server IP list is preserved."""
        from TWCManager.EMS.Fronius import Fronius
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": ["192.168.1.100", "192.168.1.101"],
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        
        fronius = Fronius(master)
        
        assert fronius.serverIP == ["192.168.1.100", "192.168.1.101"]


class TestFroniusConfiguration:
    """Test Fronius configuration handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "8080"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_fronius_custom_port(self, mock_master):
        """Test Fronius accepts custom port."""
        from TWCManager.EMS.Fronius import Fronius
        
        fronius = Fronius(mock_master)
        
        assert fronius.serverPort == "8080"
    
    def test_fronius_default_port(self):
        """Test Fronius defaults to port 80."""
        from TWCManager.EMS.Fronius import Fronius
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        fronius = Fronius(master)
        
        assert fronius.serverPort == "80"
    
    def test_fronius_cache_time(self, mock_master):
        """Test Fronius cache time setting."""
        from TWCManager.EMS.Fronius import Fronius
        
        fronius = Fronius(mock_master)
        
        assert fronius.cacheTime == 10
    
    def test_fronius_timeout(self, mock_master):
        """Test Fronius timeout setting."""
        from TWCManager.EMS.Fronius import Fronius
        
        fronius = Fronius(mock_master)
        
        assert fronius.timeout == 10


class TestFroniusDataRetrieval:
    """Test Fronius data retrieval methods."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def fronius(self, mock_master):
        """Create a Fronius instance."""
        from TWCManager.EMS.Fronius import Fronius
        return Fronius(mock_master)
    
    def test_get_consumption_disabled(self, fronius):
        """Test getConsumption returns 0 when disabled."""
        fronius.status = False
        
        result = fronius.getConsumption()
        
        assert result == 0
    
    def test_get_generation_disabled(self, fronius):
        """Test getGeneration returns 0 when disabled."""
        fronius.status = False
        
        result = fronius.getGeneration()
        
        assert result == 0
    
    def test_get_consumption_returns_negative(self, fronius):
        """Test getConsumption returns negative value."""
        fronius.status = True
        fronius.consumedW = 1000
        fronius.update = Mock()
        
        result = fronius.getConsumption()
        
        assert result == -1000.0
    
    def test_get_generation_returns_positive(self, fronius):
        """Test getGeneration returns positive value."""
        fronius.status = True
        fronius.generatedW = 2000
        fronius.update = Mock()
        
        result = fronius.getGeneration()
        
        assert result == 2000.0
    
    def test_get_generation_zero_handling(self, fronius):
        """Test getGeneration handles zero generation."""
        fronius.status = True
        fronius.generatedW = 0
        fronius.update = Mock()
        
        result = fronius.getGeneration()
        
        assert result == 0.0


class TestFroniusEnergyValues:
    """Test Fronius energy value tracking."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def fronius(self, mock_master):
        """Create a Fronius instance."""
        from TWCManager.EMS.Fronius import Fronius
        return Fronius(mock_master)
    
    def test_consumed_w_default(self, fronius):
        """Test consumedW defaults to 0."""
        assert fronius.consumedW == 0
    
    def test_generated_w_default(self, fronius):
        """Test generatedW defaults to 0."""
        assert fronius.generatedW == 0
    
    def test_import_w_default(self, fronius):
        """Test importW defaults to 0."""
        assert fronius.importW == 0
    
    def test_export_w_default(self, fronius):
        """Test exportW defaults to 0."""
        assert fronius.exportW == 0
    
    def test_voltage_default(self, fronius):
        """Test voltage defaults to 0."""
        assert fronius.voltage == 0
    
    def test_consumed_w_update(self, fronius):
        """Test consumedW can be updated."""
        fronius.consumedW = 1500
        
        assert fronius.consumedW == 1500
    
    def test_generated_w_update(self, fronius):
        """Test generatedW can be updated."""
        fronius.generatedW = 3000
        
        assert fronius.generatedW == 3000


class TestFroniusURLConstruction:
    """Test Fronius URL construction."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def fronius(self, mock_master):
        """Create a Fronius instance."""
        from TWCManager.EMS.Fronius import Fronius
        return Fronius(mock_master)
    
    def test_get_inverter_data_url_construction(self, fronius):
        """Test getInverterData constructs correct URL."""
        fronius.getInverterValue = Mock(return_value=True)
        
        fronius.getInverterData("192.168.1.100")
        
        # Verify getInverterValue was called with a URL
        fronius.getInverterValue.assert_called_once()
        call_args = fronius.getInverterValue.call_args[0][0]
        assert "http://" in call_args
        assert "192.168.1.100" in call_args
        assert "solar_api" in call_args
    
    def test_get_inverter_data_includes_port(self, fronius):
        """Test getInverterData includes port in URL."""
        fronius.serverPort = "8080"
        fronius.getInverterValue = Mock(return_value=True)
        
        fronius.getInverterData("192.168.1.100")
        
        call_args = fronius.getInverterValue.call_args[0][0]
        assert ":8080" in call_args


class TestFroniusErrorHandling:
    """Test Fronius error handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def fronius(self, mock_master):
        """Create a Fronius instance."""
        from TWCManager.EMS.Fronius import Fronius
        return Fronius(mock_master)
    
    def test_fetch_failed_flag_default(self, fronius):
        """Test fetchFailed flag defaults to False."""
        assert fronius.fetchFailed is False
    
    def test_fetch_failed_flag_set(self, fronius):
        """Test fetchFailed flag can be set."""
        fronius.fetchFailed = True
        
        assert fronius.fetchFailed is True


class TestFroniusStatus:
    """Test Fronius status tracking."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Fronius": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "80"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def fronius(self, mock_master):
        """Create a Fronius instance."""
        from TWCManager.EMS.Fronius import Fronius
        return Fronius(mock_master)
    
    def test_status_enabled(self, fronius):
        """Test status reflects enabled state."""
        assert fronius.status is True
    
    def test_last_fetch_default(self, fronius):
        """Test lastFetch defaults to 0."""
        assert fronius.lastFetch == 0
    
    def test_last_fetch_update(self, fronius):
        """Test lastFetch can be updated."""
        import time
        current_time = time.time()
        fronius.lastFetch = current_time
        
        assert fronius.lastFetch == current_time
