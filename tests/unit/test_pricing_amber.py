"""
Unit tests for the Amber Electric pricing module.
"""

from unittest.mock import Mock, MagicMock, patch
from TWCManager.Pricing.AmberPricing import AmberPricing


def _master(cfg):
    master = Mock()
    master.config = {"pricing": {"Amber": cfg}}
    return master


class TestAmberInit:
    def test_init_disabled(self):
        master = _master({"enabled": False})
        AmberPricing(master)
        master.releaseModule.assert_called_once_with(
            "lib.TWCManager.Pricing", "AmberPricing"
        )

    def test_init_no_token_unloads(self):
        master = _master({"enabled": True})
        p = AmberPricing(master)
        master.releaseModule.assert_called_once_with(
            "lib.TWCManager.Pricing", "AmberPricing"
        )
        assert p.status is False

    def test_init_enabled_with_token(self):
        master = _master({"enabled": True, "token": "abc", "siteId": "site1"})
        p = AmberPricing(master)
        master.releaseModule.assert_not_called()
        assert p.status is True
        assert p.getCapabilities("Forecasting") is False
        assert p.getCapabilities("SpikeDetection") is True


class TestAmberUpdate:
    @patch("TWCManager.Pricing.AmberPricing.requests")
    def test_current_price_parsed(self, mock_requests):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {
                "channelType": "general",
                "startTime": "2000-01-01T00:00:00Z",
                "endTime": "2099-01-01T00:00:00Z",
                "perKwh": 42.0,
                "spikeStatus": "none",
                "descriptor": "low",
                "renewables": 55,
            },
            {
                "channelType": "feedIn",
                "startTime": "2000-01-01T00:00:00Z",
                "endTime": "2099-01-01T00:00:00Z",
                "perKwh": -8.0,
            },
        ]
        mock_requests.get.return_value = resp
        mock_requests.exceptions.ConnectionError = Exception
        mock_requests.exceptions.HTTPError = Exception

        p = AmberPricing(_master({"enabled": True, "token": "abc", "siteId": "s1"}))
        assert p.getImportPrice() == 0.42  # 42 c/kWh -> 0.42 $/kWh
        assert p.getExportPrice() == 0.08  # abs(-8 c/kWh) -> 0.08
        assert p.getSpikeStatus() == "none"
        assert p.getRenewables() == 55

    @patch("TWCManager.Pricing.AmberPricing.requests")
    def test_site_auto_discovery(self, mock_requests):
        sites_resp = MagicMock()
        sites_resp.raise_for_status.return_value = None
        sites_resp.json.return_value = [{"id": "siteX", "status": "active"}]

        prices_resp = MagicMock()
        prices_resp.raise_for_status.return_value = None
        prices_resp.json.return_value = [
            {
                "channelType": "general",
                "startTime": "2000-01-01T00:00:00Z",
                "endTime": "2099-01-01T00:00:00Z",
                "perKwh": 30.0,
            }
        ]
        mock_requests.get.side_effect = [sites_resp, prices_resp]
        mock_requests.exceptions.ConnectionError = Exception
        mock_requests.exceptions.HTTPError = Exception

        p = AmberPricing(_master({"enabled": True, "token": "abc"}))
        assert p.getImportPrice() == 0.30
        assert p.siteId == "siteX"
