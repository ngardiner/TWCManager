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


def _make_logger_mock():
    """Return a logger mock whose getEffectiveLevel() returns an int."""
    from unittest.mock import MagicMock
    m = MagicMock()
    m.getEffectiveLevel.return_value = 20
    return m


class TestTeslaPowerwall2Initialization:
    """Test TeslaPowerwall2 module initialization."""

    def test_init_disabled_module(self):
        """Test that module unloads when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Powerwall2": {
                    "enabled": False
                }
            }
        }

        with patch('TWCManager.EMS.TeslaPowerwall2.logger', _make_logger_mock()):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)

            master.releaseModule.assert_called_once()

    def test_init_missing_credentials(self):
        """Test that module unloads when serverIP is missing."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Powerwall2": {
                    "enabled": True,
                    "serverIP": None,
                    "password": None
                }
            }
        }

        with patch('TWCManager.EMS.TeslaPowerwall2.logger', _make_logger_mock()):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)

            master.releaseModule.assert_called_once()

    def test_init_with_credentials(self):
        """Test initialization with valid credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Powerwall2": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "password": "test_password"
                }
            }
        }

        with patch('TWCManager.EMS.TeslaPowerwall2.logger', _make_logger_mock()):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)

            master.releaseModule.assert_not_called()
            assert tp2.serverIP == "192.168.1.100"

    def test_instances_keep_their_own_fetch_cache(self):
        """One Powerwall's cached fetches must not rate-limit another (#691)."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"Powerwall2": {"enabled": True, "serverIP": "192.168.1.100"}},
        }

        with patch('TWCManager.EMS.TeslaPowerwall2.logger', _make_logger_mock()):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            first, second = TeslaPowerwall2(master), TeslaPowerwall2(master)

            first.lastFetch["/api/meters/aggregates"] = (time.time(), {})
            assert second.lastFetch == {}


class TestTeslaPowerwall2Getters:
    """Test TeslaPowerwall2 getter methods."""

    def test_get_generation_returns_float(self):
        """Test that getGeneration returns float."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Powerwall2": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "password": "test_password"
                }
            }
        }

        with patch('TWCManager.EMS.TeslaPowerwall2.logger', _make_logger_mock()):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)

            with patch.object(tp2, 'getPWJson', return_value={}):
                result = tp2.getGeneration()
                assert isinstance(result, (int, float))

    def test_get_consumption_returns_float(self):
        """Test that getConsumption returns float."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Powerwall2": {
                    "enabled": True,
                    "serverIP": "192.168.1.100",
                    "password": "test_password"
                }
            }
        }

        with patch('TWCManager.EMS.TeslaPowerwall2.logger', _make_logger_mock()):
            from TWCManager.EMS.TeslaPowerwall2 import TeslaPowerwall2
            tp2 = TeslaPowerwall2(master)

            with patch.object(tp2, 'getPWValues', return_value={}):
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

    def test_storm_watch_times_out_and_logs_no_exception_text(
        self, requests_mock, caplog
    ):   # slim Task 13: a 10 s bound, and the type alone in the log
        import requests
        from tests.unit.teslatokens import fleet_token, new_api

        api = new_api()
        token = fleet_token()
        api.storeToken(token)
        pw = self._powerwall(api)
        live = requests_mock.get(
            self.SITE, exc=requests.exceptions.ConnectionError("Bearer " + token)
        )
        pw.cloudID = None
        products = requests_mock.get(
            "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/products",
            exc=ValueError("Bearer " + token),
        )

        pw.getStormWatch()               # the products lookup fails
        pw.lastFetch = {}; pw.cloudID = "42"
        pw.getStormWatch()               # the live status fails

        assert products.last_request.timeout == 10 and live.last_request.timeout == 10
        assert token not in caplog.text and "Bearer" not in caplog.text
        assert "ValueError" in caplog.text and "ConnectionError" in caplog.text
