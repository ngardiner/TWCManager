import pytest
from TWCManager.Vehicle.Registry.Fields import (
    FEED_LAG,
    FIELDS,
    NOT_TRACKED,
    TELEMETRY_SOURCES,
    Observation,
)
from TWCManager.Vehicle.Registry.Vehicle import Vehicle


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Registry:
    # liveAt per source, as the registry reports it; absent means polled
    def __init__(self, live=None):
        self.live = live or {}

    def sourceLiveAt(self, source, vehicle):
        return self.live.get(source, NOT_TRACKED)


def mk(clock=None, live=None):
    return Vehicle("VIN", "VIN", Registry(live), clock or Clock())


def test_observe_and_latest_carry_value_source_time():
    c = Clock()
    s = mk(c)
    s.observe("chargeLimit", 80, "TeslaBLE")
    assert s.latest("chargeLimit") == Observation(80, "TeslaBLE", 1000.0)
    assert s.latest("batteryLevel") is None


def test_age_uses_clock():
    c = Clock()
    s = mk(c)
    s.observe("batteryLevel", 50, "OwnerAPI")
    c.t = 1030.0
    assert s.age("batteryLevel") == 30.0
    assert s.age("lat") is None


def test_unknown_field_rejected():
    with pytest.raises(KeyError):
        mk().observe("notAField", 1, "x")
    with pytest.raises(KeyError):
        mk().noteWrite("notAField", 1, "x")


def test_constants():
    assert (
        "chargeLimit" in FIELDS
        and "chargeCurrentRequest" in FIELDS
        and len(FIELDS) == 30
    )
    assert TELEMETRY_SOURCES == {"TeslaMateVehicle", "FleetTelemetryMQTT"}
    assert FEED_LAG == 30.0


def test_own_write_stamp_and_freshness():
    c = Clock()
    s = mk(c)
    s.observe("chargeLimit", 80, "TeslaMateVehicle", at=900.0)
    assert s.trusted("chargeLimit") is not None  # no write yet
    s.noteWrite("chargeLimit", 70, "TeslaBLE")  # at 1000
    assert s.lastWrite("chargeLimit") == Observation(70, "TeslaBLE", 1000.0)
    assert s.trusted("chargeLimit") is None  # stored 900 < write 1000
    s.observe("chargeLimit", 70, "TeslaMateVehicle", at=1000.0)
    assert s.trusted("chargeLimit") is None  # same instant: not fresher (P1)
    s.observe("chargeLimit", 70, "TeslaMateVehicle", at=1000.5)
    assert s.trusted("chargeLimit") is not None


def test_snapshot_is_a_copy():
    s = mk()
    s.observe("lat", 1.0, "OwnerAPI")
    snap = s.snapshot()
    s.observe("lat", 2.0, "OwnerAPI")
    assert snap["lat"].value == 1.0 and set(snap) == {"lat"}


def test_report_on_change_age_counts_from_feed_liveness():
    c = Clock()
    s = mk(c)
    s.observe("chargeLimit", 50, "TeslaMateVehicle", at=100.0)  # old, still current
    s._registry.live["TeslaMateVehicle"] = 990.0
    assert s.age("chargeLimit") == 10.0  # healthy: age from liveness
    s._registry.live["TeslaMateVehicle"] = None
    assert s.age("chargeLimit") == 900.0  # unhealthy: from the change
    s.observe("batteryLevel", 50, "OwnerAPI", at=100.0)
    assert s.age("batteryLevel") == 900.0  # polled source: NOT_TRACKED


def test_trusted_after_own_write_waits_for_feed_lag():  # the 2026-10-12 09:17 shape
    c = Clock(t=1000.0)
    s = mk(c)
    s.observe("chargeLimit", 50, "TeslaMateVehicle", at=900.0)
    s.noteWrite("chargeLimit", 100, "TeslaBLE")  # written at t=1000
    c.t = 1003.0
    s._registry.live["TeslaMateVehicle"] = 1003.0
    assert s.trusted("chargeLimit") is None  # too soon: use our 100
    c.t = 1000.0 + FEED_LAG + 1
    s._registry.live["TeslaMateVehicle"] = c.t
    assert s.trusted("chargeLimit").value == 50  # write did not land
    s._registry.live["TeslaMateVehicle"] = None
    assert s.trusted("chargeLimit") is None  # unhealthy proves nothing
    s.observe("chargeLimit", 100, "TeslaMateVehicle")  # change event arrives
    s._registry.live["TeslaMateVehicle"] = c.t
    assert s.trusted("chargeLimit").value == 100


def test_trusted_edge_cases():
    c = Clock()
    s = mk(c)
    assert s.trusted("lat") is None  # nothing observed
    s.observe("lat", 1.0, "OwnerAPI")
    assert s.trusted("lat").value == 1.0  # no write: fresher
    s.noteWrite("lat", 2.0, "TeslaBLE")
    c.t += FEED_LAG + 1
    assert s.trusted("lat") is None  # polled source never gets the lag exemption
    s.observe("batteryLevel", 5, "TeslaMateVehicle", at=900.0)
    s.noteWrite("batteryLevel", 6, "TeslaBLE")
    c.t = 1000.0 + FEED_LAG  # exactly FEED_LAG: strictly greater required
    s._registry.live["TeslaMateVehicle"] = c.t
    assert s.trusted("batteryLevel") is None


def test_older_observation_does_not_overwrite_newer():
    s = mk()
    s.observe("lat", 2.0, "OwnerAPI", at=950.0)
    kept = s.observe("lat", 1.0, "TeslaMateVehicle", at=900.0)
    assert s.latest("lat") == Observation(2.0, "OwnerAPI", 950.0)
    assert kept == Observation(2.0, "OwnerAPI", 950.0)


def test_equal_timestamp_observation_replaces():
    s = mk()
    s.observe("lat", 1.0, "OwnerAPI", at=950.0)
    s.observe("lat", 2.0, "TeslaMateVehicle", at=950.0)
    assert s.latest("lat") == Observation(2.0, "TeslaMateVehicle", 950.0)


def test_older_write_does_not_overwrite_newer_write():
    c = Clock(950.0)
    s = mk(c)
    s.noteWrite("chargeLimit", 80, "TeslaBLE")
    c.t = 900.0
    s.noteWrite("chargeLimit", 70, "OwnerAPI")
    assert s.lastWrite("chargeLimit") == Observation(80, "TeslaBLE", 950.0)
    c.t = 950.0
    s.noteWrite("chargeLimit", 60, "OwnerAPI")
    assert s.lastWrite("chargeLimit") == Observation(60, "OwnerAPI", 950.0)


def test_clock_read_inside_lock():
    s = mk()
    held = []
    s._clock = lambda: (held.append(s._fieldLock.locked()), 1000.0)[1]
    s.observe("lat", 1.0, "x", seen=False)
    assert held == [True, False]
    del held[:]
    s.noteWrite("lat", 1.0, "x")
    assert held == [True]


def test_per_source_record_keeps_newest_stamp():
    s = mk()
    s.observe("chargeLimit", 80, "Feed", at=200.0)
    s.observe("chargeLimit", 70, "Feed", at=100.0)
    s.observe("chargeLimit", 60, "OwnerAPI", at=300.0)
    rep = s.reported("chargeLimit")
    assert rep["Feed"].value == 80 and rep["OwnerAPI"].value == 60
    assert s.reported("lat") == {}
