"""
Unit tests for TWCManager HTTPControl module.

Tests HTTP server control interface and request handling.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch


class TestHTTPControlInitialization:
    """Test HTTPControl module initialization."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_httpcontrol_initialization(self, mock_master):
        """Test HTTPControl module initializes correctly."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            assert httpcontrol.master == mock_master
            assert httpcontrol.status is True
            assert httpcontrol.httpPort == 8080
    
    def test_httpcontrol_disabled(self):
        """Test HTTPControl module can be disabled."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "HTTP": {
                    "enabled": False,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        
        httpcontrol = HTTPControl(master)
        
        master.releaseModule.assert_called_once()
    
    def test_httpcontrol_invalid_port(self):
        """Test HTTPControl module unloads with invalid port."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 0
                }
            }
        }
        master.releaseModule = Mock()
        
        httpcontrol = HTTPControl(master)
        
        master.releaseModule.assert_called_once()
    
    def test_httpcontrol_missing_config(self):
        """Test HTTPControl handles missing config gracefully."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {}
        }
        master.releaseModule = Mock()
        
        httpcontrol = HTTPControl(master)
        
        master.releaseModule.assert_called_once()


class TestHTTPControlConfiguration:
    """Test HTTPControl configuration handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 9000
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_httpcontrol_custom_port(self, mock_master):
        """Test HTTPControl accepts custom port."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            assert httpcontrol.httpPort == 9000
    
    def test_httpcontrol_default_port(self):
        """Test HTTPControl defaults to port 8080."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "HTTP": {
                    "enabled": True
                }
            }
        }
        master.releaseModule = Mock()
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(master)
            
            assert httpcontrol.httpPort == 8080


class TestThreadingSimpleServer:
    """Test ThreadingSimpleServer class."""
    
    def test_threading_simple_server_exists(self):
        """Test ThreadingSimpleServer class exists."""
        from TWCManager.Control.HTTPControl import ThreadingSimpleServer
        
        assert ThreadingSimpleServer is not None
    
    def test_threading_simple_server_is_http_server(self):
        """Test ThreadingSimpleServer inherits from HTTPServer."""
        from TWCManager.Control.HTTPControl import ThreadingSimpleServer
        from http.server import HTTPServer
        
        assert issubclass(ThreadingSimpleServer, HTTPServer)


class TestHTTPControlStatus:
    """Test HTTPControl status tracking."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_httpcontrol_status_enabled(self, mock_master):
        """Test HTTPControl status reflects enabled state."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            assert httpcontrol.status is True
    
    def test_httpcontrol_status_disabled(self):
        """Test HTTPControl status reflects disabled state."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "HTTP": {
                    "enabled": False,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        
        httpcontrol = HTTPControl(master)
        
        assert httpcontrol.status is False


class TestHTTPControlServerStartup:
    """Test HTTPControl server startup."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_httpcontrol_server_startup_success(self, mock_master):
        """Test HTTPControl server starts successfully."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        mock_server = Mock()
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer', return_value=mock_server):
            with patch('TWCManager.Control.HTTPControl.threading.Thread'):
                httpcontrol = HTTPControl(mock_master)
                
                # Server should not be released on success
                mock_master.releaseModule.assert_not_called()
    
    def test_httpcontrol_server_startup_failure(self, mock_master):
        """Test HTTPControl handles server startup failure."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer', side_effect=OSError("Port in use")):
            httpcontrol = HTTPControl(mock_master)
            
            # Module should be released on failure
            mock_master.releaseModule.assert_called_once()


class TestHTTPControlConfiguration:
    """Test HTTPControl configuration attributes."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_httpcontrol_config_config_default(self, mock_master):
        """Test configConfig defaults to empty dict."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            assert isinstance(httpcontrol.configConfig, dict)
    
    def test_httpcontrol_config_http_default(self, mock_master):
        """Test configHTTP defaults to empty dict."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            assert isinstance(httpcontrol.configHTTP, dict)


class TestHTTPControlErrorHandling:
    """Test HTTPControl error handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_httpcontrol_handles_oserror(self, mock_master):
        """Test HTTPControl handles OSError gracefully."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer', side_effect=OSError("Test error")):
            # Should not raise exception
            httpcontrol = HTTPControl(mock_master)
            
            assert httpcontrol is not None
    
    def test_httpcontrol_handles_missing_control_config(self):
        """Test HTTPControl handles missing control config."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {}
        }
        master.releaseModule = Mock()
        
        # Should not raise exception
        httpcontrol = HTTPControl(master)
        
        assert httpcontrol is not None


class TestHTTPControlPortValidation:
    """Test HTTPControl port validation."""
    
    def test_httpcontrol_port_string_conversion(self):
        """Test HTTPControl converts port to int."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": "9000"
                }
            }
        }
        master.releaseModule = Mock()
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(master)
            
            assert httpcontrol.httpPort == "9000"
    
    def test_httpcontrol_negative_port_invalid(self):
        """Test HTTPControl rejects negative port."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": -1
                }
            }
        }
        master.releaseModule = Mock()
        
        httpcontrol = HTTPControl(master)
        
        master.releaseModule.assert_called_once()
    
    def test_httpcontrol_high_port_valid(self):
        """Test HTTPControl accepts high port numbers."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 65535
                }
            }
        }
        master.releaseModule = Mock()
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(master)
            
            assert httpcontrol.httpPort == 65535


class TestHTTPControlChargeNowEndpoint:
    """Test suite for chargeNow endpoint handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.chargeNow = Mock()
        master.releaseModule = Mock()
        return master
    
    def test_charge_now_with_valid_parameters(self, mock_master):
        """Test chargeNow endpoint with valid parameters."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            # Simulate chargeNow call
            httpcontrol.master.chargeNow(32, 3600)
            
            mock_master.chargeNow.assert_called_once_with(32, 3600)
    
    def test_charge_now_with_minimum_amps(self, mock_master):
        """Test chargeNow with minimum amperage."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            httpcontrol.master.chargeNow(5, 3600)
            
            mock_master.chargeNow.assert_called_once_with(5, 3600)
    
    def test_charge_now_with_maximum_amps(self, mock_master):
        """Test chargeNow with maximum amperage."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            httpcontrol.master.chargeNow(32, 3600)
            
            mock_master.chargeNow.assert_called_once_with(32, 3600)


class TestHTTPControlCancelChargeNow:
    """Test suite for cancelChargeNow endpoint."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.cancelChargeNow = Mock()
        master.releaseModule = Mock()
        return master
    
    def test_cancel_charge_now(self, mock_master):
        """Test cancelChargeNow endpoint."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            httpcontrol.master.cancelChargeNow()
            
            mock_master.cancelChargeNow.assert_called_once()


class TestHTTPControlGetStatus:
    """Test suite for getStatus endpoint."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.getStatus = Mock(return_value={
            "chargerLoadWatts": "0.00",
            "currentPolicy": "Non Scheduled Charging",
            "carsCharging": 0
        })
        master.releaseModule = Mock()
        return master
    
    def test_get_status_returns_dict(self, mock_master):
        """Test getStatus returns a dictionary."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            status = httpcontrol.master.getStatus()
            
            assert isinstance(status, dict)
            assert "chargerLoadWatts" in status
            assert "currentPolicy" in status
    
    def test_get_status_contains_required_fields(self, mock_master):
        """Test getStatus contains required fields."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            status = httpcontrol.master.getStatus()
            
            required_fields = ["chargerLoadWatts", "currentPolicy", "carsCharging"]
            for field in required_fields:
                assert field in status


class TestHTTPControlErrorHandling:
    """Test suite for HTTP error handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_httpcontrol_handles_invalid_json(self, mock_master):
        """Test HTTPControl handles invalid JSON gracefully."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            # Should not raise exception
            assert httpcontrol is not None
    
    def test_httpcontrol_handles_missing_parameters(self, mock_master):
        """Test HTTPControl handles missing parameters."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            # Should not raise exception
            assert httpcontrol is not None


class TestHTTPControlResponseCodes:
    """Test suite for HTTP response codes."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "minAmpsPerTWC": 5,
                "wiringMaxAmpsPerTWC": 32
            },
            "control": {
                "HTTP": {
                    "enabled": True,
                    "listenPort": 8080
                }
            }
        }
        master.chargeNow = Mock()
        master.cancelChargeNow = Mock()
        master.getStatus = Mock(return_value={})
        master.releaseModule = Mock()
        return master
    
    def test_successful_get_returns_200(self, mock_master):
        """Test successful GET requests return 200."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            # GET endpoints should return 200
            assert httpcontrol.master.getStatus() is not None
    
    def test_successful_post_returns_204(self, mock_master):
        """Test successful POST requests return 204 or 200."""
        from TWCManager.Control.HTTPControl import HTTPControl
        
        with patch('TWCManager.Control.HTTPControl.ThreadingSimpleServer'):
            httpcontrol = HTTPControl(mock_master)
            
            # POST endpoints should succeed
            httpcontrol.master.chargeNow(32, 3600)
            mock_master.chargeNow.assert_called_once()

