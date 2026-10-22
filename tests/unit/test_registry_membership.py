import pytest
from unittest.mock import Mock
from TWCManager.Vehicle.Registry.Fields import NOT_TRACKED
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"
VIN2 = "5YJ3E1EA1KF000002"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_get_or_create_is_idempotent_and_uppercases():
    r = VehicleRegistry({})
    a = r.getOrCreate(VIN.lower(), "TeslaAPI")
    b = r.getOrCreate(VIN, "TeslaBLE")
    assert a is b and a.vin == VIN and [v.key for v in r.vehicles()] == [VIN]
    assert r.get(VIN.lower()) is a


def test_get_or_create_rejects_empty_and_non_string():
    r = VehicleRegistry({})
    for bad in ("", None, 5, "anon:x"):
        with pytest.raises(ValueError):
            r.getOrCreate(bad, "TeslaAPI")
    assert r.vehicles() == []


def test_for_master_prefers_real_registry_else_private():
    real = VehicleRegistry({})
    m = Mock()
    m.vehicleRegistry = real
    assert VehicleRegistry.forMaster(m) is real
    m2 = Mock()
    m2.config = {"config": {}}
    assert isinstance(VehicleRegistry.forMaster(m2), VehicleRegistry)


def test_user_asserted_add_validates_format():
    r = VehicleRegistry({})
    with pytest.raises(ValueError):
        r.addUserAsserted("5YJ3E1EA1KF00000I")
    v = r.addUserAsserted(VIN)
    assert v.userAsserted and v.tier == "observed"
    assert r.addUserAsserted(VIN) is v


def test_user_asserted_upgrades_existing():
    r = VehicleRegistry({})
    v = r.getOrCreate(VIN, "TWC")
    assert not v.userAsserted
    assert r.addUserAsserted(VIN.lower()) is v and v.userAsserted


def test_expire_forgets_unclaimed_after_default_30_days_but_not_claimed_or_asserted():
    c = Clock()
    r = VehicleRegistry({}, clock=c)
    r.getOrCreate(VIN, "TWC")
    r.getOrCreate(VIN2, "TeslaBLE").claim("TeslaBLE", True)
    r.addUserAsserted("5YJ3E1EA1KF000003")
    r.getOrCreate("5YJ3E1EA1KF000004", "TeslaAPI").claim("TeslaAPI", False, "listed")
    c.t = 1000.0 + 29 * 86400
    assert r.expire() == []
    c.t = 1000.0 + 31 * 86400
    assert r.expire() == [VIN]
    assert [v.key for v in r.vehicles()] == [
        VIN2,
        "5YJ3E1EA1KF000003",
        "5YJ3E1EA1KF000004",
    ]
    r.get("5YJ3E1EA1KF000004").dropClaim("TeslaAPI")
    c.t += 31 * 86400
    assert r.expire() == ["5YJ3E1EA1KF000004"]


def test_expire_never_removes_anonymous_vehicles():
    c = Clock()
    r = VehicleRegistry({}, clock=c)
    from TWCManager.Vehicle.Registry.Vehicle import Vehicle

    anon = Vehicle("anon:abc", None, r, clock=c)
    with r._lock:
        r._vehicles["anon:abc"] = anon
    c.t += 365 * 86400
    assert r.expire() == []
    assert r.get("anon:abc") is anon


def test_forget_days_configurable():
    c = Clock()
    r = VehicleRegistry({"vehicle": {"forgetAfterDays": 1}}, clock=c)
    r.getOrCreate(VIN, "TWC")
    c.t = 1000.0 + 2 * 86400
    assert r.expire() == [VIN]


def test_forget():
    r = VehicleRegistry({})
    r.addUserAsserted(VIN)
    assert r.forget(VIN.lower()) is True
    assert r.forget(VIN) is False and r.vehicles() == []


def test_persistence_round_trip_and_merge():
    c = Clock()
    r = VehicleRegistry({}, clock=c)
    v = r.getOrCreate(VIN, "TWC")
    v.observe("name", "Car", "TeslaAPI")
    c.t = 2000.0
    v.observe("chargeLimit", 80, "TeslaBLE")
    data = r.toJSON()
    assert data["version"] == 1
    assert data["vehicles"][VIN] == {
        "name": "Car",
        "firstSeen": 1000.0,
        "lastSeen": 2000.0,
        "lastSeenSource": "TeslaBLE",
        "userAsserted": False,
    }
    r2 = VehicleRegistry({}, clock=Clock(3000.0))
    r2.getOrCreate(VIN, "TeslaAPI")
    r2.load(data)
    w = r2.get(VIN)
    assert (w.firstSeen, w.lastSeen, w.name) == (1000.0, 3000.0, "Car")


def test_load_into_empty_registry_keeps_persisted_times_and_marks_persisted():
    c = Clock()
    r = VehicleRegistry({}, clock=c)
    v = r.addUserAsserted(VIN)
    v.observe("name", "Car", "TeslaAPI")
    data = r.toJSON()
    c.t = 5000.0
    r2 = VehicleRegistry({}, clock=c)
    r2.load(data)
    w = r2.get(VIN)
    assert (w.firstSeen, w.lastSeen, w.userAsserted) == (1000.0, 1000.0, True)
    assert w.latest("name").source == "persisted"
    assert w.lastSeenSource == "TeslaAPI"


def test_load_clamps_future_times_and_skips_bad_vins():
    c = Clock(1000.0)
    r = VehicleRegistry({}, clock=c)
    r.load(
        {
            "version": 1,
            "vehicles": {
                VIN: {"firstSeen": 9e9, "lastSeen": 9e9},
                "": {"name": "x"},
                "anon:abc": {},
                5: {},
                VIN2: {"name": 7, "lastSeen": "x", "userAsserted": "yes"},
            },
        }
    )
    assert [v.key for v in r.vehicles()] == [VIN, VIN2]
    assert r.get(VIN).lastSeen == 1000.0 and r.get(VIN).firstSeen == 1000.0
    assert r.get(VIN2).name == "" and not r.get(VIN2).userAsserted


@pytest.mark.parametrize(
    "bad", [None, "x", 3, {"vehicles": "nope"}, {"vehicles": {VIN: "str"}}]
)
def test_load_ignores_garbage(bad):
    r = VehicleRegistry({})
    r.load(bad)
    assert r.vehicles() == []


def test_liveness_feeds_vehicle_age_and_trust():
    c = Clock()
    r = VehicleRegistry({}, clock=c)
    live = {"at": 990.0}
    r.registerLiveness(
        "TeslaMateVehicle", lambda vehicle: live["at"] if vehicle.vin == VIN else None
    )
    v = r.getOrCreate(VIN, "TeslaMateVehicle")
    v.observe("chargeLimit", 50, "TeslaMateVehicle", at=100.0)
    assert v.age("chargeLimit") == 10.0
    live["at"] = None
    assert (
        v.age("chargeLimit") == 900.0 and r.sourceLiveAt("TeslaAPI", v) is NOT_TRACKED
    )


def test_raising_liveness_counts_as_not_live():
    r = VehicleRegistry({})

    def boom(vehicle):
        raise RuntimeError("secret")

    r.registerLiveness("S", boom)
    assert r.sourceLiveAt("S", r.getOrCreate(VIN, "S")) is None


def test_read_sources_sorted_by_order_then_registration():
    r = VehicleRegistry({})

    def src(name, order):
        s = Mock()
        s.name, s.order = name, order
        return s

    cloud, a, b = src("cloud", 1), src("a", 0), src("b", 0)
    for s in (cloud, a, b, a):
        r.registerReadSource(s)
    assert r.readSources() == [a, b, cloud]


def test_find_by_name_and_attached():
    r = VehicleRegistry({})
    v = r.getOrCreate(VIN, "TeslaAPI")
    v.observe("name", "TestCar", "TeslaAPI")
    v.attach("TeslaAPI", "obj")
    assert r.findByName("TestCar") is v and r.findByName("x") is None
    assert r.attached("TeslaAPI") == ["obj"] and r.attached("HomeAssistant") == []


def test_summaries_sorted_by_key():
    r = VehicleRegistry({})
    r.getOrCreate(VIN2, "x")
    r.getOrCreate(VIN, "x")
    assert [s["key"] for s in r.summaries()] == [VIN, VIN2]


def test_get_or_create_rejects_non_ascii_like_load():
    r = VehicleRegistry({})
    with pytest.raises(ValueError):
        r.getOrCreate("ÿ1", "TeslaAPI")
    assert r.vehicles() == []


def test_liveness_non_number_counts_as_not_live():
    r = VehicleRegistry({})
    v = r.getOrCreate(VIN, "S")
    for bad in ("soon", True, float("nan"), object()):
        r.registerLiveness("S", lambda vehicle, b=bad: b)
        assert r.sourceLiveAt("S", v) is None
    r.registerLiveness("S", lambda vehicle: 5)
    assert r.sourceLiveAt("S", v) == 5.0
    r.registerLiveness("S", lambda vehicle: "x")
    v.observe("chargeLimit", 50, "S", at=10.0)
    v.age("chargeLimit")
    v.summary()


@pytest.mark.parametrize("bad", [0, -3, "x", None, True])
def test_forget_days_invalid_falls_back_to_default(bad):
    r = VehicleRegistry({"vehicle": {"forgetAfterDays": bad}})
    assert r.forgetAfter == 30 * 86400


def test_to_json_omits_anonymous():
    from TWCManager.Vehicle.Registry.Vehicle import Vehicle

    r = VehicleRegistry({})
    r.getOrCreate(VIN, "TWC")
    with r._lock:
        r._vehicles["anon:abc"] = Vehicle("anon:abc", None, r)
    assert list(r.toJSON()["vehicles"]) == [VIN]


def test_load_logs_added_at_debug(caplog):
    import logging

    r = VehicleRegistry({})
    with caplog.at_level(logging.DEBUG):
        r.load({"vehicles": {VIN: {}}})
    recs = [x for x in caplog.records if "added (persisted)" in x.getMessage()]
    assert recs and all(x.levelno == logging.DEBUG for x in recs)


def test_claim_that_does_not_keep_alive_does_not_block_expiry():
    c = Clock()
    r = VehicleRegistry({}, clock=c)
    r.getOrCreate(VIN, "TeslaBLE").claim(
        "TeslaBLE", False, "unproven", keepsAlive=False
    )
    r.getOrCreate(VIN2, "TeslaBLE").claim("TeslaBLE", True, "paired")
    c.t += 31 * 86400
    assert r.expire() == [VIN]


def test_get_or_create_rejects_a_malformed_vin():  # I4
    r = VehicleRegistry({})
    for bad in ("STATUS", "5YJ3E1EA1KF00000I", VIN + "1", "0" * 17):
        with pytest.raises(ValueError):
            r.getOrCreate(bad, "FleetTelemetryMQTT")
    assert r.vehicles() == []


def test_load_drops_a_persisted_malformed_key_without_raising():  # I4
    r = VehicleRegistry({})
    r.load({"vehicles": {"STATUS": {"lastSeen": 1.0}, VIN: {"lastSeen": 1.0}}})
    assert [v.key for v in r.vehicles()] == [VIN]
