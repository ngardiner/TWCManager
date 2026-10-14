"""Own-write stamps: every successful charge limit / charge rate send is
recorded in the registry with the value actually sent; failures record
nothing."""

import time
from unittest.mock import Mock

from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaAPI import TeslaAPI
from tests.unit.teslatokens import fleet_token, new_api
from tests.unit.test_homeassistant_registry import make_ha
from tests.unit.test_teslable_pairing_status import make as make_ble

VIN = "5YJ3E1EA1KF000001"


def test_api_charge_rate_stamps_available_current():
    reg = VehicleRegistry({})
    v = reg.getOrCreate(VIN, "TeslaAPI")
    car = Mock(VIN=VIN, _liveVehicle=Mock(return_value=v))
    TeslaAPI._recordChargeRate(car, 16)
    assert v.lastWrite("availableCurrent").value == 16
    assert v.lastWrite("availableCurrent").source == "TeslaAPI"


def test_api_charge_rate_attribute_only_fake_still_works():
    class Fake:
        pass

    car = Fake()
    TeslaAPI._recordChargeRate(car, 9)
    assert car.lastChargeRateSent == 9


def test_ble_set_charge_rate_stamps_on_success():
    ble, reg, master = make_ble()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    assert ble.setChargeRate(12, VIN) is True
    w = reg.get(VIN).lastWrite("availableCurrent")
    assert w.value == 12 and w.source == "TeslaBLE"


def test_ble_set_charge_rate_no_stamp_on_failure():
    ble, reg, master = make_ble()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value=None)
    assert ble.setChargeRate(12, VIN) is False
    assert reg.get(VIN).lastWrite("availableCurrent") is None


def test_ble_set_charge_rate_unparsed_failure_no_stamp():
    ble, reg, master = make_ble()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="bad")
    ble.parseCommandOutput = Mock(return_value=False)
    assert ble.setChargeRate(12, VIN) is False
    assert reg.get(VIN).lastWrite("availableCurrent") is None


def test_ble_set_charge_rate_unknown_vehicle_is_skipped():
    ble, reg, master = make_ble()
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    assert ble.setChargeRate(12, VIN) is True
    assert reg.get(VIN) is None


def test_ble_set_charge_rate_all_vehicles_stamps_each_success():
    ble, reg, master = make_ble()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    assert ble.setChargeRate(10) is True
    assert reg.get(VIN).lastWrite("availableCurrent").value == 10


def test_ble_apply_charge_limit_stamps_on_success():
    ble, reg, master = make_ble()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    master.getNormalChargeLimit.return_value = (False, None, None)
    assert ble.applyChargeLimit(80) is True
    w = reg.get(VIN).lastWrite("chargeLimit")
    assert w.value == 80 and w.source == "TeslaBLE"


def test_ble_apply_charge_limit_failure_no_stamp():
    ble, reg, master = make_ble()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value=None)
    master.getNormalChargeLimit.return_value = (False, None, None)
    ble.applyChargeLimit(80)
    assert reg.get(VIN).lastWrite("chargeLimit") is None


def test_ble_restore_stamps_the_restored_value():
    ble, reg, master = make_ble()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    master.getNormalChargeLimit.return_value = (True, 65, 80)
    assert ble.applyChargeLimit(-1) is True
    assert reg.get(VIN).lastWrite("chargeLimit").value == 65


def _api_vehicle(requests_mock):
    api = new_api()
    api.storeToken(fleet_token())
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    car = api.getCarApiVehicles()[-1]
    car.atHome = True
    car.lastAPIAccessTime = time.time()
    return api, car


def test_api_apply_charge_limit_stamps_on_success(requests_mock):
    api, car = _api_vehicle(requests_mock)
    requests_mock.post(
        f"https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/{VIN}/command/set_charge_limit",
        json={"response": {"result": True}},
    )
    assert car.apply_charge_limit(80) is True
    w = api.registry.get(VIN).lastWrite("chargeLimit")
    assert w.value == 80 and w.source == "TeslaAPI"


def test_api_apply_charge_limit_failure_no_stamp(requests_mock):
    api, car = _api_vehicle(requests_mock)
    requests_mock.post(
        f"https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/{VIN}/command/set_charge_limit",
        json={"response": {"result": False, "reason": "nope"}},
    )
    assert car.apply_charge_limit(80) is False
    assert api.registry.get(VIN).lastWrite("chargeLimit") is None


def test_ha_apply_charge_limit_stamps_on_success():
    from TWCManager.Vehicle.HomeAssistant import HaVehicle

    ha, reg = make_ha()
    hv = HaVehicle("Car", VIN, "d1", {"charge_limit_number": "number.limit"})
    hv.at_home = True
    hv.refresh_from_home_assistant = Mock()
    ha.carApiVehicles = [hv]
    ha.car_api_available = Mock(return_value=True)
    ha._is_vehicle_connected = Mock(return_value=True)
    ha._call_service = Mock(return_value=True)
    reg.getOrCreate(VIN, "HomeAssistant")
    assert ha.applyChargeLimit(80) == "success"
    w = reg.get(VIN).lastWrite("chargeLimit")
    assert w.value == 80 and w.source == "HomeAssistant"


def test_ha_apply_charge_limit_failure_no_stamp():
    from TWCManager.Vehicle.HomeAssistant import HaVehicle

    ha, reg = make_ha()
    hv = HaVehicle("Car", VIN, "d1", {"charge_limit_number": "number.limit"})
    hv.at_home = True
    hv.refresh_from_home_assistant = Mock()
    ha.carApiVehicles = [hv]
    ha.car_api_available = Mock(return_value=True)
    ha._is_vehicle_connected = Mock(return_value=True)
    ha._call_service = Mock(return_value=False)
    reg.getOrCreate(VIN, "HomeAssistant")
    assert ha.applyChargeLimit(80) == "error"
    assert reg.get(VIN).lastWrite("chargeLimit") is None


def test_api_charge_rate_does_not_revive_forgotten_vehicle():
    api = new_api()
    api.storeToken(fleet_token())
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    car = api.getCarApiVehicles()[-1]
    api.registry.forget(VIN)
    assert api.registry.get(VIN) is None
    TeslaAPI._recordChargeRate(car, 16)
    assert api.registry.get(VIN) is None


def _ha_rate(ok):
    from TWCManager.Vehicle.HomeAssistant import HaVehicle

    ha, reg = make_ha()
    hv = HaVehicle("Car", VIN, "d1", {"charge_current_number": "number.amps"})
    hv.refresh_from_home_assistant = Mock()
    ha.carApiVehicles = [hv]
    ha._is_vehicle_connected = Mock(return_value=True)
    ha._call_service = Mock(return_value=ok)
    reg.getOrCreate(VIN, "HomeAssistant")
    return ha, reg


def test_ha_set_charge_rate_stamps_on_success():  # M4
    ha, reg = _ha_rate(True)
    assert ha.setChargeRate(16) is True
    w = reg.get(VIN).lastWrite("availableCurrent")
    assert w.value == 16 and w.source == "HomeAssistant"


def test_ha_set_charge_rate_failure_no_stamp():  # M4
    ha, reg = _ha_rate(False)
    assert ha.setChargeRate(16) is False
    assert reg.get(VIN).lastWrite("availableCurrent") is None
