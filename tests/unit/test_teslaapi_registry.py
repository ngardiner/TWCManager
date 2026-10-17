# tests/unit/test_teslaapi_registry.py
from unittest.mock import ANY, Mock

from tests.unit.teslatokens import FakeMaster, fleet_token, new_api, wire_reads
from TWCManager.Vehicle.Reconcile.Outcome import DONE, Outcome
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from tests.unit.vehicle_helpers import commands

VIN = "5YJ3E1EA1KF000001"


def api_with_registry():
    master = FakeMaster()
    master.vehicleRegistry = VehicleRegistry({})
    api = new_api(master=master)
    wire_reads(master.vehicleRegistry, *api.providers())
    return api, master.vehicleRegistry


def test_vehicle_joins_registry_and_claims_teslaapi():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "TestCar"})
    v = reg.get(VIN)
    assert reg.attached("TeslaAPI") == [(v, v.attachment("TeslaAPI"))]
    assert v.name == "TestCar"
    api.fleetSlot.clear()
    assert not commands(v)  # no command slot: read-only
    api.fleetSlot.setTokens(fleet_token())
    assert commands(v)


def test_listing_is_a_sighting():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "TestCar"})
    v = reg.get(VIN)
    assert v.lastSeenSource == "TeslaAPI"
    assert v.latest("name").source == "TeslaAPI"


def test_master_without_registry_gets_a_private_one():
    api = new_api()
    api.addVehicle({"id": 1, "vin": "5YJ3E1EA1KF000001", "display_name": "x"})
    assert [v.vin for v, _ in api.registry.attached("TeslaAPI")] == [
        "5YJ3E1EA1KF000001"
    ] and isinstance(api.registry, VehicleRegistry)


def test_listing_without_usable_vin_is_skipped():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": None, "display_name": "x"})
    api.addVehicle({"id": 2, "vin": "", "display_name": "y"})
    assert api.registry.attached("TeslaAPI") == []


def test_is_awake_trusts_fresh_telemetry_and_asleep_without_api_call():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    api.get_car_api = Mock(side_effect=AssertionError("no API call"))
    v.observe("state", "asleep", "TeslaMateVehicle")
    assert api.is_awake(v) is False and v.syncSource == "TeslaMateVehicle"
    v.observe("state", "online", "TeslaMateVehicle")
    assert api.is_awake(v) is True


def test_sync_getters_have_legacy_defaults_before_any_state():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    assert reg.get(VIN).syncSource == "TeslaAPI"


def test_update_vehicle_data_observes_only_present_keys():  # P15
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    reg.get(VIN).observe("phases", 3, "TeslaBLE", at=5.0)
    name_before = reg.get(VIN).latest("name")
    api.get_car_api = Mock(
        return_value=(
            True,
            {
                "drive_state": {"latitude": 1.0, "longitude": 2.0},
                "charge_state": {"battery_level": 61},
                "vehicle_state": {},
            },
        )
    )
    api.is_location_home = Mock(return_value=False)
    api.is_far_from_home = Mock(return_value=False)
    assert api.update_vehicle_data(reg.get(VIN)) is True
    phases = reg.get(VIN).latest("phases")
    assert reg.get(VIN).value("batteryLevel") == 61
    assert (phases.value, phases.source, phases.at) == (3, "TeslaBLE", 5.0)
    assert reg.get(VIN).latest("chargeLimit") is None  # no default observed
    assert reg.get(VIN).latest("gpsAsOf") is None
    assert reg.get(VIN).latest("name") == name_before  # not re-observed


def test_update_vehicle_data_observes_the_cars_own_current_request():  # slim Task 12b
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    api.get_car_api = Mock(
        return_value=(
            True,
            {
                "drive_state": {"latitude": 1.0, "longitude": 2.0},
                "charge_state": {
                    "charge_current_request": 16,
                    "charger_pilot_current": 32,
                },
                "vehicle_state": {},
            },
        )
    )
    api.is_location_home = Mock(return_value=False)
    api.is_far_from_home = Mock(return_value=False)
    assert api.update_vehicle_data(reg.get(VIN)) is True
    obs = reg.get(VIN).latest("chargeCurrentRequest")
    assert (obs.value, obs.source) == (16, "TeslaAPI")
    assert reg.get(VIN).value("availableCurrent") == 32  # the pilot stays the pilot


def test_update_vehicle_data_records_the_cars_own_ac_maximum():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    api.get_car_api = Mock(
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
    api.is_location_home = Mock(return_value=False)
    api.is_far_from_home = Mock(return_value=False)
    assert api.update_vehicle_data(reg.get(VIN)) is True
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
    api.wakeOutcome.assert_called_once_with(reg.get(VIN), now=ANY, slot=api.fleetSlot)
    assert (
        master.providerOrder.debugCommand("wakeVehicle", "5YJ3E1EA1KF000009", "{}")
        is False
    )


def test_stale_telemetry_falls_back_to_api_check():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    v.observe("state", "online", "TeslaMateVehicle", at=0.0)
    api.get_car_api = Mock(return_value=(True, {"state": "asleep"}))
    assert api.is_awake(v) is False and api.get_car_api.called
    assert reg.get(VIN).syncSource == "TeslaAPI"


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
    assert (
        reg.get(VIN).syncSource == "TeslaMateVehicle"
        and reg.get(VIN).value("pluggedIn") is True
    )
    assert reg.get(VIN).value("lat", 10000) == 1.5


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

    api.is_awake = awake
    assert fleet.fetch(v, "batteryLevel") is True
    assert data.call_count == 1


# The interface: the attachment is the listing; the online check's three
# states stay internal


def test_listing_attaches_state_and_both_claims():
    from TWCManager.Vehicle.TeslaAPI import CarApiState

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "TestCar"})
    v = reg.get(VIN)
    state = v.attachment("TeslaAPI")
    assert type(state) is CarApiState
    assert (state.atHome, state.statusDeferral, state.errorCount) == (False, 0, 0)
    assert (state.lastErrorTime, state.lastAPIAccessTime) == (0, 0)
    assert (state.firstWakeAttemptTime, state.firstChargeNeededTime) == (0, 0)
    assert (state.lastChargeNeededTime, state.delayNextWakeAttempt) == (0, 0)
    assert (state.online, state.onlineCheckedAt) == (None, 0)
    owner, fleet = v.claims["OwnerAPI"], v.claims["FleetAPI"]
    assert owner.commands is False and owner.status == fleet.status == "listed"
    assert fleet.commands is False  # no Fleet token yet
    api.storeToken(fleet_token())
    assert fleet.commands is True  # follows the slot
    assert v.latest("name").source == "TeslaAPI" and v.name == "TestCar"
    assert reg.attached("TeslaAPI") == [(v, state)]


def test_online_state_is_true_false_or_none_and_is_awake_stays_a_bool(
    requests_mock,
):
    api, reg = api_with_registry()
    api.storeToken(fleet_token())
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    check = requests_mock.get(FLEET_CAR, status_code=500, json={})
    assert api.onlineState(v, now=1000.0) is None  # no answer
    assert api.is_awake(v, now=1119.0) is False and check.call_count == 1  # cached
    assert api.onlineState(v, now=1121.0) is None and check.call_count == 2
    online = requests_mock.get(FLEET_CAR, json={"response": {"state": "online"}})
    assert api.onlineState(v, now=1300.0) is True and online.call_count == 1
    v.observe("state", "asleep", "TeslaMateVehicle")
    assert (api.onlineState(v), api.is_awake(v)) == (False, False)
    v.observe("state", "online", "TeslaMateVehicle")
    assert (api.onlineState(v), api.is_awake(v)) == (True, True)
    assert online.call_count == 1  # telemetry answered, no GET


def test_update_vehicle_data_has_a_single_caller():
    import pathlib
    import subprocess

    out = subprocess.run(
        ["git", "grep", "-n", "update_vehicle_data(", "--", "lib"],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parents[2],
    ).stdout.splitlines()
    rest = [
        line
        for line in out
        if "def update_vehicle_data" not in line
        and not line.startswith("lib/TWCManager/Vehicle/Providers/TeslaAPIProviders.py")
    ]
    assert out and rest == []


def test_an_incomplete_vehicle_data_answer_logs_what_is_missing(caplog):
    import logging

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    answers = [
        ({"charge_state": {}}, "drive_state missing from API response"),
        ({"drive_state": {}}, "charge_state missing from API response"),
        (
            {"drive_state": {"latitude": 1.0}, "charge_state": {}},
            "latitude/longitude missing from drive_state",
        ),
    ]
    for response, missing in answers:
        api.get_car_api = Mock(return_value=(True, response))
        caplog.clear()
        assert api.update_vehicle_data(v) is False
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.ERROR, "update_vehicle_data: %s for %s" % (missing, VIN))
        ]
    assert v.value("lat") == 1.0 and v.latest("lon") is None  # lat first
