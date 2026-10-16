import logging
from unittest.mock import Mock

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Providers.Provider import Provider
from TWCManager.Vehicle.Registry.Vehicle import Vehicle
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Reader(Provider):
    # A provider that reads: observes result under its own name and vouches
    # for field when result holds it
    def __init__(self, name, cost, result, fields=("chargeLimit", "batteryLevel"), rank=50):
        self.name, self.rank, self.FIELDS = name, rank, frozenset(fields)
        self.local = cost.tier == C.TIER_LOCAL
        self.DECLARATIONS = {C.READ_CHARGE: C.Declaration(cost)}
        self.result, self.calls, self.lockHeld, self.isReady = result, [], False, True

    def ready(self, vehicle, capability):
        return capability == C.READ_CHARGE and self.isReady

    def fetch(self, vehicle, field, allowWake=False):
        self.calls.append((field, allowWake))
        if vehicle._fields._lock.locked() or vehicle._lock.locked():
            self.lockHeld = True
        if self.result is None:
            return False
        for k, v in self.result.items():
            vehicle.observe(k, v, self.name)
        return field in self.result


def setup(*providers, clock=None, settings=None):
    m = Mock()
    m.settings, m.config = settings or {}, {}
    reg = m.vehicleRegistry = VehicleRegistry({}, clock=clock or Clock())
    reg.providerOrder = o = ProviderOrder(m)
    for p in providers:
        o.register(p)
    return reg, reg.getOrCreate(VIN, "test")


def test_fresh_value_returned_without_fetch():
    c = Clock()
    src = Reader("TeslaBLE", C.LOCAL, {"chargeLimit": 90})
    reg, v = setup(src, clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1010.0
    assert v.read("chargeLimit", maxAge=30).value == 80 and src.calls == []


def test_free_first_and_stops_at_first_success():
    c = Clock()
    # Registered paid first, ranked paid first: the cost tier still wins
    paid = Reader("FleetAPI", C.FLEET_DATA, {"chargeLimit": 70}, rank=1)
    cloud = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 90, "batteryLevel": 55}, rank=2)
    local = Reader("TeslaBLE", C.LOCAL, None, rank=3)
    reg, v = setup(paid, cloud, local, clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1100.0
    obs = v.read("chargeLimit", maxAge=30)
    assert (obs.value, obs.source, obs.at) == (90, "OwnerAPI", 1100.0)
    assert v.latest("batteryLevel").value == 55
    assert local.calls == [("chargeLimit", False)] and cloud.calls == [("chargeLimit", False)]
    assert paid.calls == []  # no paid read once a free one answered
    assert not local.lockHeld and not cloud.lockHeld


def test_never_wakes_unless_asked():
    src = Reader("TeslaBLE", C.LOCAL, {"chargeLimit": 1})
    reg, v = setup(src)
    v.read("chargeLimit", maxAge=-1)
    v.read("chargeLimit", maxAge=-1, allowWake=True)
    assert [w for _, w in src.calls] == [False, True]


def test_max_age_none_never_fetches():
    src = Reader("TeslaBLE", C.LOCAL, {"chargeLimit": 1})
    reg, v = setup(src)
    assert v.read("chargeLimit", maxAge=None) is None and src.calls == []


def test_provider_without_field_or_not_ready_is_skipped():
    loc = Reader("A", C.LOCAL, {"lat": 1.0}, fields=("lat", "lon"))
    shy = Reader("B", C.LOCAL, {"chargeLimit": 5})
    shy.isReady = False
    reg, v = setup(loc, shy)
    assert v.read("chargeLimit", maxAge=0) is None
    assert loc.calls == [] and shy.calls == []


def test_readiness_is_asked_just_before_each_fetch():
    # An earlier fetch may make a later provider ready (a refused Owner read
    # hands reads to Fleet in the same walk)
    paid = Reader("FleetAPI", C.FLEET_DATA, {"chargeLimit": 70})
    paid.isReady = False
    free = Reader("OwnerAPI", C.CLOUD_FREE, None)

    def refused(vehicle, field, allowWake=False):
        free.calls.append(field)
        paid.isReady = True
        return False

    free.fetch = refused
    reg, v = setup(paid, free)
    assert v.read("chargeLimit", maxAge=0).value == 70 and len(paid.calls) == 1


def test_all_providers_fail_returns_stale_latest():
    c = Clock()
    reg, v = setup(Reader("TeslaBLE", C.LOCAL, None), clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 5000.0
    assert v.read("chargeLimit", maxAge=1).value == 80


def test_raising_provider_counts_as_failure_and_next_is_tried():
    ble = Reader("TeslaBLE", C.LOCAL, None)

    def boom(vehicle, field, allowWake=False):
        raise RuntimeError("radio")

    ble.fetch = boom
    reg, v = setup(ble, Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 7}))
    assert v.read("chargeLimit", maxAge=0).value == 7


def test_true_with_nothing_recorded_continues():
    # A provider that says it read but left no value (a deferred API car
    # with nothing stored) does not end the walk
    empty = Reader("OwnerAPI", C.CLOUD_FREE, {})
    empty.fetch = lambda vehicle, field, allowWake=False: True
    reg, v = setup(empty, Reader("HomeAssistant", C.HA_BILLED, {"chargeLimit": 3}))
    assert v.read("chargeLimit", maxAge=0).value == 3


def test_true_after_recording_itself_ends_the_walk():
    def self_(vehicle, field, allowWake=False):
        vehicle.observe(field, 3, "TeslaAPI")
        return True

    first = Reader("OwnerAPI", C.CLOUD_FREE, None)
    first.fetch = self_
    later = Reader("HomeAssistant", C.HA_BILLED, {"chargeLimit": 9})
    reg, v = setup(first, later)
    obs = v.read("chargeLimit", maxAge=0)
    assert (obs.value, obs.source) == (3, "TeslaAPI") and later.calls == []


def test_no_registry_or_no_order_returns_latest():
    v = Vehicle(VIN, VIN, None)
    assert v.read("chargeLimit", maxAge=0) is None
    v.observe("chargeLimit", 1, "X")
    assert v.read("chargeLimit", maxAge=-1).value == 1
    reg = VehicleRegistry({})
    v = reg.getOrCreate(VIN, "test")
    v.observe("chargeLimit", 2, "X")
    assert reg.providerOrder is None and v.read("chargeLimit", maxAge=-1).value == 2


def test_raising_fetch_logs_no_message_or_traceback(caplog):
    ble = Reader("TeslaBLE", C.LOCAL, None)

    def boom(vehicle, field, allowWake=False):
        raise RuntimeError("token=SECRET")

    ble.fetch = boom
    reg, v = setup(ble)
    with caplog.at_level(logging.DEBUG):
        v.read("chargeLimit", maxAge=0)
    assert "SECRET" not in caplog.text and "Traceback" not in caplog.text
    assert "RuntimeError" in caplog.text and "TeslaBLE" in caplog.text


def test_value_not_newer_than_our_write_is_not_fresh():
    c = Clock()
    src = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 90})
    reg, v = setup(src, clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1005.0
    v.noteWrite("chargeLimit", 90, "TeslaBLE")
    c.t = 1010.0
    obs = v.read("chargeLimit", maxAge=30)
    assert src.calls == [("chargeLimit", False)] and obs.value == 90


def test_value_not_newer_than_our_write_falls_back_to_latest():
    c = Clock()
    src = Reader("OwnerAPI", C.CLOUD_FREE, None)
    reg, v = setup(src, clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1005.0
    v.noteWrite("chargeLimit", 90, "TeslaBLE")
    c.t = 1010.0
    assert v.read("chargeLimit", maxAge=30).value == 80
    assert len(src.calls) == 1


def test_result_lacking_requested_field_continues_to_next_provider():
    a = Reader("TeslaBLE", C.LOCAL, {"batteryLevel": 55})
    b = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 9})
    reg, v = setup(a, b)
    assert v.read("chargeLimit", maxAge=0).value == 9
    assert v.latest("batteryLevel").value == 55 and len(b.calls) == 1


def test_read_with_providers_skips_other_providers():
    first = Reader("TeslaBLE", C.LOCAL, {"chargeLimit": 1})
    second = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 2})
    reg, v = setup(first, second)
    assert v.read("chargeLimit", 0, providers=[second]).value == 2
    assert first.calls == [] and second.calls == [("chargeLimit", False)]
    assert v.read("chargeLimit", -1, providers=["TeslaBLE"]).value == 1  # by name too


def test_read_with_providers_keeps_cost_order():
    first = Reader("TeslaBLE", C.LOCAL, None)
    second = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 2})
    reg, v = setup(first, second)
    v.read("chargeLimit", 0, providers=[second, first])
    assert first.calls == [("chargeLimit", False)] and len(second.calls) == 1


def test_api_only_skips_ble_reads():
    ble = Reader("TeslaBLE", C.LOCAL, {"chargeLimit": 1})
    ble.SUPPRESSED = {"api_only": C.ALL}
    api = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 2})
    reg, v = setup(ble, api, settings={"commandPolicy": "api_only"})
    assert v.read("chargeLimit", 0).value == 2 and ble.calls == []


def test_ble_only_keeps_cloud_reads():
    ble = Reader("TeslaBLE", C.LOCAL, None)
    ble.SUPPRESSED = {"api_only": C.ALL}
    api = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 2})
    reg, v = setup(ble, api, settings={"commandPolicy": "ble_only"})
    assert v.read("chargeLimit", 0).value == 2 and len(ble.calls) == 1


def test_force_reads_past_a_live_feed():
    c = Clock()
    src = Reader("OwnerAPI", C.CLOUD_FREE, {"chargeLimit": 75})
    reg, v = setup(src, clock=c)
    reg.registerLiveness("TeslaMateVehicle", lambda vehicle: c())
    v.observe("chargeLimit", 80, "TeslaMateVehicle")
    c.t = 5000.0
    assert v.read("chargeLimit", 300).value == 80 and src.calls == []
    assert v.read("chargeLimit", 300, force=True).value == 75
    assert src.calls == [("chargeLimit", False)]


def test_force_still_never_fetches_without_max_age():
    src = Reader("TeslaBLE", C.LOCAL, {"chargeLimit": 1})
    reg, v = setup(src)
    assert v.read("chargeLimit", None, force=True) is None and src.calls == []


def test_a_provider_registered_twice_reads_once():
    # Re-registering a name replaces the provider, keeping its place
    old = Reader("OwnerAPI", C.CLOUD_FREE, None)
    new = Reader("OwnerAPI", C.CLOUD_FREE, None)
    reg, v = setup(old, Reader("HomeAssistant", C.HA_BILLED, None), new, new)
    v.read("chargeLimit", maxAge=0)
    assert old.calls == [] and len(new.calls) == 1
