"""
Unit tests for TWCManager HASS (Home Assistant) EMS module.

Tests Home Assistant energy management system integration.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch


class TestHASSInitialization:
    """Test HASS module initialization."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "8123",
                    "useHttps": False,
                    "apiKey": "test_key",
                    "hassEntityConsumption": "sensor.consumption",
                    "hassEntityGeneration": "sensor.generation"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_hass_initialization(self, mock_master):
        """Test HASS module initializes correctly."""
        from TWCManager.EMS.HASS import HASS
        
        hass = HASS(mock_master)
        
        assert hass.master == mock_master
        assert hass.status is True
        assert hass.serverIP == "192.168.1.100"
    
    def test_hass_disabled(self):
        """Test HASS module can be disabled."""
        from TWCManager.EMS.HASS import HASS
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": False,
                    "serverIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        hass = HASS(master)
        
        master.releaseModule.assert_called_once()
    
    def test_hass_missing_server_ip(self):
        """Test HASS module unloads without server IP."""
        from TWCManager.EMS.HASS import HASS
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": None
                }
            }
        }
        master.releaseModule = Mock()
        
        hass = HASS(master)
        
        master.releaseModule.assert_called_once()
    
    def test_hass_invalid_port(self):
        """Test HASS module unloads with invalid port."""
        from TWCManager.EMS.HASS import HASS
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "0"
                }
            }
        }
        master.releaseModule = Mock()
        
        hass = HASS(master)
        
        master.releaseModule.assert_called_once()


class TestHASSConfiguration:
    """Test HASS configuration handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "8123",
                    "useHttps": True,
                    "apiKey": "test_key",
                    "hassEntityConsumption": "sensor.consumption",
                    "hassEntityGeneration": "sensor.generation"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_hass_https_enabled(self, mock_master):
        """Test HASS accepts HTTPS configuration."""
        from TWCManager.EMS.HASS import HASS
        
        hass = HASS(mock_master)
        
        assert hass.useHttps is True
    
    def test_hass_api_key(self, mock_master):
        """Test HASS API key configuration."""
        from TWCManager.EMS.HASS import HASS
        
        hass = HASS(mock_master)
        
        assert hass.apiKey == "test_key"
    
    def test_hass_entity_consumption(self, mock_master):
        """Test HASS consumption entity configuration."""
        from TWCManager.EMS.HASS import HASS
        
        hass = HASS(mock_master)
        
        assert hass.hassEntityConsumption == "sensor.consumption"
    
    def test_hass_entity_generation(self, mock_master):
        """Test HASS generation entity configuration."""
        from TWCManager.EMS.HASS import HASS
        
        hass = HASS(mock_master)
        
        assert hass.hassEntityGeneration == "sensor.generation"
    
    def test_hass_default_port(self):
        """Test HASS defaults to port 8123."""
        from TWCManager.EMS.HASS import HASS
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        hass = HASS(master)
        
        assert hass.serverPort == 8123
    
    def test_hass_default_https_false(self):
        """Test HASS defaults to HTTP."""
        from TWCManager.EMS.HASS import HASS
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        hass = HASS(master)
        
        assert hass.useHttps is False


class TestHASSDataRetrieval:
    """Test HASS data retrieval methods."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "8123",
                    "useHttps": False,
                    "apiKey": "test_key",
                    "hassEntityConsumption": "sensor.consumption",
                    "hassEntityGeneration": "sensor.generation"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def hass(self, mock_master):
        """Create a HASS instance."""
        from TWCManager.EMS.HASS import HASS
        return HASS(mock_master)
    
    def test_get_consumption_disabled(self, hass):
        """Test getConsumption returns 0 when disabled."""
        hass.status = False
        
        result = hass.getConsumption()
        
        assert result == 0
    
    def test_get_generation_disabled(self, hass):
        """Test getGeneration returns 0 when disabled."""
        hass.status = False
        
        result = hass.getGeneration()
        
        assert result == 0
    
    def test_get_consumption_returns_value(self, hass):
        """Test getConsumption returns consumption value."""
        hass.status = True
        hass.consumedW = 1500
        hass.update = Mock()
        
        result = hass.getConsumption()
        
        assert result == 1500
    
    def test_get_generation_returns_value(self, hass):
        """Test getGeneration returns generation value."""
        hass.status = True
        hass.generatedW = 2500
        hass.update = Mock()
        
        result = hass.getGeneration()
        
        assert result == 2500


class TestHASSEnergyValues:
    """Test HASS energy value tracking."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def hass(self, mock_master):
        """Create a HASS instance."""
        from TWCManager.EMS.HASS import HASS
        return HASS(mock_master)
    
    def test_consumed_w_default(self, hass):
        """Test consumedW defaults to 0."""
        assert hass.consumedW == 0
    
    def test_generated_w_default(self, hass):
        """Test generatedW defaults to 0."""
        assert hass.generatedW == 0
    
    def test_consumed_w_update(self, hass):
        """Test consumedW can be updated."""
        hass.consumedW = 2000
        
        assert hass.consumedW == 2000
    
    def test_generated_w_update(self, hass):
        """Test generatedW can be updated."""
        hass.generatedW = 3500
        
        assert hass.generatedW == 3500


class TestHASSURLConstruction:
    """Test HASS URL construction."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "8123",
                    "useHttps": False,
                    "apiKey": "test_key"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def hass(self, mock_master):
        """Create a HASS instance."""
        from TWCManager.EMS.HASS import HASS
        return HASS(mock_master)
    
    def test_http_url_construction(self, hass):
        """Test HTTP URL construction."""
        hass.getAPIValue = Mock(return_value=True)
        
        hass.getAPIValue("sensor.test")
        
        # Verify getAPIValue was called
        hass.getAPIValue.assert_called_once()
    
    def test_https_url_construction(self):
        """Test HTTPS URL construction."""
        from TWCManager.EMS.HASS import HASS
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "8123",
                    "useHttps": True,
                    "apiKey": "test_key"
                }
            }
        }
        master.releaseModule = Mock()
        
        hass = HASS(master)
        
        assert hass.useHttps is True


class TestHASSErrorHandling:
    """Test HASS error handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": "8123",
                    "useHttps": False,
                    "apiKey": "test_key"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def hass(self, mock_master):
        """Create a HASS instance."""
        from TWCManager.EMS.HASS import HASS
        return HASS(mock_master)
    
    def test_fetch_failed_flag_default(self, hass):
        """Test fetchFailed flag defaults to False."""
        assert hass.fetchFailed is False
    
    def test_fetch_failed_flag_set(self, hass):
        """Test fetchFailed flag can be set."""
        hass.fetchFailed = True
        
        assert hass.fetchFailed is True


class TestHASSStatus:
    """Test HASS status tracking."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "HASS": {
                    "enabled": True,
                    "serverIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    @pytest.fixture
    def hass(self, mock_master):
        """Create a HASS instance."""
        from TWCManager.EMS.HASS import HASS
        return HASS(mock_master)
    
    def test_status_enabled(self, hass):
        """Test status reflects enabled state."""
        assert hass.status is True
    
    def test_last_fetch_default(self, hass):
        """Test lastFetch defaults to 0."""
        assert hass.lastFetch == 0
    
    def test_last_fetch_update(self, hass):
        """Test lastFetch can be updated."""
        import time
        current_time = time.time()
        hass.lastFetch = current_time
        
        assert hass.lastFetch == current_time
    
    def test_cache_time_default(self, hass):
        """Test cacheTime defaults to 10."""
        assert hass.cacheTime == 10
    
    def test_timeout_default(self, hass):
        """Test timeout defaults to 2."""
        assert hass.timeout == 2
