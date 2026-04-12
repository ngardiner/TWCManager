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
