import time
from unittest.mock import Mock
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.Providers.TeslaBLEProvider import TeslaBLEProvider
from tests.unit.teslatokens import FakeMaster, new_api, owner_token, wire_reads

VIN = "5YJ3E1EA1KF000001"


def setup(ble_result, clock=time.time):
    master = FakeMaster()
    reg = master.vehicleRegistry = VehicleRegistry({}, clock=clock)
    ble = Mock()
    ble.get_charge_state.return_value = ble_result
    ble.get_location_state.return_value = None
    master.modules.update(
        {"TeslaBLE": ble}
    )
    api = new_api(master=master)
    # The cloud read source reads through the Owner slot
    api.storeToken(owner_token())
    wire_reads(reg, TeslaBLEProvider(ble, master), *api.providers())
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    api.is_awake = Mock(return_value=True)
    # The API source observes into the registry; the stand-in must too.
    api.update_vehicle_data = Mock(
        side_effect=lambda vehicle, slot=None: vehicle.observe(
            "batteryLevel", 50, "OwnerAPI"
        )
        and True
    )
    reg.get(VIN).claim("TeslaBLE", True, "paired")
    return api, ble, reg


def state(reg):
    return reg.get(VIN).attachment("TeslaAPI")


def test_ble_read_fills_fields_and_api_untouched():
    api, ble, reg = setup(
        {
            "batteryLevel": 61,
            "chargeLimit": 80,
            "chargingState": "Charging",
            "phases": None,
        }
    )
    api.update_charge(reg.get(VIN))
    assert (reg.get(VIN).value("batteryLevel", 10000), reg.get(VIN).latest("chargeLimit").source) == (
        61,
        "TeslaBLE",
    )
    assert reg.get(VIN).latest("phases") is None  # P14: None dropped
    api.update_vehicle_data.assert_not_called()
    ble.wakeVehicle.assert_not_called()


def test_api_source_checks_free_online_state_before_paid_vehicle_data():
    api, ble, reg = setup(None)
    api.is_awake = Mock(return_value=False)
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).latest("batteryLevel") is None
    api.update_vehicle_data.assert_not_called()
    ble.wakeVehicle.assert_not_called()


def test_status_deferral_counts_as_success_without_fetch():  # P13
    api, ble, reg = setup(None)
    api.update_vehicle_data = Mock(return_value=True)  # deferral path: no observe
    reg.get(VIN).observe("batteryLevel", 40, "OwnerAPI", at=0.0)
    state(reg).statusDeferral = time.time() + 1800
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).value("batteryLevel", 10000) == 40
    api.update_vehicle_data.assert_not_called()
    ble.wakeVehicle.assert_not_called()


def test_update_location_zero_forces_a_fresh_read():  # P12
    api, ble, reg = setup(None)
    reg.get(VIN).observe("lat", 1.0, "OwnerAPI")
    reg.get(VIN).observe("lon", 2.0, "OwnerAPI")
    ble.get_location_state.return_value = {
        "latitude": 3.0,
        "longitude": 4.0,
        "heading": 0,
        "gpsAsOf": 5,
    }
    assert reg.get(VIN).read("lat", 0).value == 3.0
    ble.get_location_state.assert_called_once()


def test_ble_failure_falls_to_api_without_waking():  # A6
    api, ble, reg = setup(None)
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).value("batteryLevel", 10000) == 50
    api.update_vehicle_data.assert_called_once()
    ble.wakeVehicle.assert_not_called()


def test_all_sources_failing_keeps_the_stale_value_and_does_not_wake():
    api, ble, reg = setup(None)
    api.update_vehicle_data = Mock(return_value=False)
    reg.get(VIN).observe("batteryLevel", 40, "OwnerAPI", at=0.0)
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).value("batteryLevel", 10000) == 40
    api.update_vehicle_data.assert_called_once()
    ble.wakeVehicle.assert_not_called()


def test_unpaired_vin_skips_ble():
    api, ble, reg = setup({"batteryLevel": 1})
    reg.get(VIN).dropClaim("TeslaBLE")
    api.update_charge(reg.get(VIN))
    ble.get_charge_state.assert_not_called()
    api.update_vehicle_data.assert_called_once()


def test_fresh_observation_skips_both():
    api, ble, reg = setup({"batteryLevel": 1})
    reg.get(VIN).observe("batteryLevel", 50, "TeslaMateVehicle")
    api.update_charge(reg.get(VIN))
    ble.get_charge_state.assert_not_called()
    api.update_vehicle_data.assert_not_called()


def test_allow_wake_wakes_once_then_retries():
    api, ble, reg = setup(None)
    ble.wakeVehicle.return_value = True
    ble.get_charge_state.side_effect = [None, {"batteryLevel": 9}]
    assert reg.get(VIN).read("batteryLevel", maxAge=0, allowWake=True).value == 9
    ble.wakeVehicle.assert_called_once_with(VIN)


def test_location_read_observes_the_position():
    api, ble, reg = setup(None)
    ble.get_location_state.return_value = {
        "latitude": 1.0,
        "longitude": 2.0,
        "heading": 0,
        "gpsAsOf": 5,
    }
    assert reg.get(VIN).read("lat", 0) is not None
    assert (reg.get(VIN).value("lat", 10000), reg.get(VIN).value("lon", 10000)) == (1.0, 2.0)


def test_status_deferral_skips_the_online_check():
    api, ble, reg = setup(None)
    reg.get(VIN).observe("batteryLevel", 40, "OwnerAPI", at=0.0)
    state(reg).statusDeferral = time.time() + 1800
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).value("batteryLevel", 10000) == 40
    api.is_awake.assert_not_called()
    api.update_vehicle_data.assert_not_called()


def test_ble_read_marks_car_awake_but_cache_hit_does_not():
    api, ble, reg = setup({"batteryLevel": 61})
    state(reg).lastAPIAccessTime = 0
    api.update_charge(reg.get(VIN))
    assert state(reg).lastAPIAccessTime > 0
    state(reg).lastAPIAccessTime = 0
    api.update_charge(reg.get(VIN))
    assert state(reg).lastAPIAccessTime == 0
    ble.get_charge_state.assert_called_once()


def test_ble_read_observes_actual_current():
    api, ble, reg = setup({"batteryLevel": 61, "actualCurrent": 16})
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).value("actualCurrent", 0) == 16
    assert reg.get(VIN).latest("actualCurrent").source == "TeslaBLE"


def test_a_location_read_with_no_fix_time_keeps_the_older_one():
    api, ble, reg = setup(None)
    reg.get(VIN).observe("gpsAsOf", 5, "OwnerAPI", at=1.0)
    ble.get_location_state.return_value = {
        "latitude": 1.0,
        "longitude": 2.0,
        "heading": 0,
        "gpsAsOf": None,
    }
    assert reg.get(VIN).read("lat", 0).value == 1.0
    assert reg.get(VIN).latest("gpsAsOf").value == 5


def test_api_source_never_wakes_even_when_allowed():
    api, ble, reg = setup(None)
    api.wakeVehicle = Mock()
    reg.get(VIN).dropClaim("TeslaBLE")
    assert reg.get(VIN).read("batteryLevel", maxAge=0, allowWake=True).value == 50
    api.wakeVehicle.assert_not_called()


def test_null_gps_as_of_is_not_observed():
    api, ble, reg = setup(None)
    api.slotFor = Mock(return_value=Mock())
    api.get_car_api = Mock(
        return_value=(
            True,
            {
                "drive_state": {"latitude": 1.0, "longitude": 2.0, "gps_as_of": None},
                "charge_state": {"battery_level": 70},
            },
        )
    )
    del api.update_vehicle_data  # use the real method
    assert api.update_vehicle_data(reg.get(VIN)) is True
    assert reg.get(VIN).latest("gpsAsOf") is None and reg.get(VIN).value("batteryLevel", 10000) == 70


def test_teslable_provider_reads_through_the_module(tmp_path):
    from TWCManager.Vehicle.Providers import Capability as C
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
    (p,) = ble.providers()
    assert p.ble is ble and p.cost(C.READ_CHARGE) == C.LOCAL
    assert p.FIELDS == C.CHARGE_FIELDS | C.LOCATION_FIELDS


def test_ble_reply_with_no_values_falls_through_to_api():
    api, ble, reg = setup(None)
    reg.get(VIN).observe("lat", 1.0, "OwnerAPI", at=0.0)
    ble.get_location_state.return_value = {
        "latitude": None,
        "longitude": None,
        "heading": 90,
        "gpsAsOf": None,
    }
    reg.get(VIN).read("lat", 0)
    api.update_vehicle_data.assert_called_once()


def test_ble_read_on_an_injected_clock_marks_car_awake():
    # The registry clock and time.time() must not be compared.
    api, ble, reg = setup({"batteryLevel": 61}, clock=lambda: 50.0)
    state(reg).lastAPIAccessTime = 0
    api.update_charge(reg.get(VIN))
    assert state(reg).lastAPIAccessTime > 0


def test_read_stamped_like_the_previous_one_still_counts_as_fetched():
    # A coarse clock can stamp a real read equal to the last one.
    api, ble, reg = setup({"batteryLevel": 61}, clock=lambda: 50.0)
    v = reg.get(VIN)
    v.observe("batteryLevel", 40, "TeslaBLE")
    v.noteWrite("batteryLevel", 40, "OwnerAPI")  # equal stamp: not trusted
    state(reg).lastAPIAccessTime = 0
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).value("batteryLevel", 10000) == 61
    ble.get_charge_state.assert_called_once()
    assert state(reg).lastAPIAccessTime > 0


def test_a_status_deferral_ends_the_read_walk_before_home_assistant():  # 13b M1
    # Owner and HA both ready: the deferred car reports its stored values
    # with no request, and HA (billed, after the cloud API) is never read
    from TWCManager.Vehicle.Providers.HomeAssistantProvider import (
        HomeAssistantProvider,
    )

    api, ble, reg = setup(None)
    ha = Mock()
    ha.enabled.return_value = True
    hv = Mock()
    reg.get(VIN).attach("HomeAssistant", hv)
    reg.providerOrder.register(HomeAssistantProvider(ha))
    api.update_vehicle_data = Mock(return_value=True)
    reg.get(VIN).observe("batteryLevel", 40, "OwnerAPI", at=0.0)
    state(reg).statusDeferral = time.time() + 1800
    api.update_charge(reg.get(VIN))
    assert reg.get(VIN).value("batteryLevel", 10000) == 40
    api.is_awake.assert_not_called()
    api.update_vehicle_data.assert_not_called()
    hv.parse_from_home_assistant.assert_not_called()
