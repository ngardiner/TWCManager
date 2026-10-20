from TWCManager.Vehicle.Registry.VIN import isWellFormedVIN
from TWCManager.Vehicle.Registry.Vehicle import Vehicle
from TWCManager.Vehicle.Registry.Fields import NOT_TRACKED
from tests.unit.vehicle_helpers import commands

VIN = "5YJ3E1EA1KF000001"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_vin_format():
    assert isWellFormedVIN(VIN) and isWellFormedVIN(VIN.lower())
    assert not isWellFormedVIN("5YJ3E1EA1KF00000I")  # I
    assert not isWellFormedVIN("5YJ3E1EA1KF0000011")  # 18 chars
    assert not isWellFormedVIN("")
    assert not isWellFormedVIN("0" * 17)  # legacy placeholder (M9)


def test_identity_default_has_no_claims():
    v = Vehicle(VIN, VIN, registry=None)
    assert (v.key, v.vin, v.isAnonymous, v.claims) == (VIN, VIN, False, {})
    a = Vehicle("anon:1234", None, registry=None)
    assert a.isAnonymous and a.claims == {}


def test_commanding_claim_makes_managed_and_callable_is_live():
    flag = {"ro": True}
    v = Vehicle(VIN, VIN, registry=None)
    v.claim("TeslaAPI", lambda: not flag["ro"], "listed")
    assert not commands(v)
    flag["ro"] = False
    assert commands(v)
    assert v.dropClaim("TeslaAPI") is True and not commands(v)
    assert v.dropClaim("TeslaAPI") is False


def test_observe_bumps_last_seen_unless_told_not_to():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=None, clock=c)
    assert (v.firstSeen, v.lastSeen, v.lastSeenSource) == (1000.0, 1000.0, "created")
    c.t = 2000.0
    v.observe("chargeLimit", 80, "TeslaMateVehicle")
    assert (v.lastSeen, v.lastSeenSource) == (2000.0, "TeslaMateVehicle")
    c.t = 3000.0
    v.observe("name", "Car", "OwnerAPI", seen=False)
    assert v.lastSeen == 2000.0 and v.name == "Car"


def test_attachments_and_summary():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=None, clock=c)
    obj = object()
    v.attach("TeslaAPI", obj)
    assert v.attachment("TeslaAPI") is obj and v.attachment("HomeAssistant") is None
    v.claim("TeslaBLE", True, "paired")
    v.observe("batteryLevel", 42, "TeslaBLE")
    c.t = 1010.0
    s = v.summary()
    assert commands(v) and set(s) == {
        "vin",
        "name",
        "anonymous",
        "point",
        "lastSeenAge",
        "lastSeenSource",
    }
    assert s["vin"] == VIN and not s["anonymous"] and s["lastSeenSource"] == "TeslaBLE"
    assert s["lastSeenAge"] == 10.0 and s["point"] is None
    assert Vehicle("anon:8244", None, registry=None).summary()["point"] == "8244"
    assert v.value("batteryLevel") == 42 and v.value("lon", 0) == 0


def test_registry_liveness_feeds_age_and_trusted():
    class Reg:
        def sourceLiveAt(self, source, vehicle):
            return 1500.0

    c = Clock()
    v = Vehicle(VIN, VIN, registry=Reg(), clock=c)
    v.observe("chargeLimit", 80, "TeslaMateVehicle", at=1000.0)
    c.t = 1600.0
    assert v.age("chargeLimit") == 100.0
    v.noteWrite("chargeLimit", 90, "OwnerAPI")
    c.t = 1700.0
    assert v.trusted("chargeLimit").value == 80
    assert v.age("name") is None


def test_twc_vin_point():
    v = Vehicle(VIN, VIN, registry=None)
    v.observe("twcVin", "1234", "TWC")
    assert v.summary()["point"] == "1234"


def test_rejected_older_stamp_keeps_last_seen_pair_consistent():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=None, clock=c)
    c.t = 3000.0
    v.observe("chargeLimit", 80, "TeslaMateVehicle", at=2000.0)
    v.observe("chargeLimit", 70, "OwnerAPI", at=1500.0)
    assert (v.lastSeen, v.lastSeenSource) == (2000.0, "TeslaMateVehicle")


def test_vin_rejects_non_ascii():
    assert not isWellFormedVIN("5YJ3E1EA1KF00000ß")
    assert not isWellFormedVIN("5YJ3E1EA1KF00000ſ")


def test_future_stamp_clamped_to_clock():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=None, clock=c)
    v.observe("chargeLimit", 80, "TeslaMateVehicle", at=5000.0)
    assert v.lastSeen == 1000.0


def test_raising_claim_counts_as_false():
    def boom():
        raise RuntimeError("x")

    v = Vehicle(VIN, VIN, registry=None)
    v.claim("TeslaAPI", boom)
    assert not commands(v)


def test_summary_evaluates_no_claim():
    calls = []
    v = Vehicle(VIN, VIN, registry=None)
    v.claim("TeslaAPI", lambda: calls.append(1))
    v.summary()
    assert calls == []


class FeedReg:
    """Feed is a live report-on-change source; everything else is polled."""

    def __init__(self, clock):
        self.clock = clock
        self.live = True

    def sourceLiveAt(self, source, vehicle):
        if source != "Feed":
            return NOT_TRACKED
        return self.clock() if self.live else None


def test_live_feed_vouches_for_an_equal_value_another_source_observed():  # P8/3
    # TeslaMate publishes on change: once the API or BLE re-observes the value
    # the feed last reported, only the feed's own record can vouch for it.
    c = Clock()
    reg = FeedReg(c)
    v = Vehicle(VIN, VIN, registry=reg, clock=c)
    v.observe("chargeLimit", 80, "Feed", at=100.0)
    v.observe("chargeLimit", 80, "OwnerAPI", at=900.0)
    c.t = 5000.0
    assert v.latest("chargeLimit").source == "OwnerAPI"
    assert v.age("chargeLimit") == 0.0
    reg.live = False  # feed down: the API reading ages normally
    assert v.age("chargeLimit") == 4100.0


def test_live_feed_vouches_for_a_different_value_another_source_observed():  # C1
    # A healthy feed vouches for a field it has reported, whatever value a
    # polled source saw since: it would have reported a change.
    c = Clock()
    reg = FeedReg(c)
    v = Vehicle(VIN, VIN, registry=reg, clock=c)
    v.observe("chargeLimit", 80, "Feed", at=100.0)
    v.observe("chargeLimit", 90, "TeslaBLE", at=900.0)
    c.t = 5000.0
    assert v.age("chargeLimit") == 0.0 and v.value("chargeLimit") == 90
    reg.live = False
    assert v.age("chargeLimit") == 4100.0


def test_feed_vouches_only_for_fields_it_reported():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=FeedReg(c), clock=c)
    v.observe("chargeLimit", 80, "Feed", at=100.0)
    v.observe("lat", 51.5, "TeslaBLE", at=900.0)
    c.t = 5000.0
    assert v.age("lat") == 4100.0


def test_feed_outage_costs_one_cloud_read_not_one_per_window():  # C1 probe
    from TWCManager.Vehicle.Providers import Capability as C
    from TWCManager.Vehicle.Providers.Provider import Provider
    from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
    from tests.unit.teslatokens import wire_reads

    for cloudLat in (51.50000001, 51.5):  # differing digits, then identical
        c = Clock()
        live = {"up": False}
        reg = VehicleRegistry({}, clock=c)
        reg.registerLiveness("TeslaMateVehicle", lambda v: c.t if live["up"] else None)
        calls = []

        class Cloud(Provider):
            name = "OwnerAPI"
            FIELDS = frozenset({"lat"})
            DECLARATIONS = {C.READ_CHARGE: C.CLOUD_FREE}

            def ready(self, vehicle, capability):
                return True

            def fetch(self, vehicle, field, allowWake=False):
                calls.append(c.t)
                vehicle.observe("lat", cloudLat, "OwnerAPI")
                return True

        wire_reads(reg, Cloud())
        v = reg.getOrCreate(VIN, "test")
        v.observe("lat", 51.5, "TeslaMateVehicle", at=100.0)
        assert v.read("lat", maxAge=0).source == "OwnerAPI"  # feed down
        assert len(calls) == 1
        live["up"] = True  # feed back, car parked: no new latitude
        for step in range(1, 7):
            c.t = 1000.0 + 301 * step
            v.read("lat", maxAge=300)
        assert len(calls) == 1, cloudLat


def test_feed_vouch_resolves_own_write_after_feed_lag():
    from TWCManager.Vehicle.Registry.Fields import FEED_LAG

    c = Clock()
    v = Vehicle(VIN, VIN, registry=FeedReg(c), clock=c)
    v.observe("chargeLimit", 50, "Feed", at=100.0)
    v.observe("chargeLimit", 50, "OwnerAPI", at=900.0)
    v.noteWrite("chargeLimit", 100, "TeslaBLE")  # at 1000
    c.t = 1003.0
    assert v.trusted("chargeLimit") is None
    c.t = 1000.0 + FEED_LAG + 1
    assert v.trusted("chargeLimit").value == 50


class _Untracked:
    def sourceLiveAt(self, source, vehicle):
        return NOT_TRACKED


def test_sync_source_is_a_fresh_telemetry_state_else_teslaapi():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=_Untracked(), clock=c)
    assert v.syncSource == "TeslaAPI"  # no state
    v.observe("state", "online", "TeslaMateVehicle")
    assert v.syncSource == "TeslaMateVehicle"
    c.t += Vehicle.SYNC_TIMEOUT
    assert v.syncSource == "TeslaMateVehicle"  # <= SYNC_TIMEOUT is fresh
    c.t += 1  # 3,601 s on an untracked feed
    assert v.syncSource == "TeslaAPI"
    v.observe("state", "online", "OwnerAPI")
    assert v.syncSource == "TeslaAPI"
    assert Vehicle.SYNC_TIMEOUT == 3600


def test_telemetry_state_ages_only_telemetry():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=_Untracked(), clock=c)
    assert v.telemetryState() == (None, None)
    api = v.observe("state", "online", "OwnerAPI", at=900.0)
    assert v.telemetryState() == (api, None)
    tm = v.observe("state", "asleep", "FleetTelemetryMQTT", at=950.0)
    assert v.telemetryState() == (tm, 50.0)
