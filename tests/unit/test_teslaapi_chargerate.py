"""
TeslaAPI.setChargeRate: set_charging_amps is a paid Fleet API command, so a
rate the car already accepted is not resent; requests time out.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from TWCManager.Vehicle.TeslaAPI import CarApiVehicle, TeslaAPI

VIN1 = "5YJ3E1EA1KF000001"


def make_api():
    master = Mock()
    master.config = {"config": {}, "vehicle": {"teslaAPI": {"enabled": True}}}
    master.tokenSyncEnabled.return_value = False
    api = TeslaAPI(master)
    api.carApiVehicles = []
    api.carApiBearerToken = "token"
    api.baseURL = "https://fleet.example/api/1/vehicles"
    return api


def make_vehicle():
    v = SimpleNamespace(VIN=VIN1, lastAPIAccessTime=0)
    v.lastChargeRateSent = CarApiVehicle.lastChargeRateSent
    return v


def response(result=True):
    return SimpleNamespace(
        text=json.dumps({"response": {"result": result, "reason": ""}})
    )


POST = "TWCManager.Vehicle.TeslaAPI.requests.post"


def test_same_rate_not_resent():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post:
        assert api.setChargeRate(16, v)
        assert api.setChargeRate(16, v)
        assert api.setChargeRate(16.4, v)
    assert post.call_count == 1
    assert v.lastChargeRateSent == 16


def test_changed_rate_is_sent():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post:
        api.setChargeRate(16, v)
        api.setChargeRate(20, v)
    assert post.call_count == 2
    assert post.call_args.kwargs["json"] == {"charging_amps": 20}
    assert v.lastChargeRateSent == 20


def test_set_again_bypasses_dedupe():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post:
        api.setChargeRate(16, v)
        api.setChargeRate(16, v, set_again=True)
    assert post.call_count == 2


def test_low_rate_still_sent_twice():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post, patch(
        "TWCManager.Vehicle.TeslaAPI.time.sleep"
    ):
        api.setChargeRate(3, v)
        assert post.call_count == 2
        api.setChargeRate(3, v)
        assert post.call_count == 2


def test_failed_post_does_not_record_rate():
    api, v = make_api(), make_vehicle()
    with patch(POST, side_effect=requests.exceptions.ConnectionError()) as post:
        assert api.setChargeRate(16, v) is False
        assert api.setChargeRate(16, v) is False
    assert post.call_count == 2
    assert v.lastChargeRateSent is None


def test_rejected_command_does_not_record_rate():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response(result=False)) as post:
        api.setChargeRate(16, v)
        api.setChargeRate(16, v)
    assert post.call_count == 2
    assert v.lastChargeRateSent is None


def test_post_has_timeout():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post:
        api.setChargeRate(16, v)
    assert post.call_args.kwargs["timeout"] == 30
