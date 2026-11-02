"""
Unit tests for the aWATTar spot-market pricing module.
"""

from unittest.mock import Mock, MagicMock, patch
from TWCManager.Pricing.aWATTarPricing import aWATTarPricing


def _master(enabled=True):
    master = Mock()
    master.config = {"pricing": {"aWATTar": {"enabled": enabled}}}
    return master


class TestAwattarInit:
    def test_init_disabled(self):
        master = _master(enabled=False)
        aWATTarPricing(master)
        master.releaseModule.assert_called_once_with(
            "lib.TWCManager.Pricing", "aWATTarPricing"
        )

    def test_init_enabled(self):
        master = _master(enabled=True)
        p = aWATTarPricing(master)
        master.releaseModule.assert_not_called()
        assert p.status is True
        assert p.getCapabilities("AdvancePricing") is True
        assert p.getCapabilities("Forecasting") is False


class TestAwattarUpdate:
    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_mwh_converted_to_kwh(self, mock_requests):
        resp = MagicMock()
        resp.json.return_value = {
            "data": [{"marketprice": 120.0, "unit": "Eur/MWh"}]
        }
        resp.raise_for_status.return_value = None
        mock_requests.get.return_value = resp

        p = aWATTarPricing(_master())
        assert p.getImportPrice() == 0.12  # 120 EUR/MWh -> 0.12 EUR/kWh

    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_cache_prevents_second_fetch(self, mock_requests):
        resp = MagicMock()
        resp.json.return_value = {"data": [{"marketprice": 90.0, "unit": "Eur/MWh"}]}
        resp.raise_for_status.return_value = None
        mock_requests.get.return_value = resp

        p = aWATTarPricing(_master())
        p.getImportPrice()
        p.getImportPrice()
        assert mock_requests.get.call_count == 1  # second call served from cache

    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_connection_error_is_safe(self, mock_requests):
        class ConnErr(Exception):
            pass

        mock_requests.exceptions.ConnectionError = ConnErr
        mock_requests.exceptions.HTTPError = Exception
        mock_requests.get.side_effect = ConnErr("down")

        p = aWATTarPricing(_master())
        # No data ever fetched -> falls back to the default 0, no exception
        assert p.getImportPrice() == 0
        assert p.fetchFailed is True
