"""
A car's appearance (model, paint, wheels, trim) is read from the Tesla API's
vehicle_config and TeslaMate's topics, kept across restarts, and handed to the
Vehicles pages, which draw the car with the Tesla compositor.
"""

from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaMateVehicle import TeslaMateVehicle
from TWCManager.Vehicle.Watch import Watch

from tests.unit.reconcile_helpers import attachFleet, liveFeed, runPending
from tests.unit.test_command_path_parity import FleetCar, paid
from tests.unit.test_limit_reconciler import setup

from tests.unit.teslatokens import (
    OWNER_API_URL,
    add_home_vehicle,
    new_api,
    no_threads,
    owner_token,
)

VIN = "5YJ3E1EA1KF000001"

VEHICLE_DATA = {
    "response": {
        "option_codes": "AD15,MDL3,PBSB,W38B",
        "drive_state": {"latitude": 37.0, "longitude": -122.0},
        "charge_state": {"battery_level": 55},
        "vehicle_state": {"vehicle_name": "Car"},
        "vehicle_config": {
            "car_type": "model3",
            "exterior_color": "DeepBlue",
            "wheel_type": "Pinwheel18CapKit",
            "trim_badging": "74d",
        },
    }
}


def test_vehicle_data_reads_appearance(no_threads, requests_mock):
    api = new_api()
    api.storeToken(owner_token())
    vehicle = add_home_vehicle(api)
    data = requests_mock.get(
        OWNER_API_URL + "/" + VIN + "/vehicle_data", json=VEHICLE_DATA
    )

    assert api.update_vehicle_data(vehicle) is True

    assert "vehicle_config" in data.last_request.qs["endpoints"][0]
    assert vehicle.appearance() == {
        "car_type": "model3",
        "exterior_color": "DeepBlue",
        "wheel_type": "Pinwheel18CapKit",
        "trim_badging": "74d",
        "option_codes": "AD15,MDL3,PBSB,W38B",
    }


def test_teslamate_topics_map_to_appearance():
    events = TeslaMateVehicle.events
    assert events["model"][0] == "carType"
    assert events["model"][1]("Y") == "modely"
    assert events["model"][1]("Cybertruck") == "cybertruck"
    assert events["exterior_color"][0] == "exteriorColor"
    assert events["wheel_type"][0] == "wheelType"
    assert events["trim_badging"][0] == "trimBadging"
    # An empty payload is no reading
    assert events["trim_badging"][1]("") is None


def test_appearance_omits_unknown_keys():
    r = VehicleRegistry({})
    v = r.getOrCreate(VIN, "TWC")
    assert v.appearance() == {}
    v.observe("wheelType", "Pinwheel18", "TeslaMateVehicle")
    v.observe("trimBadging", None, "TeslaMateVehicle")
    assert v.appearance() == {"wheel_type": "Pinwheel18"}
    assert v.summary()["appearance"] == {"wheel_type": "Pinwheel18"}


def test_appearance_persists_without_counting_as_a_sighting():
    r = VehicleRegistry({})
    v = r.getOrCreate(VIN, "TWC")
    v.observe("carType", "model3", "OwnerAPI")
    v.observe("exteriorColor", "DeepBlue", "OwnerAPI")
    data = r.toJSON()
    assert data["vehicles"][VIN]["appearance"] == {
        "car_type": "model3",
        "exterior_color": "DeepBlue",
    }

    r2 = VehicleRegistry({})
    r2.load(data)
    w = r2.get(VIN)
    assert w.appearance() == {"car_type": "model3", "exterior_color": "DeepBlue"}
    assert w.latest("carType").seen is False
    assert w.lastSeen == data["vehicles"][VIN]["lastSeen"]


def test_load_ignores_a_garbled_appearance():
    r = VehicleRegistry({})
    r.load({"vehicles": {VIN: {"appearance": "blue"}}})
    assert r.get(VIN).appearance() == {}
    r.load({"vehicles": {VIN: {"appearance": {"car_type": 3, "bogus": "x"}}}})
    assert r.get(VIN).appearance() == {}


def looks(requests_mock):
    return [q for q in requests_mock.request_history if "vehicle_config" in q.url]


def coveredCar(requests_mock, state="online"):
    # A Fleet-listed car a live Fleet Telemetry feed covers, so the watch
    # makes no reads of its own
    m, r, v, c = setup(here=False)
    car = FleetCar(requests_mock, state=state)
    attachFleet(m, v, car)
    v.observe("state", state, "FleetTelemetryMQTT")
    liveFeed(m, "FleetTelemetryMQTT")
    return m, r, v, c, car


def test_a_covered_car_with_unknown_looks_is_read_once(requests_mock):
    m, r, v, c, car = coveredCar(requests_mock)
    w = Watch(m, clock=c)
    w.pass_()
    runPending(r.cloud)
    assert len(looks(requests_mock)) == 1 and paid(requests_mock) == (1, 0, 0)
    assert "endpoints=vehicle_config" in looks(requests_mock)[0].url
    c.t += 7200
    w.pass_()
    runPending(r.cloud)
    assert paid(requests_mock) == (1, 0, 0)


def test_a_failed_look_is_tried_again_an_hour_later(requests_mock):
    m, r, v, c, car = coveredCar(requests_mock)
    car.state = "asleep"  # unavailable, though the feed said online
    w = Watch(m, clock=c)
    w.pass_()
    runPending(r.cloud)
    assert len(looks(requests_mock)) == 1
    car.state = "online"
    c.t += 1800
    w.pass_()
    runPending(r.cloud)
    assert len(looks(requests_mock)) == 1
    c.t += 1800
    w.pass_()
    runPending(r.cloud)
    assert len(looks(requests_mock)) == 2


def test_no_look_read_for_a_known_or_sleeping_car(requests_mock):
    m, r, v, c, car = coveredCar(requests_mock)
    v.observe("carType", "model3", "TeslaMateVehicle")
    Watch(m, clock=c).pass_()
    runPending(r.cloud)
    m2, r2, v2, c2, car2 = coveredCar(requests_mock, state="asleep")
    Watch(m2, clock=c2).pass_()
    runPending(r2.cloud)
    assert looks(requests_mock) == [] and paid(requests_mock) == (0, 0, 0)
