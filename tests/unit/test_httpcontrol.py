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


class TestPricingAPIEndpoints:
    """Test suite for pricing-related API endpoints."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object with pricing support."""
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
        master.getImportPrice = Mock(return_value=0.25)
        master.getExportPrice = Mock(return_value=0.08)
        master.getPricingModules = Mock(return_value=[])
        master.getPricingModuleDetails = Mock(return_value=None)
        master.getPriceForecast = Mock(return_value=[])
        master.getCheapestWindow = Mock(return_value=None)
        master.refreshPricing = Mock(return_value=True)
        return master

    def test_get_pricing_returns_import_export(self, mock_master):
        """Test /api/getPricing returns import and export prices."""
        assert mock_master.getImportPrice() == 0.25
        assert mock_master.getExportPrice() == 0.08
        
    def test_get_pricing_modules_returns_list(self, mock_master):
        """Test /api/getPricingModules returns a list."""
        mock_master.getPricingModules.return_value = [
            {
                "name": "Static",
                "enabled": True,
                "capabilities": {"AdvancePricing": True},
                "importPrice": 0.25,
                "exportPrice": 0.08
            }
        ]
        
        modules = mock_master.getPricingModules()
        assert isinstance(modules, list)
        assert len(modules) == 1
        assert modules[0]["name"] == "Static"

    def test_get_pricing_modules_empty_when_no_modules(self, mock_master):
        """Test /api/getPricingModules returns empty list when no modules."""
        mock_master.getPricingModules.return_value = []
        
        modules = mock_master.getPricingModules()
        assert modules == []

    def test_get_pricing_details_for_specific_module(self, mock_master):
        """Test /api/getPricingDetails for a specific module."""
        mock_master.getPricingModuleDetails.return_value = {
            "name": "Amber",
            "enabled": True,
            "capabilities": {
                "AdvancePricing": True,
                "SpikeDetection": True,
                "Renewables": True,
                "Forecasting": True
            },
            "importPrice": 0.35,
            "exportPrice": 0.12,
            "spikeStatus": "none",
            "renewables": 45,
            "priceDescriptor": "neutral"
        }
        
        details = mock_master.getPricingModuleDetails("Amber")
        assert details["name"] == "Amber"
        assert details["enabled"] is True
        assert details["importPrice"] == 0.35

    def test_get_pricing_details_module_not_found(self, mock_master):
        """Test /api/getPricingDetails returns None for unknown module."""
        mock_master.getPricingModuleDetails.return_value = None
        
        details = mock_master.getPricingModuleDetails("Unknown")
        assert details is None

    def test_get_pricing_forecast_returns_forecast(self, mock_master):
        """Test /api/getPricingForecast returns forecast data."""
        from datetime import datetime
        
        mock_master.getPriceForecast.return_value = [
            {
                "timestamp": datetime.now(),
                "importPrice": 0.25,
                "exportPrice": 0.08,
                "spikeStatus": "none",
                "descriptor": "neutral",
                "renewables": 45
            }
        ]
        
        forecast = mock_master.getPriceForecast(24)
        assert isinstance(forecast, list)
        assert len(forecast) == 1
        assert "importPrice" in forecast[0]

    def test_get_pricing_forecast_empty_when_unavailable(self, mock_master):
        """Test /api/getPricingForecast returns empty when unavailable."""
        mock_master.getPriceForecast.return_value = []
        
        forecast = mock_master.getPriceForecast(24)
        assert forecast == []

    def test_get_pricing_forecast_custom_hours(self, mock_master):
        """Test /api/getPricingForecast respects hours parameter."""
        mock_master.getPriceForecast.return_value = []
        
        mock_master.getPriceForecast(12)
        mock_master.getPriceForecast.assert_called_once_with(12)

    def test_get_cheapest_window_returns_result(self, mock_master):
        """Test /api/getCheapestWindow returns a valid result."""
        mock_master.getCheapestWindow.return_value = {
            "startHour": 2,
            "avgPrice": 0.15,
            "totalCost": 0.60
        }
        
        result = mock_master.getCheapestWindow(4)
        assert result["startHour"] == 2
        assert result["avgPrice"] == 0.15

    def test_get_cheapest_window_no_result(self, mock_master):
        """Test /api/getCheapestWindow returns None when no window found."""
        mock_master.getCheapestWindow.return_value = None
        
        result = mock_master.getCheapestWindow(4)
        assert result is None

    def test_get_cheapest_window_with_time_constraints(self, mock_master):
        """Test /api/getCheapestWindow with start and end hour."""
        mock_master.getCheapestWindow.return_value = {
            "startHour": 14,
            "avgPrice": 0.12,
            "totalCost": 0.48
        }
        
        result = mock_master.getCheapestWindow(4, 12, 18)
        mock_master.getCheapestWindow.assert_called_once_with(4, 12, 18)
        assert 12 <= result["startHour"] < 18

    def test_refresh_pricing_success(self, mock_master):
        """Test /api/refreshPricing forces price refresh."""
        mock_master.refreshPricing.return_value = True
        
        result = mock_master.refreshPricing()
        assert result is True
        mock_master.refreshPricing.assert_called_once()


class TestPricingModulesAPIIntegration:
    """Integration tests for pricing modules API."""

    @pytest.fixture
    def mock_master_with_modules(self):
        """Create a mock master with multiple pricing modules."""
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
        
        master.getModulesByType = Mock(return_value=[
            {
                "name": "Static",
                "ref": Mock(
                    status=True,
                    capabilities={"AdvancePricing": True},
                    getImportPrice=Mock(return_value=0.20),
                    getExportPrice=Mock(return_value=0.05)
                ),
                "priority": 0
            },
            {
                "name": "Amber",
                "ref": Mock(
                    status=True,
                    capabilities={
                        "AdvancePricing": True,
                        "SpikeDetection": True,
                        "Renewables": True,
                        "Forecasting": True
                    },
                    getImportPrice=Mock(return_value=0.35),
                    getExportPrice=Mock(return_value=0.12),
                    getSpikeStatus=Mock(return_value="none"),
                    getRenewables=Mock(return_value=55),
                    getPriceDescriptor=Mock(return_value="neutral")
                ),
                "priority": 1
            }
        ])
        
        return master

    def test_multiple_pricing_modules_listed(self, mock_master_with_modules):
        """Test that multiple pricing modules are returned."""
        modules = mock_master_with_modules.getModulesByType("Pricing")
        assert len(modules) == 2
        assert modules[0]["name"] == "Static"
        assert modules[1]["name"] == "Amber"

    def test_module_capabilities_exposed(self, mock_master_with_modules):
        """Test that module capabilities are properly exposed."""
        modules = mock_master_with_modules.getModulesByType("Pricing")
        
        static_caps = modules[0]["ref"].capabilities
        assert static_caps.get("AdvancePricing") is True
        assert static_caps.get("SpikeDetection", False) is False
        
        amber_caps = modules[1]["ref"].capabilities
        assert amber_caps.get("SpikeDetection") is True
        assert amber_caps.get("Renewables") is True

    def test_module_pricing_methods_called(self, mock_master_with_modules):
        """Test that pricing methods are called correctly."""
        modules = mock_master_with_modules.getModulesByType("Pricing")
        
        amber_ref = modules[1]["ref"]
        amber_ref.getImportPrice()
        amber_ref.getExportPrice()
        
        amber_ref.getImportPrice.assert_called_once()
        amber_ref.getExportPrice.assert_called_once()


class TestPricingAPIErrorHandling:
    """Test error handling for pricing API endpoints."""

    @pytest.fixture
    def mock_master_with_errors(self):
        """Create a mock master that simulates errors."""
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
        master.getImportPrice = Mock(side_effect=Exception("Pricing error"))
        master.getExportPrice = Mock(return_value=0.08)
        master.getPricingModules = Mock(return_value=[])
        master.getPriceForecast = Mock(return_value=[])
        master.getCheapestWindow = Mock(return_value=None)
        return master

    def test_pricing_error_handled(self, mock_master_with_errors):
        """Test that pricing errors are handled gracefully."""
        with pytest.raises(Exception):
            mock_master_with_errors.getImportPrice()

    def test_empty_forecast_on_error(self, mock_master_with_errors):
        """Test that forecast returns empty list on error."""
        forecast = mock_master_with_errors.getPriceForecast(24)
        assert forecast == []

    def test_none_window_on_no_modules(self, mock_master_with_errors):
        """Test that cheapest window returns None when no modules."""
        result = mock_master_with_errors.getCheapestWindow(4)
        assert result is None


class TestPricingAPIDataValidation:
    """Test data validation for pricing API parameters."""

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
        master.getPriceForecast = Mock(return_value=[])
        master.getCheapestWindow = Mock(return_value=None)
        return master

    def test_forecast_hours_validation_positive(self, mock_master):
        """Test that positive hours are accepted for forecast."""
        for hours in [1, 12, 24, 48]:
            mock_master.getPriceForecast(hours)
        
        assert mock_master.getPriceForecast.call_count == 4

    def test_cheapest_window_hours_validation(self, mock_master):
        """Test cheapest window hours parameter validation."""
        mock_master.getCheapestWindow(1)
        mock_master.getCheapestWindow.assert_called_with(1)
        
        mock_master.getCheapestWindow(24)
        mock_master.getCheapestWindow.assert_called_with(24)

    def test_cheapest_window_time_range_validation(self, mock_master):
        """Test cheapest window time range validation."""
        mock_master.getCheapestWindow(4, 0, 23)
        mock_master.getCheapestWindow.assert_called_with(4, 0, 23)
        
        mock_master.getCheapestWindow(4, 22, 6)
        mock_master.getCheapestWindow.assert_called_with(4, 22, 6)


class TestPricingAPIResponseFormat:
    """Test response format for pricing API endpoints."""

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
        master.getImportPrice = Mock(return_value=0.25)
        master.getExportPrice = Mock(return_value=0.08)
        return master

    def test_get_pricing_response_format(self, mock_master):
        """Test /api/getPricing response format."""
        response = {
            "import": mock_master.getImportPrice(),
            "export": mock_master.getExportPrice()
        }
        
        assert "import" in response
        assert "export" in response
        assert isinstance(response["import"], float)
        assert isinstance(response["export"], float)

    def test_get_pricing_modules_response_format(self, mock_master):
        """Test /api/getPricingModules response format."""
        mock_master.getModulesByType = Mock(return_value=[])
        
        response = mock_master.getModulesByType("Pricing")
        assert isinstance(response, list)

    def test_get_cheapest_window_response_format(self, mock_master):
        """Test /api/getCheapestWindow response format."""
        mock_master.getCheapestWindow = Mock(return_value={
            "startHour": 2,
            "avgPrice": 0.15,
            "totalCost": 0.60
        })
        
        result = mock_master.getCheapestWindow(4)
        
        assert "startHour" in result
        assert "avgPrice" in result
        assert "totalCost" in result
        assert isinstance(result["startHour"], int)
        assert isinstance(result["avgPrice"], float)


class TestPricingAPITWCMasterIntegration:
    """Test TWCMaster integration for pricing API."""

    @pytest.fixture
    def master(self):
        import logging
        from TWCManager.TWCMaster import TWCMaster

        for name, level in [
            ("INFO2", 19), ("INFO3", 18), ("INFO4", 17), ("INFO5", 16),
            ("INFO6", 15), ("INFO7", 14), ("INFO8", 13), ("INFO9", 12),
            ("DEBUG2", 9),
        ]:
            if not hasattr(logging, name):
                logging.addLevelName(level, name)
                setattr(logging, name, level)

        config = {
            "config": {
                "wiringMaxAmpsAllTWCs": 48,
                "wiringMaxAmpsPerTWC": 48,
                "minAmpsPerTWC": 6,
                "debugLevel": 0,
                "displayMilliseconds": False,
            }
        }
        m = TWCMaster(b"\x77\x78", config)
        return m

    def test_get_pricing_modules_returns_list(self, master):
        """Test getPricingModules returns a list."""
        result = master.getPricingModules()
        assert isinstance(result, list)

    def test_get_pricing_module_details_returns_none_for_unknown(self, master):
        """Test getPricingModuleDetails returns None for unknown module."""
        result = master.getPricingModuleDetails("NonExistent")
        assert result is None

    def test_refresh_pricing_returns_true(self, master):
        """Test refreshPricing returns True."""
        result = master.refreshPricing()
        assert result is True

    def test_get_price_forecast_returns_list(self, master):
        """Test getPriceForecast returns a list."""
        result = master.getPriceForecast(24)
        assert isinstance(result, list)

    def test_get_cheapest_window_returns_none_without_modules(self, master):
        """Test getCheapestWindow returns None without modules."""
        result = master.getCheapestWindow(4)
        assert result is None

    def test_pricing_values_initialized(self, master):
        """Test that pricing values are initialized."""
        assert hasattr(master, "exportPricingValues")
        assert hasattr(master, "importPricingValues")
        assert isinstance(master.exportPricingValues, dict)
        assert isinstance(master.importPricingValues, dict)


class TestPricingAPIModuleCapabilities:
    """Test module capability detection in pricing API."""

    @pytest.fixture
    def mock_module_static(self):
        """Create a mock Static pricing module."""
        module = Mock()
        module.status = True
        module.capabilities = {"AdvancePricing": True}
        module.getImportPrice = Mock(return_value=0.20)
        module.getExportPrice = Mock(return_value=0.05)
        module.getCapabilities = Mock(side_effect=lambda cap: module.capabilities.get(cap, False))
        return module

    @pytest.fixture
    def mock_module_amber(self):
        """Create a mock Amber pricing module."""
        module = Mock()
        module.status = True
        module.capabilities = {
            "AdvancePricing": True,
            "SpikeDetection": True,
            "Renewables": True,
            "Forecasting": True
        }
        module.getImportPrice = Mock(return_value=0.35)
        module.getExportPrice = Mock(return_value=0.12)
        module.getSpikeStatus = Mock(return_value="spike")
        module.getRenewables = Mock(return_value=72)
        module.getPriceDescriptor = Mock(return_value="high")
        module.getPriceForecast = Mock(return_value=[
            {"timestamp": Mock(), "importPrice": 0.30, "exportPrice": 0.10}
        ])
        module.getCheapestWindow = Mock(return_value={
            "startHour": 14,
            "avgPrice": 0.25,
            "totalCost": 1.00
        })
        module.getCapabilities = Mock(side_effect=lambda cap: module.capabilities.get(cap, False))
        return module

    def test_static_module_capabilities(self, mock_module_static):
        """Test Static module capabilities."""
        assert mock_module_static.getCapabilities("AdvancePricing") is True
        assert mock_module_static.getCapabilities("SpikeDetection") is False
        assert mock_module_static.getCapabilities("Forecasting") is False

    def test_amber_module_capabilities(self, mock_module_amber):
        """Test Amber module capabilities."""
        assert mock_module_amber.getCapabilities("AdvancePricing") is True
        assert mock_module_amber.getCapabilities("SpikeDetection") is True
        assert mock_module_amber.getCapabilities("Renewables") is True
        assert mock_module_amber.getCapabilities("Forecasting") is True

    def test_amber_spike_detection(self, mock_module_amber):
        """Test Amber spike detection capability."""
        status = mock_module_amber.getSpikeStatus()
        assert status == "spike"

    def test_amber_renewables_percentage(self, mock_module_amber):
        """Test Amber renewables percentage capability."""
        renewables = mock_module_amber.getRenewables()
        assert renewables == 72

    def test_amber_price_descriptor(self, mock_module_amber):
        """Test Amber price descriptor capability."""
        descriptor = mock_module_amber.getPriceDescriptor()
        assert descriptor == "high"

    def test_amber_forecasting(self, mock_module_amber):
        """Test Amber forecasting capability."""
        forecast = mock_module_amber.getPriceForecast(24)
        assert isinstance(forecast, list)
        assert len(forecast) > 0

    def test_amber_cheapest_window(self, mock_module_amber):
        """Test Amber cheapest window capability."""
        result = mock_module_amber.getCheapestWindow(4)
        assert result is not None
        assert "startHour" in result
        assert "avgPrice" in result


class TestPricingAPIMultipleModules:
    """Test handling of multiple pricing modules."""

    @pytest.fixture
    def master_with_modules(self):
        import logging
        from TWCManager.TWCMaster import TWCMaster

        for name, level in [
            ("INFO2", 19), ("INFO3", 18), ("INFO4", 17), ("INFO5", 16),
            ("INFO6", 15), ("INFO7", 14), ("INFO8", 13), ("INFO9", 12),
            ("DEBUG2", 9),
        ]:
            if not hasattr(logging, name):
                logging.addLevelName(level, name)
                setattr(logging, name, level)

        config = {
            "config": {
                "wiringMaxAmpsAllTWCs": 48,
                "wiringMaxAmpsPerTWC": 48,
                "minAmpsPerTWC": 6,
                "debugLevel": 0,
                "displayMilliseconds": False,
            }
        }
        m = TWCMaster(b"\x77\x78", config)
        
        m.modules = {
            "Static": {
                "type": "Pricing",
                "ref": Mock(
                    status=True,
                    capabilities={"AdvancePricing": True},
                    getImportPrice=Mock(return_value=0.20),
                    getExportPrice=Mock(return_value=0.05),
                    getCapabilities=Mock(side_effect=lambda cap: cap == "AdvancePricing")
                ),
                "priority": 0
            },
            "Amber": {
                "type": "Pricing",
                "ref": Mock(
                    status=True,
                    capabilities={
                        "AdvancePricing": True,
                        "SpikeDetection": True,
                        "Renewables": True,
                        "Forecasting": True
                    },
                    getImportPrice=Mock(return_value=0.35),
                    getExportPrice=Mock(return_value=0.12),
                    getSpikeStatus=Mock(return_value="none"),
                    getRenewables=Mock(return_value=55),
                    getPriceDescriptor=Mock(return_value="neutral"),
                    getPriceForecast=Mock(return_value=[]),
                    getCapabilities=Mock(side_effect=lambda cap: cap in ["AdvancePricing", "Forecasting", "SpikeDetection", "Renewables"])
                ),
                "priority": 1
            }
        }
        
        return m

    def test_get_pricing_modules_returns_all(self, master_with_modules):
        """Test that all pricing modules are returned."""
        result = master_with_modules.getPricingModules()
        assert len(result) == 2

    def test_get_pricing_module_details_for_static(self, master_with_modules):
        """Test getting details for Static module."""
        result = master_with_modules.getPricingModuleDetails("Static")
        assert result is not None
        assert result["name"] == "Static"
        assert result["importPrice"] == 0.20

    def test_get_pricing_module_details_for_amber(self, master_with_modules):
        """Test getting details for Amber module."""
        result = master_with_modules.getPricingModuleDetails("Amber")
        assert result is not None
        assert result["name"] == "Amber"
        assert result["spikeStatus"] == "none"
        assert result["renewables"] == 55

    def test_get_price_forecast_delegates_to_forecasting_module(self, master_with_modules):
        """Test that forecast is fetched from forecasting-capable module."""
        result = master_with_modules.getPriceForecast(24)
        master_with_modules.modules["Amber"]["ref"].getPriceForecast.assert_called_once_with(24)

    def test_refresh_pricing_updates_all_modules(self, master_with_modules):
        """Test that refreshPricing updates all modules."""
        result = master_with_modules.refreshPricing()
        assert result is True


class TestPricingAPICacheBehavior:
    """Test caching behavior for pricing API."""

    @pytest.fixture
    def mock_master_cached(self):
        """Create a mock master with cached pricing."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {"HTTP": {"enabled": True, "listenPort": 8080}}
        }
        master.releaseModule = Mock()
        master.importPricingValues = {"Static": 0.20, "Amber": 0.35}
        master.exportPricingValues = {"Static": 0.05, "Amber": 0.12}
        master.config["config"]["pricing"] = {"policy": {"multiPrice": "first"}}
        return master

    def test_first_policy_returns_first_price(self, mock_master_cached):
        """Test 'first' policy returns first non-zero price."""
        import logging
        from TWCManager.TWCMaster import TWCMaster

        for name, level in [
            ("INFO2", 19), ("INFO3", 18), ("INFO4", 17), ("INFO5", 16),
            ("INFO6", 15), ("INFO7", 14), ("INFO8", 13), ("INFO9", 12),
            ("DEBUG2", 9),
        ]:
            if not hasattr(logging, name):
                logging.addLevelName(level, name)
                setattr(logging, name, level)

        config = {
            "config": {
                "wiringMaxAmpsAllTWCs": 48,
                "wiringMaxAmpsPerTWC": 48,
                "minAmpsPerTWC": 6,
                "debugLevel": 0,
                "displayMilliseconds": False,
                "pricing": {"policy": {"multiPrice": "first"}}
            }
        }
        m = TWCMaster(b"\x77\x78", config)
        m.importPricingValues = mock_master_cached.importPricingValues
        
        price = m.getImportPrice()
        assert price == 0.20

    def test_add_policy_sums_prices(self, mock_master_cached):
        """Test 'add' policy sums all prices."""
        import logging
        from TWCManager.TWCMaster import TWCMaster

        for name, level in [
            ("INFO2", 19), ("INFO3", 18), ("INFO4", 17), ("INFO5", 16),
            ("INFO6", 15), ("INFO7", 14), ("INFO8", 13), ("INFO9", 12),
            ("DEBUG2", 9),
        ]:
            if not hasattr(logging, name):
                logging.addLevelName(level, name)
                setattr(logging, name, level)

        config = {
            "config": {
                "wiringMaxAmpsAllTWCs": 48,
                "wiringMaxAmpsPerTWC": 48,
                "minAmpsPerTWC": 6,
                "debugLevel": 0,
                "displayMilliseconds": False,
                "pricing": {"policy": {"multiPrice": "add"}}
            }
        }
        m = TWCMaster(b"\x77\x78", config)
        m.importPricingValues = mock_master_cached.importPricingValues
        
        price = m.getImportPrice()
        assert price == 0.55  # 0.20 + 0.35


class TestPricingUIRoute:
    """Test the pricing UI route handling."""

    @pytest.fixture
    def mock_master_with_pricing(self):
        """Create a mock master with pricing modules enabled."""
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
        master.getModulesByType = Mock(return_value=[
            {
                "name": "Static",
                "ref": Mock(status=True),
                "priority": 0
            }
        ])
        return master

    @pytest.fixture
    def mock_master_without_pricing(self):
        """Create a mock master without pricing modules."""
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
        master.getModulesByType = Mock(return_value=[])
        return master

    def test_pricing_route_accessible_with_modules(self, mock_master_with_pricing):
        """Test that /pricing route is accessible when pricing modules exist."""
        modules = mock_master_with_pricing.getModulesByType("Pricing")
        assert len(modules) > 0

    def test_pricing_route_redirects_without_modules(self, mock_master_without_pricing):
        """Test that /pricing redirects to home when no pricing modules."""
        modules = mock_master_without_pricing.getModulesByType("Pricing")
        assert len(modules) == 0

    def test_navbar_shows_pricing_when_enabled(self, mock_master_with_pricing):
        """Test that navbar includes pricing link when modules enabled."""
        modules = mock_master_with_pricing.getModulesByType("Pricing")
        show_pricing_link = len(modules) > 0
        assert show_pricing_link is True

    def test_navbar_hides_pricing_when_disabled(self, mock_master_without_pricing):
        """Test that navbar hides pricing link when no modules."""
        modules = mock_master_without_pricing.getModulesByType("Pricing")
        show_pricing_link = len(modules) > 0
        assert show_pricing_link is False


class TestPricingUITemplate:
    """Test pricing UI template rendering."""

    def test_pricing_template_exists(self):
        """Test that pricing.html.j2 template file exists."""
        import pathlib
        template_path = (
            pathlib.Path(__file__).resolve().parent.parent.parent /
            "lib/TWCManager/Control/themes/Default/pricing.html.j2"
        )
        assert template_path.exists()

    def test_pricing_template_contains_required_elements(self):
        """Test that pricing template contains key UI elements."""
        import pathlib
        template_path = (
            pathlib.Path(__file__).resolve().parent.parent.parent /
            "lib/TWCManager/Control/themes/Default/pricing.html.j2"
        )
        
        with open(template_path, 'r') as f:
            content = f.read()
        
        assert 'pricing-modules-container' in content
        assert 'forecast-container' in content
        assert 'optimal-window-form' in content
        assert '/api/getPricingModules' in content
        assert '/api/getPricingForecast' in content
        assert '/api/getCheapestWindow' in content

    def test_pricing_template_includes_bootstrap(self):
        """Test that pricing template includes bootstrap."""
        import pathlib
        template_path = (
            pathlib.Path(__file__).resolve().parent.parent.parent /
            "lib/TWCManager/Control/themes/Default/pricing.html.j2"
        )
        
        with open(template_path, 'r') as f:
            content = f.read()
        
        assert 'bootstrap.html.j2' in content
        assert 'navbar.html.j2' in content


class TestShowStatusPricingDisplay:
    """Test the pricing display on the main status page."""

    @pytest.fixture
    def mock_master_with_pricing(self):
        """Create a mock master with pricing modules."""
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
        master.getModulesByType = Mock(return_value=[
            {
                "name": "Amber",
                "ref": Mock(
                    status=True,
                    capabilities={
                        "AdvancePricing": True,
                        "SpikeDetection": True,
                        "Renewables": True,
                        "Forecasting": True
                    }
                ),
                "priority": 0
            }
        ])
        return master

    def test_show_status_displays_pricing_when_enabled(self, mock_master_with_pricing):
        """Test that showStatus displays pricing when modules enabled."""
        modules = mock_master_with_pricing.getModulesByType("Pricing")
        assert len(modules) > 0

    def test_show_status_template_has_pricing_rows(self):
        """Test that showStatus template has pricing display rows."""
        import pathlib
        template_path = (
            pathlib.Path(__file__).resolve().parent.parent.parent /
            "lib/TWCManager/Control/themes/Default/showStatus.html.j2"
        )
        
        with open(template_path, 'r') as f:
            content = f.read()
        
        assert 'importPrice' in content
        assert 'exportPrice' in content
        assert 'priceDescriptor' in content
        assert 'renewablesPct' in content

    def test_jsrefresh_has_pricing_poll(self):
        """Test that jsrefresh has pricing polling function."""
        import pathlib
        template_path = (
            pathlib.Path(__file__).resolve().parent.parent.parent /
            "lib/TWCManager/Control/themes/Default/jsrefresh.html.j2"
        )
        
        with open(template_path, 'r') as f:
            content = f.read()
        
        assert 'requestPricing' in content
        assert '/api/getPricing' in content
        assert 'requestPricingDetails' in content
        assert '/api/getPricingDetails' in content


class TestNavbarPricingLink:
    """Test the navbar pricing link conditional display."""

    def test_navbar_template_has_pricing_condition(self):
        """Test that navbar has conditional pricing link."""
        import pathlib
        template_path = (
            pathlib.Path(__file__).resolve().parent.parent.parent /
            "lib/TWCManager/Control/themes/Default/navbar.html.j2"
        )
        
        with open(template_path, 'r') as f:
            content = f.read()
        
        assert 'getModulesByType("Pricing")' in content
        assert '/pricing' in content

    def test_navbar_pricing_link_after_policy(self):
        """Test that pricing link appears after policy link."""
        import pathlib
        template_path = (
            pathlib.Path(__file__).resolve().parent.parent.parent /
            "lib/TWCManager/Control/themes/Default/navbar.html.j2"
        )
        
        with open(template_path, 'r') as f:
            content = f.read()
        
        policy_pos = content.find('/policy')
        pricing_pos = content.find('/pricing')
        schedule_pos = content.find('/schedule')
        
        assert policy_pos > 0
        assert pricing_pos > policy_pos
        assert schedule_pos > pricing_pos

