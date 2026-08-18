"""
Unit tests for TWCManager Pricing modules.

Tests StaticPricing, aWATTarPricing, and PVPCesPricing modules.
"""

import os
import sys

# Ensure lib/ takes precedence over the root TWCManager.py script so that
# `TWCManager.*` imports resolve to the package in lib/TWCManager/ rather
# than the top-level entry-point script.
_lib_path = os.path.join(os.path.dirname(__file__), "..", "..", "lib")
if _lib_path not in sys.path:
    sys.path.insert(0, _lib_path)

import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def make_master(pricing_config=None):
    """Return a mock master with a minimal config."""
    master = Mock()
    master.config = {
        "config": {"debugLevel": 0},
        "pricing": pricing_config or {},
    }
    master.releaseModule = Mock()
    master.debugLog = Mock()
    return master


# ---------------------------------------------------------------------------
# StaticPricing
# ---------------------------------------------------------------------------


class TestStaticPricing:
    """Tests for the StaticPricing module."""

    @pytest.fixture
    def master_enabled(self):
        return make_master(
            {
                "Static": {
                    "enabled": True,
                    "peak": {"import": 0.25, "export": 0.10},
                }
            }
        )

    @pytest.fixture
    def master_disabled(self):
        return make_master({"Static": {"enabled": False}})

    @pytest.fixture
    def master_no_config(self):
        return make_master({})

    def test_import_price_returned(self, master_enabled):
        from TWCManager.Pricing.StaticPricing import StaticPricing

        module = StaticPricing(master_enabled)
        assert module.getImportPrice() == pytest.approx(0.25)

    def test_export_price_returned(self, master_enabled):
        from TWCManager.Pricing.StaticPricing import StaticPricing

        module = StaticPricing(master_enabled)
        assert module.getExportPrice() == pytest.approx(0.10)

    def test_disabled_module_unloads(self, master_disabled):
        from TWCManager.Pricing.StaticPricing import StaticPricing

        StaticPricing(master_disabled)
        master_disabled.releaseModule.assert_called_once()

    def test_disabled_import_returns_zero(self, master_disabled):
        from TWCManager.Pricing.StaticPricing import StaticPricing

        module = StaticPricing(master_disabled)
        assert module.getImportPrice() == 0

    def test_disabled_export_returns_zero(self, master_disabled):
        from TWCManager.Pricing.StaticPricing import StaticPricing

        module = StaticPricing(master_disabled)
        assert module.getExportPrice() == 0

    def test_missing_config_unloads(self, master_no_config):
        from TWCManager.Pricing.StaticPricing import StaticPricing

        StaticPricing(master_no_config)
        master_no_config.releaseModule.assert_called_once()

    def test_get_capabilities(self, master_enabled):
        from TWCManager.Pricing.StaticPricing import StaticPricing

        module = StaticPricing(master_enabled)
        assert module.getCapabilities("AdvancePricing") is True
        assert module.getCapabilities("NonExistent") is False


# ---------------------------------------------------------------------------
# aWATTarPricing
# ---------------------------------------------------------------------------


class TestAWATTarPricing:
    """Tests for the aWATTarPricing module."""

    @pytest.fixture
    def master_enabled(self):
        return make_master({"aWATTar": {"enabled": True}})

    @pytest.fixture
    def master_disabled(self):
        return make_master({"aWATTar": {"enabled": False}})

    def test_disabled_module_unloads(self, master_disabled):
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

        aWATTarPricing(master_disabled)
        master_disabled.releaseModule.assert_called_once()

    def test_disabled_import_returns_zero(self, master_disabled):
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

        module = aWATTarPricing(master_disabled)
        assert module.getImportPrice() == 0

    def test_disabled_export_returns_zero(self, master_disabled):
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

        module = aWATTarPricing(master_disabled)
        assert module.getExportPrice() == 0

    def test_cache_respected(self, master_enabled):
        """After a successful fetch, lastFetch should be set so the cache is active."""
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

        module = aWATTarPricing(master_enabled)

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [{"marketprice": 150.0, "unit": "Eur/MWh"}]
        }

        with patch.object(module.requests, "get", return_value=mock_response) as mock_get:
            module.update()
            first_fetch_time = module.lastFetch
            assert first_fetch_time > 0

            # Second call should not hit the network (cache still valid)
            module.update()
            assert mock_get.call_count == 1

    def test_mwh_to_kwh_conversion(self, master_enabled):
        """Prices returned in Eur/MWh must be divided by 1000."""
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

        module = aWATTarPricing(master_enabled)

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [{"marketprice": 200.0, "unit": "Eur/MWh"}]
        }

        with patch.object(module.requests, "get", return_value=mock_response):
            module.update()

        assert module.importPrice == pytest.approx(0.20)

    def test_connection_error_sets_fetch_failed(self, master_enabled):
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing
        import requests as req

        module = aWATTarPricing(master_enabled)

        with patch.object(
            module.requests,
            "get",
            side_effect=module.requests.exceptions.ConnectionError,
        ):
            module.update()

        assert module.fetchFailed is True

    def test_connection_error_does_not_update_last_fetch(self, master_enabled):
        """A connection error should not update lastFetch (allow retry sooner)."""
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

        module = aWATTarPricing(master_enabled)

        with patch.object(
            module.requests,
            "get",
            side_effect=module.requests.exceptions.ConnectionError,
        ):
            module.update()

        assert module.lastFetch == 0

    def test_get_capabilities(self, master_enabled):
        from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

        module = aWATTarPricing(master_enabled)
        assert module.getCapabilities("AdvancePricing") is True


# ---------------------------------------------------------------------------
# PVPCesPricing
# ---------------------------------------------------------------------------


class TestPVPCesPricing:
    """Tests for the PVPCesPricing module."""

    @pytest.fixture
    def master_enabled(self):
        return make_master(
            {"PVPCes": {"enabled": True, "token": "test-token-123"}}
        )

    @pytest.fixture
    def master_disabled(self):
        return make_master({"PVPCes": {"enabled": False, "token": "x"}})

    def _make_price_response(self, prices_by_hour):
        """Build a minimal API response with 24 hourly values."""
        values = [{"value": prices_by_hour.get(h, 0)} for h in range(24)]
        return {"indicator": {"values": values}}

    def test_disabled_module_unloads(self, master_disabled):
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing

        PVPCesPricing(master_disabled)
        master_disabled.releaseModule.assert_called_once()

    def test_disabled_import_returns_zero(self, master_disabled):
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing

        module = PVPCesPricing(master_disabled)
        assert module.getImportPrice() == 0

    def test_import_price_current_hour(self, master_enabled):
        """Import price should reflect the current hour's value from the API."""
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing
        from datetime import datetime

        module = PVPCesPricing(master_enabled)
        now = datetime.now()
        price_mwh = 80000.0  # 80 Eur/MWh → 0.08 Eur/kWh
        response_data = self._make_price_response({now.hour: price_mwh})

        mock_response = Mock()
        mock_response.json.return_value = response_data

        with patch.object(module.requests, "get", return_value=mock_response):
            price = module.getImportPrice()

        assert price == pytest.approx(round(price_mwh / 1000, 5))

    def test_cache_not_refetched_same_hour(self, master_enabled):
        """Within the same hour, the API should only be called once."""
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing
        from datetime import datetime

        module = PVPCesPricing(master_enabled)
        response_data = self._make_price_response({datetime.now().hour: 50000.0})
        mock_response = Mock()
        mock_response.json.return_value = response_data

        with patch.object(module.requests, "get", return_value=mock_response) as mock_get:
            module.update()
            module.update()
            assert mock_get.call_count == 1

    def test_token_included_in_headers(self, master_enabled):
        """API token from config must appear in Authorization header."""
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing

        module = PVPCesPricing(master_enabled)
        assert "Token token=test-token-123" in module.headers.get("Authorization", "")

    def test_get_cheapest_start_hour_simple(self, master_enabled):
        """getCheapestStartHour should return the cheapest contiguous window start."""
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing

        module = PVPCesPricing(master_enabled)
        # Hours 2 and 3 are cheapest
        prices = {h: 100000.0 for h in range(24)}
        prices[2] = 10000.0
        prices[3] = 10000.0
        module.todayImportPrice = self._make_price_response(prices)

        result = module.getCheapestStartHour(numHours=2, ini=0, end=10)
        assert result == 2

    def test_get_cheapest_start_hour_cross_midnight(self, master_enabled):
        """getCheapestStartHour should handle end < ini (cross-midnight window)."""
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing

        module = PVPCesPricing(master_enabled)
        # Build 48-slot response (hours 0-47 for cross-midnight support)
        prices = {h: 100000.0 for h in range(48)}
        prices[25] = 5000.0  # cheapest slot is hour 1 of next day (slot 25)
        prices[26] = 5000.0
        response = {"indicator": {"values": [{"value": prices.get(h, 100000.0)} for h in range(48)]}}
        module.todayImportPrice = response

        # Window: 22:00 to 04:00 (ini=22, end=4), needing 2 hours
        result = module.getCheapestStartHour(numHours=2, ini=22, end=4)
        # end becomes 28 (4+24), cheapest 2-hour window starting at slot 25 → hour 1
        assert result == 1

    def test_get_capabilities(self, master_enabled):
        from TWCManager.Pricing.PVPCesPricing import PVPCesPricing

        module = PVPCesPricing(master_enabled)
        assert module.getCapabilities("AdvancePricing") is True


# ---------------------------------------------------------------------------
# TWCMaster pricing aggregation
# ---------------------------------------------------------------------------


class TestTWCMasterPricingAggregation:
    """Tests for TWCMaster's getImportPrice/getExportPrice aggregation logic."""

    @pytest.fixture
    def master(self):
        import logging
        from TWCManager.TWCMaster import TWCMaster

        # Register the custom log levels that TWCManager.py normally installs.
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

    def test_get_import_price_first_policy(self, master):
        """With 'first' policy, only the first non-zero price is returned."""
        master.importPricingValues = {"ModuleA": 0.25, "ModuleB": 0.15}
        master.config["config"]["pricing"] = {"policy": {"multiPrice": "first"}}
        # dict ordering in Python 3.7+ is insertion order
        price = master.getImportPrice()
        assert price == pytest.approx(0.25)

    def test_get_import_price_add_policy(self, master):
        """With 'add' policy, prices from all modules are summed."""
        master.importPricingValues = {"ModuleA": 0.10, "ModuleB": 0.05}
        master.config["config"]["pricing"] = {"policy": {"multiPrice": "add"}}
        assert master.getImportPrice() == pytest.approx(0.15)

    def test_get_export_price_first_policy(self, master):
        master.exportPricingValues = {"ModuleA": 0.08, "ModuleB": 0.04}
        master.config["config"]["pricing"] = {"policy": {"multiPrice": "first"}}
        assert master.getExportPrice() == pytest.approx(0.08)

    def test_get_import_price_default_is_first(self, master):
        """Default multiPrice policy (no config key) must be 'first'."""
        master.importPricingValues = {"ModuleA": 0.20, "ModuleB": 0.10}
        # No pricing key in config
        price = master.getImportPrice()
        assert price == pytest.approx(0.20)

    def test_empty_pricing_values_returns_zero(self, master):
        master.importPricingValues = {}
        master.exportPricingValues = {}
        assert master.getImportPrice() == 0.0
        assert master.getExportPrice() == 0.0


# ---------------------------------------------------------------------------
# AmberPricing
# ---------------------------------------------------------------------------


class TestAmberPricing:
    """Tests for the AmberPricing module (Australian Amber Electric)."""

    @pytest.fixture
    def master_enabled(self):
        return make_master(
            {"Amber": {"enabled": True, "token": "test-amber-token"}}
        )

    @pytest.fixture
    def master_disabled(self):
        return make_master({"Amber": {"enabled": False, "token": "x"}})

    @pytest.fixture
    def master_no_token(self):
        return make_master({"Amber": {"enabled": True}})

    def _make_current_price_response(self, general_price=25.0, feedin_price=8.0, spike_status="none", descriptor="neutral", renewables=45):
        """Build a minimal Amber API /prices/current response that covers current time."""
        now_utc = datetime.utcnow()
        minute = now_utc.minute
        if minute < 30:
            start = now_utc.replace(minute=0, second=0, microsecond=0)
            end = now_utc.replace(minute=30, second=0, microsecond=0)
        else:
            start = now_utc.replace(minute=30, second=0, microsecond=0)
            end = (now_utc + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

        return [
            {
                "type": "CurrentInterval",
                "duration": 30,
                "spotPerKwh": general_price - 5,
                "perKwh": general_price,
                "date": start.strftime("%Y-%m-%d"),
                "nemTime": end.strftime("%Y-%m-%dT%H:%M:%S+10:00"),
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "renewables": renewables,
                "channelType": "general",
                "spikeStatus": spike_status,
                "descriptor": descriptor,
            },
            {
                "type": "CurrentInterval",
                "duration": 30,
                "spotPerKwh": feedin_price - 2,
                "perKwh": feedin_price,
                "date": start.strftime("%Y-%m-%d"),
                "nemTime": end.strftime("%Y-%m-%dT%H:%M:%S+10:00"),
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "renewables": renewables,
                "channelType": "feedIn",
                "spikeStatus": spike_status,
                "descriptor": descriptor,
            },
        ]

    def test_disabled_module_unloads(self, master_disabled):
        from TWCManager.Pricing.AmberPricing import AmberPricing

        AmberPricing(master_disabled)
        master_disabled.releaseModule.assert_called_once()

    def test_missing_token_unloads(self, master_no_token):
        from TWCManager.Pricing.AmberPricing import AmberPricing

        AmberPricing(master_no_token)
        master_no_token.releaseModule.assert_called_once()

    def test_disabled_import_returns_zero(self, master_disabled):
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_disabled)
        assert module.getImportPrice() == 0

    def test_disabled_export_returns_zero(self, master_disabled):
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_disabled)
        assert module.getExportPrice() == 0

    def test_import_price_converts_cents_to_dollars(self, master_enabled):
        """Amber returns prices in c/kWh; module must convert to $/kWh."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"  # Skip auto-discovery

        mock_response = Mock()
        mock_response.json.return_value = self._make_current_price_response(
            general_price=35.0
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            price = module.getImportPrice()

        assert price == pytest.approx(0.35)

    def test_export_price_from_feedin_channel(self, master_enabled):
        """Export price should come from feedIn channel."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_current_price_response(
            feedin_price=12.0
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            price = module.getExportPrice()

        assert price == pytest.approx(0.12)

    def test_spike_detection(self, master_enabled):
        """getSpikeStatus should return spike status from API."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_current_price_response(
            spike_status="spike", descriptor="spike"
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            status = module.getSpikeStatus()

        assert status == "spike"

    def test_renewables_percentage(self, master_enabled):
        """getRenewables should return renewables percentage."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_current_price_response(
            renewables=72
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            renewables = module.getRenewables()

        assert renewables == 72

    def test_price_descriptor(self, master_enabled):
        """getPriceDescriptor should return human-readable price level."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_current_price_response(
            descriptor="extremelyLow"
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            descriptor = module.getPriceDescriptor()

        assert descriptor == "extremelyLow"

    def test_cache_respected(self, master_enabled):
        """After a successful fetch, cache should prevent immediate re-fetch."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_current_price_response()

        with patch.object(module.requests, "get", return_value=mock_response) as mock_get:
            module.update()
            first_fetch_time = module.lastFetch
            assert first_fetch_time > 0

            module.update()
            assert mock_get.call_count == 1

    def test_site_discovery(self, master_enabled):
        """Module should auto-discover siteId from /sites endpoint."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        assert module.siteId is None

        sites_response = [
            {"id": "site-123", "status": "active", "nmi": "1234567890"}
        ]

        with patch.object(module.requests, "get") as mock_get:
            mock_get.return_value = Mock(
                json=lambda: sites_response,
                raise_for_status=Mock()
            )
            module.discoverSiteId()

        assert module.siteId == "site-123"

    def test_get_capabilities(self, master_enabled):
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        assert module.getCapabilities("AdvancePricing") is True
        assert module.getCapabilities("SpikeDetection") is True
        assert module.getCapabilities("Renewables") is True
        assert module.getCapabilities("NonExistent") is False

    def test_negative_prices_supported(self, master_enabled):
        """Amber prices can go negative; module should handle this."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_current_price_response(
            general_price=-5.0
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            price = module.getImportPrice()

        assert price == pytest.approx(-0.05)

    def _make_forecast_response(self, num_intervals=48, base_price=25.0, price_pattern=None):
        """Build a forecast response with multiple future intervals."""
        now_utc = datetime.utcnow()
        intervals = []
        
        for i in range(num_intervals):
            start = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=30 * i)
            end = start + timedelta(minutes=30)
            
            if price_pattern:
                price = price_pattern(i)
            else:
                price = base_price + (i % 10) * 2
            
            intervals.append({
                "type": "ForecastInterval",
                "duration": 30,
                "spotPerKwh": price - 5,
                "perKwh": price,
                "date": start.strftime("%Y-%m-%d"),
                "nemTime": end.strftime("%Y-%m-%dT%H:%M:%S+10:00"),
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "renewables": 40 + (i % 30),
                "channelType": "general",
                "spikeStatus": "spike" if price > 50 else "none",
                "descriptor": "high" if price > 40 else "neutral",
            })
            
            intervals.append({
                "type": "ForecastInterval",
                "duration": 30,
                "spotPerKwh": 8,
                "perKwh": 8,
                "date": start.strftime("%Y-%m-%d"),
                "nemTime": end.strftime("%Y-%m-%dT%H:%M:%S+10:00"),
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "renewables": 40 + (i % 30),
                "channelType": "feedIn",
                "spikeStatus": "none",
                "descriptor": "neutral",
            })
        
        return intervals

    def test_get_price_forecast_returns_list(self, master_enabled):
        """getPriceForecast should return a list of forecast entries."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_forecast_response(num_intervals=24)

        with patch.object(module.requests, "get", return_value=mock_response):
            forecast = module.getPriceForecast(hoursAhead=12)

        assert isinstance(forecast, list)
        assert len(forecast) > 0

    def test_forecast_entries_have_required_fields(self, master_enabled):
        """Each forecast entry should have timestamp, importPrice, exportPrice."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_forecast_response(num_intervals=10)

        with patch.object(module.requests, "get", return_value=mock_response):
            forecast = module.getPriceForecast(hoursAhead=5)

        assert len(forecast) > 0
        entry = forecast[0]
        assert "timestamp" in entry
        assert "importPrice" in entry
        assert "exportPrice" in entry
        assert "spikeStatus" in entry
        assert "descriptor" in entry
        assert "renewables" in entry

    def test_get_cheapest_window_finds_minimum(self, master_enabled):
        """getCheapestWindow should identify the lowest-priced window."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        def price_pattern(i):
            if 20 <= i < 30:
                return 10.0
            return 40.0

        mock_response = Mock()
        mock_response.json.return_value = self._make_forecast_response(
            num_intervals=48, price_pattern=price_pattern
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            result = module.getCheapestWindow(numHours=4)

        assert result is not None
        assert "startHour" in result
        assert "avgPrice" in result
        assert result["avgPrice"] == pytest.approx(0.10)

    def test_get_cheapest_window_with_time_restriction(self, master_enabled):
        """getCheapestWindow should respect startHour/endHour constraints."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        def price_pattern(i):
            hour = (i * 30) // 60
            if hour == 2:
                return 5.0
            elif hour == 14:
                return 8.0
            return 30.0

        mock_response = Mock()
        mock_response.json.return_value = self._make_forecast_response(
            num_intervals=48, price_pattern=price_pattern
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            result = module.getCheapestWindow(numHours=2, startHour=12, endHour=18)

        assert result is not None
        assert 12 <= result["startHour"] < 18

    def test_get_cheapest_window_crosses_midnight(self, master_enabled):
        """getCheapestWindow should handle windows crossing midnight."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        def price_pattern(i):
            hour = (i * 30) // 60
            if 22 <= hour or hour < 4:
                return 8.0
            return 35.0

        mock_response = Mock()
        mock_response.json.return_value = self._make_forecast_response(
            num_intervals=48, price_pattern=price_pattern
        )

        with patch.object(module.requests, "get", return_value=mock_response):
            result = module.getCheapestWindow(numHours=3, startHour=22, endHour=6)

        assert result is not None

    def test_forecast_capability_flag(self, master_enabled):
        """Module should report Forecasting capability."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        assert module.getCapabilities("Forecasting") is True

    def test_forecast_cache_respected(self, master_enabled):
        """Forecast should be cached and not refetched immediately."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        mock_response = Mock()
        mock_response.json.return_value = self._make_forecast_response(num_intervals=10)

        with patch.object(module.requests, "get", return_value=mock_response) as mock_get:
            module._updateForecast(5)
            assert len(module.priceForecast) > 0
            
            module._updateForecast(5)
            assert mock_get.call_count == 1

    def test_cheapest_window_returns_none_for_invalid_hours(self, master_enabled):
        """getCheapestWindow should return None for invalid numHours."""
        from TWCManager.Pricing.AmberPricing import AmberPricing

        module = AmberPricing(master_enabled)
        module.siteId = "test-site-id"

        result = module.getCheapestWindow(numHours=0)
        assert result is None

        result = module.getCheapestWindow(numHours=30)
        assert result is None


class TestTWCMasterForecasting:
    """Tests for TWCMaster's forecast aggregation methods."""

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

    def test_get_price_forecast_returns_empty_without_modules(self, master):
        """getPriceForecast should return empty list if no forecasting modules."""
        result = master.getPriceForecast(hoursAhead=12)
        assert result == []

    def test_get_cheapest_window_returns_none_without_modules(self, master):
        """getCheapestWindow should return None if no forecasting modules."""
        result = master.getCheapestWindow(numHours=4)
        assert result is None

    def test_get_price_forecast_delegates_to_module(self, master):
        """getPriceForecast should delegate to first forecasting-capable module."""
        mock_module = Mock()
        mock_module.getCapabilities.return_value = True
        mock_module.getPriceForecast.return_value = [
            {"timestamp": datetime.now(), "importPrice": 0.25, "exportPrice": 0.08}
        ]
        
        master.modules = {
            "Amber": {
                "type": "Pricing",
                "ref": mock_module,
                "priority": 0,
            }
        }
        
        result = master.getPriceForecast(hoursAhead=12)
        
        assert len(result) == 1
        mock_module.getPriceForecast.assert_called_once_with(12)

    def test_get_cheapest_window_delegates_to_module(self, master):
        """getCheapestWindow should delegate to forecasting modules and return best."""
        mock_module1 = Mock()
        mock_module1.getCapabilities.return_value = True
        mock_module1.getCheapestWindow.return_value = {
            "startHour": 2,
            "avgPrice": 0.15,
            "totalCost": 0.60,
        }
        
        mock_module2 = Mock()
        mock_module2.getCapabilities.return_value = True
        mock_module2.getCheapestWindow.return_value = {
            "startHour": 14,
            "avgPrice": 0.10,
            "totalCost": 0.40,
        }
        
        master.modules = {
            "Amber1": {
                "type": "Pricing",
                "ref": mock_module1,
                "priority": 0,
            },
            "Amber2": {
                "type": "Pricing",
                "ref": mock_module2,
                "priority": 1,
            },
        }
        
        result = master.getCheapestWindow(numHours=4)
        
        assert result["avgPrice"] == pytest.approx(0.10)
        assert result["startHour"] == 14

    def test_get_cheapest_window_ignores_non_forecasting_modules(self, master):
        """getCheapestWindow should skip modules without Forecasting capability."""
        mock_module_no_forecast = Mock()
        mock_module_no_forecast.getCapabilities.return_value = False
        
        master.modules = {
            "Static": {
                "type": "Pricing",
                "ref": mock_module_no_forecast,
                "priority": 0,
            }
        }
        
        result = master.getCheapestWindow(numHours=4)
        assert result is None
