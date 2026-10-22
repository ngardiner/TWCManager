# tests/unit/test_targeted_charge_limit.py
#
# Concern rulings, item 2: the real TeslaBLE and TeslaAPI applyChargeLimit
# take the facade's vin and remoteOnly. A targeted call acts on that car
# only; a broadcast is unchanged.
from unittest.mock import Mock, patch

from tests.unit.teslatokens import add_home_vehicle, fleet_token, new_api
from tests.unit.test_teslable_pairing_status import make as make_ble

VIN = "5YJ3E1EA1KF000001"
OTHER = "5YJ3E1EA1KF000009"


def ble2():
    ble, reg, master = make_ble()
    master.settings["Vehicles"][OTHER] = {"privKey": "y"}
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    master.getNormalChargeLimit.return_value = (False, None, None)
    return ble


def sentTo(ble):
    return [c.args[0] for c in ble.sendCommand.call_args_list]


def test_ble_targeted_limit_touches_only_that_car():
    ble = ble2()
    assert ble.applyChargeLimit(80, vin=VIN.lower()) is True
    assert sentTo(ble) == [VIN]


def test_ble_broadcast_limit_is_unchanged():
    ble = ble2()
    assert ble.applyChargeLimit(80) is True
    assert sorted(sentTo(ble)) == [VIN, OTHER]


def test_ble_declines_a_remote_only_limit():
    ble = ble2()
    assert ble.applyChargeLimit(-1, vin=VIN, remoteOnly=True) is False
    assert sentTo(ble) == []


def api2():
    api = new_api()
    api.storeToken(fleet_token())
    cars = []
    for i, vin in enumerate((VIN, OTHER), start=1):
        api.addVehicle({"id": i, "vin": vin, "display_name": vin})
        car = api.getCarApiVehicles()[-1]
        car.atHome = True
        car.update_charge = Mock(return_value=True)
        car.update_location = Mock(return_value=True)
        car.ready = Mock(return_value=True)
        car.chargeLimit = 70
        car.apply_charge_limit = Mock(return_value=True)
        cars.append(car)
    api.car_api_available = Mock(return_value=True)
    master = api.master
    master.getNormalChargeLimit = Mock(return_value=(True, 70, 70))
    master.saveNormalChargeLimit = Mock()
    master.removeNormalChargeLimit = Mock()
    return api, cars


def test_teslaapi_targeted_limit_touches_only_that_car():
    api, (car, other) = api2()
    with patch("time.sleep"):
        assert api.applyChargeLimit(80, vin=VIN.lower(), remoteOnly=True) == "success"
    assert car.apply_charge_limit.call_count == 1 and other.apply_charge_limit.call_count == 0
    assert other.update_charge.call_count == 0


def test_teslaapi_broadcast_limit_is_unchanged():
    api, (car, other) = api2()
    with patch("time.sleep"):
        assert api.applyChargeLimit(80) == "success"
    assert car.apply_charge_limit.call_count == other.apply_charge_limit.call_count == 1


def test_teslaapi_targeted_limit_never_wakes_another_car():
    # The real car_api_available, not a mock: a targeted limit must not send
    # a paid wake to an at-home car it was not asked about
    api, (car, other) = api2()
    del api.car_api_available
    api.refreshTokenIfNeeded = Mock()
    api.wakeVehicle = Mock(return_value={"response": {"state": "online"}})
    for c in (car, other):
        c.ready = Mock(return_value=False)
        c.stopTryingToApplyLimit = False
        c.firstChargeNeededTime = 1
        c.lastAPIAccessTime = 0
        c.delayNextWakeAttempt = 0
    with patch("time.sleep"):
        api.applyChargeLimit(80, vin=VIN, remoteOnly=True)
    woken = [c.args[0] for c in api.wakeVehicle.call_args_list]
    assert other not in woken
    assert woken == [car]
