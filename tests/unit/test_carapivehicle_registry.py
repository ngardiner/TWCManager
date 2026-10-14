# tests/unit/test_carapivehicle_registry.py
from unittest.mock import Mock

from tests.unit.teslatokens import FakeMaster, fleet_token, new_api
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"


def api_with_registry():
    master = FakeMaster()
    master.vehicleRegistry = VehicleRegistry({})
    api = new_api(master=master)
    api.carApiVehicles = []
    return api, master.vehicleRegistry


def test_vehicle_joins_registry_and_claims_teslaapi():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Callandor"})
    v = reg.get(VIN)
    assert (
        v.attachment("TeslaAPI") is api.getCarApiVehicles()[0] and v.name == "Callandor"
    )
    api.fleetSlot.clear()
    assert v.tier == "observed"  # no command slot: read-only
    api.fleetSlot.setTokens(fleet_token())
    assert v.tier == "managed"


def test_listing_is_a_sighting():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Callandor"})
    v = reg.get(VIN)
    assert v.lastSeenSource == "TeslaAPI"
    assert v.latest("name").source == "TeslaAPI"


def test_attributes_are_observations_with_source_and_defaults():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    assert car.batteryLevel == 10000 and car.chargeLimit == -1 and car.lat == 10000
    assert car.pluggedIn is None and car.chargingState == "Unknown"
    car.chargeLimit = 80
    obs = reg.get(VIN).latest("chargeLimit")
    assert (obs.value, obs.source) == (80, "TeslaAPI") and car.chargeLimit == 80
    car.actualCurrent = 12
    assert car.lastCurrentChangeTime > 0 and car.actualCurrent == 12
    assert reg.get(VIN).latest("actualCurrent").value == 12


def test_actual_current_change_time_only_moves_on_change():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    car.actualCurrent = 12
    car.lastCurrentChangeTime = 1.0
    car.actualCurrent = 12
    assert car.lastCurrentChangeTime == 1.0


def test_master_without_registry_gets_a_private_one():
    api = new_api()
    api.carApiVehicles = []
    api.addVehicle({"id": 1, "vin": "V1", "display_name": "x"})
    assert api.getCarApiVehicles()[0].VIN == "V1" and isinstance(
        api.registry, VehicleRegistry
    )


def test_listing_without_usable_vin_is_skipped():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": None, "display_name": "x"})
    api.addVehicle({"id": 2, "vin": "", "display_name": "y"})
    assert api.getCarApiVehicles() == []


def test_is_awake_trusts_fresh_telemetry_and_asleep_without_api_call():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    car.get_car_api = Mock(side_effect=AssertionError("no API call"))
    reg.get(VIN).observe("state", "asleep", "TeslaMateVehicle")
    assert car.is_awake() is False and car.syncSource == "TeslaMateVehicle"
    reg.get(VIN).observe("state", "online", "TeslaMateVehicle")
    assert car.is_awake() is True


def test_legacy_sync_setters_still_feed_the_registry():  # P7
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    car.get_car_api = Mock(side_effect=AssertionError("no API call"))
    car.syncTimestamp = 123.0
    car.syncSource = "TeslaMateVehicle"
    car.syncState = "asleep"
    car.syncLat = 1.5
    obs = reg.get(VIN).latest("state")
    assert (obs.value, obs.source) == ("asleep", "TeslaMateVehicle")
    assert reg.get(VIN).latest("lat").value == 1.5
    assert car.is_awake() is False and car.syncSource == "TeslaMateVehicle"
    assert car.syncTimestamp == obs.at


def test_sync_getters_have_legacy_defaults_before_any_state():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    assert car.syncSource == "TeslaAPI" and car.syncTimestamp == 0
    assert car.syncState == "asleep" and car.syncLat == 10000 and car.syncLon == 10000


def test_update_vehicle_data_observes_only_present_keys():  # P15
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
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


def test_debug_interface_resolves_by_vin():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    api.wakeVehicle = Mock()
    assert api.apiDebugInterface("wakeVehicle", VIN, "{}") is True
    api.wakeVehicle.assert_called_once_with(api.getCarApiVehicles()[0])
    assert api.apiDebugInterface("wakeVehicle", "5YJ3E1EA1KF000009", "{}") is False


def test_stale_telemetry_falls_back_to_api_check():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    reg.get(VIN).observe("state", "online", "TeslaMateVehicle", at=0.0)
    car.get_car_api = Mock(return_value=(True, {"state": "asleep"}))
    assert car.is_awake() is False and car.get_car_api.called
    assert car.syncSource == "TeslaAPI"


def test_writes_after_forget_land_on_the_live_vehicle():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    assert reg.forget(VIN)
    assert car.chargeLimit == -1  # a read does not bring it back
    assert reg.get(VIN) is None
    car.chargeLimit = 80  # the account still lists it: next sighting rejoins
    v = reg.get(VIN)
    assert v is not None and v.latest("chargeLimit").value == 80
    assert v.attachment("TeslaAPI") is car and "TeslaAPI" in v.claims


def test_writes_after_forget_and_relist_by_another_provider():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    reg.forget(VIN)
    fresh = reg.getOrCreate(VIN, "TWC")
    car.chargeLimit = 80
    assert reg.get(VIN) is fresh and fresh.latest("chargeLimit").value == 80
    assert fresh.attachment("TeslaAPI") is car and "TeslaAPI" in fresh.claims


def test_relisting_the_api_attaches_the_new_car():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    old = api.getCarApiVehicles()[0]
    reg.forget(VIN)
    api.carApiVehicles = []
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    new = api.getCarApiVehicles()[0]
    new.batteryLevel = 70
    v = reg.get(VIN)
    assert v.attachment("TeslaAPI") is new and v.latest("batteryLevel").value == 70
    assert old.batteryLevel == 70  # the old object reads the live vehicle too


def test_legacy_telemetry_feed_drives_a_real_car():  # P7, until Task 11
    from TWCManager.Vehicle.Telemetry import TelmetryBase

    class TeslaMateVehicle(TelmetryBase):  # named as the real module
        configName = "Probe"
        events = {
            "state": ["syncState", lambda a: a],
            "latitude": ["syncLat", lambda a: float(a)],
            "plugged_in": ["pluggedIn", lambda a: a == "true"],
        }

        def scheduleUnmappedRetry(self, delay=None):
            pass

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Callandor"})
    car = api.getCarApiVehicles()[0]
    master = api.master
    master.config["vehicle"]["Probe"] = {"enabled": True}
    feed = TeslaMateVehicle(master)
    feed.applyDataToVehicle("1", "display_name", "Callandor")
    assert feed.vehicles["1"] is car
    feed.applyDataToVehicle("1", "state", "online")
    feed.applyDataToVehicle("1", "latitude", "1.5")
    feed.applyDataToVehicle("1", "plugged_in", "true")
    obs = reg.get(VIN).latest("state")
    assert (obs.value, obs.source) == ("online", "TeslaMateVehicle")
    assert reg.get(VIN).latest("lat").source == "TeslaMateVehicle"
    assert car.syncSource == "TeslaMateVehicle" and car.pluggedIn is True
    assert car.syncLat == 1.5 and car.lat == 1.5
