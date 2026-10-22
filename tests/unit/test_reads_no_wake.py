import time
from unittest.mock import Mock
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.Registry.Sources import TeslaBLEReadSource, TeslaAPIReadSource
from tests.unit.teslatokens import FakeMaster, new_api, owner_token

VIN = "5YJ3E1EA1KF000001"


def setup(ble_result, clock=time.time):
    master = FakeMaster()
    reg = master.vehicleRegistry = VehicleRegistry({}, clock=clock)
    ble = Mock()
    ble.get_charge_state.return_value = ble_result
    ble.get_location_state.return_value = None
    master.modules.update(
        {"VehiclePriority": Mock(commandPolicy="prefer_ble"), "TeslaBLE": ble}
    )
    api = new_api(master=master)
    api.carApiVehicles = []
    # The cloud read source reads through the Owner slot
    api.storeToken(owner_token())
    reg.registerReadSource(TeslaBLEReadSource(ble, master))
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    car.is_awake = Mock(return_value=True)
    # The API source writes through descriptors; the stand-in must too.
    car.update_vehicle_data = Mock(
        side_effect=lambda slot=None: car.vehicle.observe("batteryLevel", 50, "TeslaAPI")
        and True
    )
    reg.get(VIN).claim("TeslaBLE", True, "paired")
    return car, ble, reg


def test_ble_read_fills_fields_and_api_untouched():
    car, ble, reg = setup(
        {
            "batteryLevel": 61,
            "chargeLimit": 80,
            "chargingState": "Charging",
            "phases": None,
        }
    )
    assert car.update_charge() is True
    assert (car.batteryLevel, reg.get(VIN).latest("chargeLimit").source) == (
        61,
        "TeslaBLE",
    )
    assert reg.get(VIN).latest("phases") is None  # P14: None dropped
    car.update_vehicle_data.assert_not_called()
    ble.wakeVehicle.assert_not_called()


def test_api_source_checks_free_online_state_before_paid_vehicle_data():
    car, ble, reg = setup(None)
    car.is_awake = Mock(return_value=False)
    assert car.update_charge() is False
    car.update_vehicle_data.assert_not_called()
    ble.wakeVehicle.assert_not_called()


def test_status_deferral_counts_as_success_without_fetch():  # P13
    car, ble, reg = setup(None)
    car.update_vehicle_data = Mock(return_value=True)  # deferral path: no observe
    reg.get(VIN).observe("batteryLevel", 40, "TeslaAPI", at=0.0)
    car.statusDeferral = time.time() + 1800
    assert car.update_charge() is True


def test_update_location_zero_forces_a_fresh_read():  # P12
    car, ble, reg = setup(None)
    reg.get(VIN).observe("lat", 1.0, "TeslaAPI")
    reg.get(VIN).observe("lon", 2.0, "TeslaAPI")
    ble.get_location_state.return_value = {
        "latitude": 3.0,
        "longitude": 4.0,
        "heading": 0,
        "gpsAsOf": 5,
    }
    car.carapi.is_location_home = Mock(return_value=True)
    assert car.update_location(0) is True and car.lat == 3.0
    ble.get_location_state.assert_called_once()


def test_ble_failure_falls_to_api_without_waking():  # A6
    car, ble, reg = setup(None)
    assert car.update_charge() is True and car.batteryLevel == 50
    car.update_vehicle_data.assert_called_once()
    ble.wakeVehicle.assert_not_called()


def test_all_sources_failing_reports_failure_even_with_stale_value():
    car, ble, reg = setup(None)
    car.update_vehicle_data = Mock(return_value=False)
    reg.get(VIN).observe("batteryLevel", 40, "TeslaAPI", at=0.0)
    assert car.update_charge() is False and car.batteryLevel == 40


def test_unpaired_vin_skips_ble():
    car, ble, reg = setup({"batteryLevel": 1})
    reg.get(VIN).dropClaim("TeslaBLE")
    car.update_charge()
    ble.get_charge_state.assert_not_called()
    car.update_vehicle_data.assert_called_once()


def test_fresh_observation_skips_both():
    car, ble, reg = setup({"batteryLevel": 1})
    reg.get(VIN).observe("batteryLevel", 50, "TeslaMateVehicle")
    assert car.update_charge() is True
    ble.get_charge_state.assert_not_called()
    car.update_vehicle_data.assert_not_called()


def test_allow_wake_wakes_once_then_retries():
    car, ble, reg = setup(None)
    ble.wakeVehicle.return_value = True
    ble.get_charge_state.side_effect = [None, {"batteryLevel": 9}]
    assert reg.get(VIN).read("batteryLevel", maxAge=0, allowWake=True).value == 9
    ble.wakeVehicle.assert_called_once_with(VIN)


def test_location_read_recomputes_at_home():
    car, ble, reg = setup(None)
    ble.get_location_state.return_value = {
        "latitude": 1.0,
        "longitude": 2.0,
        "heading": 0,
        "gpsAsOf": 5,
    }
    car.carapi.is_location_home = Mock(return_value=True)
    assert (
        car.update_location() is True
        and car.atHome is True
        and (car.lat, car.lon) == (1.0, 2.0)
    )


def test_status_deferral_skips_the_online_check():
    car, ble, reg = setup(None)
    reg.get(VIN).observe("batteryLevel", 40, "TeslaAPI", at=0.0)
    car.statusDeferral = time.time() + 1800
    assert car.update_charge() is True
    car.is_awake.assert_not_called()
    car.update_vehicle_data.assert_not_called()


def test_ble_read_marks_car_awake_but_cache_hit_does_not():
    car, ble, reg = setup({"batteryLevel": 61})
    car.lastAPIAccessTime = 0
    assert car.update_charge() is True and car.lastAPIAccessTime > 0
    car.lastAPIAccessTime = 0
    assert car.update_charge() is True and car.lastAPIAccessTime == 0
    ble.get_charge_state.assert_called_once()


def test_ble_actual_current_change_is_timed():
    car, ble, reg = setup({"batteryLevel": 61, "actualCurrent": 16})
    car.lastCurrentChangeTime = 0
    car.update_charge()
    assert car.actualCurrent == 16 and car.lastCurrentChangeTime > 0
    assert reg.get(VIN).latest("actualCurrent").source == "TeslaBLE"


def test_location_ignores_a_fix_time_older_than_the_position():
    car, ble, reg = setup(None)
    reg.get(VIN).observe("gpsAsOf", 5, "TeslaAPI", at=1.0)
    ble.get_location_state.return_value = {
        "latitude": 1.0,
        "longitude": 2.0,
        "heading": 0,
        "gpsAsOf": None,
    }
    car.carapi.is_location_home = Mock(return_value=True)
    assert car.update_location() is True
    car.carapi.is_location_home.assert_called_once_with(1.0, 2.0, None)
    assert reg.get(VIN).latest("gpsAsOf").value == 5


def test_api_source_never_wakes_even_when_allowed():
    car, ble, reg = setup(None)
    car.carapi.wakeVehicle = Mock()
    reg.get(VIN).dropClaim("TeslaBLE")
    assert reg.get(VIN).read("batteryLevel", maxAge=0, allowWake=True).value == 50
    car.carapi.wakeVehicle.assert_not_called()


def test_null_gps_as_of_is_not_observed():
    car, ble, reg = setup(None)
    car.carapi.slotFor = Mock(return_value=Mock())
    car.get_car_api = Mock(
        return_value=(
            True,
            {
                "drive_state": {"latitude": 1.0, "longitude": 2.0, "gps_as_of": None},
                "charge_state": {"battery_level": 70},
            },
        )
    )
    del car.update_vehicle_data  # use the real method
    car.carapi.is_location_home = Mock(return_value=True)
    assert car.update_vehicle_data() is True
    assert reg.get(VIN).latest("gpsAsOf") is None and car.batteryLevel == 70


def test_teslable_registers_its_read_source(tmp_path):
    from unittest.mock import patch
    from TWCManager.Vehicle.TeslaBLE import TeslaBLE

    master = FakeMaster(teslaControlPath="/fake/tesla-control")
    master.vehicleRegistry = VehicleRegistry({})
    with patch.object(TeslaBLE, "pipeName", str(tmp_path / "ble_data")), patch(
        "TWCManager.Vehicle.TeslaBLE.os.path.isfile", return_value=True
    ), patch("TWCManager.Vehicle.TeslaBLE.os.access", return_value=True), patch.object(
        TeslaBLE, "_start_dbus_session"
    ):
        ble = TeslaBLE(master)
    sources = master.vehicleRegistry.readSources()
    assert [s.name for s in sources] == ["TeslaBLE"] and sources[0].ble is ble


def test_ble_reply_with_no_values_falls_through_to_api():
    car, ble, reg = setup(None)
    reg.get(VIN).observe("lat", 1.0, "TeslaAPI", at=0.0)
    ble.get_location_state.return_value = {
        "latitude": None,
        "longitude": None,
        "heading": 90,
        "gpsAsOf": None,
    }
    car.update_location()
    car.update_vehicle_data.assert_called_once()


def test_ble_read_on_an_injected_clock_marks_car_awake():
    # The registry clock and time.time() must not be compared.
    car, ble, reg = setup({"batteryLevel": 61}, clock=lambda: 50.0)
    car.lastAPIAccessTime = 0
    assert car.update_charge() is True and car.lastAPIAccessTime > 0


def test_read_stamped_like_the_previous_one_still_counts_as_fetched():
    # A coarse clock can stamp a real read equal to the last one.
    car, ble, reg = setup({"batteryLevel": 61}, clock=lambda: 50.0)
    v = reg.get(VIN)
    v.observe("batteryLevel", 40, "TeslaBLE")
    v.noteWrite("batteryLevel", 40, "TeslaAPI")  # equal stamp: not trusted
    car.lastAPIAccessTime = 0
    assert car.update_charge() is True and car.batteryLevel == 61
    ble.get_charge_state.assert_called_once()
    assert car.lastAPIAccessTime > 0
