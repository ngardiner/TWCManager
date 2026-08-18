"""
Unit tests for Enphase EMS module.

Tests cover:
- Initialization and configuration
- API connection success/failure
- Data parsing and validation
- Error handling (timeouts, malformed responses)
- Caching behavior
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import logging
import time


class TestEnphaseInitialization:
    """Test Enphase module initialization."""
    
    def test_init_disabled_module(self):
        """Test that module unloads when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": False
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            
            # Should call releaseModule when disabled
            master.releaseModule.assert_called_once()
    
    def test_init_missing_cloud_api_credentials(self):
        """Test that module unloads when cloud API credentials missing."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": None,
                    "userID": None,
                    "systemID": None,
                    "serverIP": None,
                    "serverPort": None
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            
            # Should call releaseModule when misconfigured
            master.releaseModule.assert_called_once()
    
    def test_init_with_cloud_api_credentials(self):
        """Test initialization with cloud API credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            
            # Should not call releaseModule
            master.releaseModule.assert_not_called()
            assert enphase.apiKey == "test_key"
            assert enphase.userID == "test_user"
            assert enphase.systemID == "test_system"
    
    def test_init_with_local_api_credentials(self):
        """Test initialization with local API credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": 80
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            
            # Should not call releaseModule
            master.releaseModule.assert_not_called()
            assert enphase.serverIP == "192.168.1.100"
            assert enphase.serverPort == 80
            # Cache time should be reduced for local API
            assert enphase.cacheTime == 10


class TestEnphaseAPIConnection:
    """Test Enphase API connection handling."""
    
    @patch('TWCManager.EMS.Enphase.Enphase.requests')
    def test_cloud_api_connection_success(self, mock_requests):
        """Test successful cloud API connection."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        # Mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {"current_power": 5000}
        mock_requests.get.return_value = mock_response
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            enphase.requests = mock_requests
            
            result = enphase.getPortalData()
            
            assert result == {"current_power": 5000}
            assert enphase.fetchFailed is False
    
    def test_connection_timeout(self):
        """Test handling of connection timeout."""
        import requests
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            
            # Mock the requests module to raise ConnectionError
            with patch.object(enphase, 'requests') as mock_requests:
                mock_requests.exceptions.ConnectionError = requests.exceptions.ConnectionError
                mock_requests.get.side_effect = requests.exceptions.ConnectionError("Timeout")
                
                result = enphase.getPortalValue("http://test.com")
                
                assert result is False
                assert enphase.fetchFailed is True
    
    def test_http_error_handling(self):
        """Test handling of HTTP errors."""
        import requests
        
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            
            # Mock the requests module to raise HTTPError with proper response object
            with patch.object(enphase, 'requests') as mock_requests:
                mock_requests.exceptions.HTTPError = requests.exceptions.HTTPError
                mock_response = Mock()
                mock_error = requests.exceptions.HTTPError("401 Unauthorized")
                mock_error.response = Mock()
                mock_error.response.status_code = 401
                mock_response.raise_for_status.side_effect = mock_error
                mock_requests.get.return_value = mock_response
                
                result = enphase.getPortalValue("http://test.com")
                
                assert result == ""


class TestEnphaseDataParsing:
    """Test Enphase data parsing and validation."""
    
    @patch('TWCManager.EMS.Enphase.Enphase.requests')
    def test_parse_cloud_api_response(self, mock_requests):
        """Test parsing cloud API response."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        # Mock cloud API response
        mock_response = Mock()
        mock_response.json.return_value = {"current_power": 5000}
        mock_requests.get.return_value = mock_response
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            enphase.requests = mock_requests
            enphase.lastFetch = 0  # Force update
            
            enphase.update()
            
            assert enphase.generatedW == 5000
    
    @patch('TWCManager.EMS.Enphase.Enphase.requests')
    def test_parse_local_api_response(self, mock_requests):
        """Test parsing local API response."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "serverPort": 80
                }
            }
        }
        
        # Mock local API response
        mock_response = Mock()
        mock_response.json.return_value = {
            "production": [None, {"wNow": 5000}],
            "consumption": [{"wNow": 3000, "rmsVoltage": 240}]
        }
        mock_requests.get.return_value = mock_response
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            enphase.requests = mock_requests
            enphase.lastFetch = 0  # Force update
            
            enphase.update()
            
            assert enphase.generatedW == 5000
            assert enphase.consumedW == 3000
            assert enphase.voltage == 240
    
    def test_malformed_response_handling(self):
        """Test handling of malformed API response."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            enphase.lastFetch = 0
            enphase.fetchFailed = False
            
            # Simulate malformed response that returns None (not valid JSON)
            with patch.object(enphase, 'getPortalData', return_value=None):
                enphase.update()
                
                # Should handle None gracefully and set fetchFailed
                assert enphase.fetchFailed is True


class TestEnphaseCaching:
    """Test Enphase caching behavior."""
    
    @patch('TWCManager.EMS.Enphase.Enphase.requests')
    def test_cache_expiration(self, mock_requests):
        """Test that cache expires after cacheTime."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        mock_response = Mock()
        mock_response.json.return_value = {"current_power": 5000}
        mock_requests.get.return_value = mock_response
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            enphase.requests = mock_requests
            enphase.cacheTime = 1
            enphase.lastFetch = 0
            
            # First update should fetch
            result1 = enphase.update()
            assert result1 is True
            
            # Second update immediately should use cache
            result2 = enphase.update()
            assert result2 is False
            
            # After cache expires, should fetch again
            enphase.lastFetch = int(time.time()) - 2
            result3 = enphase.update()
            assert result3 is True


class TestEnphaseGetters:
    """Test Enphase getter methods."""
    
    def test_get_consumption_disabled(self):
        """Test getConsumption when module disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": False
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            
            # Should return None when disabled
            master.releaseModule.assert_called_once()
    
    def test_get_generation_returns_float(self):
        """Test that getGeneration returns float."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Enphase": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "userID": "test_user",
                    "systemID": "test_system"
                }
            }
        }
        
        with patch('TWCManager.EMS.Enphase.logger'):
            from TWCManager.EMS.Enphase import Enphase
            enphase = Enphase(master)
            enphase.generatedW = 5000
            
            result = enphase.getGeneration()
            
            assert isinstance(result, float)
            assert result == 5000.0
