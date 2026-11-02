"""
Unit tests for the Static (fixed-rate) pricing module.
"""

from unittest.mock import Mock
from TWCManager.Pricing.StaticPricing import StaticPricing


class TestStaticPricingInit:
    def test_init_disabled(self):
        """Module should unload when disabled."""
        master = Mock()
        master.config = {"pricing": {"Static": {"enabled": False}}}
        StaticPricing(master)
        master.releaseModule.assert_called_once_with(
            "lib.TWCManager.Pricing", "StaticPricing"
        )

    def test_init_no_config(self):
        """Module should unload when not configured."""
        master = Mock()
        master.config = {"pricing": {}}
        StaticPricing(master)
        master.releaseModule.assert_called_once_with(
            "lib.TWCManager.Pricing", "StaticPricing"
        )

    def test_init_enabled_reads_prices(self):
        master = Mock()
        master.config = {
            "pricing": {
                "Static": {
                    "enabled": True,
                    "currency": "EUR",
                    "peak": {"import": 0.42, "export": 0.11},
                }
            }
        }
        p = StaticPricing(master)
        master.releaseModule.assert_not_called()
        assert p.currency == "EUR"
        assert p.getImportPrice() == 0.42
        assert p.getExportPrice() == 0.11
        # Attribute surface used by policy "modules.StaticPricing.importPrice"
        assert p.importPrice == 0.42
        assert p.exportPrice == 0.11

    def test_init_missing_peak_defaults_zero(self):
        master = Mock()
        master.config = {"pricing": {"Static": {"enabled": True}}}
        p = StaticPricing(master)
        assert p.getImportPrice() == 0
        assert p.getExportPrice() == 0


class TestStaticPricingCapabilities:
    def _enabled(self):
        master = Mock()
        master.config = {
            "pricing": {"Static": {"enabled": True, "peak": {"import": 0.3}}}
        }
        return StaticPricing(master)

    def test_advance_pricing_capability(self):
        assert self._enabled().getCapabilities("AdvancePricing") is True

    def test_no_forecasting_capability(self):
        # Phase 1 Static does not forecast
        assert self._enabled().getCapabilities("Forecasting") is False

    def test_unknown_capability(self):
        assert self._enabled().getCapabilities("Nonexistent") is False
