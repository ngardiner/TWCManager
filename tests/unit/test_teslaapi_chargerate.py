"""
TeslaAPI.setChargeRate: set_charging_amps is a paid Fleet API command, so a
rate the car already accepted is not resent; requests time out.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from TWCManager.Vehicle.TeslaAPI import CarApiVehicle, TeslaAPI
from tests.unit.teslatokens import fleet_token

VIN1 = "5YJ3E1EA1KF000001"


def make_api():
    master = Mock()
    master.config = {"config": {}, "vehicle": {"teslaAPI": {"enabled": True}}}
    master.tokenSyncEnabled.return_value = False
    api = TeslaAPI(master)
    api.carApiVehicles = []
    api.storeToken(fleet_token())
    return api


def make_vehicle():
    v = SimpleNamespace(VIN=VIN1, lastAPIAccessTime=0)
    v.lastChargeRateSent = CarApiVehicle.lastChargeRateSent
    v.lastChargeRateSentTime = CarApiVehicle.lastChargeRateSentTime
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
    assert post.call_args.kwargs["timeout"] == 10


CLOCK = "TWCManager.Vehicle.TeslaAPI.time.monotonic"


def test_same_rate_skipped_inside_resend_window():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post, patch(CLOCK) as clock:
        clock.return_value = 1000.0
        api.setChargeRate(16, v)
        clock.return_value = 1000.0 + TeslaAPI.CHARGE_RATE_RESEND_AFTER - 1
        api.setChargeRate(16, v)
    assert post.call_count == 1


def test_same_rate_resent_after_resend_window():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post, patch(CLOCK) as clock:
        clock.return_value = 1000.0
        api.setChargeRate(16, v)
        clock.return_value = 1000.0 + TeslaAPI.CHARGE_RATE_RESEND_AFTER
        api.setChargeRate(16, v)
        assert post.call_count == 2
        assert v.lastChargeRateSentTime == clock.return_value
        # The resend restarts the window.
        clock.return_value += 1
        api.setChargeRate(16, v)
    assert post.call_count == 2


SLEEP = "TWCManager.Vehicle.TeslaAPI.time.sleep"


def test_low_rate_not_recorded_when_resend_fails():
    api, v = make_api(), make_vehicle()
    with patch(
        POST, side_effect=[response(), requests.exceptions.ConnectionError()]
    ), patch(SLEEP):
        assert api.setChargeRate(3, v) is False
    assert v.lastChargeRateSent is None


def test_low_rate_not_recorded_when_resend_rejected():
    api, v = make_api(), make_vehicle()
    with patch(POST, side_effect=[response(), response(result=False)]), patch(SLEEP):
        api.setChargeRate(3, v)
    assert v.lastChargeRateSent is None


def test_low_rate_recorded_after_both_sends_succeed():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()), patch(SLEEP):
        api.setChargeRate(3, v)
    assert v.lastChargeRateSent == 3


def test_numeric_string_rate_is_accepted():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post:
        api.setChargeRate("16.5", v)
    assert post.call_args.kwargs["json"] == {"charging_amps": 16}


def test_garbage_rate_is_rejected():
    api, v = make_api(), make_vehicle()
    with patch(POST, return_value=response()) as post:
        assert api.setChargeRate("lots", v) is False
        assert api.setChargeRate(None, v) is False
        assert api.setChargeRate(float("nan"), v) is False
    post.assert_not_called()
