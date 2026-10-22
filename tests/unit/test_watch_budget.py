import pytest
from unittest.mock import Mock

from TWCManager.Vehicle.WatchBudget import EVENT, ROUTINE, WatchBudget
from tests.unit.reconcile_helpers import Clock
from tests.unit.test_gen2twc_provider import make as make_master


def test_event_reads_may_spend_the_whole_bucket_but_routine_stops_at_the_floor():
    c = Clock(0.0)
    b = WatchBudget(3.0, clock=c)  # capacity 0.1
    assert b.capacity == pytest.approx(0.1)
    assert b.spend(0.002, ROUTINE) and b.tokens() == pytest.approx(0.098)
    while b.spend(0.002, ROUTINE):
        pass
    assert b.tokens() == pytest.approx(0.05, abs=0.002)  # the floor holds
    assert b.spend(0.002, EVENT) and not b.spend(0.2, EVENT)


def test_tokens_refill_at_the_monthly_rate_and_cap_at_a_day():
    c = Clock(0.0)
    b = WatchBudget(3.0, clock=c)
    while b.spend(0.002, EVENT):
        pass
    c.t += 86400 / 2
    assert b.tokens() == pytest.approx(0.05, abs=0.001)
    c.t += 86400 * 10
    assert b.tokens() == pytest.approx(0.1)


def test_restart_keeps_the_bucket_and_elapsed_time_refills_it():
    c = Clock(1000.0)
    b = WatchBudget(3.0, clock=c)
    while b.spend(0.002, EVENT):
        pass
    saved = b.toJSON()
    c.t += 3600
    b2 = WatchBudget(3.0, clock=c)
    b2.load(saved)
    assert b2.tokens() == pytest.approx(0.1 / 24, abs=0.001)
    b3 = WatchBudget(3.0, clock=c)
    b3.load({"tokens": "junk"})
    assert b3.tokens() == pytest.approx(0.1)


def test_a_zero_price_always_spends_and_settings_round_trip(tmp_path):
    m, s = make_master()
    slot = Mock(accessToken="", refreshToken="", expireTime=0, source="")
    m.getModuleByName = Mock(return_value=Mock(fleetSlot=slot, ownerSlot=slot))
    m.config = dict(m.config, config=dict(m.config["config"]))
    m.config["config"]["settingsPath"] = str(tmp_path)
    while m.watchBudget.spend(0.002, EVENT):
        pass
    assert m.watchBudget.spend(0.0, ROUTINE)
    m.saveSettings()
    m.settings.clear()
    m.loadSettings()
    assert m.watchBudget.tokens() < 0.01


def test_a_future_stamp_or_a_clock_stepping_back_does_not_stall_or_drain():
    c = Clock(1000.0)
    b = WatchBudget(3.0, clock=c)
    while b.spend(0.002, EVENT):
        pass
    b.load({"tokens": 0.0, "at": 1000.0 + 86400})  # saved by a faster clock
    c.t += 43200
    assert b.tokens() == pytest.approx(0.05, abs=0.001)
    c.t -= 3600  # a step back loses only the hour, never drains
    assert b.tokens() == pytest.approx(0.1 * 39600 / 86400, abs=0.001)


def test_mixed_traffic_logs_exhausted_once(caplog):
    c = Clock(0.0)
    b = WatchBudget(3.0, clock=c)
    with caplog.at_level("INFO"):
        for _ in range(60):
            b.spend(0.002, ROUTINE)
            b.spend(0.002, EVENT)
    msgs = [r.getMessage() for r in caplog.records if "Paid-read" in r.getMessage()]
    assert msgs == ["Paid-read budget exhausted"]
