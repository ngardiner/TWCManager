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
