"""
Unit tests for the aWATTar spot-market pricing module.
"""

import time
from unittest.mock import Mock, MagicMock, patch
from TWCManager.Pricing.aWATTarPricing import aWATTarPricing

HOUR_MS = 3600 * 1000


def _master(enabled=True, cheapWindow=None):
    cfg = {"enabled": enabled}
    if cheapWindow:
        cfg["cheapWindow"] = cheapWindow
    return Mock(config={"pricing": {"aWATTar": cfg}})


def _marketdata(prices, slotMs=HOUR_MS, startMs=None):
    # Slots as the API sends them, the first one holding now
    if startMs is None:
        startMs = int(time.time() * 1000) // slotMs * slotMs
    return {
        "object": "list",
        "data": [
            {
                "start_timestamp": startMs + i * slotMs,
                "end_timestamp": startMs + (i + 1) * slotMs,
                "marketprice": price,
                "unit": "Eur/MWh",
            }
            for i, price in enumerate(prices)
        ],
    }


def _respond(mock_requests, body):
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    mock_requests.get.return_value = resp


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
        assert p.getCapabilities("Forecasting") is True


class TestAwattarUpdate:
    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_mwh_converted_to_kwh(self, mock_requests):
        _respond(mock_requests, _marketdata([120.0]))

        p = aWATTarPricing(_master())
        assert p.getImportPrice() == 0.12  # 120 EUR/MWh -> 0.12 EUR/kWh

    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_cache_prevents_second_fetch(self, mock_requests):
        _respond(mock_requests, _marketdata([90.0]))

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


class TestAwattarForecast:
    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_forecast_from_marketdata(self, mock_requests):
        _respond(mock_requests, _marketdata([100.0, 50.0, 20.0, 80.0]))
        p = aWATTarPricing(_master())
        fc = p.getPriceForecast(24)
        assert [e["importPrice"] for e in fc] == [0.1, 0.05, 0.02, 0.08]
        assert p.slotMinutes == 60
        assert p.forecastMinPrice == 0.02 and p.forecastMaxPrice == 0.1
        assert mock_requests.get.call_count == 1

    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_cheap_window_over_hourly_slots(self, mock_requests):
        # The cheapest 2 hours start now, so the window is active
        _respond(mock_requests, _marketdata([10.0, 10.0, 90.0, 90.0]))
        p = aWATTarPricing(_master(cheapWindow={"hours": 2}))
        p.updateForecast()
        best = p.getCheapestWindow(2)
        assert best["avgPrice"] == 0.01
        assert p.cheapWindowActive == 1

    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_quarter_hour_slots(self, mock_requests):
        _respond(
            mock_requests, _marketdata([90.0] * 4 + [10.0] * 4, slotMs=HOUR_MS // 4)
        )
        p = aWATTarPricing(_master())
        p.updateForecast()
        assert p.slotMinutes == 15
        assert p.getCheapestWindow(1)["avgPrice"] == 0.01

    @patch("TWCManager.Pricing.aWATTarPricing.requests")
    def test_past_slots_left_out(self, mock_requests):
        start = int(time.time() * 1000) // HOUR_MS * HOUR_MS - 2 * HOUR_MS
        _respond(mock_requests, _marketdata([1.0, 2.0, 30.0, 40.0], startMs=start))
        p = aWATTarPricing(_master())
        assert p.getImportPrice() == 0.03
        assert len(p.getPriceForecast(24)) == 2
