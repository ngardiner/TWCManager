from TWCManager.Vehicle.Registry.VIN import isWellFormedVIN
from TWCManager.Vehicle.Registry.Vehicle import Vehicle

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


def test_identity_and_tier_default_observed():
    v = Vehicle(VIN, VIN, registry=None)
    assert (v.key, v.vin, v.isAnonymous, v.tier) == (VIN, VIN, False, "observed")
    a = Vehicle("anon:1234", None, registry=None)
    assert a.isAnonymous and a.tier == "observed"


def test_commanding_claim_makes_managed_and_callable_is_live():
    flag = {"ro": True}
    v = Vehicle(VIN, VIN, registry=None)
    v.claim("TeslaAPI", lambda: not flag["ro"], "listed")
    assert v.tier == "observed"
    flag["ro"] = False
    assert v.tier == "managed"
    assert v.dropClaim("TeslaAPI") is True and v.tier == "observed"
    assert v.dropClaim("TeslaAPI") is False


def test_anonymous_never_managed():
    a = Vehicle("anon:1234", None, registry=None)
    a.claim("TeslaBLE", True)
    assert a.tier == "observed"


def test_observe_bumps_last_seen_unless_told_not_to():
    c = Clock()
    v = Vehicle(VIN, VIN, registry=None, clock=c)
    assert (v.firstSeen, v.lastSeen, v.lastSeenSource) == (1000.0, 1000.0, "created")
    c.t = 2000.0
    v.observe("chargeLimit", 80, "TeslaMateVehicle")
    assert (v.lastSeen, v.lastSeenSource) == (2000.0, "TeslaMateVehicle")
    c.t = 3000.0
    v.observe("name", "Car", "TeslaAPI", seen=False)
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
    assert s["tier"] == "managed" and s["claims"] == [
        {"provider": "TeslaBLE", "status": "paired", "commands": True}
    ]
    assert s["fields"]["batteryLevel"] == {
        "value": 42,
        "source": "TeslaBLE",
        "age": 10.0,
        "changedAge": 10.0,
    }
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
    v.noteWrite("chargeLimit", 90, "TeslaAPI")
    c.t = 1700.0
    assert v.fresherThanWrite("chargeLimit") is False
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
    v.observe("chargeLimit", 70, "TeslaAPI", at=1500.0)
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
    assert v.tier == "observed"
    assert v.summary()["claims"][0]["commands"] is False


def test_summary_tier_from_single_claim_evaluation():
    calls = []

    def once():
        calls.append(1)
        return len(calls) == 1

    v = Vehicle(VIN, VIN, registry=None)
    v.claim("TeslaAPI", once)
    s = v.summary()
    assert len(calls) == 1
    assert s["tier"] == "managed" and s["claims"][0]["commands"] is True


def test_summary_field_age_is_liveness_aware():
    class Reg:
        def sourceLiveAt(self, source, vehicle):
            return 1500.0

    c = Clock()
    v = Vehicle(VIN, VIN, registry=Reg(), clock=c)
    v.observe("chargeLimit", 80, "TeslaMateVehicle", at=1000.0)
    c.t = 1600.0
    f = v.summary()["fields"]["chargeLimit"]
    assert f["age"] == 100.0 and f["changedAge"] == 600.0
