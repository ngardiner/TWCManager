# tests/unit/test_carapivehicle_registry.py
from unittest.mock import ANY, Mock

from tests.unit.teslatokens import FakeMaster, fleet_token, new_api, wire_reads
from TWCManager.Vehicle.Reconcile.Outcome import DONE, Outcome
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"


def api_with_registry():
    master = FakeMaster()
    master.vehicleRegistry = VehicleRegistry({})
    api = new_api(master=master)
    api.carApiVehicles = []
    wire_reads(master.vehicleRegistry, *api.providers())
    return api, master.vehicleRegistry


def test_vehicle_joins_registry_and_claims_teslaapi():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "TestCar"})
    v = reg.get(VIN)
    assert (
        v.attachment("TeslaAPI") is api.carApiVehicles[0] and v.name == "TestCar"
    )
    api.fleetSlot.clear()
    assert v.tier == "observed"  # no command slot: read-only
    api.fleetSlot.setTokens(fleet_token())
    assert v.tier == "managed"


def test_listing_is_a_sighting():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "TestCar"})
    v = reg.get(VIN)
    assert v.lastSeenSource == "TeslaAPI"
    assert v.latest("name").source == "TeslaAPI"


def test_attributes_are_observations_with_source_and_defaults():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    assert car.batteryLevel == 10000 and car.chargeLimit == -1 and car.lat == 10000
    assert car.pluggedIn is None and car.chargingState == "Unknown"
    car.chargeLimit = 80
    obs = reg.get(VIN).latest("chargeLimit")
    assert (obs.value, obs.source) == (80, "TeslaAPI") and car.chargeLimit == 80
    car.actualCurrent = 12
    assert reg.get(VIN).latest("actualCurrent").value == 12


def test_master_without_registry_gets_a_private_one():
    api = new_api()
    api.carApiVehicles = []
    api.addVehicle({"id": 1, "vin": "5YJ3E1EA1KF000001", "display_name": "x"})
    assert api.carApiVehicles[0].VIN == "5YJ3E1EA1KF000001" and isinstance(
        api.registry, VehicleRegistry
    )


def test_listing_without_usable_vin_is_skipped():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": None, "display_name": "x"})
    api.addVehicle({"id": 2, "vin": "", "display_name": "y"})
    assert api.carApiVehicles == []


def test_is_awake_trusts_fresh_telemetry_and_asleep_without_api_call():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    car.get_car_api = Mock(side_effect=AssertionError("no API call"))
    reg.get(VIN).observe("state", "asleep", "TeslaMateVehicle")
    assert car.is_awake() is False and reg.get(VIN).syncSource == "TeslaMateVehicle"
    reg.get(VIN).observe("state", "online", "TeslaMateVehicle")
    assert car.is_awake() is True


def test_sync_getters_have_legacy_defaults_before_any_state():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    assert reg.get(VIN).syncSource == "TeslaAPI"


def test_update_vehicle_data_observes_only_present_keys():  # P15
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    reg.get(VIN).observe("phases", 3, "TeslaBLE", at=5.0)
    name_before = reg.get(VIN).latest("name")
    car.get_car_api = Mock(
        return_value=(
            True,
            {
                "drive_state": {"latitude": 1.0, "longitude": 2.0},
                "charge_state": {"battery_level": 61},
                "vehicle_state": {},
            },
        )
    )
    car.carapi.is_location_home = Mock(return_value=False)
    car.carapi.is_far_from_home = Mock(return_value=False)
    assert car.update_vehicle_data() is True
    phases = reg.get(VIN).latest("phases")
    assert car.batteryLevel == 61
    assert (phases.value, phases.source, phases.at) == (3, "TeslaBLE", 5.0)
    assert reg.get(VIN).latest("chargeLimit") is None  # no default observed
    assert reg.get(VIN).latest("gpsAsOf") is None
    assert reg.get(VIN).latest("name") == name_before  # not re-observed


def test_update_vehicle_data_observes_the_cars_own_current_request():   # slim Task 12b
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    car.get_car_api = Mock(
        return_value=(
            True,
            {
                "drive_state": {"latitude": 1.0, "longitude": 2.0},
                "charge_state": {"charge_current_request": 16, "charger_pilot_current": 32},
                "vehicle_state": {},
            },
        )
    )
    car.carapi.is_location_home = Mock(return_value=False)
    car.carapi.is_far_from_home = Mock(return_value=False)
    assert car.update_vehicle_data() is True
    obs = reg.get(VIN).latest("chargeCurrentRequest")
    assert (obs.value, obs.source) == (16, "TeslaAPI")
    assert reg.get(VIN).value("availableCurrent") == 32   # the pilot stays the pilot


def test_update_vehicle_data_records_the_cars_own_ac_maximum():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    car.get_car_api = Mock(
        return_value=(
            True,
            {
                "drive_state": {"latitude": 1.0, "longitude": 2.0},
                "charge_state": {
                    "charger_pilot_current": 6,
                    "charge_current_request_max": 48,
                },
                "vehicle_state": {},
            },
        )
    )
    car.carapi.is_location_home = Mock(return_value=False)
    car.carapi.is_far_from_home = Mock(return_value=False)
    assert car.update_vehicle_data() is True
    assert reg.get(VIN).value("chargeCurrentRequestMax") == 48


def test_debug_interface_resolves_by_vin():
    from TWCManager.Vehicle.Providers.Order import ProviderOrder
    from TWCManager.Vehicle.Reconcile.Desired import DesiredStore
    from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
    from tests.unit.reconcile_helpers import inline

    api, reg = api_with_registry()
    api.storeToken(fleet_token())
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    api.wakeOutcome = Mock(return_value=Outcome(DONE))
    master = api.master
    master.providerOrder = ProviderOrder(master)
    for p in api.providers():
        master.providerOrder.register(p)
    master.desired = DesiredStore()
    master.reconciler = inline(VehicleReconciler(master))
    # True: a one-shot attempt was submitted (inline here, so it has run)
    assert master.providerOrder.debugCommand("wakeVehicle", VIN, "{}") is True
    api.wakeOutcome.assert_called_once_with(api.carApiVehicles[0], now=ANY)
    assert (
        master.providerOrder.debugCommand("wakeVehicle", "5YJ3E1EA1KF000009", "{}")
        is False
    )


def test_stale_telemetry_falls_back_to_api_check():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    reg.get(VIN).observe("state", "online", "TeslaMateVehicle", at=0.0)
    car.get_car_api = Mock(return_value=(True, {"state": "asleep"}))
    assert car.is_awake() is False and car.get_car_api.called
    assert reg.get(VIN).syncSource == "TeslaAPI"


def test_writes_after_forget_land_on_the_live_vehicle():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    assert reg.forget(VIN)
    assert car.chargeLimit == -1  # a read does not bring it back
    assert reg.get(VIN) is None
    car.chargeLimit = 80  # the account still lists it: next sighting rejoins
    v = reg.get(VIN)
    assert v is not None and v.latest("chargeLimit").value == 80
    assert v.attachment("TeslaAPI") is car and "FleetAPI" in v.claims


def test_writes_after_forget_and_relist_by_another_provider():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.carApiVehicles[0]
    reg.forget(VIN)
    fresh = reg.getOrCreate(VIN, "TWC")
    car.chargeLimit = 80
    assert reg.get(VIN) is fresh and fresh.latest("chargeLimit").value == 80
    assert fresh.attachment("TeslaAPI") is car and "FleetAPI" in fresh.claims


def test_relisting_the_api_attaches_the_new_car():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    old = api.carApiVehicles[0]
    reg.forget(VIN)
    api.carApiVehicles = []
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    new = api.carApiVehicles[0]
    new.batteryLevel = 70
    v = reg.get(VIN)
    assert v.attachment("TeslaAPI") is new and v.latest("batteryLevel").value == 70
    assert old.batteryLevel == 70  # the old object reads the live vehicle too


def test_telemetry_feed_drives_a_real_car():
    from TWCManager.Vehicle.Telemetry import TelmetryBase

    class TeslaMateVehicle(TelmetryBase):  # named as the real module
        configName = "Probe"
        events = {
            "state": ["state", lambda a: a],
            "latitude": ["lat", lambda a: float(a)],
            "plugged_in": ["pluggedIn", lambda a: a == "true"],
        }

        def scheduleUnmappedRetry(self, delay=None):
            pass

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "TestCar"})
    car = api.carApiVehicles[0]
    master = api.master
    master.config["vehicle"]["Probe"] = {"enabled": True}
    feed = TeslaMateVehicle(master)
    feed.applyDataToVehicle("1", "display_name", "TestCar")
    assert feed.vehicles["1"] is reg.get(VIN)
    feed.applyDataToVehicle("1", "state", "online")
    feed.applyDataToVehicle("1", "latitude", "1.5")
    feed.applyDataToVehicle("1", "plugged_in", "true")
    obs = reg.get(VIN).latest("state")
    assert (obs.value, obs.source) == ("online", "TeslaMateVehicle")
    assert reg.get(VIN).latest("lat").source == "TeslaMateVehicle"
    assert reg.get(VIN).syncSource == "TeslaMateVehicle" and reg.get(VIN).value("pluggedIn") is True
    assert car.lat == 1.5


# Characterization: the read gates TeslaAPI keeps for a Fleet-only car (no
# Owner token, so every vehicle_data is paid). Requests counted per URL.

FLEET_CAR = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/" + VIN
VEHICLE_DATA = {
    "response": {
        "drive_state": {"latitude": 37.0, "longitude": -122.0, "shift_state": "P"},
        "charge_state": {"battery_level": 61},
        "vehicle_state": {},
    }
}


def fleet_only_car(requests_mock, states=("online",)):
    import re

    api, reg = api_with_registry()
    api.storeToken(fleet_token())
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    online = requests_mock.get(
        FLEET_CAR, [{"json": {"response": {"state": s}}} for s in states]
    )
    data = requests_mock.get(
        re.compile(re.escape(FLEET_CAR) + r"/vehicle_data"), json=VEHICLE_DATA
    )
    return api, reg, reg.get(VIN).attachment("TeslaAPI"), online, data


def test_car_backoff_blocks_vehicle_data_but_not_the_free_online_check(
    requests_mock, caplog
):
    import time

    api, reg, car, online, data = fleet_only_car(requests_mock)
    car.lastErrorTime, car.errorCount = time.time(), 2
    caplog.set_level(1)
    reg.get(VIN).read("batteryLevel", maxAge=0)
    assert (online.call_count, data.call_count) == (1, 0)
    assert "not ready because of recent lastErrorTime" in caplog.text


def test_vehicle_data_rechecks_online_when_the_cache_expired(requests_mock):  # F6
    api, reg, car, online, data = fleet_only_car(requests_mock, ("online", "asleep"))
    calls = []

    def clock():
        # fetch's online check at t; _ready's second check at t + 121 s
        calls.append(1)
        return 5000.0 if len(calls) == 1 else 5121.0

    api.now = clock
    car.firstWakeAttemptTime = 100  # no 2-minute shortcut
    reg.get(VIN).read("batteryLevel", maxAge=0)
    assert (online.call_count, data.call_count) == (2, 0)


def test_a_vehicle_data_read_after_a_failed_wake_clears_the_first_wake_attempt(
    requests_mock,
):
    api, reg, car, online, data = fleet_only_car(requests_mock)
    car.firstWakeAttemptTime = 100
    assert reg.get(VIN).read("batteryLevel", maxAge=0).value == 61
    assert (online.call_count, data.call_count) == (1, 1)
    assert car.firstWakeAttemptTime == 0 and car.lastAPIAccessTime > 0


def test_two_concurrent_reads_pay_one_vehicle_data(requests_mock):
    api, reg, car, online, data = fleet_only_car(requests_mock)
    fleet = api.providers()[1]
    v = reg.get(VIN)
    calls = []

    def awake(*args, **kwargs):
        # The second read passed fetch's deferral check; the first runs to
        # its end before the second goes on
        calls.append(1)
        if len(calls) == 1:
            assert fleet.fetch(v, "batteryLevel") is True
        return True

    car.is_awake = awake
    assert fleet.fetch(v, "batteryLevel") is True
    assert data.call_count == 1
