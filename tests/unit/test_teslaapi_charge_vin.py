"""TeslaAPI.car_api_charge honours task["vin"]: only that car is touched."""

import re
import time
from unittest.mock import Mock

import pytest

from tests.unit.teslatokens import fleet_token, new_api


@pytest.fixture
def api(monkeypatch, requests_mock):
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi.time, "sleep", lambda s: None)
    a = new_api()
    a.storeToken(fleet_token())
    a.addVehicle({"id": 1, "vin": "V1", "display_name": "Car1"})
    a.addVehicle({"id": 2, "vin": "V2", "display_name": "Car2"})
    for v in a.getCarApiVehicles():
        v.atHome = True
        v.lastAPIAccessTime = time.time()
        v.chargingState = "Charging"
        v.batteryLevel = 50
        v.chargeLimit = 90
        v.update_charge = Mock(return_value=True)
        v.update_location = Mock(return_value=True)
    a.applyChargeLimit = Mock()
    requests_mock.register_uri(
        "POST",
        re.compile(r".*/command/charge_(start|stop)"),
        json={"response": {"result": True, "reason": ""}},
    )
    requests_mock.register_uri(
        "POST", re.compile(r".*/wake_up"), json={"response": {"state": "online"}}
    )
    return a


def _posts(requests_mock):
    return [r.url.split("/")[-3:] for r in requests_mock.request_history]


def test_vin_stop_commands_only_that_vehicle(api, requests_mock):
    v1, v2 = api.getCarApiVehicles()
    v2.firstChargeNeededTime = 123
    api.car_api_charge({"cmd": "charge", "charge": False, "vin": "V1"})
    assert _posts(requests_mock) == [["V1", "command", "charge_stop"]]
    v2.update_charge.assert_not_called()
    v2.update_location.assert_not_called()
    assert v2.firstChargeNeededTime == 123  # wake state of V2 not reset


def test_vin_start_does_not_wake_other_vehicles(api, requests_mock):
    v1, v2 = api.getCarApiVehicles()
    v1.chargingState = "Stopped"
    v2.chargingState = "Stopped"
    v2.lastAPIAccessTime = 0  # asleep: an untargeted start would defer/wake it
    api.car_api_charge({"cmd": "charge", "charge": True, "vin": "V1"})
    assert _posts(requests_mock) == [["V1", "command", "charge_start"]]
    v2.update_charge.assert_not_called()
    assert v2.firstChargeNeededTime == 0


def test_no_vin_still_commands_every_vehicle(api, requests_mock):
    api.car_api_charge({"cmd": "charge", "charge": False})
    assert sorted(_posts(requests_mock)) == [
        ["V1", "command", "charge_stop"],
        ["V2", "command", "charge_stop"],
    ]
