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


class TestStormWatchTokenSlots:
    """Storm Watch reads through the TeslaAPI energy slot."""

    SITE = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/energy_sites/42/live_status"
    OWNER_SITE = "https://owner-api.teslamotors.com/api/1/energy_sites/42/live_status"

    def _powerwall(self, api):
        master = api.master
        master.config["sources"] = {
            "Powerwall2": {"enabled": True, "serverIP": "1.2.3.4", "cloudID": "42"}
        }
        from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2

        pw = TeslaPowerwall2(master)
        pw.cloudCacheTime = 0
        pw.lastFetch = {}  # the class attribute is shared between instances
        return pw

    def test_storm_watch_403_does_not_pause_slot(self, requests_mock, caplog):
        from tests.unit.teslatokens import fleet_token, new_api

        api = new_api()
        api.storeToken(fleet_token(scp=None))
        pw = self._powerwall(api)
        live = requests_mock.get(self.SITE, status_code=403)

        pw.getStormWatch()
        pw.lastFetch = {}
        pw.getStormWatch()

        assert live.call_count == 2
        assert api.fleetSlot.usable("read") is True
        assert api.fleetSlot.usable("command") is True
        assert api.readOnly is False
        errors = [
            r for r in caplog.records
            if r.levelname == "ERROR" and "energy_device_data" in r.getMessage()
        ]
        assert len(errors) == 1

    def test_storm_watch_skips_request_when_token_lacks_energy_scope(
        self, requests_mock, caplog
    ):
        from tests.unit.teslatokens import FLEET_SCOPES, fleet_token, new_api

        api = new_api()
        scopes = [s for s in FLEET_SCOPES if s != "energy_device_data"]
        api.storeToken(fleet_token(scp=scopes))
        pw = self._powerwall(api)
        live = requests_mock.get(self.SITE, json={"response": {}})

        pw.getStormWatch()

        assert live.call_count == 0
        assert "energy_device_data" in caplog.text

    def test_storm_watch_uses_owner_slot_when_present(self, requests_mock):
        from tests.unit.teslatokens import fleet_token, new_api, owner_token

        api = new_api()
        owner = owner_token()
        api.storeToken(owner)
        api.storeToken(fleet_token())
        pw = self._powerwall(api)
        live = requests_mock.get(self.OWNER_SITE, json={"response": {"x": 1}})

        pw.getStormWatch()

        assert live.last_request.headers["Authorization"] == "Bearer " + owner
