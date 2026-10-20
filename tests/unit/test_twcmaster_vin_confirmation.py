import threading
from datetime import timedelta
from unittest.mock import Mock
import pytest
from TWCManager.TWCMaster import TWCMaster

VIN = "5YJ3E1EA1KF000001"
CONFIG = {
    "config": {
        "debugOutputToFile": False,
        "subtractChargerLoad": False,
        "treatGenerationAsGridDelivery": False,
        "wiringMaxAmpsAllTWCs": 80,
        "maxAmpsAllowedFromGrid": None,
    }
}


@pytest.fixture
def master():
    m = TWCMaster("AB", CONFIG)
    m.getModuleByName = Mock(return_value=None)
    m.queue_background_task = Mock()
    m.getModulesByType = Mock(return_value=[])
    return m


def slave(vin):
    s = Mock()
    s.TWCID = bytearray(b"\x82\x44")
    s.currentVIN = vin
    s.lifetimekWh = 100
    return s


def test_first_sighting_requeries_and_records_nothing(master):
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(slave(VIN))
    master.queue_background_task.assert_called_once_with(
        {"cmd": "getVehicleVIN", "slaveTWC": bytearray(b"\x82\x44"), "vinPart": 0},
        delay=5,
    )
    assert "Vehicles" not in master.settings or VIN not in master.settings["Vehicles"]


def test_second_matching_sighting_records_one_session(master):
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(slave(VIN))
    master.recordVehicleVIN(slave(VIN))
    assert master.settings["Vehicles"][VIN]["chargeSessions"] == 1
    assert master.settings["SlaveTWCs"]["8244"]["supportsVINQuery"] == 1
    assert master.vehicleRegistry.get(VIN).latest("twcVin").value == "8244"


def test_known_vehicle_records_on_first_sighting(master):
    master.vehicleRegistry.getOrCreate(VIN, "OwnerAPI")
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(slave(VIN))
    assert master.settings["Vehicles"][VIN]["chargeSessions"] == 1
    assert not any(
        c.args[0]["cmd"] == "getVehicleVIN"
        for c in master.queue_background_task.call_args_list
    )


def test_charging_session_begins_and_ends_anonymous_vehicle(master):
    s = slave("")
    s.reportedAmpsActual = 5.0
    s.isCharging = 0
    s.chargingDroppedBelowThresholdTime = 0
    s.plugSessions = 0  # a Mock slave: the session counter is a real int
    master.getSlaveTWCs = Mock(return_value=[s])
    master.observeChargingSessions()
    assert master.vehicleRegistry.get("anon:8244") is not None
    s.reportedAmpsActual = 0.0
    s.chargingDroppedBelowThresholdTime = 1  # long ago
    master.observeChargingSessions()
    assert master.vehicleRegistry.get("anon:8244") is None and s.plugSessions == 1


# Fix round 1: confirmation retries, per-TWC dedupe, delayed-queue ties.
import time
from datetime import datetime
from unittest.mock import patch


def charging_slave(vin):
    s = slave(vin)
    s.isCharging = 1
    s.lastVINQuery = 0
    s.vinQueryAttempt = 0
    return s


def vin_queries(master):
    return [
        c
        for c in master.queue_background_task.call_args_list
        if c.args[0]["cmd"] == "getVehicleVIN"
    ]


def test_lost_confirmation_response_is_requeried_then_confirmed(master):
    s = charging_slave(VIN)
    master.getSlaveTWCs = Mock(return_value=[s])
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(s)  # pending; delayed re-query queued
    assert s.lastVINQuery > 0 and s.vinQueryAttempt == 1
    # The delayed query's response is lost; a minute later we ask again.
    s.lastVINQuery = time.time() - 61
    master.retryVINQuery()
    retry = vin_queries(master)[-1]
    assert retry.args[0] == {
        "cmd": "getVehicleVIN",
        "slaveTWC": bytearray(b"\x82\x44"),
        "vinPart": 0,
    }
    assert s.vinQueryAttempt == 2
    # The RX loop clears the retry timer and records the parse.
    s.lastVINQuery = 0
    s.vinQueryAttempt = 0
    master.recordVehicleVIN(s)
    assert master.vehicleRegistry.get(VIN) is not None
    assert master.settings["Vehicles"][VIN]["chargeSessions"] == 1


def test_confirmation_retries_are_bounded(master):
    s = charging_slave(VIN)
    master.getSlaveTWCs = Mock(return_value=[s])
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(s)
    before = len(vin_queries(master))
    for _ in range(10):
        s.lastVINQuery = time.time() - 61
        master.retryVINQuery()
    assert len(vin_queries(master)) - before == 5  # 6 queries in all
    assert master.vehicleRegistry.isPending("8244")
    assert master.vehicleRegistry.get(VIN) is None


def test_no_retry_once_confirmed_or_rejected(master):
    s = charging_slave(VIN)
    master.getSlaveTWCs = Mock(return_value=[s])
    master.vehicleRegistry.getOrCreate(VIN, "OwnerAPI")
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(s)  # confirmed at once
    s.lastVINQuery = time.time() - 61
    s.vinQueryAttempt = 1
    master.retryVINQuery()
    assert vin_queries(master) == []


def test_queue_failure_on_pending_does_not_escape(master):
    master.queue_background_task.side_effect = TypeError("boom")
    s = charging_slave(VIN)
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(s)  # must not raise onto the serial loop


@pytest.fixture
def queued():
    m = TWCMaster("AB", CONFIG)
    # The background queue state is class-level; isolate it per test.
    m.backgroundTasksQueue = Mock()
    m.backgroundTasksCmds = {}
    m.backgroundTasksDelayed = []
    return m


def test_vin_queries_for_different_twcs_are_not_deduped(queued):
    a = {"cmd": "getVehicleVIN", "slaveTWC": bytearray(b"\x82\x44"), "vinPart": 0}
    b = {"cmd": "getVehicleVIN", "slaveTWC": b"\x11\x22", "vinPart": 0}
    queued.queue_background_task(a)
    queued.queue_background_task(b)
    assert [c.args[0] for c in queued.backgroundTasksQueue.put.call_args_list] == [
        a,
        b,
    ]
    assert a["slaveTWC"] == bytearray(b"\x82\x44")
    # Same TWC still dedupes (bytes and bytearray ids match).
    queued.queue_background_task(
        {"cmd": "getVehicleVIN", "slaveTWC": b"\x82\x44", "vinPart": "1"}
    )
    assert queued.backgroundTasksQueue.put.call_count == 2
    assert a["vinPart"] == "1"
    queued.doneBackgroundTask(a)
    queued.doneBackgroundTask(b)
    assert queued.backgroundTasksCmds == {}


def test_delayed_tasks_with_equal_due_times_do_not_compare_dicts(queued):
    fixed = datetime(2026, 10, 14, 12, 0, 0)
    with patch("TWCManager.TWCMaster.datetime") as dt:
        dt.now.return_value = fixed
        queued.queue_background_task({"cmd": "first"}, delay=5)
        queued.queue_background_task({"cmd": "second"}, delay=5)
    assert [e[-1]["cmd"] for e in queued.backgroundTasksDelayed] == ["first", "second"]


def test_a_confirmed_vin_learns_home_from_the_cars_fresh_fix_without_io(master):
    # recordVehicleVIN runs on the RS-485 thread: home is learned from the
    # registry alone, never by reading the car
    api = Mock()
    master.getModuleByName = Mock(
        side_effect=lambda n: api if n == "TeslaAPI" else None
    )
    v = master.vehicleRegistry.getOrCreate(VIN, "OwnerAPI")
    v.observe("lat", 39.0, "TeslaMateVehicle")
    v.observe("lon", -84.0, "TeslaMateVehicle")
    master.recordVehicleVIN(slave(VIN))
    assert master.getHomeLatLon() == [39.0, -84.0] and api.mock_calls == []
    master.queue_background_task.assert_any_call({"cmd": "saveSettings"})


def test_delayed_task_list_is_only_touched_under_the_tasks_lock(queued):  # M2
    # The serial thread inserts (VIN confirmation reads) while the
    # background thread pops.
    lock = queued.backgroundTasksLock = threading.Lock()

    class Guarded(list):
        def insert(self, *a):
            assert lock.locked()
            return super().insert(*a)

        def pop(self, *a):
            assert lock.locked()
            return super().pop(*a)

    queued.backgroundTasksDelayed = Guarded()
    queued.queue_background_task({"cmd": "later"}, delay=5)
    queued.backgroundTasksDelayed[0] = (
        datetime.now() - timedelta(seconds=1),
        0,
        {"cmd": "later"},
    )
    queued.backgroundTasksQueue.get = Mock(return_value={"cmd": "later"})
    assert queued.getBackgroundTask() == {"cmd": "later"}
    queued.backgroundTasksQueue.put.assert_called_once_with({"cmd": "later"})
    assert not lock.locked() and len(queued.backgroundTasksDelayed) == 0


def test_deleting_a_twc_ends_its_plug_session(master):  # M6
    from types import SimpleNamespace

    twcid = b"\x82\x44"
    slave = SimpleNamespace(TWCID=twcid)
    master.slaveTWCs[twcid] = slave
    master.slaveTWCRoundRobin = [slave]
    master.vehicleRegistry.beginPlugSession("8244")
    master.deleteSlaveTWC(twcid)
    assert master.vehicleRegistry.get("anon:8244") is None
    assert master.slaveTWCRoundRobin == []
