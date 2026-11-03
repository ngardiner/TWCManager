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
        assert p.getCapabilities("Forecasting") is True
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


def _interval(start_dt, end_dt, perKwh, channel="general"):
    return {
        "channelType": channel,
        "startTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "perKwh": perKwh,
        "descriptor": "neutral",
        "spikeStatus": "none",
        "renewables": 40,
    }


class TestAmberForecast:
    def _forecast_response(self):
        # Build a forward series of 30-min general intervals starting now.
        # Make the block 2-4 hours out clearly the cheapest.
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        intervals = []
        for slot in range(0, 24):  # 12 hours of 30-min slots
            start = now + timedelta(minutes=30 * slot)
            end = start + timedelta(minutes=30)
            # slots 4..11 (2h..5.5h out) are cheap; rest expensive
            price = 10.0 if 4 <= slot < 12 else 40.0
            intervals.append(_interval(start, end, price))
        return intervals

    @patch("TWCManager.Pricing.AmberPricing.requests")
    def test_forecast_parsed_and_min_max(self, mock_requests):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = self._forecast_response()
        mock_requests.get.return_value = resp
        mock_requests.exceptions.ConnectionError = Exception
        mock_requests.exceptions.HTTPError = Exception

        p = AmberPricing(
            _master({"enabled": True, "token": "abc", "siteId": "s1"})
        )
        fc = p.getPriceForecast(12)
        assert len(fc) > 0
        # forecast entries are JSON-serialisable (timestamp is a string)
        assert isinstance(fc[0]["timestamp"], str)
        assert p.forecastMinPrice == 0.10
        assert p.forecastMaxPrice == 0.40

    @patch("TWCManager.Pricing.AmberPricing.requests")
    def test_cheapest_window_found(self, mock_requests):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = self._forecast_response()
        mock_requests.get.return_value = resp
        mock_requests.exceptions.ConnectionError = Exception
        mock_requests.exceptions.HTTPError = Exception

        p = AmberPricing(
            _master({"enabled": True, "token": "abc", "siteId": "s1"})
        )
        best = p.getCheapestWindow(2)  # cheapest contiguous 2h block
        assert best is not None
        assert best["avgPrice"] == 0.10  # the cheap block

    @patch("TWCManager.Pricing.AmberPricing.requests")
    def test_cheap_window_inactive_without_config(self, mock_requests):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = self._forecast_response()
        mock_requests.get.return_value = resp
        mock_requests.exceptions.ConnectionError = Exception
        mock_requests.exceptions.HTTPError = Exception

        # No cheapWindow configured -> cheapWindowActive stays 0 (inert)
        p = AmberPricing(
            _master({"enabled": True, "token": "abc", "siteId": "s1"})
        )
        p.updateForecast()
        assert p.cheapWindowActive == 0

    @patch("TWCManager.Pricing.AmberPricing.requests")
    def test_cheap_window_active_now(self, mock_requests):
        # A window starting now for the next few hours; "now" is inside it.
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        intervals = []
        for slot in range(0, 24):
            start = now + timedelta(minutes=30 * slot)
            end = start + timedelta(minutes=30)
            # Cheapest block is the first 2 hours (starting now)
            price = 5.0 if slot < 4 else 50.0
            intervals.append(_interval(start, end, price))

        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = intervals
        mock_requests.get.return_value = resp
        mock_requests.exceptions.ConnectionError = Exception
        mock_requests.exceptions.HTTPError = Exception

        # cheapWindow with no hour bounds: cheapest 2h block is right now
        p = AmberPricing(
            _master(
                {
                    "enabled": True,
                    "token": "abc",
                    "siteId": "s1",
                    "cheapWindow": {"hours": 2},
                }
            )
        )
        p.updateForecast()
        assert p.cheapWindowActive == 1
