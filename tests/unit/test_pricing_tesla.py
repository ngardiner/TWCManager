"""
Unit tests for the Tesla (Powerwall tariff) pricing module.
"""

import copy
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from TWCManager.Pricing.TeslaPricing import TeslaPricing
from tests.unit.test_teslapowerwall2_ems import CPP_RATES, CPP_SEASON, DUKE, DUKE_PRICED


class Frozen(datetime):
    # 15:00 on a Wednesday in the tariff's ON_PEAK, in whatever zone asked
    when = (2026, 7, 15, 15, 0)

    @classmethod
    def now(cls, tz=None):
        return datetime(*cls.when, tzinfo=tz)


@pytest.fixture(autouse=True)
def _frozen():
    Frozen.when = (2026, 7, 15, 15, 0)
    with patch("TWCManager.Pricing.TeslaPricing.datetime", Frozen):
        yield


def _master(cfg, tariff, zone="America/New_York", powerwall=True):
    master = Mock()
    master.config = {"pricing": {"Tesla": cfg}}
    pw = Mock()
    pw.getTariff.return_value = (tariff, zone)
    master.getModuleByName.side_effect = lambda name: (
        pw if powerwall and name == "TeslaPowerwall2" else None
    )
    return master


def _pricing(tariff=DUKE_PRICED, cfg=None, **kw):
    return TeslaPricing(_master(cfg or {"enabled": True}, tariff, **kw))


class TestTeslaPricingInit:
    def test_disabled_unloads(self):
        master = _master({"enabled": False}, DUKE_PRICED)
        TeslaPricing(master)
        master.releaseModule.assert_called_once_with(
            "lib.TWCManager.Pricing", "TeslaPricing"
        )

    def test_enabled_stays_loaded(self):
        p = _pricing()
        p.master.releaseModule.assert_not_called()
        assert p.getCapabilities("Forecasting") is True
        assert p.getCapabilities("AdvancePricing") is True


class TestTeslaPricingPrices:
    def test_prices_from_tariff(self):
        p = _pricing()
        assert p.importPrice == 0.30
        assert p.exportPrice == 0.15
        assert p.getImportPrice() == 0.30 and p.getExportPrice() == 0.15
        assert p.currency == "$"

    def test_prices_follow_site_time(self):
        # 13:00 site time is OFF_PEAK
        Frozen.when = (2026, 7, 15, 13, 0)
        assert _pricing().importPrice == 0.158036

    def test_unknown_price_is_absent(self):
        for tariff in (None, DUKE):  # not read yet; no energy_charges
            p = _pricing(tariff)
            assert not hasattr(p, "importPrice")
            assert not hasattr(p, "exportPrice")
            assert p.getImportPrice() is None and p.getExportPrice() is None

    def test_no_sell_tariff_leaves_export_absent(self):
        t = copy.deepcopy(DUKE_PRICED)
        del t["sell_tariff"]
        p = _pricing(t)
        assert p.importPrice == 0.30
        assert not hasattr(p, "exportPrice")

    def test_without_powerwall_module(self, caplog):
        p = _pricing(powerwall=False)
        p.master.releaseModule.assert_not_called()
        assert not hasattr(p, "importPrice")
        p.update()
        assert p.getImportPrice() is None
        assert caplog.text.count("Powerwall") <= 1

    def test_no_zone_uses_host_clock(self):
        assert _pricing(zone=None).importPrice == 0.30

    def test_currency_codes(self):
        for code, shown in (("EUR", "€"), ("GBP", "£"), ("USD", "$"), ("AUD", "AUD")):
            assert _pricing(dict(DUKE_PRICED, currency=code)).currency == shown

    def test_unknown_price_does_not_match_lt_policy(self):
        from TWCManager.Policy.Policy import Policy

        p = _pricing(None)
        master = Mock()
        master.config = {"config": {}, "policy": {}}
        master.settings = {}
        master.modules = {"TeslaPricing": {"ref": p, "type": "Pricing"}}
        master.getModuleByName = Mock(return_value=p)
        master.getModulesByType = Mock(return_value=[])
        policy = Policy(master)
        assert (
            policy.doesConditionMatch(
                "modules.TeslaPricing.importPrice", "lt", 0.5, False
            )
            is False
        )
        p.master.getModuleByName.side_effect = None
        pw = Mock()
        pw.getTariff.return_value = (DUKE_PRICED, "America/New_York")
        p.master.getModuleByName.return_value = pw
        assert (
            policy.doesConditionMatch(
                "modules.TeslaPricing.importPrice", "lt", 0.5, False
            )
            is True
        )


try:
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except ImportError:  # Python 3.8: only the host's clock is available
    NY = None

needs_zoneinfo = pytest.mark.skipif(NY is None, reason="needs zoneinfo")


@needs_zoneinfo
class TestTariffForecast:
    def _fc(self, tariff, now, hours):
        from TWCManager.Pricing.TeslaPricing import tariffForecast

        return tariffForecast(tariff, now, hours, tz=now.tzinfo)

    def test_forecast_slots_and_prices(self):
        fc = self._fc(DUKE_PRICED, datetime(2026, 7, 15, 13, 10, tzinfo=NY), 4)
        assert len(fc) == 8
        assert fc[0]["timestamp"] == datetime(2026, 7, 15, 13, 0, tzinfo=NY)
        assert [e["importPrice"] for e in fc[:4]] == [0.158036] * 2 + [0.30] * 2
        assert fc[2]["period"] == "ON_PEAK"

    def test_forecast_across_dst_end(self):
        fc = self._fc(DUKE_PRICED, datetime(2026, 11, 1, 0, 0, tzinfo=NY), 4)
        # same-tzinfo subtraction ignores the DST change, so compare in UTC
        utc = [e["timestamp"].astimezone(timezone.utc) for e in fc]
        assert {(b - a).total_seconds() for a, b in zip(utc, utc[1:])} == {1800}
        assert len(fc) == 8
        # the repeated 01:00 hour appears twice in local time
        assert [e["timestamp"].hour for e in fc].count(1) == 4

    def test_forecast_without_zone_is_aware(self):
        now = datetime(2026, 7, 15, 13, 0).astimezone()
        fc = self._fc(DUKE_PRICED, now, 1)
        assert len(fc) == 2
        assert all(e["timestamp"].tzinfo is not None for e in fc)

    def test_unpriced_slots_are_left_out(self):
        t = copy.deepcopy(DUKE_PRICED)
        del t["energy_charges"]["Winter"]["rates"]["ON_PEAK"]
        del t["energy_charges"]["ALL"]
        fc = self._fc(t, datetime(2026, 7, 15, 13, 0, tzinfo=NY), 2)
        assert [e["timestamp"].hour for e in fc] == [13, 13]

    def test_cpp_day_in_horizon(self):
        from TWCManager.Pricing.Forecast import cheapestWindow

        t = copy.deepcopy(DUKE_PRICED)
        t["seasons"]["CPP"] = CPP_SEASON
        t["energy_charges"]["CPP"] = CPP_RATES
        fc = self._fc(t, datetime(2026, 7, 15, 22, 0, tzinfo=NY), 24)
        cpp = [
            e for e in fc if e["timestamp"].day == 16 and 6 <= e["timestamp"].hour < 9
        ]
        assert len(cpp) == 6 and all(e["importPrice"] == 0.90 for e in cpp)
        # The CPP day's own OFF_PEAK replaces Winter's SUPER_OFF_PEAK overnight,
        # so the earliest OFF_PEAK run ties for cheapest
        best = cheapestWindow(fc, 2, tz=NY)
        assert best["startTimestamp"] == "2026-07-15T22:00:00-04:00"
        assert best["avgPrice"] == 0.158036


@needs_zoneinfo
class TestTeslaPricingForecast:
    CFG = {"enabled": True, "cheapWindow": {"hours": 2, "startHour": 22, "endHour": 7}}

    def test_cheap_window_on_fixed_plan(self):
        p = _pricing(cfg=self.CFG)
        # SUPER_OFF_PEAK (00:00-05:00) is cheapest; the earliest run wins
        assert p.cheapWindowStart == "2026-07-16T00:00:00-04:00"
        assert p.cheapWindowActive == 0
        Frozen.when = (2026, 7, 16, 1, 0)
        assert p.cheapWindowActive == 1

    def test_no_cheap_window_configured(self):
        p = _pricing()
        assert p.cheapWindowStart is None and p.cheapWindowActive == 0

    def test_min_max_and_api_forecast(self):
        p = _pricing()
        p.updateForecast()
        assert p.forecastMinPrice == 0.146273
        assert p.forecastMaxPrice == 0.30
        fc = p.getPriceForecast(2)
        assert len(fc) == 4 and fc[0]["timestamp"] == "2026-07-15T15:00:00-04:00"
        best = p.getCheapestWindow(2, 22, 7)
        assert best["startTimestamp"] == "2026-07-16T00:00:00-04:00"

    def test_empty_forecast_attributes(self):
        p = _pricing(None, cfg=self.CFG)
        assert not hasattr(p, "forecastMinPrice")
        assert not hasattr(p, "forecastMaxPrice")
        assert p.cheapWindowActive == 0 and p.cheapWindowStart is None
        assert p.getPriceForecast(24) == []
        assert p.getCheapestWindow(2) is None

    def test_forecast_rebuilds_for_new_tariff(self):
        p = _pricing()
        assert p.forecastMaxPrice == 0.30
        t = copy.deepcopy(DUKE_PRICED)
        t["energy_charges"]["Winter"]["rates"]["ON_PEAK"] = 0.50
        p.master.getModuleByName("TeslaPowerwall2").getTariff.return_value = (
            t,
            "America/New_York",
        )
        assert p.forecastMaxPrice == 0.50


@needs_zoneinfo
class TestReviewFixes:
    def test_cheap_window_holds_for_its_whole_length(self):
        cfg = {
            "enabled": True,
            "cheapWindow": {"hours": 4, "startHour": 22, "endHour": 7},
        }
        Frozen.when = (2026, 7, 15, 23, 0)
        p = _pricing(cfg=cfg)
        assert p.cheapWindowStart == "2026-07-16T00:00:00-04:00"
        for hour, minute in [(h, m) for h in range(0, 4) for m in (0, 30)]:
            Frozen.when = (2026, 7, 16, hour, minute)
            assert p.cheapWindowActive == 1, (hour, minute)
        Frozen.when = (2026, 7, 16, 4, 0)
        assert p.cheapWindowActive == 0

    def test_host_clock_forecast_follows_host_dst(self, monkeypatch):
        import time
        from TWCManager.Pricing.TeslaPricing import tariffForecast

        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        try:
            now = datetime(2026, 10, 31, 20, 0).astimezone()
            fc = tariffForecast(DUKE_PRICED, now, 12, tz=None)
            slot = next(
                e
                for e in fc
                if e["timestamp"].astimezone(timezone.utc)
                == datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
            )
            assert slot["timestamp"].hour == 4  # 04:00 EST
            assert slot["period"] == "SUPER_OFF_PEAK"
        finally:
            monkeypatch.undo()
            time.tzset()
