from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"
BAD = "5YJ3E1EA1KF00000I"
OTHER = "5YJ3E1EA1KF000002"


def test_anonymous_vehicle_lives_with_the_session():
    r = VehicleRegistry({})
    a = r.beginPlugSession("8244")
    assert a.key == "anon:8244" and a.isAnonymous and r.beginPlugSession("8244") is a
    assert r.vehicleAt("8244") is a
    r.endPlugSession("8244")
    assert r.vehicles() == [] and r.vehicleAt("8244") is None


def test_unknown_vin_needs_two_matching_reads_then_merges_anonymous():
    r = VehicleRegistry({})
    a = r.beginPlugSession("8244")
    a.observe("actualCurrent", 12, "TWC")
    assert r.observeTwcVin("8244", VIN) == "pending" and r.get(VIN) is None
    assert r.observeTwcVin("8244", VIN) == "confirmed"
    v = r.get(VIN)
    assert v is not None and r.get("anon:8244") is None
    assert v.latest("twcVin").value == "8244" and v.latest("actualCurrent").value == 12
    assert r.vehicleAt("8244") is v


def test_known_vin_confirms_at_once():
    r = VehicleRegistry({})
    r.getOrCreate(VIN, "TeslaAPI")
    r.beginPlugSession("8244")
    assert r.observeTwcVin("8244", VIN) == "confirmed"


def test_misheard_vin_creates_nothing_and_is_bounded():  # Review Focus 1
    r = VehicleRegistry({})
    r.beginPlugSession("8244")
    assert r.observeTwcVin("8244", VIN) == "pending"
    assert r.observeTwcVin("8244", OTHER) == "pending"  # differs: candidate replaced
    assert r.observeTwcVin("8244", VIN) == "rejected"  # third read, no two in a row
    assert r.get(VIN) is None and r.get(OTHER) is None
    assert r.observeTwcVin("8244", VIN) == "rejected"  # stays rejected this session


def test_malformed_vin_rejected_and_session_end_clears_pending():
    r = VehicleRegistry({})
    r.beginPlugSession("8244")
    assert r.observeTwcVin("8244", BAD) == "rejected"
    assert r.observeTwcVin("8244", VIN) == "pending"
    r.endPlugSession("8244")
    r.beginPlugSession("8244")
    assert r.observeTwcVin("8244", VIN) == "pending"  # fresh session, fresh count


def test_session_end_clears_twc_vin_on_the_car():
    r = VehicleRegistry({})
    r.getOrCreate(VIN, "TeslaAPI")
    r.beginPlugSession("8244")
    r.observeTwcVin("8244", VIN)
    r.endPlugSession("8244")
    assert r.get(VIN).latest("twcVin").value is None and r.get(VIN).value("twcVin") is None


def test_is_pending_tracks_the_unconfirmed_candidate():
    r = VehicleRegistry({})
    r.beginPlugSession("8244")
    assert not r.isPending("8244")
    r.observeTwcVin("8244", VIN)
    assert r.isPending("8244")
    r.observeTwcVin("8244", VIN)
    assert not r.isPending("8244")
    r.observeTwcVin("8245", VIN)  # known now: confirms at once
    assert not r.isPending("8245")
    r.observeTwcVin("8246", OTHER)
    r.observeTwcVin("8246", BAD)  # malformed: no change
    assert r.isPending("8246")
    r.endPlugSession("8246")
    assert not r.isPending("8246")
