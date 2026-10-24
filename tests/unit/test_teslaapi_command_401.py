"""
A 401 on a vehicle command (#689). An expired token is refreshed and the
command retried once; any other 401 clears the tokens, so commands stop
using them and VehiclePriority falls back to the next module.
"""

import re
import time
from unittest.mock import Mock

import pytest

from tests.unit.teslatokens import fleet_token

VIN = "V1"
OK = {"json": {"response": {"result": True, "reason": ""}}}
ONLINE = {"json": {"response": {"state": "online"}}}
EXPIRED = {"status_code": 401, "text": "token expired (401)"}
DEAD = {"status_code": 401, "text": '{"error": "invalid bearer token"}'}
# Any Tesla token endpoint; which host refreshes a token varies by release.
TOKEN_URL = re.compile(r"https://[^/]*\.com/oauth2/v3/token")


@pytest.fixture
def api(monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as teslaapi
    from TWCManager.Vehicle.TeslaAPI import CarApiVehicle, TeslaAPI

    monkeypatch.setattr(teslaapi.time, "sleep", lambda s: None)
    master = Mock()
    master.config = {
        "config": {"teslaApiClientID": "my-client-id"},
        "vehicle": {"teslaAPI": {"enabled": True}},
    }
    master.tokenSyncEnabled.return_value = False
    a = TeslaAPI(master)
    a.carApiVehicles = []
    a.baseURL = ""
    a.carApiLastErrorTime = 0
    a.errorCount = 0
    a.setCarApiBearerToken(fleet_token(jti="old"))
    a.carApiRefreshToken = "old-refresh"
    v = CarApiVehicle({"id": 1, "vin": VIN, "display_name": "Car"}, a, master.config)
    v.atHome = True
    v.lastAPIAccessTime = time.time()
    v.chargingState = "Charging"
    v.update_charge = Mock(return_value=True)
    v.update_location = Mock(return_value=True)
    v.ready = Mock(return_value=True)
    a.carApiVehicles = [v]
    a.applyChargeLimit = Mock()
    return a


def bearer(request):
    return request.headers["Authorization"].split(" ", 1)[1]


def command(api, name):
    v = api.getCarApiVehicles()[0]
    if name == "charge_stop":
        return api.car_api_charge({"cmd": "charge", "charge": False})
    if name == "set_charging_amps":
        return api.setChargeRate(16, v)
    if name == "wake_up":
        return api.wakeVehicle(v)
    if name == "set_charge_limit":
        return v.apply_charge_limit(80)


COMMANDS = ["charge_stop", "set_charging_amps", "wake_up", "set_charge_limit"]


def url(name):
    return re.compile(r".*/" + VIN + "/(command/)?" + name + "$")


@pytest.mark.parametrize("name", COMMANDS)
def test_expired_token_is_refreshed_and_command_retried(api, requests_mock, name):
    new = fleet_token(jti="new")
    refresh = requests_mock.post(
        TOKEN_URL,
        json={"access_token": new, "refresh_token": "R2", "expires_in": 28800},
    )
    cmd = requests_mock.post(url(name), [EXPIRED, ONLINE if name == "wake_up" else OK])

    result = command(api, name)

    assert refresh.call_count == 1
    assert cmd.call_count == 2
    assert bearer(cmd.request_history[1]) == new
    assert result not in (False, "error")


@pytest.mark.parametrize("name", COMMANDS)
def test_dead_token_is_cleared(api, requests_mock, name):
    refresh = requests_mock.post(TOKEN_URL, json={})
    cmd = requests_mock.post(url(name), [DEAD])

    result = command(api, name)

    assert result in (False, "error")
    assert cmd.call_count == 1
    assert refresh.call_count == 0
    assert api.getCarApiBearerToken() == ""
    assert api.getCarApiRefreshToken() == ""
    api.master.queue_background_task.assert_any_call({"cmd": "saveSettings"})


@pytest.mark.parametrize("name", COMMANDS)
def test_expired_after_refresh_is_not_retried_again(api, requests_mock, name):
    refresh = requests_mock.post(
        TOKEN_URL,
        json={
            "access_token": fleet_token(jti="new"),
            "refresh_token": "NEW",
            "expires_in": 28800,
        },
    )
    requests_mock.post(url(name), [EXPIRED, EXPIRED, EXPIRED, EXPIRED])

    result = command(api, name)

    assert result in (False, "error")
    assert refresh.call_count == 1


def test_set_charging_amps_401_is_not_success(api, requests_mock):
    # Before #689 the 401 body, a non-empty dict, read as success.
    requests_mock.post(url("set_charging_amps"), [DEAD])

    assert api.setChargeRate(16, api.getCarApiVehicles()[0]) is False


def test_token_replaced_while_command_ran_is_kept(api):
    # A 401 for a token another thread has already replaced must not clear
    # the new one.
    sent = api.getCarApiBearerToken()
    api.setCarApiBearerToken(fleet_token(jti="pasted"))
    response = Mock(status_code=401, text="invalid bearer token")

    assert api.commandAuthFailed(response, sent) is True
    assert api.getCarApiBearerToken() != ""


def test_token_sync_keeps_synced_token(api):
    # TeslaMate owns the tokens while sync is on.
    api.master.tokenSyncEnabled.return_value = True
    sent = api.getCarApiBearerToken()
    response = Mock(status_code=401, text="invalid bearer token")

    assert api.commandAuthFailed(response, sent) is False
    assert api.getCarApiBearerToken() == sent
